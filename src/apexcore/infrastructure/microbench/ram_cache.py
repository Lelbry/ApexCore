"""Микробенчмарки для теста «Расширенный тест ОЗУ и кеша (Ram&Cache)».

Параметризованный класс :class:`RamCacheBench` запускает одну операцию
(read / write / copy / latency) на одном уровне иерархии (L1 / L2 / L3 / DRAM).

Размер тестового буфера — 50% от номинального размера соответствующего уровня
кеша; для DRAM используется фиксированный 256 МБ (как в memory.py:30). Это
обеспечивает гарантированное попадание данных в нужный уровень с запасом на
стек/код, и одновременно «промах» в DRAM для верхнего уровня.

Для read/write/copy на L1/L2 без numba цикл Python доминирует над временем
обращения к памяти, поэтому при наличии numba используется JIT-компилированный
кернел (`@njit(fastmath=True)`). Если numba недоступна или
``APEXCORE_DISABLE_NUMBA=1`` — fallback на NumPy-операции (np.sum / buf.fill /
np.copyto). Latency всегда использует pointer-chasing, как в
:class:`BuiltinRamLatencyEngine`.
"""

from __future__ import annotations

import os
import threading

import numpy as np

from apexcore.domain.cache import (
    BackendName,
    LevelName,
    OperationName,
    RamCacheMetric,
    UnitName,
)
from apexcore.infrastructure.microbench.base import time_loop

# ────────── numba fallback ──────────

try:
    if os.environ.get("APEXCORE_DISABLE_NUMBA") == "1":
        raise ImportError("disabled by env")
    from numba import njit  # type: ignore

    HAVE_NUMBA = True
except ImportError:
    HAVE_NUMBA = False


if HAVE_NUMBA:

    @njit(cache=True, fastmath=True)  # type: ignore[misc]
    def _read_loop_numba(buf: np.ndarray) -> float:
        s = 0.0
        for i in range(buf.size):
            s += buf[i]
        return s

    @njit(cache=True, fastmath=True)  # type: ignore[misc]
    def _write_loop_numba(buf: np.ndarray, value: float) -> None:
        for i in range(buf.size):
            buf[i] = value

    @njit(cache=True, fastmath=True)  # type: ignore[misc]
    def _copy_loop_numba(src: np.ndarray, dst: np.ndarray) -> None:
        for i in range(src.size):
            dst[i] = src[i]

    @njit(cache=True)  # type: ignore[misc]
    def _pointer_chase_numba(indices: np.ndarray, iters: int, start_pos: int) -> int:
        pos = start_pos
        for _ in range(iters):
            pos = indices[pos]
        return pos


def _resolve_backend() -> BackendName:
    return "numba" if HAVE_NUMBA else "numpy"


def _buffer_n_elements(buffer_bytes: int) -> int:
    """Сколько float64-элементов помещается в заданный буфер."""
    n = max(buffer_bytes // 8, 8)
    return int(n)


# ────────── public bench class ──────────


class RamCacheBench:
    """Один замер «уровень × операция» для Ram&Cache.

    Параметры:
        level: одно из L1/L2/L3/DRAM — нужно только для пометки результата.
        operation: read / write / copy / latency.
        buffer_bytes: размер тестового буфера. Должен быть подобран снаружи
            (50% от размера уровня) — здесь он используется как есть.
    """

    def __init__(
        self,
        level: LevelName,
        operation: OperationName,
        buffer_bytes: int,
    ) -> None:
        self.level: LevelName = level
        self.operation: OperationName = operation
        self.buffer_bytes = buffer_bytes

    @property
    def unit(self) -> UnitName:
        """Единица для метрики этого бенчмарка: ``ns`` для latency, ``MB/s`` иначе."""
        return "ns" if self.operation == "latency" else "MB/s"

    def run(
        self,
        duration_sec: float,
        cancel_token: threading.Event | None = None,
    ) -> RamCacheMetric:
        backend = _resolve_backend()
        try:
            if self.operation == "read":
                value, iters, elapsed = self._run_read(duration_sec, cancel_token)
            elif self.operation == "write":
                value, iters, elapsed = self._run_write(duration_sec, cancel_token)
            elif self.operation == "copy":
                value, iters, elapsed = self._run_copy(duration_sec, cancel_token)
            else:
                value, iters, elapsed = self._run_latency(duration_sec, cancel_token)
        except Exception as exc:
            return RamCacheMetric(
                level=self.level,
                operation=self.operation,
                value=0.0,
                unit=self.unit,
                backend=backend,
                duration_actual_sec=0.0,
                iterations=0,
                error=str(exc)[:200],
            )
        return RamCacheMetric(
            level=self.level,
            operation=self.operation,
            value=value,
            unit=self.unit,
            backend=backend,
            duration_actual_sec=elapsed,
            iterations=iters,
        )

    # ────────── Read ──────────

    def _run_read(
        self,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> tuple[float, int, float]:
        n = _buffer_n_elements(self.buffer_bytes)
        buf = np.full(n, 1.0, dtype=np.float64)
        bytes_per_pass = n * 8

        if HAVE_NUMBA:
            _ = _read_loop_numba(buf)  # JIT прогрев

            def work() -> None:
                _ = _read_loop_numba(buf)

        else:

            def work() -> None:
                _ = float(np.sum(buf))

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_read = iterations * bytes_per_pass
        mbps = bytes_read / elapsed / 1e6 if elapsed > 0 else 0.0
        return mbps, iterations, elapsed

    # ────────── Write ──────────

    def _run_write(
        self,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> tuple[float, int, float]:
        n = _buffer_n_elements(self.buffer_bytes)
        buf = np.empty(n, dtype=np.float64)
        bytes_per_pass = n * 8
        scalars = (3.14, 2.71)
        toggle = [0]

        if HAVE_NUMBA:
            _write_loop_numba(buf, scalars[0])  # JIT прогрев

            def work() -> None:
                _write_loop_numba(buf, scalars[toggle[0] & 1])
                toggle[0] += 1

        else:

            def work() -> None:
                buf.fill(scalars[toggle[0] & 1])
                toggle[0] += 1

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_written = iterations * bytes_per_pass
        mbps = bytes_written / elapsed / 1e6 if elapsed > 0 else 0.0
        return mbps, iterations, elapsed

    # ────────── Copy ──────────

    def _run_copy(
        self,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> tuple[float, int, float]:
        # Для copy буфер делится пополам: src + dst в одном уровне кеша.
        half = self.buffer_bytes // 2
        n = _buffer_n_elements(half)
        src = np.full(n, 1.0, dtype=np.float64)
        dst = np.empty(n, dtype=np.float64)
        # Считаем как read+write (как STREAM Triad).
        bytes_per_pass = n * 8 * 2

        if HAVE_NUMBA:
            _copy_loop_numba(src, dst)  # JIT прогрев

            def work() -> None:
                _copy_loop_numba(src, dst)

        else:

            def work() -> None:
                np.copyto(dst, src)

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        bytes_moved = iterations * bytes_per_pass
        mbps = bytes_moved / elapsed / 1e6 if elapsed > 0 else 0.0
        return mbps, iterations, elapsed

    # ────────── Latency ──────────

    def _run_latency(
        self,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> tuple[float, int, float]:
        # Pointer-chasing по перетасованным индексам (как в BuiltinRamLatencyEngine).
        # Размер буфера индексов = self.buffer_bytes (8 байт на int64).
        n = _buffer_n_elements(self.buffer_bytes)
        rng = np.random.default_rng(seed=0xCAFE)
        order = rng.permutation(n).astype(np.int64)
        indices = np.empty(n, dtype=np.int64)
        indices[order[:-1]] = order[1:]
        indices[order[-1]] = order[0]

        chunk = max(min(200_000, n * 4), 1024)

        if HAVE_NUMBA:
            _pointer_chase_numba(indices, 1024, 0)  # JIT прогрев

            def work() -> None:
                start_pos = int(np.random.default_rng().integers(0, n))
                _pointer_chase_numba(indices, chunk, start_pos)

        else:

            def work() -> None:
                pos = int(np.random.default_rng().integers(0, n))
                local = indices  # местная ссылка ускоряет цикл
                for _ in range(chunk):
                    pos = int(local[pos])

        iterations, elapsed = time_loop(work, duration_sec, cancel_token=cancel_token)
        total_accesses = iterations * chunk
        ns_per_access = (elapsed * 1e9) / total_accesses if total_accesses > 0 else 0.0
        return ns_per_access, iterations, elapsed
