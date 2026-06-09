"""Каталог микробенчмарков и порядок их выполнения.

Порядок в списке = порядок вывода в финальной таблице (повторяет порядок
строк в AIDA64 GPGPU Benchmark: Memory → FLOPS → Integer IOPS → Crypto
→ Fractal).
"""

from __future__ import annotations

from apexcore.infrastructure.microbench.base import MicroBench
from apexcore.infrastructure.microbench.crypto import Aes256Bench, Sha1Bench
from apexcore.infrastructure.microbench.flops import FlopsDpBench, FlopsSpBench
from apexcore.infrastructure.microbench.fractal import (
    JuliaSpBench,
    MandelbrotDpBench,
)
from apexcore.infrastructure.microbench.integer import (
    Int24IopsBench,
    Int32IopsBench,
    Int64IopsBench,
)
from apexcore.infrastructure.microbench.memory import (
    MemoryCopyBench,
    MemoryReadBench,
    MemoryWriteBench,
)


def build_default_microbench_registry() -> list[MicroBench]:
    """Вернуть полный набор микротестов в каноническом порядке (12 шт.)."""
    return [
        MemoryReadBench(),
        MemoryWriteBench(),
        MemoryCopyBench(),
        FlopsSpBench(),
        FlopsDpBench(),
        Int24IopsBench(),
        Int32IopsBench(),
        Int64IopsBench(),
        Aes256Bench(),
        Sha1Bench(),
        JuliaSpBench(),
        MandelbrotDpBench(),
    ]
