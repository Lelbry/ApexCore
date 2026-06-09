"""Встроенные стресс-движки CPU.

Если установлен ``numba`` — используется JIT-скомпилированный «горячий» цикл
(60-100x быстрее чистого Python). Иначе работает fallback на numpy
(достаточно для нагрузки CPU за счёт vectorized операций).

Категории:
- ``cpu_int`` — плотный цикл целочисленных операций (LCG-перемешивание).
- ``cpu_fp``  — матричное умножение float64 (попадает в SIMD/BLAS).
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads, run_threaded_loop
from apexcore.shared.timing import now

# ─── Опциональный numba: JIT существенно ускоряет CPU-Int ─────────────────────

try:
    from numba import njit  # type: ignore

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover - branch без зависимости
    HAVE_NUMBA = False

if HAVE_NUMBA:

    @njit(cache=True)  # type: ignore[misc]
    def _cpu_int_chunk(n: int, seed: int) -> int:
        # Линейный конгруэнтный генератор: тяжёлая ALU-нагрузка без выделений памяти.
        x = seed
        for i in range(n):
            x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
            x ^= i
        return x

else:

    def _cpu_int_chunk(n: int, seed: int) -> int:  # type: ignore[misc]
        x = seed
        mask = 0xFFFFFFFFFFFFFFFF
        for i in range(n):
            x = (x * 6364136223846793005 + 1442695040888963407) & mask
            x ^= i
        return x


# ─── CPU-Int ──────────────────────────────────────────────────────────────────


class BuiltinCpuIntEngine(StressEngine):
    """Целочисленная нагрузка CPU (LCG)."""

    name = "builtin_cpu_int"
    category = "cpu_int"
    is_external = False

    CHUNK = 5_000_000

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)

        # Прогрев numba (компиляция первой итерации).
        if HAVE_NUMBA:
            _cpu_int_chunk(1024, 1)

        chunk = self.CHUNK

        def work(remaining: float) -> float:
            seed = (int(now() * 1e9)) & 0xFFFFFFFFFFFFFFFF
            _cpu_int_chunk(chunk, seed)
            return float(chunk)

        total, elapsed = run_threaded_loop(work, duration_sec, n_threads, cancel_token=cancel_token)
        ops_per_sec = total / elapsed if elapsed > 0 else 0.0
        extra: dict[str, Any] = {"jit": "numba" if HAVE_NUMBA else "python"}
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=ops_per_sec,
            throughput_unit="ops/s",
            threads=n_threads,
            extra=extra,
        )


# ─── CPU-FP ───────────────────────────────────────────────────────────────────


class BuiltinCpuFpEngine(StressEngine):
    """Плавающая нагрузка CPU: матричное умножение float64.

    Внутри numpy.matmul используется BLAS, что попадает в SIMD-блок CPU и
    близко по характеру к нагрузкам типа Cinebench/Linpack.
    """

    name = "builtin_cpu_fp"
    category = "cpu_fp"
    is_external = False

    DIM = 512  # 512x512 float64 ≈ 2 МБ на матрицу — помещается в L2/L3.

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        # numpy сам параллелит matmul через BLAS, поэтому если threads=auto
        # отдадим всю работу одному потоку (BLAS использует все ядра внутри).
        # Если пользователь явно задал threads — запускаем столько Python-потоков,
        # каждый со своими буферами (могут конфликтовать с BLAS-параллелизмом).
        dim = self.DIM
        rng = np.random.default_rng(seed=42)

        def work(remaining: float) -> float:
            a = rng.standard_normal((dim, dim))
            b = rng.standard_normal((dim, dim))
            np.matmul(a, b)
            # ~ 2 * dim^3 операций умножения-сложения.
            return 2.0 * (dim ** 3)

        runner_threads = 1 if (threads is None or threads <= 0) else n_threads
        total_flops, elapsed = run_threaded_loop(
            work, duration_sec, runner_threads, cancel_token=cancel_token
        )
        gflops = total_flops / elapsed / 1e9 if elapsed > 0 else 0.0
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=gflops,
            throughput_unit="GFLOPS",
            threads=runner_threads,
            extra={"matrix_dim": dim, "blas": "numpy"},
        )
