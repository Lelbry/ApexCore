"""Тесты `application/sensor_service.py` — конвертер + InMemorySensorBus.

Тесты на сам ``SensorService`` с реальным потоком вынесены — это уже
интеграция (нужен тикающий адаптер). Здесь покрываем:

- `metric_to_sensor_snapshot()` чистой конвертацией без потоков;
- `InMemorySensorBus` publish/subscribe/unsubscribe;
- `empty_sensor_snapshot()` — корректный empty case.
"""

from __future__ import annotations

from datetime import datetime, timezone

from apexcore.application.sensor_service import (
    InMemorySensorBus,
    empty_sensor_snapshot,
    metric_to_sensor_snapshot,
)
from apexcore.domain.models import CpuCores, MetricSnapshot, SystemInfo
from apexcore.domain.sensor_models import (
    SensorGroup,
    SensorKind,
    SensorSnapshot,
    ThrottleCause,
    ThrottleState,
)

# ─── InMemorySensorBus ───────────────────────────────────────────────────


def test_bus_publish_calls_all_subscribers() -> None:
    bus = InMemorySensorBus()
    received_a: list[SensorSnapshot] = []
    received_b: list[SensorSnapshot] = []
    bus.subscribe(received_a.append)
    bus.subscribe(received_b.append)

    snap = empty_sensor_snapshot()
    bus.publish(snap)

    assert received_a == [snap]
    assert received_b == [snap]


def test_bus_unsubscribe_removes_subscriber() -> None:
    bus = InMemorySensorBus()
    received: list[SensorSnapshot] = []
    unsubscribe = bus.subscribe(received.append)

    bus.publish(empty_sensor_snapshot())
    unsubscribe()
    bus.publish(empty_sensor_snapshot())

    assert len(received) == 1


def test_bus_failing_subscriber_doesnt_block_others() -> None:
    bus = InMemorySensorBus()
    received: list[SensorSnapshot] = []

    def boom(_snap: SensorSnapshot) -> None:
        raise RuntimeError("boom")

    bus.subscribe(boom)
    bus.subscribe(received.append)

    bus.publish(empty_sensor_snapshot())  # не должен бросить

    assert len(received) == 1


# ─── metric_to_sensor_snapshot ───────────────────────────────────────────


def _make_metric(**overrides) -> MetricSnapshot:
    base = dict(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=15.0,
        ram_percent=50.0,
        ram_used_gb=16.0,
        temperatures={
            "cpu/cpu_package": 65.0,
            "cpu/p_core_1": 60.0,
            "gpunvidia/gpu_hot_spot": 63.0,
        },
        voltages={
            "cpu/cpu_core": 1.34,
            "nvml/0/power_w": 200.0,
        },
        frequencies={
            "cpu_avg": 4500.0,    # должен быть отфильтрован
            "cpu_max": 4880.0,    # тоже метаданные
            "nvml/0/clock_graphics": 2910.0,
        },
        cpu_throttled=False,
        power_w=None,
    )
    base.update(overrides)
    return MetricSnapshot(**base)


def _make_system_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0.26200",
        cpu_model="Intel i9-12900K",
        cpu_cores=CpuCores(physical=16, logical=24),
        ram_total_gb=32.0,
        gpu_list=["NVIDIA RTX 4070 Ti"],
        timestamp=datetime.now(timezone.utc),
    )


def test_converter_produces_grouped_readings() -> None:
    metric = _make_metric()
    snap = metric_to_sensor_snapshot(
        metric,
        system_info=_make_system_info(),
        gpu_devices={"gpunvidia": "NVIDIA RTX 4070 Ti", "nvml": "NVIDIA RTX 4070 Ti"},
        throttle=ThrottleState(),
    )

    cpu = snap.by_group(SensorGroup.CPU)
    gpu = snap.by_group(SensorGroup.GPU)

    cpu_temps = {r.label for r in cpu if r.kind is SensorKind.TEMPERATURE}
    assert "Package" in cpu_temps
    assert "Ядро P1" in cpu_temps

    # Vcore попал в CPU/VOLTAGE
    cpu_volts = [r for r in cpu if r.kind is SensorKind.VOLTAGE]
    assert any(r.label == "Vcore" for r in cpu_volts)

    # NVML power оказался в GPU/POWER (не в voltage, несмотря на dict)
    gpu_power = [r for r in gpu if r.kind is SensorKind.POWER]
    assert len(gpu_power) == 1
    assert gpu_power[0].value == 200.0
    assert gpu_power[0].unit == "Вт"

    # GPU clock попал в GPU/FREQUENCY
    gpu_freq = [r for r in gpu if r.kind is SensorKind.FREQUENCY]
    assert any(r.label == "Graphics clock" for r in gpu_freq)


def test_converter_filters_metadata_keys() -> None:
    metric = _make_metric()
    snap = metric_to_sensor_snapshot(metric, throttle=ThrottleState())
    freq_readings = snap.by_kind(SensorKind.FREQUENCY)
    # cpu_max — метаданные (capability), всё ещё отбрасывается.
    # cpu_avg теперь публикуется как Reading «Частота (средняя)» в группе CPU,
    # плюс остаётся NVML clock_graphics.
    labels = [r.label for r in freq_readings]
    assert any("Частота" in lbl for lbl in labels), labels
    assert any(r.source.value == "nvml" for r in freq_readings), labels


def test_converter_uses_system_info_cpu_model() -> None:
    metric = _make_metric()
    snap = metric_to_sensor_snapshot(
        metric,
        system_info=_make_system_info(),
        throttle=ThrottleState(),
    )
    cpu_readings = snap.by_group(SensorGroup.CPU)
    assert all(r.device == "Intel i9-12900K" for r in cpu_readings)


def test_converter_attaches_throttle_state() -> None:
    metric = _make_metric()
    state = ThrottleState(cause=ThrottleCause.THERMAL, detail="p_core_3")
    snap = metric_to_sensor_snapshot(metric, throttle=state)
    assert snap.throttle.cause is ThrottleCause.THERMAL


def test_converter_attaches_tjmax_threshold() -> None:
    metric = _make_metric()
    snap = metric_to_sensor_snapshot(
        metric,
        tjmax_by_key={"cpu/p_core_1": 100.0, "cpu/cpu_package": 100.0},
        throttle=ThrottleState(),
    )
    p1 = next(r for r in snap.readings if r.sensor == "p_core_1")
    assert p1.threshold_crit == 100.0


def test_empty_sensor_snapshot_has_no_readings() -> None:
    snap = empty_sensor_snapshot()
    assert snap.readings == []
    assert snap.throttle.cause is ThrottleCause.NONE
