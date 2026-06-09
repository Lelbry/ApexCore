"""Встроенные стресс-движки RAM: пропускная способность и латентность.

- ``builtin_ram_bw`` — STREAM Triad: ``c[:] = a + scalar * b`` на больших массивах,
  замеряется ГБ/с (read+write).
- ``builtin_ram_lat`` — pointer-chasing по перетасованным индексам, замеряется
  средняя латентность доступа в наносекундах.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads, run_threaded_loop
from apexcore.shared.timing import now

try:
    from numba import njit  # type: ignore

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover
    HAVE_NUMBA = False


if HAVE_NUMBA:

    @njit(cache=True)  # type: ignore[misc]
    def _pointer_chase(indices: np.ndarray, iters: int, start_pos: int) -> int:
        pos = start_pos
        for _ in range(iters):
            pos = indices[pos]
        return pos

else:

    def _pointer_chase(indices: np.ndarray, iters: int, start_pos: int) -> int:  # type: ignore[misc]
        pos = int(start_pos)
        for _ in range(iters):
            pos = int(indices[pos])
        return pos


# ─── Bandwidth (STREAM Triad) ────────────────────────────────────────────────


class BuiltinRamBandwidthEngine(StressEngine):
    """STREAM-triad: ``c[:] = a + scalar * b`` на больших float64-массивах."""

    name = "builtin_ram_bw"
    category = "ram_bw"
    is_external = False

    SIZE_MB = 256  # Размер каждого массива; *3 буфера = 768 МБ.

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        n_elements = (self.SIZE_MB * 1024 * 1024) // 8
        scalar = 3.0

        # Каждый поток получает свои буферы, чтобы избежать когерентности кешей.
        def work(remaining: float) -> float:
            a = np.full(n_elements, 1.0, dtype=np.float64)
            b = np.full(n_elements, 2.0, dtype=np.float64)
            c = np.empty(n_elements, dtype=np.float64)
            iters = 0
            local_deadline = now() + min(remaining, 0.5)
            while now() < local_deadline:
                np.multiply(b, scalar, out=c)
                np.add(a, c, out=c)
                iters += 1
            # На итерацию: read(a) + read(b) + write(c) = 3 * n_elements * 8 байт.
            return float(iters * 3 * n_elements * 8)

        total_bytes, elapsed = run_threaded_loop(
            work, duration_sec, n_threads, cancel_token=cancel_token
        )
        gbps = (total_bytes / elapsed / 1e9) if elapsed > 0 else 0.0
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=gbps,
            throughput_unit="GB/s",
            threads=n_threads,
            extra={"size_mb_per_array": self.SIZE_MB},
        )


# ─── Latency (pointer chase) ─────────────────────────────────────────────────


class BuiltinRamLatencyEngine(StressEngine):
    """Pointer-chasing: оценка средней латентности доступа к памяти.

    Заранее строится перетасованный «список» — последовательность индексов,
    где ``indices[i]`` указывает на следующее место в цепочке. На таких данных
    префетчер не может предсказать следующий адрес, и каждое чтение ловит
    cache-miss (если массив больше LLC).
    """

    name = "builtin_ram_lat"
    category = "ram_lat"
    is_external = False

    SIZE_MB = 64  # 8 МБ массива выходит за L3 у большинства CPU; 64 МБ — точно мимо.

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        n = (self.SIZE_MB * 1024 * 1024) // 8

        # Перемешанный список: каждый элемент — следующий индекс.
        rng = np.random.default_rng(seed=12345)
        order = rng.permutation(n).astype(np.int64)
        # Превращаем в «связанный список»: indices[order[i]] = order[i+1].
        indices = np.empty(n, dtype=np.int64)
        indices[order[:-1]] = order[1:]
        indices[order[-1]] = order[0]

        # Прогрев JIT.
        if HAVE_NUMBA:
            _pointer_chase(indices, 1024, 0)

        chunk = 2_000_000

        def work(remaining: float) -> float:
            start_pos = int(now() * 1e6) % n
            _pointer_chase(indices, chunk, start_pos)
            return float(chunk)

        total_accesses, elapsed = run_threaded_loop(
            work, duration_sec, n_threads, cancel_token=cancel_token
        )
        ns_per_access = (
            (elapsed * 1e9) / total_accesses if total_accesses > 0 else 0.0
        )
        # Делим на потоки, иначе складываем — каждый поток шёл параллельно.
        ns_per_access /= n_threads
        extra: dict[str, Any] = {
            "size_mb": self.SIZE_MB,
            "jit": "numba" if HAVE_NUMBA else "python",
        }
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=ns_per_access,
            throughput_unit="ns/access",
            threads=n_threads,
            extra=extra,
        )
