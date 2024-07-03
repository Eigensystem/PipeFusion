import os
import torch
import torch.distributed as dist
from packaging import version
from dataclasses import dataclass, fields

from torch import distributed as dist

from pipefuser.logger import init_logger
import pipefuser.envs as envs

logger = init_logger(__name__)

from typing import Union, Optional, List


try:
    from yunchang import set_seq_parallel_pg
    HAS_LONG_CTX_ATTN = True
except ImportError:
    HAS_LONG_CTX_ATTN = False



def check_env():
# https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/cudagraph.html
    if envs.CUDA_VERSION < version.parse("11.3"):
        raise RuntimeError(
            "NCCL CUDA Graph support requires CUDA 11.3 or above")
    if envs.TORCH_VERSION < version.parse("2.2.0"):
        # https://pytorch.org/blog/accelerating-pytorch-with-cuda-graphs/
        raise RuntimeError(
            "CUDAGraph with NCCL support requires PyTorch 2.2.0 or above. "
            "If it is not released yet, please install nightly built PyTorch "
            "with `pip3 install --pre torch torchvision torchaudio --index-url "
            "https://download.pytorch.org/whl/nightly/cu121`"
        )

@dataclass
class ModelConfig:
    model: str
    download_dir: Optional[str] = None
    trust_remote_code: bool = False
    scheduler: Optional[str] = "dpmsolver_multistep"

@dataclass
class DataConfig:
    height: int = 1024
    width: int = 1024
    batch_size: Optional[int] = None
    use_resolution_binning: bool = True,

@dataclass
class RuntimeConfig:
    seed: int = 42
    warmup_steps: int = 1
    use_cuda_graph: bool = True
    use_parallel_vae: bool = False
    use_profiler: bool = False

    def __post_init__(self):
        if self.use_cuda_graph:
            check_env()


@dataclass(frozen=True)
class DataParallelConfig():
    dp_degree: int = 1
    use_split_batch: bool = False
    do_classifier_free_guidance: bool = True

    def __post_init__(self):
        assert self.dp_degree >= 1, "dp_degree must greater than 1"
        if self.use_split_batch and self.do_classifier_free_guidance:
            assert self.dp_degree * 2 <= dist.get_world_size(), \
                ("dp_degree * 2 must be less than or equal to world_size "
                "because of classifier free guidance")
        else:
            assert self.dp_degree <= dist.get_world_size(), \
                "dp_degree must be less than or equal to world_size"
        # set classifier_free_guidance_degree parallel for split batch
        if self.use_split_batch and self.do_classifier_free_guidance:
            self.cfg_degree = 2
        else:
            self.cfg_degree = 1
            

@dataclass(frozen=True)
class SequenceParallelConfig():
    ulysses_degree: Optional[int] = None
    ring_degree: Optional[int] = None
    
    def __post_init__(self):
        if self.ulysses_degree is None:
            self.ulysses_degree = 1
            logger.info(
                f"Ulysses degree not set, "
                f"using default value {self.ulysses_degree}"
            )
        if self.ring_degree is None:
            self.ring_degree = 1
            logger.info(
                f"Ring degree not set, "
                f"using default value {self.ring_degree}"
            )
        self.sp_degree = self.ulysses_degree * self.ring_degree

        if not HAS_LONG_CTX_ATTN and self.sp_degree > 1:
            raise RuntimeError("sequence parallel kit yunchang not found")


@dataclass
class TensorParallelConfig():
    tp_degree: int = 1
    split_scheme: Optional[str] = "row"

    def __post_init__(self):
        assert self.tp_degree >= 1, "tp_degree must greater than 1"
        assert self.tp_degree <= dist.get_world_size(), \
            "tp_degree must be less than or equal to world_size"


@dataclass(frozen=True)
class PipeFusionParallelConfig():
    pp_degree: int = 1
    pipeline_patch_num: Optional[int] = None
    attn_layer_num_for_pp: Optional[List[int]] = None,

    def __post_init__(self):
        assert self.pp_degree is not None and self.pp_degree >= 1, \
            "pipefusion_degree must be set and greater than 1 to use pipefusion"
        assert self.pp_degree <= dist.get_world_size(), \
            "pipefusion_degree must be less than or equal to world_size"
        if pipeline_patch_num is None:
            pipeline_patch_num = self.pp_degree
            logger.info(
                f"Pipeline patch number not set, "
                f"using default value {self.pp_degree}"
            )
        if self.attn_layer_num_for_pp is not None:
            assert len(self.attn_layer_num_for_pp) == self.pp_degree, (
                "attn_layer_num_for_pp must have the same "
                "length as pp_degree if not None"
            )


@dataclass(frozen=True)
class ParallelConfig():
    dp_config: DataParallelConfig
    sp_config: SequenceParallelConfig
    pp_config: PipeFusionParallelConfig
    tp_config: TensorParallelConfig

    def __post_init__(self):
        if self.tp_config.tp_degree > 1:
            raise NotImplementedError("Tensor parallel is not supported yet")
        assert self.dp_config is not None, "dp_config must be set"
        assert self.sp_config is not None, "sp_config must be set"
        assert self.pp_config is not None, "pp_config must be set"
        parallel_world_size = (
            self.dp_config.dp_degree * 
            self.dp_config.cfg_degree *
            self.sp_config.sp_degree * 
            self.tp_config.tp_degree *
            self.pp_config.pp_degree
        )
        world_size = dist.get_world_size()
        assert parallel_world_size == world_size, (
            f"parallel_world_size {parallel_world_size} "
            f"must be equal to world_size {dist.get_world_size()}"
        )
        assert (world_size % 
                (self.dp_config.dp_degree * 
                 self.dp_config.cfg_degree) == 0), (
            "world_size must be divisible by dp_degree * cfg_degree"
        )
        assert world_size % self.pp_config.pp_degree == 0, (
            "world_size must be divisible by pp_degree"
        )
        assert world_size % self.sp_config.sp_degree == 0, (
            "world_size must be divisible by sp_degree"
        )
        assert world_size % self.tp_config.tp_degree == 0, (
            "world_size must be divisible by tp_degree"
        )
        self.dp_degree = self.dp_config.dp_degree
        self.cfg_degree = self.dp_config.cfg_degree
        self.sp_degree = self.sp_config.sp_degree
        self.pp_degree = self.pp_config.pp_degree
        self.tp_degree = self.tp_config.tp_degree
        

@dataclass(frozen=True)
class EngineConfig:
    model_config: ModelConfig
    data_config: DataConfig
    runtime_config: RuntimeConfig
    parallel_config: ParallelConfig

    def to_dict(self):
        """Return the configs as a dictionary, for use in **kwargs.
        """
        return dict(
            (field.name, getattr(self, field.name)) for field in fields(self))