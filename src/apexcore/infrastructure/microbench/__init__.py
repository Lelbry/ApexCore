"""Микробенчмарки CPU в стиле AIDA64 GPGPU Benchmark.

Каждый тест измеряет пропускную способность одной фундаментальной
операции (Memory R/W/Copy, SP/DP FLOPS, Integer IOPS, AES-256, SHA-1,
Julia, Mandelbrot). В отличие от стресс-движков, это короткие замеры
для построения «таблицы попугаев» — а не оценка стабильности.

Все тесты работают на Windows и Astra Linux без специфичных для ОС
зависимостей: NumPy + (опционально) numba + cryptography + hashlib.
"""

from apexcore.infrastructure.microbench.base import CancelledError
from apexcore.infrastructure.microbench.registry import (
    build_default_microbench_registry,
)

__all__ = ["CancelledError", "build_default_microbench_registry"]
