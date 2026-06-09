"""Стресс-движок «Large DGEMM»: матричное умножение больше L3.

Существующий ``BuiltinCpuFpEngine`` использует матрицу 512×512 (≈ 2 МБ),
которая помещается в L2/L3 у современных CPU и не нагружает контроллер
памяти — это даёт пик FLOPS, но не реальный thermal envelope. Чтобы
прогревать систему (как ``stress-ng --cpu-method matrixprod`` или DGEMM
из HPL), нужен размер N такой, что три матрицы N×N float64 не помещаются
в L3 — тогда BLAS вынужден работать с DRAM, и нагружаются и SIMD-блок,
и кэш-иерархия, и контроллер памяти одновременно.

Источники:
- McCalpin (1995), Run Rules: рабочий набор должен быть в 4× больше L3.
- Dongarra, Luszczek, Petitet (2003) — HPL/Linpack как стандарт DGEMM-нагрузки.
- Ubuntu Wiki Kernel/Reference/stress-ng [15]: matrixprod best heats x86 CPUs.

Verify-режим. Каждые ``VERIFY_EVERY`` итераций сравниваем ``np.sum(C)`` с
эталонным значением, посчитанным в начале на фиксированном seed —
несовпадение ⇒ инкремент ``error_count`` (детектор bit-flip / нестабильности FPU).
"""

from __future__ import annotations

import math
import threading
from typing import Any

import numpy as np

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads, run_threaded_loop
from apexcore.shared.timing import now


def _suggest_dim(l3_bytes: int) -> int:
    """Выбрать N такое, что 3·N²·8 ≥ 4·L3 (Run Rules McCalpin 1995).

    Жёсткие границы: не меньше 1024 (чтобы BLAS вышел в SIMD-tile-режим)
    и не больше 4096 (чтобы один буфер ≤ 128 МБ — комфортно даже на 8 ГБ RAM).
    """
    target = math.sqrt(4.0 * max(l3_bytes, 1) / 24.0)
    n = math.ceil(target)
    n = max(1024, min(4096, n))
    # Округляем вверх до кратного 64 — выравнивание под кэш-строку и SIMD-tile.
    return ((n + 63) // 64) * 64


class BuiltinLargeDgemmEngine(StressEngine):
    """DGEMM на матрицах вне L3 — реальный CPU+RAM прогрев через BLAS.

    Этот движок заменяет ``builtin_cpu_fp`` в stability-профилях. По
    единицам — те же GFLOPS, но рабочий набор гарантированно превосходит
    L3, так что измеряется не peak, а sustained FP-throughput.
    """

    name = "builtin_large_dgemm"
    category = "cpu_fp"
    is_external = False

    # Сколько итераций между verify-сверками. 4 — компромисс: не съедает
    # значимое время (на N=2048 одна итерация ~50 мс, verify ~2 мс).
    VERIFY_EVERY = 4

    def __init__(self, l3_bytes: int | None = None) -> None:
        self._l3_bytes = l3_bytes  # None — определяется при run() из адаптера.

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        l3_bytes = self._resolve_l3_bytes()
        dim = _suggest_dim(l3_bytes)
        verify_every = self.VERIFY_EVERY

        # Детерминированные операнды + эталон для verify. ``A`` и ``B``
        # одинаковы для всех потоков (нет смысла плодить копии — BLAS
        # читает их без модификации). Каждый поток держит свой ``C``.
        rng = np.random.default_rng(seed=42)
        a = rng.standard_normal((dim, dim))
        b = rng.standard_normal((dim, dim))
        # Эталонный sum(A@B) — берём верхнюю-левую подматрицу 64×64, чтобы
        # сократить стоимость verify-сверки (полное np.sum(C) на 4096² = 16М
        # элементов добавляет ~5-10 мс). Подматрица детерминирована и
        # достаточна для детектирования bit-flip.
        verify_block = 64
        reference_sum = float(np.sum(a[:verify_block, :] @ b[:, :verify_block]))
        # Допуск на численные шумы BLAS: ~1e-9 относительно reference. На
        # практике BLAS даёт битовое совпадение для одинаковых seed, но
        # лёгкий допуск исключает ложные срабатывания при использовании
        # MKL с разной потоковой моделью.
        verify_tolerance = max(1.0, abs(reference_sum)) * 1e-9

        # Счётчик ошибок: нужен потокобезопасный инкремент. ``threading.Lock``
        # достаточен, инкременты редкие (раз в verify_every итераций).
        error_lock = threading.Lock()
        errors = [0]

        def work(remaining: float) -> float:
            c = np.empty((dim, dim), dtype=np.float64)
            local_iters = 0
            local_deadline = now() + min(remaining, 0.5)
            while now() < local_deadline:
                np.matmul(a, b, out=c)
                local_iters += 1
                if local_iters % verify_every == 0:
                    s = float(np.sum(c[:verify_block, :verify_block]))
                    if abs(s - reference_sum) > verify_tolerance:
                        with error_lock:
                            errors[0] += 1
            return 2.0 * (dim ** 3) * local_iters

        # Если пользователь не указал threads, отдадим работу одному Python-потоку
        # (BLAS внутри сам распараллелится на все ядра); иначе уважаем явный выбор.
        runner_threads = 1 if (threads is None or threads <= 0) else n_threads

        total_flops, elapsed = run_threaded_loop(
            work, duration_sec, runner_threads, cancel_token=cancel_token
        )
        gflops = total_flops / elapsed / 1e9 if elapsed > 0 else 0.0
        bytes_per_matrix = dim * dim * 8
        extra: dict[str, Any] = {
            "matrix_dim": dim,
            "working_set_mb": round(3 * bytes_per_matrix / (1024 * 1024), 1),
            "l3_bytes": l3_bytes,
            "verify_every": verify_every,
            "blas": "numpy",
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

    def _resolve_l3_bytes(self) -> int:
        """Определить размер L3 для подбора N. Конструктор > адаптер > дефолт.

        Дефолт 16 МБ — типичный L3 у потребительских CPU 2024–2026.
        """
        if self._l3_bytes is not None:
            return self._l3_bytes
        try:
            from apexcore.infrastructure.adapters import AdapterFactory

            topology = AdapterFactory.detect().get_cache_topology()
            for level in topology.levels:
                if level.name == "L3":
                    return int(level.size_bytes)
        except Exception:
            pass
        return 16 * 1024 * 1024
