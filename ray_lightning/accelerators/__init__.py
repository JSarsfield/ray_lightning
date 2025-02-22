# Copyright The PyTorch Lightning team.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from lightning_fabric.accelerators.registry import \
    call_register_accelerators  # noqa: F401
from pytorch_lightning.accelerators import AcceleratorRegistry
from ray_lightning.accelerators.delayed_gpu_accelerator import _GPUAccelerator

#  these lines are to register the delayed gpu accelerator as `_gpu`
ray_accelrator_registry = AcceleratorRegistry.register("_gpu", _GPUAccelerator, description="Custom ray accelerator")
ACCELERATORS_BASE_MODULE = "ray_lightning.accelerators"
call_register_accelerators(ray_accelrator_registry, ACCELERATORS_BASE_MODULE)

__all__ = ["_GPUAccelerator"]
