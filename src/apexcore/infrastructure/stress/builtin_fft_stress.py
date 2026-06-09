"""Стресс-движок «FFT» в стиле Prime95 Small/Large/Blend.

Prime95 Small FFT — индустриальный стандарт максимального FPU stress
(GIMPS, ArchWiki «Stress testing» [16]). Здесь реализован аналог поверх
``numpy.fft``: на современных сборках numpy это либо встроенный FFT
(pocketfft), либо MKL FFT — оба используют SIMD/AVX. Размер массива
определяется режимом:

- ``small`` — кэш-резидентный (≤ L2), максимум IPC, лучший прогрев
  логики ядра. Это то, что Prime95 Smallest FFT нагревает сильнее всего.
- ``large`` — > L3, нагружает контроллер памяти + FPU pipeline.
  Аналог Prime95 Large FFT.
- ``blend`` — чередование small/large в одном прогоне (Prime95 Blend).

Verify-режим. Сохраняем сумму амплитуд эталонного сигнала и сверяем
после каждых ``VERIFY_EVERY`` FFT (с допуском на численные шумы) —
несовпадение ⇒ ``error_count`` увеличивается (bit-flip / FPU-сбой).
"""

from __future__ import annotations

import threading
from typing import Any, Literal

import numpy as np

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads, run_threaded_loop
from apexcore.shared.timing import now

FftSize = Literal["small", "large", "blend"]


def _resolve_fft_n(size: FftSize, l2_bytes: int, l3_bytes: int) -> tuple[int, int]:
    """Подобрать число точек FFT. Возвращает (n_small, n_large).

    Один complex128-элемент = 16 байт. Для small берём L2/16, для large —
    max(L3·4/16, 1М). Всегда округляем вверх до степени двойки — pocketfft
    и MKL FFT именно на ней дают пиковую производительность.
    """
    n_small = max(2 ** 14, l2_bytes // 16)
    n_large = max(2 ** 20, (l3_bytes * 4) // 16)
    return _next_pow2(n_small), _next_pow2(n_large)


def _next_pow2(n: int) -> int:
    if n < 2:
        return 2
    p = 1
    while p < n:
        p <<= 1
    return p


class BuiltinFftStressEngine(StressEngine):
    """FFT-нагрузка в трёх режимах: small / large / blend.

    Имя движка фиксированное (``builtin_fft_stress``), режим задаётся через
    конструктор и попадает в ``extra``. Регистрационный реестр держит
    отдельные экземпляры для разных режимов.
    """

    name = "builtin_fft_stress"
    category = "cpu_fp"
    is_external = False

    VERIFY_EVERY = 8

    def __init__(
        self,
        size: FftSize = "large",
        l2_bytes: int | None = None,
        l3_bytes: int | None = None,
    ) -> None:
        self._size: FftSize = size
        self._l2_bytes = l2_bytes
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
        l2_bytes, l3_bytes = self._resolve_cache_bytes()
        n_small, n_large = _resolve_fft_n(self._size, l2_bytes, l3_bytes)

        # Эталон считаем для small-сигнала (быстрее) — этого достаточно
        # для детектирования сбоев FPU; сам прогон будет large/blend по запросу.
        rng = np.random.default_rng(seed=2026)
        signal_small = rng.standard_normal(n_small).astype(np.complex128)
        signal_large = rng.standard_normal(n_large).astype(np.complex128)
        # Норма Парсеваля сохраняется FFT'ом — если что-то "разъехалось",
        # это явный признак ошибки. Берём именно L2-норму (sum(|x|²)).
        ref_norm_small = float(np.sum(np.abs(signal_small) ** 2))
        ref_norm_large = float(np.sum(np.abs(signal_large) ** 2))
        # Допуск численного шума pocketfft/MKL: < 1e-7 относительно нормы.
        tol_small = ref_norm_small * 1e-7 + 1.0
        tol_large = ref_norm_large * 1e-7 + 1.0

        error_lock = threading.Lock()
        errors = [0]
        size = self._size
        verify_every = self.VERIFY_EVERY

        def work(remaining: float) -> float:
            local_iters = 0
            ops = 0.0
            local_deadline = now() + min(remaining, 0.5)
            # Каждый поток держит свою копию сигнала — иначе np.fft перезаписал
            # бы общий массив (pocketfft возвращает новый, но проще копировать).
            x_small = signal_small.copy()
            x_large = signal_large.copy()
            i = 0
            while now() < local_deadline:
                if size == "small":
                    y = np.fft.fft(x_small)
                    n = n_small
                    ref = ref_norm_small
                    tol = tol_small
                elif size == "large":
                    y = np.fft.fft(x_large)
                    n = n_large
                    ref = ref_norm_large
                    tol = tol_large
                else:  # blend — чередуем
                    if i % 2 == 0:
                        y = np.fft.fft(x_small)
                        n = n_small
                        ref = ref_norm_small
                        tol = tol_small
                    else:
                        y = np.fft.fft(x_large)
                        n = n_large
                        ref = ref_norm_large
                        tol = tol_large
                local_iters += 1
                # 5·n·log2(n) — стандартная формула для complex FFT.
                ops += 5.0 * n * float(np.log2(n))
                if local_iters % verify_every == 0:
                    norm = float(np.sum(np.abs(y) ** 2))
                    # FFT по pocketfft даёт sum(|Y|²) = N · sum(|x|²)
                    if abs(norm - ref * n) > tol * n:
                        with error_lock:
                            errors[0] += 1
                i += 1
            return ops

        runner_threads = 1 if (threads is None or threads <= 0) else n_threads
        total_ops, elapsed = run_threaded_loop(
            work, duration_sec, runner_threads, cancel_token=cancel_token
        )
        gflops = total_ops / elapsed / 1e9 if elapsed > 0 else 0.0
        extra: dict[str, Any] = {
            "size": self._size,
            "n_small": n_small,
            "n_large": n_large,
            "l2_bytes": l2_bytes,
            "l3_bytes": l3_bytes,
            "verify_every": verify_every,
            "fft_backend": "numpy.fft",
        }
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=gflops,
            throughput_unit="GFLOPS",
            threads=runner_threads,
            error_count=errors[0],
            extra=extra,
        )

    def _resolve_cache_bytes(self) -> tuple[int, int]:
        if self._l2_bytes is not None and self._l3_bytes is not None:
            return self._l2_bytes, self._l3_bytes
        l2 = self._l2_bytes
        l3 = self._l3_bytes
        try:
            from apexcore.infrastructure.adapters import AdapterFactory

            topology = AdapterFactory.detect().get_cache_topology()
            for level in topology.levels:
                if level.name == "L2" and l2 is None:
                    l2 = int(level.size_bytes)
                elif level.name == "L3" and l3 is None:
                    l3 = int(level.size_bytes)
        except Exception:
            pass
        return (l2 or 256 * 1024, l3 or 16 * 1024 * 1024)
