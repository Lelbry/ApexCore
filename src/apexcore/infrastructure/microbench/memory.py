"""Memory Read / Write / Copy — пропускная способность DRAM.

Тесты используют буфер, заведомо больший LLC любого десктопного CPU
(256 МБ × 8 байт элементов float64), что заставляет каждое обращение
ходить в основную память. NumPy in-place операции компилируются в
векторизованные циклы (SSE/AVX2/AVX-512) внутри ufunc'ов.

Источники
---------
McCalpin, J. D. (1995). Memory Bandwidth and Machine Balance in Current
High Performance Computers. IEEE Computer Society TCCA Newsletter, Dec.
http://www.cs.virginia.edu/stream/

McVoy, L. & Staelin, C. (1996). lmbench: portable tools for performance
analysis. USENIX Annual Technical Conference, 279-294.
"""

from __future__ import annotations

import threading

import numpy as np

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

# 256 МБ — больше LLC у любого десктопного CPU (Alder Lake — 30 МБ L3,
# Threadripper Pro 7995WX — 384 МБ; для последнего нужен бóльший буфер,
# но 256 МБ остаётся репрезентативным для массового железа).
BUFFER_MB = 256
BYTES_PER_ELEM = 8  # float64


def _buffer_n_elements() -> int:
    return (BUFFER_MB * 1024 * 1024) // BYTES_PER_ELEM


class MemoryReadBench:
    """Чтение большого буфера через ``np.sum`` (один проход read-only)."""

    name = "memory_read"
    category = "memory"
    unit = "MB/s"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        n = _buffer_n_elements()
        buf = np.full(n, 1.0, dtype=np.float64)

        def work() -> None:
            # np.sum проходит весь массив одним чтением;
            # результат используется (присваивается локально), чтобы оптимизатор
            # не выкинул вычисление.
            _ = float(np.sum(buf))

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_read = iterations * n * BYTES_PER_ELEM
        mb_per_sec = bytes_read / elapsed / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"buffer_mb": BUFFER_MB, "dtype": "float64", "source_file": "memory.py"},
        )


class MemoryWriteBench:
    """Запись большого буфера через ``buf[:] = scalar`` (один проход write-only)."""

    name = "memory_write"
    category = "memory"
    unit = "MB/s"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        n = _buffer_n_elements()
        buf = np.empty(n, dtype=np.float64)

        # Чередуем константы, чтобы оптимизатор / write-combine не подменили
        # реальные сторы no-op'ами. Используем два разных значения по очереди.
        scalars = (3.14, 2.71)
        toggle = [0]

        def work() -> None:
            buf.fill(scalars[toggle[0] & 1])
            toggle[0] += 1

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_written = iterations * n * BYTES_PER_ELEM
        mb_per_sec = bytes_written / elapsed / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={"buffer_mb": BUFFER_MB, "dtype": "float64", "source_file": "memory.py"},
        )


class MemoryCopyBench:
    """Копирование буфера через ``np.copyto`` (read + write, аналог ``memcpy``).

    Замеряется как сумма прочитанных и записанных байт — соответствует тому,
    как Memory Copy указывают AIDA64 и STREAM Triad (там тоже 2× от размера).
    """

    name = "memory_copy"
    category = "memory"
    unit = "MB/s"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        n = _buffer_n_elements()
        src = np.full(n, 1.0, dtype=np.float64)
        dst = np.empty(n, dtype=np.float64)

        def work() -> None:
            np.copyto(dst, src)

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_moved = iterations * n * BYTES_PER_ELEM * 2  # read + write
        mb_per_sec = bytes_moved / elapsed / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "buffer_mb": BUFFER_MB,
                "dtype": "float64",
                "counted": "read+write",
                "source_file": "memory.py",
            },
        )
