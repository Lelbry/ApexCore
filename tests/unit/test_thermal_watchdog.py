"""Юнит-тесты ``application/thermal_watchdog.py``."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from apexcore.application.telemetry_service import InMemoryMetricsBus
from apexcore.application.thermal_watchdog import (
    ThermalWatchdog,
    WatchdogTrigger,
    _is_cpu_temp_key,
    detect_tjmax,
)
from apexcore.domain.models import MetricSnapshot


def _snap(temps: dict[str, float]) -> MetricSnapshot:
    return MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0,
        ram_percent=10.0,
        temperatures=temps,
    )


def test_is_cpu_temp_key_filters_gpu_and_storage():
    assert _is_cpu_temp_key("cpu/package")
    assert _is_cpu_temp_key("cpu/cpu_core_1")
    assert _is_cpu_temp_key("coretemp/temp1")
    assert _is_cpu_temp_key("k10temp/tdie")
    # GPU и SSD должны фильтроваться
    assert not _is_cpu_temp_key("gpu/temperature")
    assert not _is_cpu_temp_key("storage/nvme0_temp")
    assert not _is_cpu_temp_key("ssd/composite")
    # ACPI thermal zone (Windows perf-counter / MSAcpi) ИСКЛЮЧЁН: на практике
    # это температура корпуса/чипсета, а не самого CPU (статично 25–30°C
    # даже при полной нагрузке) — лучше «нет данных», чем ложь.
    assert not _is_cpu_temp_key("thermal_zone_0")
    assert not _is_cpu_temp_key("\\thermal zone information(_total)\\temperature")


def test_watchdog_does_not_trigger_below_threshold():
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0, margin_celsius=5.0)
    wd.start()
    bus.publish(_snap({"cpu/package": 70.0}))
    bus.publish(_snap({"cpu/package": 90.0}))
    assert not wd.triggered
    assert not token.is_set()
    wd.stop()


def test_watchdog_triggers_at_threshold_instant_mode():
    """Со включённым `grace_window_sec=0` — instant trigger по threshold (старое поведение)."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    triggers: list[WatchdogTrigger] = []
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=0.0,
        on_trigger=triggers.append,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 80.0}))
    bus.publish(_snap({"cpu/package": 96.0}))  # 96 ≥ 100 − 5 — триггер
    assert wd.triggered
    assert token.is_set()
    assert wd.trigger is not None
    assert wd.trigger.sensor_key == "cpu/package"
    assert wd.trigger.temperature_c == 96.0
    assert wd.trigger.reason == "threshold_reached"
    assert len(triggers) == 1
    wd.stop()


def test_watchdog_only_one_trigger():
    """После первого срабатывания дальнейшие тики игнорируются."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    triggers: list[WatchdogTrigger] = []
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=0.0,
        on_trigger=triggers.append,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 96.0}))
    bus.publish(_snap({"cpu/package": 99.0}))
    bus.publish(_snap({"cpu/package": 99.5}))
    assert len(triggers) == 1
    wd.stop()


def test_watchdog_ignores_gpu_temperature():
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0, margin_celsius=5.0)
    wd.start()
    # GPU 105 °C — игнор; CPU 70 °C — норма; не триггер.
    bus.publish(_snap({"gpu/temperature": 105.0, "cpu/package": 70.0}))
    assert not wd.triggered
    wd.stop()


def test_watchdog_stop_is_idempotent():
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0)
    wd.start()
    wd.stop()
    wd.stop()  # повторный вызов — без падения


def test_detect_tjmax_returns_fallback_when_unavailable(monkeypatch):
    """Если LHM/hwmon недоступны — возвращается fallback и источник 'fallback'."""
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.lhm.read_lhm_tjmax",
        lambda: {},
    )
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.hwmon_thresholds.read_hwmon_tjmax",
        lambda: {},
    )
    val, source = detect_tjmax(fallback=95.0)
    assert val == 95.0
    assert source == "fallback"


# ─── P0.7: had_data / no_data_reason ────────────────────────────────────────


def test_watchdog_had_data_false_when_no_snapshots():
    """До первого snapshot нельзя сказать, были ли данные."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0)
    wd.start()
    assert not wd.had_data  # 0 snapshots → пока нет ответа
    assert wd.no_data_reason is None  # тоже неопределённо
    wd.stop()


def test_watchdog_had_data_true_when_cpu_present():
    """Хотя бы один snapshot с CPU-keys → had_data=True."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0)
    wd.start()
    bus.publish(_snap({"cpu/package": 60.0}))
    assert wd.had_data
    assert wd.no_data_reason is None  # данные были — reason не нужен
    wd.stop()


def test_watchdog_no_data_reason_when_all_empty(monkeypatch):
    """Все snapshot'ы без CPU-keys → no_data_reason возвращает классификацию.

    **Регрессия P0.7**: дифференциация «trigger не сработал» (есть данные,
    < порога) vs «нет данных» (CPU keys пустые). Без этой логики UI
    показывал «watchdog не зафиксирован» даже когда данных вообще нет —
    семантически неверно (см. план §0.7).
    """
    # Мокаем classify_lhm_no_cpu_reason чтобы не дёргать probe на CI.
    from apexcore.application import diagnostics_sensors
    from apexcore.domain.sensor_models import DegradedReason

    monkeypatch.setattr(
        diagnostics_sensors,
        "_classify_lhm_no_cpu_reason",
        lambda: DegradedReason.HVCI_BLOCKED,
    )

    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(bus=bus, cancel_token=token, tjmax=100.0)
    wd.start()
    # Только GPU-температура — ни одного CPU-key.
    bus.publish(_snap({"gpu/temperature": 70.0}))
    bus.publish(_snap({"gpu/temperature": 72.0}))
    bus.publish(_snap({}))  # совсем пустой

    assert not wd.had_data
    reason = wd.no_data_reason
    assert reason is DegradedReason.HVCI_BLOCKED
    wd.stop()


# ─── Grace-window: T ≥ Tj_max − margin даёт окно перед forced stop ───────────


def test_watchdog_grace_warning_zone_does_not_trigger_immediately():
    """С `grace_window_sec=60` (по умолчанию) при T в зоне warning trigger
    НЕ выставляется сразу — даём прогону измерить sustainable performance."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    triggers: list[WatchdogTrigger] = []
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=60.0,
        on_trigger=triggers.append,
    )
    wd.start()
    # T=96 ≥ 95 (warning), но < 100 (tjmax) — grace timer запущен, не trigger.
    bus.publish(_snap({"cpu/package": 96.0}))
    assert not wd.triggered
    assert not token.is_set()
    assert wd.in_grace_window
    assert wd.grace_remaining_sec is not None and wd.grace_remaining_sec > 0
    wd.stop()


def test_watchdog_grace_reset_when_temp_drops():
    """T опустилась ниже threshold → grace-таймер сбрасывается."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=60.0,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 96.0}))   # warning → grace запущен
    assert wd.in_grace_window
    bus.publish(_snap({"cpu/package": 80.0}))   # упало → reset
    assert not wd.in_grace_window
    assert wd.grace_remaining_sec is None
    assert not wd.triggered
    wd.stop()


def test_watchdog_grace_expired_fires_trigger():
    """Если T держится в зоне warning ≥ grace_window_sec — forced stop с reason."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    triggers: list[WatchdogTrigger] = []
    # Очень короткое grace-окно (0.05 сек) → таймер истечёт быстро.
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=0.05,
        on_trigger=triggers.append,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 96.0}))   # запуск grace
    import time
    time.sleep(0.07)
    bus.publish(_snap({"cpu/package": 96.5}))   # grace истёк → trigger
    assert wd.triggered
    assert token.is_set()
    assert wd.trigger is not None
    assert wd.trigger.reason == "grace_window_expired"
    assert len(triggers) == 1
    wd.stop()


def test_watchdog_hard_stop_at_tjmax_even_with_grace():
    """Достижение абсолютного Tj_max — instant stop, минуя grace-window."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    triggers: list[WatchdogTrigger] = []
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=60.0,
        on_trigger=triggers.append,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 100.5}))   # T ≥ tjmax → hard stop
    assert wd.triggered
    assert token.is_set()
    assert wd.trigger is not None
    assert wd.trigger.reason == "tjmax_reached"
    assert len(triggers) == 1
    wd.stop()


def test_watchdog_grace_not_reset_by_missing_data():
    """Snapshot без CPU-keys не сбрасывает grace state (защита от пропуска сенсора)."""
    bus = InMemoryMetricsBus()
    token = threading.Event()
    wd = ThermalWatchdog(
        bus=bus,
        cancel_token=token,
        tjmax=100.0,
        margin_celsius=5.0,
        grace_window_sec=60.0,
    )
    wd.start()
    bus.publish(_snap({"cpu/package": 96.0}))   # grace запущен
    assert wd.in_grace_window
    bus.publish(_snap({"gpu/temperature": 50.0}))   # нет CPU-keys
    bus.publish(_snap({}))   # пустой
    # Grace state сохранился — таймер не сброшен пропуском сенсора.
    assert wd.in_grace_window
    wd.stop()
