"""Юнит-тесты ``application/safety_gate.py``."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from apexcore.application.safety_gate import (
    SafetyGate,
    SafetyReport,
    detect_virtualization,
)
from apexcore.domain.models import MetricSnapshot


class _FakeAdapter:
    """Минимальный адаптер для конструктора SafetyGate."""

    name = "fake"

    def get_system_info(self):  # pragma: no cover
        raise NotImplementedError

    def get_current_metrics(self) -> MetricSnapshot:  # pragma: no cover
        raise NotImplementedError

    def check_prerequisites(self) -> bool:
        return True

    def get_available_temps(self) -> list[str]:
        return []

    def get_frequencies_mhz(self) -> dict[str, float]:
        return {}


def _patch_psutil(
    monkeypatch: pytest.MonkeyPatch,
    *,
    battery_percent: float | None,
    on_ac: bool,
    free_ram_gb: float,
) -> None:
    """Подменяем psutil.sensors_battery/virtual_memory на детерминированные."""
    import psutil

    if battery_percent is None:
        monkeypatch.setattr(psutil, "sensors_battery", lambda: None)
    else:
        battery = SimpleNamespace(
            percent=battery_percent,
            power_plugged=on_ac,
            secsleft=99999,
        )
        monkeypatch.setattr(psutil, "sensors_battery", lambda: battery)

    vm = SimpleNamespace(
        total=(16 * 1024 ** 3),
        available=int(free_ram_gb * 1024 ** 3),
        used=0,
        free=int(free_ram_gb * 1024 ** 3),
        percent=0.0,
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: vm)


def test_desktop_without_battery_passes(monkeypatch):
    _patch_psutil(monkeypatch, battery_percent=None, on_ac=True, free_ram_gb=10.0)
    monkeypatch.setattr(
        "apexcore.application.safety_gate.detect_virtualization",
        lambda: (False, None),
    )
    gate = SafetyGate(_FakeAdapter())
    report = gate.check_pre_flight()
    assert not report.blocked
    assert report.battery_percent is None
    assert not report.is_virtualized


def test_blocked_on_low_battery(monkeypatch):
    _patch_psutil(monkeypatch, battery_percent=30.0, on_ac=False, free_ram_gb=10.0)
    monkeypatch.setattr(
        "apexcore.application.safety_gate.detect_virtualization",
        lambda: (False, None),
    )
    gate = SafetyGate(_FakeAdapter())
    report = gate.check_pre_flight()
    assert report.blocked
    assert report.on_battery
    assert any("батарее" in r.lower() for r in report.block_reasons)


def test_warns_on_battery_above_threshold(monkeypatch):
    _patch_psutil(monkeypatch, battery_percent=70.0, on_ac=False, free_ram_gb=10.0)
    monkeypatch.setattr(
        "apexcore.application.safety_gate.detect_virtualization",
        lambda: (False, None),
    )
    gate = SafetyGate(_FakeAdapter())
    report = gate.check_pre_flight()
    assert not report.blocked  # ≥ 50% — не блокируем
    assert report.on_battery
    assert report.warn_reasons


def test_blocked_in_vm(monkeypatch):
    _patch_psutil(monkeypatch, battery_percent=None, on_ac=True, free_ram_gb=10.0)
    monkeypatch.setattr(
        "apexcore.application.safety_gate.detect_virtualization",
        lambda: (True, "kvm"),
    )
    gate = SafetyGate(_FakeAdapter())
    report = gate.check_pre_flight()
    assert report.blocked
    assert report.is_virtualized
    assert report.virtualization_kind == "kvm"
    assert any("kvm" in r.lower() for r in report.block_reasons)


def test_blocked_on_low_free_ram(monkeypatch):
    _patch_psutil(monkeypatch, battery_percent=None, on_ac=True, free_ram_gb=0.5)
    monkeypatch.setattr(
        "apexcore.application.safety_gate.detect_virtualization",
        lambda: (False, None),
    )
    gate = SafetyGate(_FakeAdapter())
    report = gate.check_pre_flight()
    assert report.blocked
    assert any("ram" in r.lower() for r in report.block_reasons)


def test_cooling_sanity_warns_when_no_temp_rise():
    gate = SafetyGate(_FakeAdapter())
    report = SafetyReport()
    cb, finished = gate.cooling_sanity_subscriber(report)

    # Симулируем «холодная T → почти не выросла за 30+ с».
    snap1 = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=99.0,
        ram_percent=10.0,
        temperatures={"cpu/package": 50.0},
    )
    cb(snap1)
    assert report.initial_temp_c == 50.0

    # Чтобы триггер сработал, нужно сместить time.monotonic вперёд.
    import time

    real_monotonic = time.monotonic
    base = real_monotonic()
    # Подменяем monotonic на ленту, возвращающую +40 секунд.
    monkeypatched_done = {"done": False}

    def fake_monotonic() -> float:
        if monkeypatched_done["done"]:
            return base + 0.0
        return base + 40.0

    time.monotonic = fake_monotonic  # type: ignore[assignment]
    try:
        snap2 = MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=99.0,
            ram_percent=10.0,
            temperatures={"cpu/package": 51.0},  # ΔT всего 1°C
        )
        cb(snap2)
    finally:
        time.monotonic = real_monotonic  # type: ignore[assignment]
    assert finished.is_set()
    assert report.cooling_sanity_ok is False
    assert any("cooling-sanity" in w.lower() for w in report.warn_reasons)


def test_cooling_sanity_passes_when_temp_rises():
    gate = SafetyGate(_FakeAdapter())
    report = SafetyReport()
    cb, finished = gate.cooling_sanity_subscriber(report)

    cb(MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=99.0,
        ram_percent=10.0,
        temperatures={"cpu/package": 50.0},
    ))

    import time

    base = time.monotonic()
    real_monotonic = time.monotonic
    time.monotonic = lambda: base + 35.0  # type: ignore[assignment]
    try:
        cb(MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=99.0,
            ram_percent=10.0,
            temperatures={"cpu/package": 70.0},  # ΔT = 20°C
        ))
    finally:
        time.monotonic = real_monotonic  # type: ignore[assignment]
    assert finished.is_set()
    assert report.cooling_sanity_ok is True
    # warn-список не должен пополняться cooling-sanity записью.
    assert not any("cooling-sanity" in w.lower() for w in report.warn_reasons)


def test_detect_virtualization_no_crash():
    """Проверка что функция отрабатывает без исключений на любой ОС."""
    is_vm, kind = detect_virtualization()
    assert isinstance(is_vm, bool)
    if is_vm:
        assert kind is None or isinstance(kind, str)
