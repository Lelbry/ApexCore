"""Оркестратор «Оценок общей производительности».

Последовательный прогон:
    1. DGEMM (CPU+RAM compute via BLAS)
    2. короткий cooldown (5 с)
    3. STREAM (RAM bandwidth)
    4. cooldown
    5. disk_seq_read на boot-диске
    6. disk_random_read
    7. disk_seq_write (один проход ~256 МБ)

Без watchdog/SafetyGate (в отличие от стресса) — нагрузка короткая и не
рассчитана на проверку охлаждения. Cooldown между CPU-фазами нужен,
чтобы DGEMM не «съел» STREAM через термальный throttling.

Возвращает :class:`GeneralBenchmarkReport`. Если roofline недоступен
(неизвестный CPU/DRAM) или нет места на boot-диске — соответствующие
ratio = None, score = None, но отчёт всё равно собирается с заполненными
измерениями (пользователь видит, что измерено, что нет).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from apexcore.application.general_benchmark_score import (
    GeneralBenchmarkScoreContext,
    _disk_ratio_from_components,
    compute_general_benchmark_score,
)
from apexcore.application.roofline import compute_dram_peak, compute_flops_peak
from apexcore.domain.general_benchmark import GeneralBenchmarkReport
from apexcore.domain.ports import OSAdapter
from apexcore.infrastructure.disk_inventory import get_boot_drive
from apexcore.infrastructure.disk_peak import UNKNOWN_PROFILE, lookup_disk_peak
from apexcore.infrastructure.microbench.base import CancelledError
from apexcore.infrastructure.microbench.disk import (
    DiskRandomReadBench,
    DiskSequentialReadBench,
    DiskSequentialWriteBench,
)
from apexcore.infrastructure.stress.builtin_large_dgemm import BuiltinLargeDgemmEngine
from apexcore.infrastructure.stress.builtin_large_stream import BuiltinLargeStreamEngine

logger = logging.getLogger(__name__)

PhaseName = Literal[
    "dgemm",
    "stream",
    "disk_seq_read",
    "disk_random_read",
    "disk_seq_write",
]
ProgressCallback = Callable[[PhaseName, int, int], None]

PHASES: tuple[PhaseName, ...] = (
    "dgemm",
    "stream",
    "disk_seq_read",
    "disk_random_read",
    "disk_seq_write",
)

# По умолчанию каждый CPU-движок крутится ~30 с (хватает для устойчивого
# измерения BLAS-throughput'а на современных CPU). Disk-фазы короче —
# read достаточно 10 с warmup'нутого цикла, write фиксирован размером файла.
DEFAULT_CPU_PHASE_DURATION_SEC = 30.0
DEFAULT_DISK_READ_DURATION_SEC = 10.0
COOLDOWN_BETWEEN_CPU_PHASES_SEC = 5.0

# Минимальный запас места на boot-диске для disk-фаз:
# 256 МБ read-файл + 256 МБ write-файл + 512 МБ запас на FS overhead.
MIN_BOOT_DRIVE_FREE_BYTES = 1 * 1024**3


@dataclass
class GeneralBenchmarkParams:
    """Параметры одного прогона комплексного бенчмарка."""

    cpu_phase_duration_sec: float = DEFAULT_CPU_PHASE_DURATION_SEC
    disk_read_duration_sec: float = DEFAULT_DISK_READ_DURATION_SEC
    cooldown_sec: float = COOLDOWN_BETWEEN_CPU_PHASES_SEC


class GeneralBenchmarkOrchestrator:
    """Запускает все фазы и собирает :class:`GeneralBenchmarkReport`.

    Без репозитория: сохранение в БД делает вызывающий код (Screen / CLI-
    команда). Это упрощает unit-тесты — оркестратор pure от persistence.
    """

    def __init__(self, adapter: OSAdapter) -> None:
        self._adapter = adapter

    def run(
        self,
        params: GeneralBenchmarkParams | None = None,
        *,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> GeneralBenchmarkReport:
        params = params or GeneralBenchmarkParams()
        started_at = datetime.now(timezone.utc)
        system_info = self._adapter.get_system_info()

        # Roofline-пики (детерминированы по конфигу). None — не страшно,
        # просто соответствующий ratio станет None.
        dgemm_peak = compute_flops_peak(system_info, "dp")
        dram_peak_mb_s = compute_dram_peak(system_info)
        stream_peak_gb_s = (
            dram_peak_mb_s / 1000.0 if dram_peak_mb_s and dram_peak_mb_s > 0 else None
        )

        # Boot-диск + его media-профиль.
        boot_path, physical = get_boot_drive()
        if physical is not None:
            disk_profile = lookup_disk_peak(physical.media_type, physical.bus_type)
            disk_model = physical.model
            disk_media_type = physical.media_type
            disk_bus_type = physical.bus_type
        else:
            disk_profile = UNKNOWN_PROFILE
            disk_model = None
            disk_media_type = None
            disk_bus_type = None

        notes: list[str] = []

        # Sanity-check: в виртуальной среде compute_dram_peak часто врёт.
        if stream_peak_gb_s is not None and stream_peak_gb_s < 5.0:
            notes.append(
                "Очень низкий DRAM-пик (< 5 ГБ/с) — возможно, виртуальная среда; "
                "балл может быть некорректен."
            )

        # Проверка свободного места на boot-диске.
        disk_phase_skipped_reason: str | None = None
        try:
            free_bytes = shutil.disk_usage(boot_path).free
            if free_bytes < MIN_BOOT_DRIVE_FREE_BYTES:
                disk_phase_skipped_reason = (
                    f"На {boot_path} меньше 1 ГБ свободно "
                    f"({free_bytes / 1024**3:.2f} ГБ) — disk-фаза пропущена."
                )
        except OSError as exc:
            disk_phase_skipped_reason = (
                f"shutil.disk_usage({boot_path}) упал: {exc}; disk-фаза пропущена."
            )
        if disk_phase_skipped_reason:
            notes.append(disk_phase_skipped_reason)

        # DGEMM/STREAM используют builtin_large_*-движки (BLAS параллелит ВНУТРИ).
        # CPU-фазу запускаем в 1 python-поток + threadpool_limits: иначе N
        # python-потоков × np.matmul плодят N буферов C (~128 МБ каждый) и едят
        # память → на машинах с малым ОЗУ (напр. 14.8 ГБ) система уходит в OOM
        # вместе с браузером. STREAM — memory-bound, держим ~logical/4 потоков.
        _logical = os.cpu_count() or 4
        _blas_limit = max(2, _logical - 2)
        _ram_threads = max(2, _logical // 4)
        try:
            from threadpoolctl import threadpool_limits as _threadpool_limits
        except Exception:  # pragma: no cover — зависимость объявлена
            _threadpool_limits = None

        # ─── Фаза 1: DGEMM ────────────────────────────────────────────────
        dgemm_gflops: float | None = None
        dgemm_duration = 0.0
        cancelled = False
        try:
            if on_progress is not None:
                on_progress("dgemm", 1, len(PHASES))
            dgemm = BuiltinLargeDgemmEngine()
            _blas_ctx = (
                _threadpool_limits(limits=_blas_limit)
                if _threadpool_limits is not None
                else contextlib.nullcontext()
            )
            with _blas_ctx:
                res = dgemm.run(
                    duration_sec=params.cpu_phase_duration_sec,
                    threads=1,
                    cancel_token=cancel_token,
                )
            dgemm_duration = res.duration_actual_sec
            if res.throughput > 0:
                dgemm_gflops = float(res.throughput)
        except CancelledError:
            cancelled = True
            notes.append("DGEMM прерван пользователем")
        except Exception as exc:
            logger.exception("DGEMM фаза упала: %s", exc)
            notes.append(f"DGEMM фаза упала: {exc}")

        # Cooldown CPU.
        if not cancelled:
            _interruptible_sleep(params.cooldown_sec, cancel_token)

        # ─── Фаза 2: STREAM ───────────────────────────────────────────────
        stream_gb_s: float | None = None
        stream_duration = 0.0
        if not cancelled:
            try:
                if on_progress is not None:
                    on_progress("stream", 2, len(PHASES))
                stream = BuiltinLargeStreamEngine()
                res = stream.run(
                    duration_sec=params.cpu_phase_duration_sec,
                    threads=_ram_threads,
                    cancel_token=cancel_token,
                )
                stream_duration = res.duration_actual_sec
                if res.throughput > 0:
                    stream_gb_s = float(res.throughput)
            except CancelledError:
                cancelled = True
                notes.append("STREAM прерван пользователем")
            except Exception as exc:
                logger.exception("STREAM фаза упала: %s", exc)
                notes.append(f"STREAM фаза упала: {exc}")

        # Cooldown перед disk-фазами.
        if not cancelled and disk_phase_skipped_reason is None:
            _interruptible_sleep(params.cooldown_sec, cancel_token)

        # ─── Disk-фазы ────────────────────────────────────────────────────
        disk_seq_read_mb_s: float | None = None
        disk_random_read_mb_s: float | None = None
        disk_seq_write_mb_s: float | None = None
        disk_seq_read_duration = 0.0
        disk_random_read_duration = 0.0
        disk_seq_write_duration = 0.0

        if not cancelled and disk_phase_skipped_reason is None:
            try:
                if on_progress is not None:
                    on_progress("disk_seq_read", 3, len(PHASES))
                bench = DiskSequentialReadBench(target_dir=boot_path)
                res = bench.run(
                    duration_sec=params.disk_read_duration_sec,
                    cancel_token=cancel_token,
                )
                disk_seq_read_mb_s = float(res.value)
                disk_seq_read_duration = res.duration_actual_sec
            except CancelledError:
                cancelled = True
                notes.append("disk_seq_read прерван пользователем")
            except Exception as exc:
                logger.exception("disk_seq_read упал: %s", exc)
                notes.append(f"disk_seq_read упал: {exc}")

        if not cancelled and disk_phase_skipped_reason is None:
            try:
                if on_progress is not None:
                    on_progress("disk_random_read", 4, len(PHASES))
                bench = DiskRandomReadBench(target_dir=boot_path)
                res = bench.run(
                    duration_sec=params.disk_read_duration_sec,
                    cancel_token=cancel_token,
                )
                disk_random_read_mb_s = float(res.value)
                disk_random_read_duration = res.duration_actual_sec
            except CancelledError:
                cancelled = True
                notes.append("disk_random_read прерван пользователем")
            except Exception as exc:
                logger.exception("disk_random_read упал: %s", exc)
                notes.append(f"disk_random_read упал: {exc}")

        if not cancelled and disk_phase_skipped_reason is None:
            try:
                if on_progress is not None:
                    on_progress("disk_seq_write", 5, len(PHASES))
                bench = DiskSequentialWriteBench(target_dir=boot_path)
                res = bench.run(
                    duration_sec=0.0,  # write всегда фиксированный размер
                    cancel_token=cancel_token,
                )
                disk_seq_write_mb_s = float(res.value)
                disk_seq_write_duration = res.duration_actual_sec
            except CancelledError:
                cancelled = True
                notes.append("disk_seq_write прерван пользователем")
            except Exception as exc:
                logger.exception("disk_seq_write упал: %s", exc)
                notes.append(f"disk_seq_write упал: {exc}")

        # ─── Расчёт ratio + score ─────────────────────────────────────────
        r_dgemm = (
            dgemm_gflops / dgemm_peak
            if dgemm_gflops is not None and dgemm_peak and dgemm_peak > 0
            else None
        )
        r_stream = (
            stream_gb_s / stream_peak_gb_s
            if stream_gb_s is not None and stream_peak_gb_s and stream_peak_gb_s > 0
            else None
        )
        r_disk = _disk_ratio_from_components(
            seq_read_ratio=(
                disk_seq_read_mb_s / disk_profile.seq_read_mb_s
                if disk_seq_read_mb_s is not None
                else None
            ),
            random_read_ratio=(
                disk_random_read_mb_s / disk_profile.random_read_mb_s
                if disk_random_read_mb_s is not None
                else None
            ),
            seq_write_ratio=(
                disk_seq_write_mb_s / disk_profile.seq_write_mb_s
                if disk_seq_write_mb_s is not None
                else None
            ),
        )
        score = compute_general_benchmark_score(r_dgemm, r_stream, r_disk)

        ended_at = datetime.now(timezone.utc)

        return GeneralBenchmarkReport(
            system_info=system_info,
            started_at=started_at,
            ended_at=ended_at,
            dgemm_duration_sec=dgemm_duration,
            stream_duration_sec=stream_duration,
            disk_seq_read_duration_sec=disk_seq_read_duration,
            disk_random_read_duration_sec=disk_random_read_duration,
            disk_seq_write_duration_sec=disk_seq_write_duration,
            dgemm_gflops=dgemm_gflops,
            stream_gb_s=stream_gb_s,
            disk_seq_read_mb_s=disk_seq_read_mb_s,
            disk_random_read_mb_s=disk_random_read_mb_s,
            disk_seq_write_mb_s=disk_seq_write_mb_s,
            dgemm_peak_gflops=dgemm_peak,
            stream_peak_gb_s=stream_peak_gb_s,
            disk_seq_read_peak_mb_s=disk_profile.seq_read_mb_s,
            disk_random_read_peak_mb_s=disk_profile.random_read_mb_s,
            disk_seq_write_peak_mb_s=disk_profile.seq_write_mb_s,
            r_dgemm=_min1(r_dgemm),
            r_stream=_min1(r_stream),
            r_disk=r_disk,
            score=score,
            boot_drive_path=boot_path,
            disk_model=disk_model,
            disk_media_type=disk_media_type,
            disk_bus_type=disk_bus_type,
            disk_media_label=disk_profile.media_label,
            notes=notes,
            cancelled=cancelled,
        )


def _min1(r: float | None) -> float | None:
    """Тот же clamp ≤ 1.0 что и в general_benchmark_score, но для удобства
    использования в отчёте (мы сохраняем clamp'нутые ratio — пользователь
    не должен видеть «1.23» в полях r_*, иначе непонятно, почему GM(1.23,
    0.5, 0.4) ≠ 0.7)."""
    if r is None or r <= 0:
        return None
    return min(r, 1.0)


def _interruptible_sleep(
    duration_sec: float, cancel_token: threading.Event | None
) -> None:
    """``time.sleep`` с проверкой ``cancel_token`` каждые 0.1 с."""
    if duration_sec <= 0:
        return
    deadline = time.perf_counter() + duration_sec
    while time.perf_counter() < deadline:
        if cancel_token is not None and cancel_token.is_set():
            return
        time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))


# Pure-функция собирателя контекста (без запуска движков) — для тестов score
# на готовых измерениях. Симметрично compute_stress_score_context.
def compute_general_benchmark_context_from_report(
    report: GeneralBenchmarkReport,
) -> GeneralBenchmarkScoreContext:
    """Свернуть готовый отчёт в :class:`GeneralBenchmarkScoreContext`."""
    return GeneralBenchmarkScoreContext(
        dgemm_gflops=report.dgemm_gflops,
        stream_gb_s=report.stream_gb_s,
        disk_seq_read_mb_s=report.disk_seq_read_mb_s,
        disk_random_read_mb_s=report.disk_random_read_mb_s,
        disk_seq_write_mb_s=report.disk_seq_write_mb_s,
        dgemm_peak_gflops=report.dgemm_peak_gflops,
        stream_peak_gb_s=report.stream_peak_gb_s,
        disk_seq_read_peak_mb_s=report.disk_seq_read_peak_mb_s,
        disk_random_read_peak_mb_s=report.disk_random_read_peak_mb_s,
        disk_seq_write_peak_mb_s=report.disk_seq_write_peak_mb_s,
        r_dgemm=report.r_dgemm,
        r_stream=report.r_stream,
        r_disk=report.r_disk,
        score=report.score,
        disk_media_label=report.disk_media_label,
        boot_drive_path=report.boot_drive_path,
    )


__all__ = [
    "COOLDOWN_BETWEEN_CPU_PHASES_SEC",
    "DEFAULT_CPU_PHASE_DURATION_SEC",
    "DEFAULT_DISK_READ_DURATION_SEC",
    "PHASES",
    "GeneralBenchmarkOrchestrator",
    "GeneralBenchmarkParams",
    "PhaseName",
    "ProgressCallback",
    "compute_general_benchmark_context_from_report",
]
