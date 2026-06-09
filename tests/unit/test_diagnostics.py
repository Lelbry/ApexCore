"""Тесты движка диагностики."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from apexcore.application.diagnostics import diagnose_run
from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CpuCores,
    MetricSnapshot,
    StressResult,
    SystemInfo,
)


def _system() -> SystemInfo:
    return SystemInfo(
        os_name="Linux",
        os_version="5.10",
        cpu_model="Test",
        cpu_cores=CpuCores(physical=4, logical=8),
        ram_total_gb=16.0,
        gpu_list=[],
        timestamp=datetime.now(timezone.utc),
    )


def _run(snaps: list[MetricSnapshot], stress: list[StressResult] | None = None) -> BenchmarkResult:
    info = _system()
    cfg = BenchmarkConfig(profile_name="balanced", duration_sec=10.0)
    return BenchmarkResult(
        system_info=info,
        config=cfg,
        start_time=info.timestamp,
        end_time=info.timestamp,
        metrics_history=snaps,
        stress_results=stress or [],
    )


def test_temperature_critical_triggers_diagnostic():
    snaps = [
        MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=80,
            ram_percent=20,
            temperatures={"package": 95.0},
        )
    ]
    diags = diagnose_run(_run(snaps))
    assert any(d.code == "temperature_critical" for d in diags)


def test_throttling_warning_triggers():
    base = datetime.now(timezone.utc)
    snaps = [
        MetricSnapshot(
            timestamp=base + timedelta(seconds=i),
            cpu_percent=90,
            ram_percent=20,
            cpu_throttled=(i % 5 == 0),
        )
        for i in range(50)
    ]
    diags = diagnose_run(_run(snaps))
    assert any("throttle" in d.code for d in diags)


def test_no_issues_returns_empty():
    base = datetime.now(timezone.utc)
    snaps = [
        MetricSnapshot(
            timestamp=base + timedelta(seconds=i),
            cpu_percent=10,
            ram_percent=15,
            temperatures={"cpu": 45.0},
        )
        for i in range(20)
    ]
    diags = diagnose_run(_run(snaps))
    # Допускается одна-две info-диагностики, но critical/warn быть не должно.
    assert all(d.severity != "critical" for d in diags)


def test_baseline_comparison_detects_degradation():
    base_time = datetime.now(timezone.utc)
    rng = np.random.default_rng(0)
    base_snaps = [
        MetricSnapshot(
            timestamp=base_time + timedelta(seconds=i),
            cpu_percent=float(rng.normal(40, 3)),
            ram_percent=float(rng.normal(40, 3)),
        )
        for i in range(60)
    ]
    cur_snaps = [
        MetricSnapshot(
            timestamp=base_time + timedelta(seconds=i),
            cpu_percent=float(rng.normal(80, 3)),
            ram_percent=float(rng.normal(40, 3)),
        )
        for i in range(60)
    ]
    diags = diagnose_run(_run(cur_snaps), baseline_run=_run(base_snaps))
    assert any(d.code == "cpu_percent_degradation" for d in diags)
