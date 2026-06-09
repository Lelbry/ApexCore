"""Фасад полного стресс-теста: SafetyGate → Watchdog → ParallelRunner → Verdict.

Этот модуль — единственная точка входа для пункта меню «5.1 Полный
системный стресс» и CLI-команды ``apexcore stress run --profile
system_stress_full``. Он связывает между собой защитные механизмы
(``SafetyGate``, ``ThermalWatchdog``), параллельный runner движков и
агрегацию итогового вердикта PASS/FAIL по упрощённой схеме (см. план).

Порядок шагов:

1. Pre-flight через ``SafetyGate.check_pre_flight()``. Если есть
   ``block_reasons`` и ``--force`` не выставлен — отказ. Иначе
   возвращаются warnings, которые UI должен показать пользователю.
2. Запуск ``TelemetryService`` (если ещё не запущен) и подписка
   ``ThermalWatchdog`` + ``cooling_sanity_subscriber`` на шину.
3. ``ParallelStressRunner.run()`` параллельно запускает все движки плана.
4. Останов watchdog/sanity, остановка телеметрии, сбор истории.
5. Вычисление ``StressVerdict`` (PASS/FAIL + по подкритериям) и
   ``ThermalStabilityResult`` через существующий ``compute_thermal_stability()``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apexcore.application.parallel_runner import (
    EngineSpec,
    ParallelStressResult,
    ParallelStressRunner,
    ProgressCallback,
)
from apexcore.application.safety_gate import SafetyGate, SafetyReport
from apexcore.application.telemetry_service import (
    InMemoryMetricsBus,
    TelemetryService,
)
from apexcore.application.thermal import (
    PASS_THRESHOLD_PERCENT,
    compute_thermal_stability,
)
from apexcore.application.thermal_watchdog import (
    ThermalWatchdog,
    WatchdogTrigger,
)
from apexcore.domain.models import (
    MetricSnapshot,
    StressResult,
    SystemInfo,
    ThermalStabilityResult,
)
from apexcore.domain.ports import OSAdapter

logger = logging.getLogger(__name__)


@dataclass
class StressVerdict:
    """Упрощённый pass/fail вердикт после полного стресс-прогона.

    Hard PASS / Soft PASS / FAIL по таблице §4.1 отчёта в P0 не реализуем
    (это backlog P1). Здесь — бинарный вердикт по четырём независимым
    под-критериям.
    """

    passed: bool
    reason: str
    sub_results: dict[str, bool] = field(default_factory=dict)


@dataclass
class StressFinalReport:
    """Итог полного стресс-прогона. Сохраняется в БД через payload_json."""

    profile_name: str
    started_at: datetime
    finished_at: datetime
    duration_actual_sec: float
    requested_duration_sec: float
    safety: SafetyReport
    parallel: ParallelStressResult
    thermal: ThermalStabilityResult
    watchdog_triggered: bool
    watchdog_trigger: WatchdogTrigger | None
    watchdog_tjmax_c: float
    watchdog_tjmax_source: str
    verdict: StressVerdict
    system_info: SystemInfo
    metrics_history: list[MetricSnapshot] = field(default_factory=list)
    # ЦП: средняя/пиковая нагрузка и температура за прогон.
    cpu_avg_load_pct: float | None = None
    cpu_peak_load_pct: float | None = None
    cpu_avg_temp_c: float | None = None
    cpu_peak_temp_c: float | None = None
    cpu_thermal_limit_c: float | None = None
    # ОЗУ: средняя/пиковая занятость.
    ram_avg_load_pct: float | None = None
    ram_peak_load_pct: float | None = None
    # ОЗУ: температура самого горячего DIMM (1–4) за прогон. Источник —
    # LHM-сенсоры с подстрокой «dimm» в ключе. None, если ни на одном
    # модуле памяти нет температурного датчика (типично для DDR4 без
    # SPD-температур или когда LHM их не публикует).
    ram_avg_temp_c: float | None = None
    ram_peak_temp_c: float | None = None
    # GPU-метрики (если NVIDIA GPU виден через ``nvidia-smi``).
    gpu_avg_temp_c: float | None = None
    gpu_peak_temp_c: float | None = None
    gpu_avg_load_pct: float | None = None
    gpu_peak_load_pct: float | None = None
    gpu_peak_mem_gb: float | None = None
    gpu_mem_total_gb: float | None = None
    gpu_thermal_limit_c: float | None = None
    gpu_name: str | None = None
    # Вольтаж (В): среднее и пик за прогон. Источник — LHM SensorType.Voltage,
    # полезен пользователю при ручном разгоне (Vcore CPU/GPU/DIMM). На бытовых
    # NVIDIA Vcore через драйвер часто недоступен — поля остаются None.
    cpu_avg_vcore_v: float | None = None
    cpu_peak_vcore_v: float | None = None
    gpu_avg_vcore_v: float | None = None
    gpu_peak_vcore_v: float | None = None
    ram_avg_vcore_v: float | None = None
    ram_peak_vcore_v: float | None = None
    # Энергопотребление CPU (Вт) — из LHM cpu_power/* (package как самый
    # представительный показатель полного CPU-power). Полезно для
    # пользователя: видеть реальную «нагрузку на блок питания» во время
    # стресса. None если LHM CPU-power не отдал данные.
    cpu_avg_power_w: float | None = None
    cpu_peak_power_w: float | None = None
    # GPU power через NVML (`nvml/<N>/power_w`) или LHM (`gpu_power/*`).
    # На NVIDIA с pynvml работает без admin (read-only NVML). На AMD/Intel
    # iGPU обычно нет — оставляем None, в таблице «—».
    gpu_avg_power_w: float | None = None
    gpu_peak_power_w: float | None = None
    # Диагностика источника CPU-температуры (этап 1: «нет данных» в финальной
    # карточке). Если ``cpu_temp_source_ok=False`` и температуры пусты —
    # рендер показывает actionable-подсказку (LHM DLL, права админа).
    cpu_temp_source_ok: bool = True
    cpu_temp_source_message: str | None = None
    cpu_temp_source_advice: list[str] = field(default_factory=list)
    # Был ли GPU включён в план нагрузки. Сейчас комбинированный стресс — это
    # CPU+RAM, GPU только мониторится как фон → ``False``. В рендере влияет
    # на лейбл секции и колонку «Статус».
    gpu_was_stressed: bool = False
    # Этапы 3a/3b: Roofline-контекст и детерминированный stress-score.
    # Подробности — `application/stress_score.py` + `docs/stress_score.md`.
    # Все поля плоские (а не один dataclass) ради совместимости с
    # ``payload_json`` БД-сериализатором и единого паттерна StressFinalReport.
    stress_score: float | None = None
    stress_r_dgemm: float | None = None
    stress_r_stream: float | None = None
    stress_r_stability: float | None = None
    # 4-й компонент GM: thermal headroom (research stress_test_mark_method.md).
    # Требует CPU temp + распознанный TJmax + длительность ≥ 90 сек.
    stress_r_thermal: float | None = None
    stress_t_max_c: float | None = None
    stress_tjmax_c: int | None = None
    # Длительность прогона — нужна рендеру для пояснения «< 90 сек» в
    # сообщении «Оценка под нагрузкой недоступна».
    stress_duration_sec: float | None = None
    roofline_dgemm_peak_gflops: float | None = None
    roofline_stream_peak_gb_s: float | None = None
    roofline_simd_level: str | None = None
    roofline_clock_ghz: float | None = None
    roofline_dram_mts: float | None = None
    roofline_dram_modules: int | None = None


def compute_stress_verdict(
    parallel: ParallelStressResult,
    thermal: ThermalStabilityResult,
    watchdog_triggered: bool,
    watchdog_tjmax_c: float,
) -> StressVerdict:
    """Сложить под-критерии в один pass/fail.

    Условия PASS:
    - watchdog не сработал (либо grace-window истёк, но реального tjmax не достигли);
    - суммарный error_count по всем движкам == 0;
    - средняя температура < Tj_max - 10°C (если данных хватает; иначе пропуск);
    - прогон не был отменён пользователем.

    ``frame_rate_stability_pct`` НЕ участвует в PASS/FAIL: на гетерогенных
    Intel (Alder/Raptor Lake) метрика ``min(cpu_avg)/max(cpu_avg)``
    систематически даёт ~50% при отсутствии реального throttle (P-cores
    boost 5.2 GHz vs E-cores 3.9 GHz + idle states между ядрами). Это
    архитектурный артефакт, а не дефект системы. Stability остаётся
    как **информационный** показатель и входит в r_thermal/r_stability
    стресс-балла, но не блокирует вердикт.
    """
    sub: dict[str, bool] = {}

    sub["no_watchdog_trigger"] = not watchdog_triggered
    if not sub["no_watchdog_trigger"]:
        return StressVerdict(
            passed=False,
            reason="thermal watchdog: температура CPU достигла порога остановки",
            sub_results=sub,
        )

    total_errors = sum(r.error_count for r in parallel.results)
    sub["no_verify_errors"] = total_errors == 0
    if not sub["no_verify_errors"]:
        return StressVerdict(
            passed=False,
            reason=f"verify-ошибок: {total_errors} (см. error_count в результатах движков)",
            sub_results=sub,
        )

    # Информационный sub-flag (не FAIL): см. docstring.
    if thermal.frame_rate_stability_pct is not None:
        sub["freq_stability_97"] = (
            thermal.frame_rate_stability_pct >= PASS_THRESHOLD_PERCENT
        )
    else:
        sub["freq_stability_97"] = True

    if thermal.temp_avg_c is not None:
        avg_ok = thermal.temp_avg_c < (watchdog_tjmax_c - 10.0)
        sub["avg_temp_safe"] = avg_ok
        if not avg_ok:
            return StressVerdict(
                passed=False,
                reason=(
                    f"средняя T CPU={thermal.temp_avg_c:.1f}°C "
                    f"≥ безопасного предела "
                    f"({watchdog_tjmax_c - 10:.0f}°C = температурный лимит − 10°C)"
                ),
                sub_results=sub,
            )
    else:
        sub["avg_temp_safe"] = True

    sub["cancelled"] = parallel.cancelled
    if parallel.cancelled:
        return StressVerdict(
            passed=False,
            reason="прогон был отменён (Ctrl+C или watchdog)",
            sub_results=sub,
        )

    return StressVerdict(
        passed=True,
        reason="все защитные пороги в норме, ошибок не зафиксировано",
        sub_results=sub,
    )


class StressOrchestrator:
    """Фасад: запускает полный стресс-прогон и возвращает StressFinalReport.

    Конструктор принимает уже подготовленный ``OSAdapter``; шину/телеметрию
    создаёт сам, чтобы не путать с существующими сервисами scoring v2.
    Если внешний код хочет реюзать свою шину — можно передать её в
    ``run(..., bus=...)`` (для интеграции с LiveStressView).
    """

    def __init__(
        self,
        adapter: OSAdapter,
        *,
        sampling_rate_sec: float = 0.5,
        watchdog_margin_celsius: float = 5.0,
        min_battery_percent: float = SafetyGate.MIN_BATTERY_PERCENT_DEFAULT,
        min_free_ram_gb: float = SafetyGate.MIN_FREE_RAM_GB,
    ) -> None:
        self._adapter = adapter
        self._sampling_rate = sampling_rate_sec
        self._watchdog_margin = watchdog_margin_celsius
        self._safety_gate = SafetyGate(
            adapter,
            min_battery_percent=min_battery_percent,
            min_free_ram_gb=min_free_ram_gb,
        )

    def check_pre_flight(self) -> SafetyReport:
        return self._safety_gate.check_pre_flight()

    def run(
        self,
        *,
        profile_name: str,
        plan: list[EngineSpec],
        duration_sec: float,
        cancel_token: threading.Event | None = None,
        bus: InMemoryMetricsBus | None = None,
        on_progress: ProgressCallback | None = None,
        enable_watchdog: bool = True,
        force: bool = False,
        pre_flight: SafetyReport | None = None,
    ) -> StressFinalReport:
        """Запустить полный стресс-прогон.

        Параметры:
            profile_name: имя профиля для сохранения в отчёте.
            plan: уже собранный список движков (см. ``application/parallel_runner``).
            duration_sec: длительность прогона.
            cancel_token: внешний cancel; если None — создаётся свой.
            bus: внешняя шина (если LiveStressView хочет подписаться).
            on_progress: callback на прогресс отдельных движков.
            enable_watchdog: True (default) — подключить ThermalWatchdog.
            force: True — игнорировать ``block_reasons`` SafetyGate.
            pre_flight: уже посчитанный отчёт SafetyGate (UI мог его показать
                юзеру). Если None — будет посчитан здесь.

        Бросает ``RuntimeError`` если pre-flight заблокирован и ``force=False``.
        """
        report_safety = pre_flight if pre_flight is not None else self.check_pre_flight()
        if report_safety.blocked and not force:
            raise RuntimeError(
                "SafetyGate заблокировал запуск:\n  - "
                + "\n  - ".join(report_safety.block_reasons)
            )

        token = cancel_token if cancel_token is not None else threading.Event()
        own_bus = bus is None
        bus_real = bus if bus is not None else InMemoryMetricsBus()
        telemetry = TelemetryService(
            self._adapter, bus_real, sampling_rate_sec=self._sampling_rate
        )

        watchdog: ThermalWatchdog | None = None
        if enable_watchdog:
            watchdog = ThermalWatchdog(
                bus=bus_real,
                cancel_token=token,
                margin_celsius=self._watchdog_margin,
            )
            watchdog.start()

        cooling_cb, _cooling_finished = self._safety_gate.cooling_sanity_subscriber(
            report_safety
        )
        unsubscribe_cooling = bus_real.subscribe(cooling_cb)

        # Запускаем телеметрию только если шина наша. Если bus передан снаружи,
        # подразумеваем, что вызывающий код уже крутит свой TelemetryService.
        if own_bus:
            telemetry.start(record_history=True)

        started_dt = datetime.now(timezone.utc)
        try:
            parallel = ParallelStressRunner().run(
                plan=plan,
                duration_sec=duration_sec,
                cancel_token=token,
                on_progress=on_progress,
            )
        finally:
            # Внешний шинный владелец сам соберёт историю — мы возвращаем пустой список.
            history = telemetry.stop() if own_bus else []
            try:
                unsubscribe_cooling()
            except Exception:
                logger.debug("отписка cooling cb упала", exc_info=True)
            if watchdog is not None:
                watchdog.stop()

        finished_dt = datetime.now(timezone.utc)
        thermal = compute_thermal_stability(history)

        watchdog_triggered = bool(watchdog and watchdog.triggered)
        watchdog_trigger = watchdog.trigger if watchdog else None
        watchdog_tjmax_c = watchdog.tjmax if watchdog else 100.0
        watchdog_tjmax_source = watchdog.tjmax_source if watchdog else "disabled"

        verdict = compute_stress_verdict(
            parallel=parallel,
            thermal=thermal,
            watchdog_triggered=watchdog_triggered,
            watchdog_tjmax_c=watchdog_tjmax_c,
        )

        return StressFinalReport(
            profile_name=profile_name,
            started_at=started_dt,
            finished_at=finished_dt,
            duration_actual_sec=parallel.duration_actual_sec,
            requested_duration_sec=duration_sec,
            safety=report_safety,
            parallel=parallel,
            thermal=thermal,
            watchdog_triggered=watchdog_triggered,
            watchdog_trigger=watchdog_trigger,
            watchdog_tjmax_c=watchdog_tjmax_c,
            watchdog_tjmax_source=watchdog_tjmax_source,
            verdict=verdict,
            system_info=self._adapter.get_system_info(),
            metrics_history=history,
        )


def collect_stress_results(report: StressFinalReport) -> list[StressResult]:
    """Удобный аксессор: только список движковых результатов."""
    return list(report.parallel.results)


__all__ = [
    "StressFinalReport",
    "StressOrchestrator",
    "StressVerdict",
    "collect_stress_results",
    "compute_stress_verdict",
]
