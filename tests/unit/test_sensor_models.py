"""Тесты `domain/sensor_models.py` — контракт DTO для раздела «Датчики»."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from apexcore.domain.sensor_models import (
    SensorGroup,
    SensorKind,
    SensorReading,
    SensorSnapshot,
    SourceBackend,
    ThrottleCause,
    ThrottleState,
)

# ─── ThrottleState ───────────────────────────────────────────────────────


def test_throttle_state_default_is_none() -> None:
    state = ThrottleState()
    assert state.cause is ThrottleCause.NONE
    assert state.detail == ""
    assert state.active is False


def test_throttle_state_active_when_cause_set() -> None:
    state = ThrottleState(cause=ThrottleCause.THERMAL, detail="core 3 at Tjmax")
    assert state.active is True


def test_throttle_state_is_frozen() -> None:
    state = ThrottleState(cause=ThrottleCause.POWER)
    with pytest.raises(ValidationError):
        state.cause = ThrottleCause.NONE  # type: ignore[misc]


def test_throttle_state_forbids_extras() -> None:
    with pytest.raises(ValidationError):
        ThrottleState(cause=ThrottleCause.NONE, foo="bar")  # type: ignore[call-arg]


# ─── SensorReading ───────────────────────────────────────────────────────


def _make_reading(**overrides) -> SensorReading:
    base = dict(
        group=SensorGroup.CPU,
        device="Intel i9-12900K",
        sensor="p_core_1",
        label="Ядро P1",
        kind=SensorKind.TEMPERATURE,
        value=55.0,
        unit="°C",
        source=SourceBackend.LHM,
    )
    base.update(overrides)
    return SensorReading(**base)


def test_sensor_reading_required_fields() -> None:
    r = _make_reading()
    assert r.group is SensorGroup.CPU
    assert r.value == 55.0
    assert r.threshold_warn is None
    assert r.threshold_crit is None


def test_sensor_reading_with_thresholds() -> None:
    r = _make_reading(threshold_warn=80.0, threshold_crit=100.0)
    assert r.threshold_warn == 80.0
    assert r.threshold_crit == 100.0


def test_sensor_reading_is_frozen() -> None:
    r = _make_reading()
    with pytest.raises(ValidationError):
        r.value = 90.0  # type: ignore[misc]


def test_sensor_reading_forbids_extras() -> None:
    with pytest.raises(ValidationError):
        SensorReading(  # type: ignore[call-arg]
            group=SensorGroup.CPU, device="x", sensor="y", label="Z",
            kind=SensorKind.TEMPERATURE, value=1.0, unit="°C",
            source=SourceBackend.LHM, extra_field="boom",
        )


# ─── SensorSnapshot ──────────────────────────────────────────────────────


def test_sensor_snapshot_empty_defaults() -> None:
    snap = SensorSnapshot(timestamp=datetime.now(timezone.utc))
    assert snap.readings == []
    assert snap.throttle.cause is ThrottleCause.NONE


def test_sensor_snapshot_by_group() -> None:
    cpu = _make_reading(group=SensorGroup.CPU)
    gpu = _make_reading(group=SensorGroup.GPU, sensor="gpu_core", label="GPU Core")
    snap = SensorSnapshot(
        timestamp=datetime.now(timezone.utc),
        readings=[cpu, gpu],
    )
    assert snap.by_group(SensorGroup.CPU) == [cpu]
    assert snap.by_group(SensorGroup.GPU) == [gpu]
    assert snap.by_group(SensorGroup.MEMORY) == []


def test_sensor_snapshot_by_kind() -> None:
    temp = _make_reading(kind=SensorKind.TEMPERATURE)
    volt = _make_reading(kind=SensorKind.VOLTAGE, sensor="cpu_core", label="Vcore", unit="В", value=1.2)
    snap = SensorSnapshot(
        timestamp=datetime.now(timezone.utc),
        readings=[temp, volt],
    )
    assert snap.by_kind(SensorKind.TEMPERATURE) == [temp]
    assert snap.by_kind(SensorKind.VOLTAGE) == [volt]


def test_sensor_snapshot_is_frozen() -> None:
    snap = SensorSnapshot(timestamp=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        snap.readings = []  # type: ignore[misc]
