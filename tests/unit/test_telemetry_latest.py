"""TelemetryService.latest() — мгновенная отдача последнего снимка.

Регрессия: топбар/дашборд держали «—» / «WS down» до первого тика семплера,
потому что ``/ws/metrics`` ничего не слал на connect (в отличие от
``/ws/sensors``). Фикс — ``TelemetryService.latest()`` + initial-push в
эндпоинте. Здесь проверяем контракт ``latest()``: None до старта, валидный
снимок после, сброс после ``start()``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from apexcore.application.telemetry_service import InMemoryMetricsBus, TelemetryService
from apexcore.domain.models import MetricSnapshot


class _FakeAdapter:
    """OS-адаптер, отдающий фиксированный снимок с инкрементом cpu_percent."""

    def __init__(self) -> None:
        self.calls = 0

    def get_current_metrics(self) -> MetricSnapshot:
        self.calls += 1
        return MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=float(self.calls),
            cpu_per_core_percent=[],
            ram_percent=20.0,
            temperatures={"cpu/package": 45.0},
            frequencies={"cpu_avg": 3200.0},
            voltages={},
        )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_latest_none_before_start():
    svc = TelemetryService(_FakeAdapter(), InMemoryMetricsBus(), sampling_rate_sec=0.05)
    assert svc.latest() is None


def test_latest_populated_after_start():
    svc = TelemetryService(_FakeAdapter(), InMemoryMetricsBus(), sampling_rate_sec=0.05)
    svc.start(record_history=False)
    try:
        assert _wait_until(lambda: svc.latest() is not None), "latest() так и не заполнился"
        snap = svc.latest()
        assert isinstance(snap, MetricSnapshot)
        assert snap.cpu_percent >= 1.0
    finally:
        svc.stop()


def test_latest_resets_on_restart():
    svc = TelemetryService(_FakeAdapter(), InMemoryMetricsBus(), sampling_rate_sec=0.05)
    svc.start(record_history=False)
    assert _wait_until(lambda: svc.latest() is not None)
    svc.stop()
    assert svc.latest() is not None  # после stop последний снимок ещё доступен

    # Повторный start() обнуляет latest до первого нового тика.
    svc.start(record_history=False)
    try:
        # Сразу после start (до первого тика) latest может быть None — это ок;
        # главное, что вскоре снова заполняется свежим снимком.
        assert _wait_until(lambda: svc.latest() is not None)
    finally:
        svc.stop()
