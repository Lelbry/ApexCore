"""24/32/64-bit Integer IOPS — пропускная способность ALU.

Каждый тест прогоняет в горячем цикле зависимую цепочку целочисленных
операций (LCG: ``x = x * a + c``) на значениях указанной разрядности.
Зависимость по данным между итерациями (``x[i+1] = f(x[i])``)
гарантирует, что планировщик CPU не сможет распараллелить цепочку
out-of-order — это даёт оценку именно последовательной throughput
ALU-канала.

Реализация — на ``numba``-JIT, если он установлен (compile-once,
~50-100× быстрее чистого Python). Fallback на NumPy векторизованную
форму, которая даёт сопоставимый по характеру результат, хотя и
получает выигрыш от SIMD.

Формула throughput
------------------
В каждой итерации цикла выполняется одна MUL + одна ADD = 2 IOPS,
плюс маска (AND) → пренебрежимо. Считаем как:

    GIOPS = (iterations * CHUNK * 2) / elapsed_sec / 1e9

Источники
---------
Konstantinidis, E. & Cotronis, Y. (2017). A quantitative roofline model
for GPU kernel performance estimation using mini-benchmarks and
hardware metric profiling. Journal of Parallel and Distributed
Computing, 107, 37-56. DOI: https://doi.org/10.1016/j.jpdc.2017.04.002

Hennessy, J. L. & Patterson, D. A. (2017). Computer Architecture: A
Quantitative Approach (6th ed.). Morgan Kaufmann. — раздел про
ILP и зависимые цепочки операций.
"""

from __future__ import annotations

import threading

import numpy as np

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

try:
    from numba import njit  # type: ignore

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover
    HAVE_NUMBA = False

# Размер «горячего» цикла для одной итерации замера. Подобран так, чтобы
# на современных CPU один вызов занимал ~10-50 мс — достаточно, чтобы
# overhead от Python-интерпретатора был пренебрежим.
CHUNK = 50_000_000


# ─── numba kernel'ы (если доступны) ──────────────────────────────────────

if HAVE_NUMBA:

    @njit(cache=True, nogil=True)  # type: ignore[misc]
    def _chain_u64(n: int, seed: int) -> int:
        x = np.uint64(seed)
        a = np.uint64(6364136223846793005)
        c = np.uint64(1442695040888963407)
        for _ in range(n):
            x = x * a + c
        return int(x)

    @njit(cache=True, nogil=True)  # type: ignore[misc]
    def _chain_u32(n: int, seed: int) -> int:
        x = np.uint32(seed & 0xFFFFFFFF)
        a = np.uint32(1664525)
        c = np.uint32(1013904223)
        for _ in range(n):
            x = x * a + c
        return int(x)

    @njit(cache=True, nogil=True)  # type: ignore[misc]
    def _chain_u24(n: int, seed: int) -> int:
        # 24-битная арифметика поверх uint32 с маской — типовой DSP-сценарий
        # (24-bit ADC samples, мантисса float32 в 24 битах).
        x = np.uint32(seed & 0xFFFFFF)
        a = np.uint32(0x10DCD)
        c = np.uint32(0x269EC3)
        mask = np.uint32(0xFFFFFF)
        for _ in range(n):
            x = (x * a + c) & mask
        return int(x)

else:
    # Fallback без numba: прогоняем те же операции через NumPy на массиве,
    # многократно. Это не «зависимая цепочка», а векторизованный батч —
    # цифры будут другие, но смысл (throughput ALU) сохраняется.
    def _chain_u64(n: int, seed: int) -> int:  # type: ignore[misc]
        BATCH = 1 << 16
        passes = max(n // BATCH, 1)
        x = np.full(BATCH, np.uint64(seed) | np.uint64(1), dtype=np.uint64)
        a = np.uint64(6364136223846793005)
        c = np.uint64(1442695040888963407)
        for _ in range(passes):
            x = x * a + c
        return int(x[0])

    def _chain_u32(n: int, seed: int) -> int:  # type: ignore[misc]
        BATCH = 1 << 16
        passes = max(n // BATCH, 1)
        x = np.full(BATCH, np.uint32(seed) | np.uint32(1), dtype=np.uint32)
        a = np.uint32(1664525)
        c = np.uint32(1013904223)
        for _ in range(passes):
            x = x * a + c
        return int(x[0])

    def _chain_u24(n: int, seed: int) -> int:  # type: ignore[misc]
        BATCH = 1 << 16
        passes = max(n // BATCH, 1)
        x = np.full(BATCH, (np.uint32(seed) & np.uint32(0xFFFFFF)) | np.uint32(1), dtype=np.uint32)
        a = np.uint32(0x10DCD)
        c = np.uint32(0x269EC3)
        mask = np.uint32(0xFFFFFF)
        for _ in range(passes):
            x = (x * a + c) & mask
        return int(x[0])


# ─── Движки ──────────────────────────────────────────────────────────────


def _backend_label() -> str:
    return "numba" if HAVE_NUMBA else "numpy-batched"


class Int64IopsBench:
    """64-bit Integer IOPS (зависимая цепочка LCG)."""

    name = "int_iops_64"
    category = "integer"
    unit = "GIOPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        # Прогрев JIT (один холостой вызов с малым n).
        _chain_u64(1024, 1)

        def work() -> None:
            _chain_u64(CHUNK, 1)

        iterations, elapsed = time_loop(work, duration_sec, warmup_calls=0, cancel_token=cancel_token)
        ops = iterations * CHUNK * 2  # одна MUL + одна ADD на итерацию
        giops = ops / elapsed / 1e9
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=giops,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"chunk": CHUNK, "backend": _backend_label(), "bits": 64, "source_file": "integer.py"},
        )


class Int32IopsBench:
    """32-bit Integer IOPS."""

    name = "int_iops_32"
    category = "integer"
    unit = "GIOPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        _chain_u32(1024, 1)

        def work() -> None:
            _chain_u32(CHUNK, 1)

        iterations, elapsed = time_loop(work, duration_sec, warmup_calls=0, cancel_token=cancel_token)
        ops = iterations * CHUNK * 2
        giops = ops / elapsed / 1e9
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=giops,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"chunk": CHUNK, "backend": _backend_label(), "bits": 32, "source_file": "integer.py"},
        )


class Int24IopsBench:
    """24-bit Integer IOPS — DSP-стиль (24-битная мантисса / 24-bit samples)."""

    name = "int_iops_24"
    category = "integer"
    unit = "GIOPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        _chain_u24(1024, 1)

        def work() -> None:
            _chain_u24(CHUNK, 1)

        iterations, elapsed = time_loop(work, duration_sec, warmup_calls=0, cancel_token=cancel_token)
        # 2 арифметических + 1 маска ≈ 3 IOPS, но AIDA64 считает только
        # MUL+ADD пара = 2 IOPS, чтобы оставаться сравнимым с 32/64-bit.
        ops = iterations * CHUNK * 2
        giops = ops / elapsed / 1e9
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=giops,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"chunk": CHUNK, "backend": _backend_label(), "bits": 24, "source_file": "integer.py"},
        )
