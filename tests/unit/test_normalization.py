"""Тесты регрессионного детектора (бывший normalize_run, для compare/diagnose)."""

from __future__ import annotations

from datetime import datetime, timezone

from apexcore.application.normalization import (
    baseline_from_run,
    normalize_run,
)
from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CpuCores,
    StressResult,
    SystemInfo,
)


def _make_run(cpu_int: float, cpu_fp: float, ram_bw: float) -> BenchmarkResult:
    info = SystemInfo(
        os_name="Linux",
        os_version="5.10",
        cpu_model="Test",
        cpu_cores=CpuCores(physical=4, logical=8),
        ram_total_gb=16.0,
        gpu_list=[],
        timestamp=datetime.now(timezone.utc),
    )
    cfg = BenchmarkConfig(profile_name="balanced", duration_sec=5.0)
    sr = [
        StressResult(engine="cpu_int", category="cpu_int", duration_actual_sec=5,
                     throughput=cpu_int, throughput_unit="ops/s"),
        StressResult(engine="cpu_fp", category="cpu_fp", duration_actual_sec=5,
                     throughput=cpu_fp, throughput_unit="GFLOPS"),
        StressResult(engine="ram_bw", category="ram_bw", duration_actual_sec=5,
                     throughput=ram_bw, throughput_unit="GB/s"),
    ]
    return BenchmarkResult(
        system_info=info,
        config=cfg,
        start_time=info.timestamp,
        end_time=info.timestamp,
        stress_results=sr,
    )


def test_normalize_run_zscore_better_run_has_higher_subscore():
    """Регрессия: лучший прогон относительно baseline → подскор > 0.5."""
    base = _make_run(1e9, 1e9, 1e9)
    bprof = baseline_from_run(base, name="baseline")
    better = _make_run(2e9, 2e9, 2e9)
    norm = normalize_run(better, bprof, method="z_score")
    assert all(v > 0.5 for v in norm.subscores.values())
    assert norm.composite > 0.5


def test_normalize_run_minmax_clamps_into_unit_range():
    """min_max метод гарантирует баллы в [0, 1]."""
    base = _make_run(1e9, 1e9, 1e9)
    bprof = baseline_from_run(base, name="baseline")
    same = _make_run(1e9, 1e9, 1e9)
    norm = normalize_run(same, bprof, method="min_max")
    assert all(0 <= v <= 1 for v in norm.subscores.values())


def test_normalize_run_no_thermal_stability_in_subscores():
    """В scoring v2 thermal_stability не входит в subscores нормализации."""
    base = _make_run(1e9, 1e9, 1e9)
    bprof = baseline_from_run(base, name="baseline")
    norm = normalize_run(base, bprof)
    assert "thermal_stability" not in norm.subscores
