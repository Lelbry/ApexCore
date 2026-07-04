"""GPU-compute инфраструктура (OpenCL через ctypes, без внешних зависимостей).

Экспортирует :class:`OpenClGpuBackend` — реализацию
:class:`apexcore.domain.ports.GpuComputeBackend` — и фабрику
:func:`build_default_gpu_backend`, которую использует application-слой, чтобы
не зависеть от конкретной реализации бэкенда напрямую.
"""

from __future__ import annotations

from apexcore.domain.ports import GpuComputeBackend
from apexcore.infrastructure.gpu.opencl_backend import OpenClGpuBackend


def build_default_gpu_backend() -> GpuComputeBackend:
    """Собрать GPU-compute бэкенд по умолчанию.

    Сейчас это единственная реализация — OpenCL через ctypes. Фабрика — точка
    расширения: если позже появится CUDA/Level-Zero путь, выбор реализации
    (или композит с фолбэком) прячется здесь, а вызывающий код не меняется.
    Бэкенд всегда конструируется успешно; фактическую доступность железа
    проверяет :meth:`GpuComputeBackend.is_available`.
    """

    return OpenClGpuBackend()


__all__ = ["OpenClGpuBackend", "build_default_gpu_backend"]
