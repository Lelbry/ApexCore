"""Расширенный STREAM Triad с verify-режимом и динамическим размером.

Существующий ``BuiltinRamBandwidthEngine`` использует фиксированные 256 МБ
без verify (это бенчмарк sustained bandwidth по McCalpin 1995, не
валидатор). Этот движок добавляет два важных свойства, нужных для
``system_stress_full``:

1. Динамический размер. Берём ``min(70% свободной RAM, 4·L3)`` — это
   гарантирует, что массив не помещается в L3 (как требует Run Rules
   STREAM) и одновременно не выдавливает соседние процессы из памяти.
2. Verify. После каждой N-й итерации сверяем выборочные элементы ``c[i]``
   с ожидаемым значением ``a[i] + s·b[i]``. Расхождение ⇒ ``error_count``.
   Это native-аналог опции ``--verify`` у stress-ng [13].

Внутреннее ядро — те же ``np.multiply`` + ``np.add``, что у базового
движка, но операции выполняются на более крупных массивах и с verify.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import psutil

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads, run_threaded_loop
from apexcore.shared.timing import now


class BuiltinLargeStreamEngine(StressEngine):
    """STREAM Triad с verify, динамическим размером и явным pass/fail."""

    name = "builtin_large_stream"
    category = "ram_bw"
    is_external = False

    VERIFY_EVERY = 4
    VERIFY_SAMPLES = 1024  # Сколько индексов проверяем (random sub-sample).
    MIN_SIZE_MB = 256
    MAX_SIZE_MB = 4096  # 4 ГБ массива × 3 = 12 ГБ — потолок, дальше отказ.

    def __init__(
        self,
        size_mb: int | None = None,
        l3_bytes: int | None = None,
    ) -> None:
        self._size_mb = size_mb
        self._l3_bytes = l3_bytes

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        size_mb = self._resolve_size_mb()
        n_elements = (size_mb * 1024 * 1024) // 8
        scalar = 3.0
        verify_every = self.VERIFY_EVERY

        # Индексы для verify-сверки выбираем один раз (детерминированно).
        rng = np.random.default_rng(seed=20260507)
        sample_idx = rng.integers(low=0, high=n_elements, size=self.VERIFY_SAMPLES)

        error_lock = threading.Lock()
        errors = [0]

        def work(remaining: float) -> float:
            # Каждый поток получает свои буферы (избегаем cache-line
            # bouncing через MESI на multi-CCX/multi-socket системах).
            a = np.full(n_elements, 1.0, dtype=np.float64)
            b = np.full(n_elements, 2.0, dtype=np.float64)
            c = np.empty(n_elements, dtype=np.float64)
            iters = 0
            local_deadline = now() + min(remaining, 0.5)
            expected_value = 1.0 + scalar * 2.0  # 1 + 3·2 = 7
            tol = abs(expected_value) * 1e-12 + 1e-12
            while now() < local_deadline:
                np.multiply(b, scalar, out=c)
                np.add(a, c, out=c)
                iters += 1
                if iters % verify_every == 0:
                    sampled = c[sample_idx]
                    bad = np.abs(sampled - expected_value) > tol
                    if bool(bad.any()):
                        with error_lock:
                            errors[0] += int(bad.sum())
            return float(iters * 3 * n_elements * 8)

        total_bytes, elapsed = run_threaded_loop(
            work, duration_sec, n_threads, cancel_token=cancel_token
        )
        gbps = (total_bytes / elapsed / 1e9) if elapsed > 0 else 0.0
        extra: dict[str, Any] = {
            "size_mb_per_array": size_mb,
            "verify_every": verify_every,
            "verify_samples": self.VERIFY_SAMPLES,
            "n_elements": n_elements,
        }
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=gbps,
            throughput_unit="GB/s",
            threads=n_threads,
            error_count=errors[0],
            extra=extra,
        )

    def _resolve_size_mb(self) -> int:
        if self._size_mb is not None:
            return max(self.MIN_SIZE_MB, min(self.MAX_SIZE_MB, self._size_mb))
        # min(70% свободной RAM на ПОТОК, 4·L3). По одному массиву на поток —
        # три буфера (a, b, c), поэтому делим бюджет на 3.
        free_mb = self._free_ram_mb()
        per_thread_budget = (free_mb * 0.7) / 3
        l3_mb = self._resolve_l3_mb()
        target = min(per_thread_budget, 4 * l3_mb)
        return int(max(self.MIN_SIZE_MB, min(self.MAX_SIZE_MB, target)))

    def _free_ram_mb(self) -> float:
        try:
            return psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return float(self.MIN_SIZE_MB * 4)  # 1 ГБ — безопасный дефолт

    def _resolve_l3_mb(self) -> float:
        if self._l3_bytes is not None:
            return self._l3_bytes / (1024 * 1024)
        try:
            from apexcore.infrastructure.adapters import AdapterFactory

            topology = AdapterFactory.detect().get_cache_topology()
            for level in topology.levels:
                if level.name == "L3":
                    return level.size_bytes / (1024 * 1024)
        except Exception:
            pass
        return 16.0  # 16 МБ — типичный L3 для потребительских CPU 2024–2026
