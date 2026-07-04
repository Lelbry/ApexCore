"""Оркестратор GPU-бенчмарка (кроссвендорный OpenCL-путь, Roofline).

Спецификация: ``new-app/docs/gpu_benchmark.md`` §8. Последовательный прогон
фаз для выбранного устройства с коротким cooldown между ними (по образцу
:mod:`general_benchmark`):

    0. enumerate + resolve peak (compute_gpu_peak)
    1. FP32                       → fp32_gflops
    2. cooldown
    3. FP64 (только если поддерживается) → fp64_gflops (вне балла)
    4. cooldown
    5. MEM_BANDWIDTH (STREAM-triad) → mem_bandwidth_gb_s
    6. cooldown
    7. PCIe H2D → pcie_h2d_gb_s, затем PCIe D2H → pcie_d2h_gb_s (вне балла)
    8. scoring: r_fp32, r_mem, score = GM(r_fp32, r_mem) × 10 000

Graceful degrade: если бэкенд недоступен или устройств нет — возвращаем
отчёт с ``notes=["OpenCL/GPU недоступен …"]``, ``score=None``,
``cancelled=False`` (не бросаем исключение — тот же принцип, что у сенсоров
и общей оценки). FP64/PCIe заполняют отчёт, но на ``score`` не влияют:
обнуляют балл только пропажа FP32, VRAM или их архитектурных пиков.

Возвращает :class:`GpuBenchmarkReport`. Сохранение в БД делает вызывающий
код (Screen / CLI / WebUI-контроллер) — оркестратор pure от persistence,
как и :class:`GeneralBenchmarkOrchestrator`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from apexcore.application.gpu_benchmark_score import (
    GpuBenchmarkScoreContext,
    _clamp_ratio,
    compute_gpu_benchmark_score,
)
from apexcore.application.gpu_roofline import (
    compute_gpu_peak,
    integrated_gpu_mem_bandwidth_peak_gb_s,
)
from apexcore.domain.gpu import (
    GpuBenchmarkReport,
    GpuDeviceInfo,
    GpuDeviceType,
    GpuMeasurement,
    GpuWorkloadKind,
)
from apexcore.domain.ports import GpuComputeBackend, OSAdapter

logger = logging.getLogger(__name__)

PhaseName = Literal[
    "fp32",
    "fp64",
    "mem_bandwidth",
    "pcie_h2d",
    "pcie_d2h",
]
ProgressCallback = Callable[[PhaseName, int, int], None]

# Порядок фаз (для прогресса). FP64 и PCIe присутствуют всегда — если
# устройство FP64 не поддерживает, фаза пропускается, но нумерация total
# сохраняется стабильной (пользователь видит «пропущено», а не сдвиг).
PHASES: tuple[PhaseName, ...] = (
    "fp32",
    "fp64",
    "mem_bandwidth",
    "pcie_h2d",
    "pcie_d2h",
)

# Compute-фазы (FP32/FP64/MEM) крутятся ~5 с — этого хватает для устойчивого
# измерения throughput'а OpenCL-кернела. PCIe-копирование короче (~2 с):
# оно ограничено шиной, дольше гонять смысла нет. Cooldown между фазами —
# как в общей оценке, чтобы предыдущий кернел не «съел» следующий через
# термальный троттлинг GPU.
DEFAULT_COMPUTE_PHASE_DURATION_SEC = 5.0
DEFAULT_MEM_PHASE_DURATION_SEC = 5.0
DEFAULT_PCIE_PHASE_DURATION_SEC = 2.0
COOLDOWN_BETWEEN_PHASES_SEC = 2.0


@dataclass
class GpuBenchmarkParams:
    """Параметры одного прогона GPU-бенчмарка."""

    fp32_duration_sec: float = DEFAULT_COMPUTE_PHASE_DURATION_SEC
    fp64_duration_sec: float = DEFAULT_COMPUTE_PHASE_DURATION_SEC
    mem_duration_sec: float = DEFAULT_MEM_PHASE_DURATION_SEC
    pcie_duration_sec: float = DEFAULT_PCIE_PHASE_DURATION_SEC
    cooldown_sec: float = COOLDOWN_BETWEEN_PHASES_SEC


class GpuBenchmarkOrchestrator:
    """Запускает все фазы для выбранного GPU и собирает :class:`GpuBenchmarkReport`.

    Конструктор принимает адаптер ОС (для снимка :class:`SystemInfo`) и
    :class:`GpuComputeBackend` (перечисление устройств + замеры). Бэкенд
    инъектируется явно — так unit-тесты подставляют in-memory fake без
    реального OpenCL/железа (симметрично тому, как general_benchmark берёт
    адаптер, но здесь дополнительно нужен GPU-порт).
    """

    def __init__(self, adapter: OSAdapter, backend: GpuComputeBackend) -> None:
        self._adapter = adapter
        self._backend = backend

    def run(
        self,
        device_index: int = 0,
        params: GpuBenchmarkParams | None = None,
        *,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> GpuBenchmarkReport:
        params = params or GpuBenchmarkParams()
        started_at = datetime.now(timezone.utc)
        system_info = self._adapter.get_system_info()

        # ─── Фаза 0: доступность бэкенда + перечисление устройств ──────────
        devices: list[GpuDeviceInfo] = []
        if self._backend.is_available():
            try:
                devices = self._backend.list_devices()
            except Exception as exc:  # graceful degrade — не роняем прогон
                logger.exception("list_devices() упал: %s", exc)
                devices = []

        if not devices:
            return _unavailable_report(system_info, started_at)

        if device_index < 0 or device_index >= len(devices):
            return _unavailable_report(
                system_info,
                started_at,
                note=(
                    f"Запрошен device_index={device_index}, но обнаружено "
                    f"{len(devices)} устройств — прогон невозможен."
                ),
                device=devices[0] if devices else None,
            )

        device = devices[device_index]
        notes: list[str] = []

        # ─── Фаза 0b: Roofline-пики устройства ────────────────────────────
        peak = compute_gpu_peak(device)
        # iGPU не имеет собственной VRAM (память общая с CPU) → per-model пик
        # неизвестен и compute_gpu_peak вернул mem-пик None. Берём потолок из
        # пропускной способности системной DRAM (тот же CPU/RAM-Roofline, что
        # у «Общей оценки»/стресс-балла) — иначе r_mem=None обнулил бы весь
        # балл встроенной графики. env-override APEXCORE_GPU_MEM_PEAK_GB_S
        # уже учтён внутри compute_gpu_peak (тогда mem-пик не None → сюда не
        # заходим и остаётся приоритетным).
        if (
            device.device_type == GpuDeviceType.INTEGRATED
            and peak.mem_bandwidth_peak_gb_s is None
        ):
            igpu_mem = integrated_gpu_mem_bandwidth_peak_gb_s(system_info)
            if igpu_mem is not None:
                gb_s, mem_note = igpu_mem
                peak = peak.model_copy(
                    update={
                        "mem_bandwidth_peak_gb_s": gb_s,
                        "source": _extend_source(peak.source, "igpu_dram"),
                        "notes": [*peak.notes, mem_note],
                    }
                )
        notes.extend(peak.notes)
        # arch, разрешённый roofline'ом, кладём и в отчётное устройство —
        # чтобы UI/БД видели ключ архитектуры даже если он не пришёл из OpenCL.
        if peak.arch is not None and device.arch is None:
            device = device.model_copy(update={"arch": peak.arch})

        # ─── Compute-фазы ─────────────────────────────────────────────────
        fp32_gflops: float | None = None
        fp64_gflops: float | None = None
        mem_bandwidth_gb_s: float | None = None
        pcie_h2d_gb_s: float | None = None
        pcie_d2h_gb_s: float | None = None
        fp32_duration = 0.0
        fp64_duration = 0.0
        mem_duration = 0.0
        pcie_duration = 0.0
        cancelled = False

        total = len(PHASES)

        # Фаза 1: FP32.
        if not cancelled and _is_cancelled(cancel_token):
            cancelled = True
        if not cancelled:
            if on_progress is not None:
                on_progress("fp32", 1, total)
            fp32_gflops, fp32_duration = self._measure(
                device_index,
                GpuWorkloadKind.FP32,
                params.fp32_duration_sec,
                cancel_token,
                notes,
                "FP32",
            )
            cancelled = _is_cancelled(cancel_token)

        # Cooldown.
        if not cancelled:
            _interruptible_sleep(params.cooldown_sec, cancel_token)
            cancelled = _is_cancelled(cancel_token)

        # Фаза 2: FP64 (только если поддерживается устройством И бэкендом).
        if not cancelled:
            fp64_ok = device.fp64_supported and self._backend.supports(
                device_index, GpuWorkloadKind.FP64
            )
            if fp64_ok:
                if on_progress is not None:
                    on_progress("fp64", 2, total)
                fp64_gflops, fp64_duration = self._measure(
                    device_index,
                    GpuWorkloadKind.FP64,
                    params.fp64_duration_sec,
                    cancel_token,
                    notes,
                    "FP64",
                )
                cancelled = _is_cancelled(cancel_token)
            else:
                notes.append(
                    "FP64 не поддерживается устройством — фаза пропущена "
                    "(на балл не влияет)"
                )

        # Cooldown.
        if not cancelled:
            _interruptible_sleep(params.cooldown_sec, cancel_token)
            cancelled = _is_cancelled(cancel_token)

        # Фаза 3: MEM_BANDWIDTH.
        if not cancelled:
            if on_progress is not None:
                on_progress("mem_bandwidth", 3, total)
            mem_bandwidth_gb_s, mem_duration = self._measure(
                device_index,
                GpuWorkloadKind.MEM_BANDWIDTH,
                params.mem_duration_sec,
                cancel_token,
                notes,
                "MEM_BANDWIDTH",
            )
            cancelled = _is_cancelled(cancel_token)

        # Cooldown.
        if not cancelled:
            _interruptible_sleep(params.cooldown_sec, cancel_token)
            cancelled = _is_cancelled(cancel_token)

        # Фаза 4: PCIe H2D, затем D2H (обе информационные, вне балла).
        if not cancelled:
            if on_progress is not None:
                on_progress("pcie_h2d", 4, total)
            pcie_h2d_gb_s, d_h2d = self._measure(
                device_index,
                GpuWorkloadKind.PCIE_H2D,
                params.pcie_duration_sec,
                cancel_token,
                notes,
                "PCIe H2D",
            )
            pcie_duration += d_h2d
            cancelled = _is_cancelled(cancel_token)

        if not cancelled:
            if on_progress is not None:
                on_progress("pcie_d2h", 5, total)
            pcie_d2h_gb_s, d_d2h = self._measure(
                device_index,
                GpuWorkloadKind.PCIE_D2H,
                params.pcie_duration_sec,
                cancel_token,
                notes,
                "PCIe D2H",
            )
            pcie_duration += d_d2h
            cancelled = _is_cancelled(cancel_token)

        if cancelled:
            notes.append("Прогон прерван пользователем")

        # ─── Фаза 5: ratio + score ────────────────────────────────────────
        r_fp32 = _ratio(fp32_gflops, peak.fp32_peak_gflops)
        r_mem = _ratio(mem_bandwidth_gb_s, peak.mem_bandwidth_peak_gb_s)
        r_fp64 = _ratio(fp64_gflops, peak.fp64_peak_gflops)  # информационный

        if r_fp32 is None:
            notes.append("r_fp32 недоступен (нет замера FP32 или его пика) → балл не построен")
        if r_mem is None:
            notes.append("r_mem недоступен (нет замера VRAM или её пика) → балл не построен")

        score = compute_gpu_benchmark_score(r_fp32, r_mem)  # FP64 вне score

        ended_at = datetime.now(timezone.utc)

        return GpuBenchmarkReport(
            system_info=system_info,
            device=device,
            started_at=started_at,
            ended_at=ended_at,
            fp32_duration_sec=fp32_duration,
            fp64_duration_sec=fp64_duration,
            mem_bandwidth_duration_sec=mem_duration,
            pcie_duration_sec=pcie_duration,
            fp32_gflops=fp32_gflops,
            fp64_gflops=fp64_gflops,
            mem_bandwidth_gb_s=mem_bandwidth_gb_s,
            pcie_h2d_gb_s=pcie_h2d_gb_s,
            pcie_d2h_gb_s=pcie_d2h_gb_s,
            fp32_peak_gflops=peak.fp32_peak_gflops,
            fp64_peak_gflops=peak.fp64_peak_gflops,
            mem_bandwidth_peak_gb_s=peak.mem_bandwidth_peak_gb_s,
            r_fp32=_clamp_ratio(r_fp32),
            r_fp64=_clamp_ratio(r_fp64),
            r_mem=_clamp_ratio(r_mem),
            score=score,
            arch=peak.arch,
            peak_source=peak.source,
            notes=notes,
            cancelled=cancelled,
        )

    def _measure(
        self,
        device_index: int,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None,
        notes: list[str],
        label: str,
    ) -> tuple[float | None, float]:
        """Прогнать одну фазу через бэкенд. Возвращает (throughput|None, длит.).

        Исключение бэкенда не роняет прогон — фаза помечается note'ой, её
        измерение остаётся ``None`` (соответствующий ratio станет None).
        """
        try:
            m: GpuMeasurement = self._backend.measure(
                device_index, kind, duration_sec, cancel_token
            )
        except Exception as exc:
            logger.exception("%s фаза упала: %s", label, exc)
            notes.append(f"{label} фаза упала: {exc}")
            return None, 0.0
        value = float(m.throughput) if m.throughput and m.throughput > 0 else None
        if value is None:
            notes.append(f"{label}: нулевой throughput — измерение отброшено")
        return value, float(m.duration_sec)


def _ratio(measured: float | None, peak: float | None) -> float | None:
    """``measured / peak`` либо None, если любой из них отсутствует/≤0."""
    if measured is None or peak is None or peak <= 0:
        return None
    return measured / peak


def _extend_source(source: str, tag: str) -> str:
    """Дописать ``tag`` в ``peak.source`` (``"a+b"``-формат), без дублей.

    ``compute_gpu_peak`` кодирует происхождение пика как отсортированный
    ``"+".join(...)`` (или ``"fallback"`` / ``"unknown"``). При добавлении
    iGPU-DRAM-пика помечаем это меткой ``igpu_dram``, сохраняя формат.
    """
    parts = [p for p in source.split("+") if p and p not in ("unknown",)]
    if tag not in parts:
        parts.append(tag)
    return "+".join(parts) if parts else tag


def _is_cancelled(cancel_token: threading.Event | None) -> bool:
    """True, если токен отмены задан и установлен."""
    return cancel_token is not None and cancel_token.is_set()


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


def _placeholder_device() -> GpuDeviceInfo:
    """Синтетическое устройство для отчётов, где реального GPU нет.

    ``GpuBenchmarkReport.device`` — обязательное поле, поэтому даже в случае
    «OpenCL/GPU недоступен» нужно чем-то его заполнить. Индекс -1 и тип
    ``UNKNOWN`` явно сигнализируют, что это не настоящее устройство.
    """
    return GpuDeviceInfo(
        index=-1,
        name="Устройство недоступно",
        device_type=GpuDeviceType.UNKNOWN,
    )


def _unavailable_report(
    system_info,
    started_at: datetime,
    *,
    note: str | None = None,
    device: GpuDeviceInfo | None = None,
) -> GpuBenchmarkReport:
    """Собрать «пустой» отчёт для случая, когда прогон невозможен.

    ``score=None``, ``cancelled=False`` — как требует спецификация
    (``gpu_benchmark.md`` §10.3): «нет OpenCL / нет GPU-устройства →
    нет измерений → score=None». Исключение не бросается.
    """
    now = datetime.now(timezone.utc)
    return GpuBenchmarkReport(
        system_info=system_info,
        device=device or _placeholder_device(),
        started_at=started_at,
        ended_at=now,
        score=None,
        peak_source="unknown",
        notes=[
            note
            or "OpenCL/GPU недоступен — ICD-loader не загрузился или "
            "GPU-устройств не найдено; балл не построен."
        ],
        cancelled=False,
    )


# Pure-функция собирателя контекста (без запуска бэкенда) — для UI/тестов
# на готовом отчёте. Симметрично compute_general_benchmark_context_from_report.
def compute_gpu_benchmark_context_from_report(
    report: GpuBenchmarkReport,
) -> GpuBenchmarkScoreContext:
    """Свернуть готовый отчёт в :class:`GpuBenchmarkScoreContext`."""
    return GpuBenchmarkScoreContext(
        fp32_gflops=report.fp32_gflops,
        fp64_gflops=report.fp64_gflops,
        mem_bandwidth_gb_s=report.mem_bandwidth_gb_s,
        pcie_h2d_gb_s=report.pcie_h2d_gb_s,
        pcie_d2h_gb_s=report.pcie_d2h_gb_s,
        fp32_peak_gflops=report.fp32_peak_gflops,
        fp64_peak_gflops=report.fp64_peak_gflops,
        mem_bandwidth_peak_gb_s=report.mem_bandwidth_peak_gb_s,
        r_fp32=report.r_fp32,
        r_fp64=report.r_fp64,
        r_mem=report.r_mem,
        score=report.score,
        device_name=report.device.name,
        arch=report.arch,
        peak_source=report.peak_source,
    )


__all__ = [
    "COOLDOWN_BETWEEN_PHASES_SEC",
    "DEFAULT_COMPUTE_PHASE_DURATION_SEC",
    "DEFAULT_MEM_PHASE_DURATION_SEC",
    "DEFAULT_PCIE_PHASE_DURATION_SEC",
    "PHASES",
    "GpuBenchmarkOrchestrator",
    "GpuBenchmarkParams",
    "PhaseName",
    "ProgressCallback",
    "compute_gpu_benchmark_context_from_report",
]
