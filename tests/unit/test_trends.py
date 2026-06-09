"""Тесты модуля временных трендов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apexcore.application.trends import (
    build_run_trend,
    rolling_mean,
    rolling_p95,
)
from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CpuCores,
    SystemInfo,
)


def test_rolling_mean_simple():
    out = rolling_mean([1, 2, 3, 4, 5], 3)
    assert out == [1.0, 1.5, 2.0, 3.0, 4.0]


def test_rolling_p95_handles_short_window():
    out = rolling_p95([10, 20, 30], 1)
    assert out == [10, 20, 30]


def _run(score: float, when: datetime) -> BenchmarkResult:
    info = SystemInfo(
        os_name="Linux", os_version="5", cpu_model="x", cpu_cores=CpuCores(physical=1, logical=1),
        ram_total_gb=1, gpu_list=[], timestamp=when,
    )
    cfg = BenchmarkConfig(profile_name="x", duration_sec=1)
    return BenchmarkResult(
        system_info=info, config=cfg, start_time=when, end_time=when,
        final_score=score,
    )


def test_build_run_trend_sorts_old_to_new_and_extracts_score():
    base = datetime.now(timezone.utc)
    runs = [
        _run(0.5, base + timedelta(hours=2)),
        _run(0.1, base + timedelta(hours=0)),
        _run(0.9, base + timedelta(hours=1)),
    ]
    trend = build_run_trend(runs, metric="final_score", window=2)
    assert trend.values == [0.1, 0.9, 0.5]
    assert len(trend.rolling_mean) == 3
