"""Юнит-тесты thermal module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apexcore.application import thermal
from apexcore.domain.models import MetricSnapshot


def _snap(
    cpu_avg: float | None = None,
    temp: float | None = None,
    throttled: bool = False,
    seconds_offset: int = 0,
) -> MetricSnapshot:
    base = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    freqs = {"cpu_avg": cpu_avg} if cpu_avg is not None else {}
    temps = {"package": temp} if temp is not None else {}
    return MetricSnapshot(
        timestamp=base + timedelta(seconds=seconds_offset),
        cpu_percent=80.0,
        ram_percent=50.0,
        frequencies=freqs,
        temperatures=temps,
        cpu_throttled=throttled,
    )


# ─── Frame rate stability ───────────────────────────────────────────────────


def test_thermal_stability_perfect_clocks_100pct():
    """Все частоты одинаковые → 100% stability."""
    snaps = [_snap(cpu_avg=3500.0) for _ in range(10)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.frame_rate_stability_pct == pytest.approx(100.0)
    assert result.pass_threshold_97 is True
    assert result.clock_min_mhz == 3500.0
    assert result.clock_max_mhz == 3500.0


def test_thermal_stability_drops_to_75pct_fail():
    """Частоты упали с 4000 до 3000 → 75% — fail."""
    snaps = [_snap(cpu_avg=4000.0)] + [_snap(cpu_avg=3000.0) for _ in range(5)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.frame_rate_stability_pct == pytest.approx(75.0)
    assert result.pass_threshold_97 is False


def test_thermal_stability_above_97_pass():
    """98% — pass."""
    snaps = [_snap(cpu_avg=4000.0), _snap(cpu_avg=3920.0)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.frame_rate_stability_pct == pytest.approx(98.0)
    assert result.pass_threshold_97 is True


# ─── Температуры ────────────────────────────────────────────────────────────


def test_thermal_temperatures_aggregated():
    """temp_max = глобальный максимум; temp_avg = среднее по всем snapshots."""
    snaps = [
        _snap(temp=70.0),
        _snap(temp=85.0),
        _snap(temp=80.0),
    ]
    result = thermal.compute_thermal_stability(snaps)
    assert result.temp_max_c == pytest.approx(85.0)
    assert result.temp_avg_c == pytest.approx((70 + 85 + 80) / 3)


def test_thermal_no_temperatures_returns_none():
    snaps = [_snap(cpu_avg=3000.0) for _ in range(5)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.temp_max_c is None
    assert result.temp_avg_c is None


# ─── Throttle ────────────────────────────────────────────────────────────────


def test_thermal_throttle_observed():
    snaps = [_snap(cpu_avg=3000.0, throttled=False), _snap(cpu_avg=2000.0, throttled=True)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.throttle_observed is True


def test_thermal_no_throttle():
    snaps = [_snap(cpu_avg=3000.0, throttled=False) for _ in range(5)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.throttle_observed is False


# ─── Edge cases ──────────────────────────────────────────────────────────────


def test_thermal_empty_history():
    result = thermal.compute_thermal_stability([])
    assert result.frame_rate_stability_pct is None
    assert result.pass_threshold_97 is None
    assert result.samples == 0
    assert result.throttle_observed is False


def test_thermal_no_clock_data():
    """История есть, но нет cpu_avg → frame_rate_stability_pct None."""
    snaps = [_snap(temp=70.0) for _ in range(5)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.frame_rate_stability_pct is None
    assert result.pass_threshold_97 is None
    assert result.temp_max_c == 70.0


def test_thermal_samples_count():
    snaps = [_snap(cpu_avg=3000.0) for _ in range(42)]
    result = thermal.compute_thermal_stability(snaps)
    assert result.samples == 42


# ─── TSC ────────────────────────────────────────────────────────────────────


def test_tsc_basic():
    """S_cold=1000, S_steady=900 → TSC = 0.1 (10% deg)."""
    assert thermal.compute_tsc(1000.0, 900.0) == pytest.approx(0.1)


def test_tsc_no_degradation():
    assert thermal.compute_tsc(1000.0, 1000.0) == pytest.approx(0.0)


def test_tsc_negative_when_steady_higher():
    """Возможно негативный TSC, если steady почему-то выше cold."""
    assert thermal.compute_tsc(1000.0, 1100.0) == pytest.approx(-0.1)


def test_tsc_none_for_invalid_inputs():
    assert thermal.compute_tsc(None, 900.0) is None
    assert thermal.compute_tsc(1000.0, None) is None
    assert thermal.compute_tsc(0.0, 900.0) is None
    assert thermal.compute_tsc(-100.0, 900.0) is None
