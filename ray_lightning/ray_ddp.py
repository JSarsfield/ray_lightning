from typing import Callable, Dict, List, Union, Any, Tuple, Optional

import warnings

import torch

from pytorch_lightning.strategies import DDPSpawnStrategy
from pytorch_lightning.utilities.rank_zero import rank_zero_only

from lightning_fabric.utilities.distributed import (
    _get_default_process_group_backend_for_device,
)

import ray
from pytorch_lightning.utilities.rank_zero import rank_zero_info
from pytorch_lightning.utilities.seed import reset_seed
from ray.util import PublicAPI

from ray_lightning.launchers import RayLauncher
from ray_lightning.accelerators import \
    _GPUAccelerator  # noqa: F401

import logging
import os

log = logging.getLogger(__name__)


@PublicAPI(stability="beta")
class RayStrategy(DDPSpawnStrategy):
    """Pytorch Lightning strategy for DDP training on a Ray cluster.

    This strategy is used to manage distributed training using DDP and
    Ray for process launching. Internally, the specified number of
    Ray actors are launched in the cluster and are registered as part of a
    Pytorch DDP process group. The Pytorch Lightning trainer is instantiated
    on the driver and sent to each of these training workers where training is
    executed. The distributed training protocol is handled by Pytorch DDP.
    Each training worker is configured to reserve ``num_cpus_per_worker``
    CPUS and 1 GPU if ``use_gpu`` is set to ``True``.
    If using this strategy, you should run your code like a normal Python
    script: ``python train.py``, and only on the head node if running in a
    distributed Ray cluster. There is no need to run this script on every
    single node.

    Args:
        num_workers (int): Number of training workers to use.
        num_cpus_per_worker (int): Number of CPUs per worker.
        use_gpu (bool): Whether to use GPU for allocation. For GPU to be
            used, you must also set the ``gpus`` arg in your Pytorch Lightning
            Trainer to a value > 0.
        init_hook (Callable): A function to run on each worker
            upon instantiation.
        resources_per_worker (Optional[Dict]): If specified, the resources
            defined in this Dict will be reserved for each worker. The
            ``CPU`` and ``GPU`` keys (case-sensitive) can be defined to
            override the number of CPU/GPUs used by each worker.
        **ddp_kwargs: Additional arguments to pass into
            ``DistributedDataParallel`` initialization
    Example:
        .. code-block:: python

            import pytorch_lightning as ptl
            from ray_lightning import RayAccelerator
            ptl_model = MNISTClassifier(...)
            strategy = RayStrategy(num_workers=4, cpus_per_worker=1,
                use_gpu=True)
            # Don't set ``gpus`` in ``Trainer``.
            # The actual number of GPUs is determined by ``num_workers``.
            trainer = pl.Trainer(..., strategy=strategy)
            trainer.fit(ptl_model)
    """

    strategy_name = "ddp_ray"

    def __init__(self,
                 num_workers: int = 1,
                 num_cpus_per_worker: int = 1,
                 use_gpu: bool = False,
                 init_hook: Optional[Callable] = None,
                 resources_per_worker: Optional[Dict] = None,
                 worker_runtime_env: Optional[Dict] = None,
                 **ddp_kwargs: Union[Any, Dict[str, Any]]):
        """Initialize the Ray strategy."""
        resources_per_worker = resources_per_worker if resources_per_worker \
            else {}
        self.worker_runtime_env = worker_runtime_env if worker_runtime_env \
            else {}
        self.nickname = "ddp_ray"
        self.num_workers = int(num_workers)
        self.num_cpus_per_worker = resources_per_worker.pop(
            "CPU", num_cpus_per_worker)

        if "GPU" in resources_per_worker:
            self.num_gpus_per_worker = resources_per_worker.pop("GPU")
        else:
            self.num_gpus_per_worker = int(use_gpu)

        self.use_gpu = self.num_gpus_per_worker > 0

        if self.use_gpu and self.num_gpus_per_worker < 1 and num_workers > 1:
            warnings.warn("Identified less than 1 GPU being set per worker. "
                          "If using NCCL backend (which is the default for "
                          "GPU training), GPU devices cannot be shared "
                          "across processes/workers and training is likely "
                          "to fail. It is recommended to use 1 GPU per "
                          "worker for training, or if you must use "
                          "fractional GPUs, then use the gloo backend by "
                          "setting PL_TORCH_DISTRIBUTED_BACKEND=gloo "
                          "environment variable.")

        self.additional_resources_per_worker = resources_per_worker
        self.init_hook = init_hook

        self._local_rank = 0
        self._global_rank = 0
        self._node_rank = 0

        self._is_remote = False
        self._device = None

        super().__init__(
            accelerator="_gpu" if use_gpu else "cpu",
            parallel_devices=[],
            cluster_environment=None,
            **ddp_kwargs)

    def _configure_launcher(self):
        """Configure the Ray launcher.

        This function is overriding ddp_spawn_strategy's method.
        It is run on the driver process.

        the distributed training logic is handled by the launcher.
        """
        self._launcher = RayLauncher(self)

    def set_remote(self, remote: bool):
        """Set the remote flag. (this is useful for the remote workers)

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        self._is_remote = remote

    def set_global_to_local(self,
                            global_to_local: List[Optional[Tuple[int, int]]]):
        """Set the global to local rank mapping.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        self.global_to_local = global_to_local

    def set_world_ranks(self, process_idx: int = 0):
        """Set the appropriate rank attributes for the trainer.

        This function is overriding ddp_spawn_strategy's method.
        It is run on the worker processes.
        """
        # Ranks should only be set once all the actors are created and
        # training has begun (otherwise self.global_to_local has not been
        # initialized).
        # If this method is called on the driver (i.e. self._is_remote is
        # False, then do a no-op).
        if self._is_remote:
            self._global_rank = process_idx
            self._local_rank, self._node_rank = self.global_to_local[
                self.global_rank]

    def _worker_setup(self, process_idx: int):
        """Setup the workers and pytorch DDP connections.

        This function is overriding ddp_spawn_strategy's method.
        It is run on the worker processes.
        """
        reset_seed()
        self.set_world_ranks(process_idx)
        rank_zero_only.rank = self.global_rank
        self._process_group_backend = self._get_process_group_backend()

        # Copied from
        # pytorch_lightning.utilities.distributed.init_dist_connection
        if not torch.distributed.is_available():
            raise RuntimeError("torch.distributed is not available. "
                               "Cannot initialize distributed process group")

        if torch.distributed.is_initialized():
            log.debug(
                "torch.distributed is already initialized. Exiting early")
            return

        global_rank = self.global_rank
        world_size = self.world_size
        torch_distributed_backend = _get_default_process_group_backend_for_device(self.root_device)

        # Taken from pytorch_lightning.utilities.distributed
        if torch.distributed.is_available(
        ) and not torch.distributed.is_initialized():
            log.info(f"Initializing distributed: GLOBAL_RANK: {global_rank}, "
                     f"MEMBER: {global_rank + 1}/{world_size}")
            torch.distributed.init_process_group(
                torch_distributed_backend,
                rank=global_rank,
                world_size=world_size,
                init_method="env://")

        # on rank=0 let everyone know training is starting
        rank_zero_info(f"{'-' * 100}\n"
                       f"distributed_backend={torch_distributed_backend}\n"
                       f"All distributed processes registered. "
                       f"Starting with {world_size} processes\n"
                       f"{'-' * 100}\n")

    @property
    def world_size(self) -> int:
        """Return the world size.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        return self.num_workers

    @property
    def local_rank(self) -> int:
        """Return the local rank.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        return self._local_rank

    @local_rank.setter
    def local_rank(self, value: int):
        """Set the local rank.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        self._local_rank = value

    @property
    def global_rank(self) -> int:
        """Return the global rank.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        return self._global_rank

    @global_rank.setter
    def global_rank(self, value: int):
        """Set the global rank.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        self._global_rank = value

    @property
    def node_rank(self) -> int:
        """Return the node rank.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        return self._node_rank

    @property
    def root_device(self):
        """Return the root device.

        This function is overriding ddp_spawn_strategy's method.
        It is run on the worker processes.
        """
        # get the root device
        # if the root device not set, figure it out
        # thru `get_gpu_ids` if `use_gpu` is True
        if self._device:
            return self._device
        if self.use_gpu and torch.cuda.is_available():
            if self._is_remote:
                # GPU IDs are assigned by Ray after you specify "use_gpu"
                # GPU `ray.get_gpu_ids()` may return ints or may return
                # strings. We should always convert to strings.
                gpu_ids = [str(id) for id in ray.get_gpu_ids()]

                if len(gpu_ids) > 0:
                    # By default, there should only be one GPU ID if
                    # `use_gpu=True`.
                    # If there are multiple GPUs, use the first one.
                    # If using fractional GPUs, these IDs are not guaranteed
                    # to be unique across different processes.
                    gpu_id = gpu_ids[0]

                    cuda_visible_str = os.environ.get("CUDA_VISIBLE_DEVICES",
                                                      "")
                    if cuda_visible_str and cuda_visible_str != "NoDevFiles":
                        cuda_visible_list = cuda_visible_str.split(",")
                        device_id = cuda_visible_list.index(gpu_id)
                    else:
                        raise RuntimeError(
                            "CUDA_VISIBLE_DEVICES set incorrectly. "
                            f"Got {cuda_visible_str}, expected to include "
                            "{gpu_id}. Did you override the "
                            "`CUDA_VISIBLE_DEVICES` environment variable? "
                            "If not, please help file an issue on Github.")
            else:
                # If the root device is requested on the driver, just return
                # the 0th device.
                device_id = 0
            return torch.device(f"cuda:{device_id}")
        else:
            return torch.device("cpu")

    @root_device.setter
    def root_device(self, device):
        """Set the root device.

        This function is a new RayStrategy method.
        It is run on the worker processes.
        """
        self._device = device

    @property
    def distributed_sampler_kwargs(self):
        """Returns the args to use for torch.data.DistributedSampler.

        This function is overriding ddp_spawn_strategy's method.
        It is run on the worker processes.
        """
        distributed_sampler_kwargs = dict(
            num_replicas=self.num_workers, rank=self.global_rank)
        return distributed_sampler_kwargs
    
    # def teardown(self) -> None:
    #     """Teardown the workers and pytorch DDP connections.

    #     This function is overriding ddp_spawn_strategy's method.
    #     It is run on the driver processes.
    #     """
    #     self.accelerator = None
    #     super().teardown()
