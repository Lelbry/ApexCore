"""Single/Double-Precision FLOPS — пиковая производительность FPU.

Реализация — матричное умножение через ``numpy.matmul``, который на
большинстве сборок NumPy линкуется с BLAS (OpenBLAS на Astra Linux,
Intel MKL/Accelerate в зависимости от дистрибутива). BLAS использует
SIMD-инструкции CPU (AVX2/AVX-512/FMA) и сам распараллеливает работу
по физическим ядрам, поэтому достигаемые цифры близки к пиковой
производительности FPU.

Формула throughput
------------------
Для матриц N×N матричное умножение требует ровно ``2 * N**3``
операций с плавающей точкой (умножение-сложение). Отсюда:

    GFLOPS = (iterations * 2 * N**3) / elapsed_sec / 1e9

Источники
---------
Lawson, C. L., Hanson, R. J., Kincaid, D. R. & Krogh, F. T. (1979).
Basic Linear Algebra Subprograms for FORTRAN Usage. ACM Transactions
on Mathematical Software, 5(3), 308-323.
DOI: https://doi.org/10.1145/355841.355847

Dongarra, J., Luszczek, P. & Petitet, A. (2003). The LINPACK benchmark:
past, present and future. Concurrency and Computation: Practice and
Experience, 15(9), 803-820.
DOI: https://doi.org/10.1002/cpe.728

Whaley, R. C., Petitet, A. & Dongarra, J. (2001). Automated empirical
optimizations of software and the ATLAS project. Parallel Computing,
27(1-2), 3-35. DOI: https://doi.org/10.1016/S0167-8191(00)00087-9
"""

from __future__ import annotations

import threading

import numpy as np

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

# 1024×1024 — компромисс: достаточно большие, чтобы BLAS вошёл в
# установившийся режим и насытил FPU, но при этом fp32-матрица занимает
# 4 МБ (помещается в L2/L3 у среднего CPU), а fp64 — 8 МБ. Это даёт
# хорошее покрытие SIMD без излишней нагрузки на пропускную DRAM.
DIM = 1024


class FlopsSpBench:
    """Single-precision (fp32) FLOPS через ``numpy.matmul``."""

    name = "flops_sp"
    category = "flops"
    unit = "GFLOPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        rng = np.random.default_rng(seed=42)
        a = rng.standard_normal((DIM, DIM)).astype(np.float32)
        b = rng.standard_normal((DIM, DIM)).astype(np.float32)
        # Заранее выделяем буфер результата, чтобы ходить по тому же региону
        # памяти (стабильные кеш-эффекты между итерациями).
        out = np.empty((DIM, DIM), dtype=np.float32)

        def work() -> None:
            np.matmul(a, b, out=out)

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        total_flops = iterations * 2 * (DIM ** 3)
        gflops = total_flops / elapsed / 1e9
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=gflops,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"dim": DIM, "dtype": "float32", "backend": "numpy.matmul", "source_file": "flops.py"},
        )


class FlopsDpBench:
    """Double-precision (fp64) FLOPS через ``numpy.matmul``."""

    name = "flops_dp"
    category = "flops"
    unit = "GFLOPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        rng = np.random.default_rng(seed=42)
        a = rng.standard_normal((DIM, DIM)).astype(np.float64)
        b = rng.standard_normal((DIM, DIM)).astype(np.float64)
        out = np.empty((DIM, DIM), dtype=np.float64)

        def work() -> None:
            np.matmul(a, b, out=out)

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        total_flops = iterations * 2 * (DIM ** 3)
        gflops = total_flops / elapsed / 1e9
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=gflops,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"dim": DIM, "dtype": "float64", "backend": "numpy.matmul", "source_file": "flops.py"},
        )
