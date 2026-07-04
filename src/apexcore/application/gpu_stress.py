"""GPU стресс-тест / термостабильность (кроссвендорный аналог FurMark, compute).

Длительная максимальная FP32-нагрузка на GPU («power virus») + посекундная
телеметрия (температура / мощность / частота / загрузка) + вердикт
PASS/WARN/FAIL/UNKNOWN. Это GPU-аналог полного CPU-стресса
(:mod:`application.stress_orchestrator`), но headless и без внешних утилит.

Load-генератор — уже готовый бэкенд OpenCL: ``backend.measure(idx,
SUSTAINED_STRESS, duration_sec, cancel_token)`` крутит максимально-ALU FP32
кернел заданное время и уважает ``cancel_token``. Оркестратор запускает его в
рабочем потоке, а в вызывающем потоке раз в ~1 с снимает телеметрию GPU, зовёт
``on_progress`` и после окончания нагрузки считает сводки + вердикт.

Устройство сбора телеметрии инъектируется (:class:`TelemetrySampler`): в
проде — реальный NVML/hwmon-семплер, в тестах — фейк со заскриптованной
серией. Благодаря этому весь модуль юнит-тестируется без GPU.

Graceful degrade (тот же принцип, что у сенсоров, общей оценки и
GPU-бенчмарка): бэкенд недоступен / устройств нет / плохой индекс → отчёт с
``verdict=UNKNOWN`` + note, **без исключения**. Нет телеметрии → нагрузку всё
равно прогоняем, вердикт ``UNKNOWN`` + note «телеметрия недоступна». Отмена
посреди прогона → ``cancelled=True`` и вердикт по частичным данным.

БЕЗОПАСНОСТЬ: тест максимально греет GPU. Нагрузка ограничена сверху
``duration_sec`` и ``cancel_token`` — здесь НЕТ ничего неограниченного, кернел
не меняется (переиспользуем ``SUSTAINED_STRESS`` как есть).

Методика троттлинга (пороги — ниже, в константах ``THROTTLE_*``):
GPU штатно бустится в первые секунды и затем «оседает» (boost-settle) — это
норма, а не дефект. Поэтому небольшую просадку частоты трактуем максимум как
WARN, и только достижение теплового лимита или крупный обвал частоты — как
FAIL. Три независимых сигнала троттлинга:

  1. Обвал частоты: средняя частота ядра в последней трети прогона ниже, чем
     в первой трети, на > ``THROTTLE_CLOCK_DROP_WARN_PCT`` (WARN) /
     ``THROTTLE_CLOCK_DROP_FAIL_PCT`` (FAIL). Сравниваем first-third vs
     last-third, чтобы игнорировать начальный буст и мерить установившийся
     режим.
  2. Тепловой лимит: пиковая температура в пределах
     ``THROTTLE_TEMP_MARGIN_WARN_C`` от порога NVML slowdown → WARN; достигла
     самого порога (или выше) → FAIL. Если порог неизвестен — фолбэк на
     абсолютные ``FALLBACK_TEMP_WARN_C`` / ``FALLBACK_TEMP_FAIL_C``.
  3. Обвал загрузки: под sustained-нагрузкой средняя загрузка GPU не должна
     проседать. Если ``avg_util`` < ``UTIL_COLLAPSE_WARN_PCT`` при том, что
     нагрузка реально шла — WARN (что-то перехватило GPU или троттлинг увёл
     частоту в пол); это информационный сигнал.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from apexcore.domain.gpu import (
    GpuDeviceInfo,
    GpuDeviceType,
    GpuStressReport,
    GpuStressSample,
    GpuStressVerdict,
    GpuWorkloadKind,
)
from apexcore.domain.ports import GpuComputeBackend, OSAdapter

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, float], None]
"""``on_progress(elapsed_sec, duration_sec)`` — прогресс прогона нагрузки."""

# ── Периодичность семплирования телеметрии. ──
SAMPLE_INTERVAL_SEC = 1.0
# Верхняя граница числа отсчётов в отчёте (для UI-спарклайна). Длинные прогоны
# прореживаются равномерно, чтобы JSON в БД не распухал.
MAX_SAMPLES_IN_REPORT = 240

# ── Пороги троттлинга (см. модульный docstring). ──
# Просадка частоты first-third → last-third.
THROTTLE_CLOCK_DROP_WARN_PCT = 5.0    # штатный boost-settle: до 5 % — тишина
THROTTLE_CLOCK_DROP_FAIL_PCT = 15.0   # обвал частоты > 15 % — тепловой троттлинг
# Близость пиковой температуры к порогу NVML slowdown.
THROTTLE_TEMP_MARGIN_WARN_C = 3.0     # в пределах 3 °C от slowdown — WARN
# Абсолютные фолбэк-пороги, если порог slowdown неизвестен (нет NVML).
FALLBACK_TEMP_WARN_C = 84.0
FALLBACK_TEMP_FAIL_C = 90.0
# Обвал средней загрузки под sustained-нагрузкой.
UTIL_COLLAPSE_WARN_PCT = 50.0
# Минимум отсчётов, ниже которого частотный тренд ненадёжен (не судим по нему).
MIN_SAMPLES_FOR_TREND = 6


# ─────────────────────────── Телеметрия ──────────────────────────────────────


@dataclass
class GpuTelemetryReading:
    """Один снимок телеметрии тестируемого GPU (любое поле может быть None)."""

    temp_c: float | None = None
    power_w: float | None = None
    clock_mhz: float | None = None
    util_pct: float | None = None

    def is_empty(self) -> bool:
        """True, если ни один сенсор не отдал значение."""
        return (
            self.temp_c is None
            and self.power_w is None
            and self.clock_mhz is None
            and self.util_pct is None
        )


@runtime_checkable
class TelemetrySampler(Protocol):
    """Источник телеметрии тестируемого GPU (инъектируется в оркестратор).

    Реализация должна деградировать корректно: при отсутствии сенсоров
    возвращать пустой :class:`GpuTelemetryReading`, а не бросать. Оркестратор
    зовёт ``sample()`` раз в ~1 с из вызывающего потока.
    """

    def thermal_limit_c(self) -> float | None:
        """Порог теплового замедления (NVML slowdown), °C, или None."""
        ...

    def sample(self) -> GpuTelemetryReading:
        """Снять один снимок телеметрии тестируемого GPU."""
        ...


class NvmlTelemetrySampler:
    """Реальный семплер телеметрии GPU через NVML (NVIDIA) с hwmon-фолбэком.

    NVIDIA — primary-путь (эталонная машина — RTX 4070 Ti; nvidia-ml-py уже
    жёсткая зависимость). Индекс NVML резолвится по **имени** устройства
    (OpenCL-индекс и NVML-индекс не обязаны совпадать): матчим
    ``device.name`` против ``read_nvml_device_names()``; при единственном GPU
    или отсутствии матча — NVML-индекс 0.

    AMD/Intel на Linux читаются через переданный ``OSAdapter`` (hwmon-ключи
    ``gpuamd/*`` / ``gpuintel/*`` в ``MetricSnapshot``). Если ни NVML, ни
    hwmon ничего не дают — ``sample()`` возвращает пустой reading, и
    оркестратор выставит ``UNKNOWN``.

    Все обращения к сенсорам обёрнуты в ``try/except`` — источник телеметрии
    никогда не должен ронять стресс-прогон.
    """

    def __init__(self, device: GpuDeviceInfo, adapter: OSAdapter | None = None) -> None:
        self._device = device
        self._adapter = adapter
        self._nvml_index: int | None = None
        self._nvml_resolved = False
        self._thermal_limit: float | None = None
        self._thermal_limit_resolved = False

    # ── NVML-индекс тестируемого устройства. ──
    def _resolve_nvml_index(self) -> int | None:
        if self._nvml_resolved:
            return self._nvml_index
        self._nvml_resolved = True
        try:
            from apexcore.infrastructure.sensors import nvidia_ml

            names = nvidia_ml.read_nvml_device_names()
        except Exception as exc:
            logger.debug("NVML device names для стресса не прочитались: %s", exc)
            names = {}
        if not names:
            self._nvml_index = None
            return None
        # Точный матч по имени (OpenCL-имя NVIDIA совпадает с NVML-именем).
        target = (self._device.name or "").strip().lower()
        for idx, name in names.items():
            if name.strip().lower() == target:
                self._nvml_index = idx
                return idx
        # Единственный GPU в системе — берём его (индекс 0), даже если имена
        # разошлись форматированием.
        if len(names) == 1:
            self._nvml_index = next(iter(names.keys()))
            return self._nvml_index
        self._nvml_index = None
        return None

    def thermal_limit_c(self) -> float | None:
        if self._thermal_limit_resolved:
            return self._thermal_limit
        self._thermal_limit_resolved = True
        idx = self._resolve_nvml_index()
        if idx is None:
            return None
        try:
            from apexcore.infrastructure.sensors import nvidia_ml

            thresholds = nvidia_ml.read_nvml_thresholds()
        except Exception as exc:
            logger.debug("NVML thresholds для стресса не прочитались: %s", exc)
            return None
        # Порог замедления — приоритетно; иначе gpu_max как консервативный лимит.
        for key in (f"nvml/{idx}/threshold_slowdown", f"nvml/{idx}/threshold_gpu_max"):
            value = thresholds.get(key)
            if value is not None and value > 0:
                self._thermal_limit = float(value)
                return self._thermal_limit
        return None

    def sample(self) -> GpuTelemetryReading:
        reading = self._sample_nvml()
        if reading.is_empty():
            hwmon = self._sample_hwmon()
            if not hwmon.is_empty():
                return hwmon
        return reading

    def _sample_nvml(self) -> GpuTelemetryReading:
        idx = self._resolve_nvml_index()
        if idx is None:
            return GpuTelemetryReading()
        try:
            from apexcore.infrastructure.sensors import nvidia_ml

            temps, power, freqs = nvidia_ml.read_nvml_all()
            util = nvidia_ml.read_nvml_utilization()
        except Exception as exc:
            logger.debug("NVML sample для стресса упал: %s", exc)
            return GpuTelemetryReading()
        return GpuTelemetryReading(
            temp_c=temps.get(f"nvml/{idx}/temperature"),
            power_w=power.get(f"nvml/{idx}/power_w"),
            clock_mhz=freqs.get(f"nvml/{idx}/clock_graphics"),
            util_pct=util.get(f"nvml/{idx}/util_gpu"),
        )

    def _sample_hwmon(self) -> GpuTelemetryReading:
        """AMD/Intel GPU через ``OSAdapter`` (hwmon-ключи gpuamd/gpuintel)."""
        if self._adapter is None:
            return GpuTelemetryReading()
        try:
            snap = self._adapter.get_current_metrics()
        except Exception as exc:
            logger.debug("hwmon GPU sample для стресса упал: %s", exc)
            return GpuTelemetryReading()

        prefixes: tuple[str, ...]
        if self._device.vendor.upper().startswith("AMD") or "radeon" in self._device.name.lower():
            prefixes = ("gpuamd",)
        elif self._device.vendor.upper().startswith("INTEL"):
            prefixes = ("gpuintel",)
        else:
            prefixes = ("gpuamd", "gpuintel")

        temp_c = _first_matching(snap.temperatures, prefixes)
        clock = _first_matching(snap.frequencies, prefixes)
        # power/util в hwmon-снимке отдельного поля не имеют → остаются None.
        return GpuTelemetryReading(temp_c=temp_c, clock_mhz=clock)


def _first_matching(values: dict[str, float], prefixes: tuple[str, ...]) -> float | None:
    """Первое значение, чей ключ начинается с одного из ``prefixes``."""
    for key, value in values.items():
        for prefix in prefixes:
            if key.startswith(prefix):
                return float(value)
    return None


# ─────────────────── Сводки + вердикт (pure, тестируется отдельно) ────────────


@dataclass
class _Series:
    """Накопитель серий телеметрии за прогон (пофичево, чтобы дырки не мешали)."""

    temps: list[float] = field(default_factory=list)
    powers: list[float] = field(default_factory=list)
    clocks: list[float] = field(default_factory=list)
    utils: list[float] = field(default_factory=list)
    count: int = 0

    def add(self, reading: GpuTelemetryReading) -> None:
        self.count += 1
        if reading.temp_c is not None:
            self.temps.append(reading.temp_c)
        if reading.power_w is not None:
            self.powers.append(reading.power_w)
        if reading.clock_mhz is not None:
            self.clocks.append(reading.clock_mhz)
        if reading.util_pct is not None:
            self.utils.append(reading.util_pct)

    def has_any_telemetry(self) -> bool:
        return bool(self.temps or self.powers or self.clocks or self.utils)


@dataclass
class GpuStressSummary:
    """Сводки серий + результат детекции троттлинга (без БД-специфики).

    Отделено от :class:`GpuStressReport`, чтобы ``compute_gpu_stress_verdict``
    была чистой и тестировалась изолированно от pydantic-модели и оркестратора.
    """

    max_temp_c: float | None = None
    avg_temp_c: float | None = None
    max_power_w: float | None = None
    avg_power_w: float | None = None
    min_clock_mhz: float | None = None
    avg_clock_mhz: float | None = None
    max_clock_mhz_observed: float | None = None
    avg_util_pct: float | None = None
    throttle_detected: bool = False
    throttle_reasons: list[str] = field(default_factory=list)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _clock_trend_drop_pct(clocks: list[float]) -> float | None:
    """Просадка частоты: (first_third_avg − last_third_avg) / first_third_avg × 100.

    Положительное значение = частота упала к концу прогона. None, если
    отсчётов слишком мало для надёжного тренда. Сравниваем крайние трети,
    чтобы отфильтровать начальный буст и мерить установившийся режим.
    """
    n = len(clocks)
    if n < MIN_SAMPLES_FOR_TREND:
        return None
    third = max(1, n // 3)
    first_avg = sum(clocks[:third]) / third
    last_avg = sum(clocks[-third:]) / third
    if first_avg <= 0:
        return None
    return (first_avg - last_avg) / first_avg * 100.0


def summarize_gpu_stress(
    series: _Series,
    *,
    thermal_limit_c: float | None,
    load_ran: bool,
) -> GpuStressSummary:
    """Свернуть серии в сводки + детектировать троттлинг (pure).

    ``load_ran`` — реально ли гонялась нагрузка (для сигнала обвала загрузки:
    на нулевом прогоне низкая загрузка — не аномалия). Логика вердикта —
    в :func:`compute_gpu_stress_verdict`; здесь только числа и флаги причин.
    """
    summary = GpuStressSummary(
        max_temp_c=max(series.temps) if series.temps else None,
        avg_temp_c=_avg(series.temps),
        max_power_w=max(series.powers) if series.powers else None,
        avg_power_w=_avg(series.powers),
        min_clock_mhz=min(series.clocks) if series.clocks else None,
        avg_clock_mhz=_avg(series.clocks),
        max_clock_mhz_observed=max(series.clocks) if series.clocks else None,
        avg_util_pct=_avg(series.utils),
    )

    reasons: list[str] = []

    # (1) Обвал частоты first-third → last-third.
    drop = _clock_trend_drop_pct(series.clocks)
    if drop is not None:
        if drop >= THROTTLE_CLOCK_DROP_FAIL_PCT:
            reasons.append(
                f"обвал частоты ядра на {drop:.0f}% к концу прогона "
                f"(≥ {THROTTLE_CLOCK_DROP_FAIL_PCT:.0f}% — тепловой троттлинг)"
            )
        elif drop >= THROTTLE_CLOCK_DROP_WARN_PCT:
            reasons.append(
                f"частота ядра просела на {drop:.0f}% к концу прогона "
                f"(норм. boost-settle до {THROTTLE_CLOCK_DROP_WARN_PCT:.0f}%)"
            )

    # (2) Близость пиковой температуры к тепловому лимиту.
    if summary.max_temp_c is not None:
        if thermal_limit_c is not None and thermal_limit_c > 0:
            if summary.max_temp_c >= thermal_limit_c:
                reasons.append(
                    f"температура достигла порога замедления "
                    f"({summary.max_temp_c:.0f}°C ≥ {thermal_limit_c:.0f}°C)"
                )
            elif summary.max_temp_c >= thermal_limit_c - THROTTLE_TEMP_MARGIN_WARN_C:
                reasons.append(
                    f"температура у порога замедления "
                    f"({summary.max_temp_c:.0f}°C, порог {thermal_limit_c:.0f}°C)"
                )
        else:
            if summary.max_temp_c >= FALLBACK_TEMP_FAIL_C:
                reasons.append(
                    f"высокая температура {summary.max_temp_c:.0f}°C "
                    f"(≥ {FALLBACK_TEMP_FAIL_C:.0f}°C, порог устройства неизвестен)"
                )
            elif summary.max_temp_c >= FALLBACK_TEMP_WARN_C:
                reasons.append(
                    f"повышенная температура {summary.max_temp_c:.0f}°C "
                    f"(≥ {FALLBACK_TEMP_WARN_C:.0f}°C, порог устройства неизвестен)"
                )

    # (3) Обвал загрузки под нагрузкой.
    if load_ran and summary.avg_util_pct is not None and summary.avg_util_pct < UTIL_COLLAPSE_WARN_PCT:
        reasons.append(
            f"средняя загрузка GPU {summary.avg_util_pct:.0f}% "
            f"(< {UTIL_COLLAPSE_WARN_PCT:.0f}% под нагрузкой — просадка/перехват)"
        )

    summary.throttle_reasons = reasons
    summary.throttle_detected = bool(reasons)
    return summary


def compute_gpu_stress_verdict(
    summary: GpuStressSummary,
    *,
    has_telemetry: bool,
    thermal_limit_c: float | None,
    cancelled: bool,
) -> tuple[GpuStressVerdict, list[str]]:
    """Свернуть сводки в вердикт PASS/WARN/FAIL/UNKNOWN + пояснения (pure).

    Правила:
    - Нет телеметрии → ``UNKNOWN`` (нагрузку прогнали, но судить не по чему).
    - Достижение теплового лимита ИЛИ обвал частоты ≥ FAIL-порога → ``FAIL``.
    - Иначе любой сигнал троттлинга (settle / T у лимита / обвал загрузки) →
      ``WARN``.
    - Отмена без FAIL-сигнала → best-effort ``WARN`` (частичные данные;
      судить о полной стабильности нельзя), с явной note.
    - Иначе → ``PASS``.

    Возвращает ``(verdict, notes)``. FAIL/WARN-причины — в
    ``summary.throttle_reasons``; ``notes`` здесь — только пояснения к самому
    вердикту (телеметрия/отмена/итог).
    """
    notes: list[str] = []

    if not has_telemetry:
        notes.append(
            "Телеметрия GPU недоступна (нет NVML/hwmon-сенсоров тестируемого "
            "устройства) — нагрузка выполнена, но судить о стабильности не по чему."
        )
        return GpuStressVerdict.UNKNOWN, notes

    # Разделяем FAIL-причины от WARN-причин по формулировке из summarize_*.
    fail_reasons = [
        r
        for r in summary.throttle_reasons
        if "тепловой троттлинг" in r or "достигла порога" in r
    ]
    if fail_reasons:
        notes.append("Тепловой троттлинг под нагрузкой — устройство не держит sustained-режим.")
        return GpuStressVerdict.FAIL, notes

    if summary.throttle_detected:
        notes.append("Обнаружены признаки просадки под нагрузкой (не критично).")
        verdict = GpuStressVerdict.WARN
    elif cancelled:
        notes.append(
            "Прогон отменён — вердикт по частичным данным; о полной стабильности "
            "судить нельзя (запустите на полную длительность)."
        )
        verdict = GpuStressVerdict.WARN
    else:
        margin = ""
        if thermal_limit_c is not None and summary.max_temp_c is not None:
            margin = f" (пик {summary.max_temp_c:.0f}°C при пороге {thermal_limit_c:.0f}°C)"
        notes.append(f"Частоты держатся, температура в норме, троттлинга нет{margin}.")
        verdict = GpuStressVerdict.PASS

    return verdict, notes


# ─────────────────────────── Оркестратор ─────────────────────────────────────


class GpuStressOrchestrator:
    """Фасад GPU-стресс-теста: нагрузка в потоке + телеметрия + вердикт.

    Конструктор принимает ``OSAdapter`` (снимок :class:`SystemInfo`, hwmon для
    AMD/Intel) и :class:`GpuComputeBackend` (перечисление устройств + движок
    ``SUSTAINED_STRESS``). Оба инъектируются явно, как у
    :class:`GpuBenchmarkOrchestrator`, — так unit-тесты подставляют фейки без
    реального OpenCL/GPU.

    ``sample_interval_sec`` — период семплирования телеметрии (по умолчанию
    ~1 с). Отдельный knob на конструкторе (а не в :meth:`run`, чью сигнатуру
    потребляют CLI/WebUI) нужен тестам, чтобы прогнать много отсчётов на
    коротком fake-прогоне без реальных секундных пауз.
    """

    def __init__(
        self,
        adapter: OSAdapter,
        backend: GpuComputeBackend,
        *,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
    ) -> None:
        self._adapter = adapter
        self._backend = backend
        self._sample_interval = max(1e-3, sample_interval_sec)

    def run(
        self,
        device_index: int = 0,
        duration_sec: float = 60.0,
        *,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        telemetry: TelemetrySampler | None = None,
    ) -> GpuStressReport:
        """Запустить GPU-стресс на ``duration_sec`` секунд.

        Параметры:
            device_index: индекс устройства из ``backend.list_devices()``.
            duration_sec: длительность нагрузки (обычно 60–600 с).
            cancel_token: внешний cancel; если None — создаётся свой (для
                внутреннего останова семплера при завершении нагрузки).
            on_progress: ``on_progress(elapsed_sec, duration_sec)`` — зовётся
                раз в ~1 с из вызывающего потока.
            telemetry: источник телеметрии; None → реальный
                :class:`NvmlTelemetrySampler` по тестируемому устройству.

        Никогда не бросает: бэкенд/устройство/индекс недоступны → отчёт с
        ``verdict=UNKNOWN`` + note. Отмена → ``cancelled=True``, вердикт по
        частичным данным.
        """
        started_at = datetime.now(timezone.utc)
        system_info = self._adapter.get_system_info()

        # ── Доступность бэкенда + перечисление устройств. ──
        devices: list[GpuDeviceInfo] = []
        if self._backend.is_available():
            try:
                devices = self._backend.list_devices()
            except Exception as exc:  # graceful degrade — не роняем прогон
                logger.exception("list_devices() упал: %s", exc)
                devices = []

        if not devices:
            return _unavailable_report(
                system_info,
                started_at,
                duration_sec,
                note=(
                    "OpenCL/GPU недоступен — ICD-loader не загрузился или "
                    "GPU-устройств не найдено; стресс-тест не выполнен."
                ),
            )

        if device_index < 0 or device_index >= len(devices):
            return _unavailable_report(
                system_info,
                started_at,
                duration_sec,
                note=(
                    f"Запрошен device_index={device_index}, но обнаружено "
                    f"{len(devices)} устройств — стресс-тест не выполнен."
                ),
                device=devices[0],
            )

        device = devices[device_index]
        sampler: TelemetrySampler = telemetry or NvmlTelemetrySampler(device, self._adapter)

        # ── Запуск нагрузки в рабочем потоке. ──
        token = cancel_token if cancel_token is not None else threading.Event()
        load_result: dict[str, object] = {}

        def _worker() -> None:
            try:
                m = self._backend.measure(
                    device_index, GpuWorkloadKind.SUSTAINED_STRESS, duration_sec, token
                )
                load_result["measurement"] = m
            except Exception as exc:  # фиксируем, не роняем поток
                logger.exception("SUSTAINED_STRESS упал: %s", exc)
                load_result["error"] = exc

        worker = threading.Thread(target=_worker, name="gpu-stress-load", daemon=True)

        series = _Series()
        try:
            thermal_limit = sampler.thermal_limit_c()
        except Exception as exc:
            logger.debug("thermal_limit_c() упал: %s", exc)
            thermal_limit = None

        load_start = time.perf_counter()
        worker.start()

        # ── Цикл семплирования в вызывающем потоке (раз в ~1 с). ──
        interval = self._sample_interval
        next_sample = load_start
        while worker.is_alive():
            now = time.perf_counter()
            if now >= next_sample:
                self._take_sample(sampler, series)
                elapsed = now - load_start
                if on_progress is not None:
                    try:
                        on_progress(min(elapsed, duration_sec), duration_sec)
                    except Exception:
                        logger.debug("on_progress упал", exc_info=True)
                next_sample += interval
            # Ждём либо следующего семпла, либо завершения нагрузки.
            worker.join(timeout=max(0.0, min(next_sample - time.perf_counter(), interval)))

        # Нагрузка завершилась — финальный отсчёт (чтобы «хвост» прогрева попал
        # в серию даже на очень коротких прогонах).
        self._take_sample(sampler, series)
        duration_actual = time.perf_counter() - load_start

        cancelled = token.is_set()
        if on_progress is not None:
            try:
                on_progress(min(duration_actual, duration_sec), duration_sec)
            except Exception:
                logger.debug("on_progress (финальный) упал", exc_info=True)

        ended_at = datetime.now(timezone.utc)

        # ── Сводки + вердикт. ──
        load_ran = "measurement" in load_result and not cancelled
        summary = summarize_gpu_stress(
            series, thermal_limit_c=thermal_limit, load_ran=load_ran
        )
        verdict, notes = compute_gpu_stress_verdict(
            summary,
            has_telemetry=series.has_any_telemetry(),
            thermal_limit_c=thermal_limit,
            cancelled=cancelled,
        )

        if "error" in load_result:
            notes.append(f"Ошибка движка нагрузки: {load_result['error']}")
        if cancelled:
            notes.append("Прогон прерван пользователем.")

        return GpuStressReport(
            system_info=system_info,
            device=device,
            started_at=started_at,
            ended_at=ended_at,
            duration_sec=duration_actual,
            requested_duration_sec=duration_sec,
            max_temp_c=summary.max_temp_c,
            avg_temp_c=summary.avg_temp_c,
            max_power_w=summary.max_power_w,
            avg_power_w=summary.avg_power_w,
            min_clock_mhz=summary.min_clock_mhz,
            avg_clock_mhz=summary.avg_clock_mhz,
            max_clock_mhz_observed=summary.max_clock_mhz_observed,
            avg_util_pct=summary.avg_util_pct,
            throttle_detected=summary.throttle_detected,
            throttle_reasons=summary.throttle_reasons,
            thermal_limit_c=thermal_limit,
            verdict=verdict,
            notes=notes,
            cancelled=cancelled,
            samples=_downsample(
                series_to_samples(series, interval), MAX_SAMPLES_IN_REPORT
            ),
            samples_taken=series.count,
        )

    def _take_sample(self, sampler: TelemetrySampler, series: _Series) -> None:
        """Снять один отсчёт телеметрии (не роняет прогон при ошибке семплера)."""
        try:
            reading = sampler.sample()
        except Exception as exc:
            logger.debug("telemetry sample упал: %s", exc)
            reading = GpuTelemetryReading()
        series.add(reading)


def series_to_samples(
    series: _Series, interval_sec: float = SAMPLE_INTERVAL_SEC
) -> list[GpuStressSample]:
    """Развернуть накопленные серии в отсчёты для спарклайна.

    ``t_sec`` восстанавливается по порядковому номеру × ``interval_sec``
    (семплирование равномерное). Серии хранятся пофичево (тики, где сенсор
    молчал, в списки не попадали), поэтому точную привязку «какой сенсор
    молчал в тике N» мы не держим — для спарклайна достаточно равномерной
    сетки: индексируем по общему ``count`` и подставляем значение там, где для
    данного сенсора хватает отсчётов.
    """
    samples: list[GpuStressSample] = []
    for i in range(series.count):
        samples.append(
            GpuStressSample(
                t_sec=i * interval_sec,
                temp_c=series.temps[i] if i < len(series.temps) else None,
                power_w=series.powers[i] if i < len(series.powers) else None,
                clock_mhz=series.clocks[i] if i < len(series.clocks) else None,
                util_pct=series.utils[i] if i < len(series.utils) else None,
            )
        )
    return samples


def _downsample(samples: list[GpuStressSample], limit: int) -> list[GpuStressSample]:
    """Проредить отсчёты до ``limit`` штук равномерно (для JSON в БД)."""
    n = len(samples)
    if n <= limit or limit <= 0:
        return samples
    step = n / limit
    return [samples[int(i * step)] for i in range(limit)]


def _placeholder_device() -> GpuDeviceInfo:
    """Синтетическое устройство для отчётов, где реального GPU нет."""
    return GpuDeviceInfo(
        index=-1,
        name="Устройство недоступно",
        device_type=GpuDeviceType.UNKNOWN,
    )


def _unavailable_report(
    system_info,
    started_at: datetime,
    requested_duration_sec: float,
    *,
    note: str,
    device: GpuDeviceInfo | None = None,
) -> GpuStressReport:
    """«Пустой» отчёт, когда стресс невозможен: ``verdict=UNKNOWN`` + note, без исключения."""
    now = datetime.now(timezone.utc)
    return GpuStressReport(
        system_info=system_info,
        device=device or _placeholder_device(),
        started_at=started_at,
        ended_at=now,
        duration_sec=0.0,
        requested_duration_sec=requested_duration_sec,
        verdict=GpuStressVerdict.UNKNOWN,
        notes=[note],
        cancelled=False,
    )


__all__ = [
    "FALLBACK_TEMP_FAIL_C",
    "FALLBACK_TEMP_WARN_C",
    "MAX_SAMPLES_IN_REPORT",
    "SAMPLE_INTERVAL_SEC",
    "THROTTLE_CLOCK_DROP_FAIL_PCT",
    "THROTTLE_CLOCK_DROP_WARN_PCT",
    "THROTTLE_TEMP_MARGIN_WARN_C",
    "UTIL_COLLAPSE_WARN_PCT",
    "GpuStressOrchestrator",
    "GpuStressSummary",
    "GpuTelemetryReading",
    "NvmlTelemetrySampler",
    "ProgressCallback",
    "TelemetrySampler",
    "compute_gpu_stress_verdict",
    "series_to_samples",
    "summarize_gpu_stress",
]
