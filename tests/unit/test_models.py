"""Тесты доменных моделей (перенесено из test_core.py + расширения)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CpuCores,
    Diagnostic,
    DiagnosticSeverity,
    MetricSnapshot,
    MicroBenchResult,
    MicroBenchSuiteResult,
    OverallScore,
    StressResult,
    SystemInfo,
    ThermalStabilityResult,
)


def _sample_system_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.22621",
        cpu_model="Intel Core i7-12700K",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        gpu_list=["NVIDIA GeForce RTX 3080"],
        cpu_arch="AMD64",
        hostname="test-host",
        timestamp=datetime.now(timezone.utc),
    )


def test_system_info_roundtrip():
    info = _sample_system_info()
    js = info.model_dump_json()
    back = SystemInfo.model_validate_json(js)
    assert back == info


def test_metric_snapshot_defaults():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0,
        ram_percent=20.0,
    )
    assert snap.disk_read_mb == 0.0
    assert snap.frequencies == {}
    assert snap.cpu_throttled is False
    # Регрессионный контракт: legacy-вызов без voltages должен оставаться валидным,
    # поле — пустой словарь по умолчанию (аддитивное расширение, не breaking).
    assert snap.voltages == {}


def test_metric_snapshot_voltages_roundtrip():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=50.0,
        ram_percent=40.0,
        voltages={"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95},
    )
    js = snap.model_dump_json()
    back = MetricSnapshot.model_validate_json(js)
    assert back.voltages == {"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95}


def test_cpu_cores_required_fields():
    with pytest.raises(ValidationError):
        CpuCores()  # type: ignore[call-arg]


def test_benchmark_result_with_history():
    info = _sample_system_info()
    cfg = BenchmarkConfig(profile_name="cpu_heavy", duration_sec=10.0)
    snap = MetricSnapshot(timestamp=info.timestamp, cpu_percent=50.0, ram_percent=40.0)
    sr = StressResult(
        engine="builtin_cpu_int",
        category="cpu_int",
        duration_actual_sec=10.0,
        throughput=1e7,
        throughput_unit="ops/s",
    )
    result = BenchmarkResult(
        system_info=info,
        config=cfg,
        start_time=info.timestamp,
        end_time=info.timestamp,
        metrics_history=[snap],
        stress_results=[sr],
        final_score=0.5,
    )
    js = result.model_dump_json()
    back = BenchmarkResult.model_validate_json(js)
    assert back.final_score == pytest.approx(0.5)
    assert len(back.metrics_history) == 1
    assert back.stress_results[0].engine == "builtin_cpu_int"


def test_diagnostic_severity_enum():
    d = Diagnostic(code="x", severity=DiagnosticSeverity.WARN, message="m")
    js = d.model_dump_json()
    assert "warn" in js


# ─────────────────────────── Scoring v2 ─────────────────────────────────────────


def test_overall_score_roundtrip():
    """OverallScore сериализуется и десериализуется без потерь."""
    score = OverallScore(
        overall_ratio=0.45,
        overall_score=450.0,
        subscores={"R_MEM": 0.5, "R_CPU_compute": 0.4, "r_memory": 0.5},
        ci_lower=440.0,
        ci_upper=460.0,
        ci_method="t_logscale",
        n_runs=5,
        reference_id="roofline-v1",
        weights_profile="default",
        scoring_version="2.0.0",
        provisional=False,
        notes=["roofline_partial"],
    )
    js = score.model_dump_json()
    back = OverallScore.model_validate_json(js)
    assert back == score
    assert back.overall_score == pytest.approx(450.0)
    assert back.notes == ["roofline_partial"]


def test_overall_score_defaults():
    """Минимальный конструктор работает с дефолтами."""
    score = OverallScore(overall_ratio=1.0, overall_score=1000.0)
    assert score.scoring_version == "2.0.0"
    assert score.weights_profile == "default"
    assert score.reference_id == "roofline-v1"
    assert score.n_runs == 1
    assert score.provisional is False
    assert score.subscores == {}
    assert score.notes == []
    assert score.ci_lower is None


def test_overall_score_extra_forbidden():
    """Незнакомые поля должны отвергаться (контракт фиксирован)."""
    with pytest.raises(ValidationError):
        OverallScore(
            overall_ratio=1.0,
            overall_score=1000.0,
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_thermal_stability_result_roundtrip():
    res = ThermalStabilityResult(
        frame_rate_stability_pct=98.5,
        pass_threshold_97=True,
        tsc=0.02,
        clock_min_mhz=3500.0,
        clock_max_mhz=3550.0,
        temp_max_c=82.0,
        temp_avg_c=75.4,
        throttle_observed=False,
        samples=1200,
    )
    js = res.model_dump_json()
    back = ThermalStabilityResult.model_validate_json(js)
    assert back == res
    assert back.pass_threshold_97 is True


def test_thermal_stability_all_none():
    """Когда нет данных — все опциональные поля None, проходит валидацию."""
    res = ThermalStabilityResult()
    assert res.frame_rate_stability_pct is None
    assert res.pass_threshold_97 is None
    assert res.throttle_observed is False
    assert res.samples == 0


def test_microbench_suite_with_overall():
    """MicroBenchSuiteResult обратно совместим (overall=None по умолчанию)."""
    info = _sample_system_info()
    suite = MicroBenchSuiteResult(
        system_info=info,
        results=[
            MicroBenchResult(
                name="memory_read",
                category="memory",
                value=10000.0,
                unit="MB/s",
                duration_actual_sec=5.0,
            )
        ],
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
    )
    # Без overall — legacy совместимость.
    assert suite.overall is None
    assert suite.preset is None
    assert suite.n_runs == 1

    # Теперь с overall.
    suite.overall = OverallScore(overall_ratio=0.42, overall_score=420.0)
    suite.preset = "fast"
    js = suite.model_dump_json()
    back = MicroBenchSuiteResult.model_validate_json(js)
    assert back.overall is not None
    assert back.overall.overall_score == pytest.approx(420.0)
    assert back.preset == "fast"


def test_benchmark_result_with_thermal():
    """BenchmarkResult содержит thermal-поле; legacy без него тоже валидируется."""
    info = _sample_system_info()
    cfg = BenchmarkConfig(profile_name="stability", duration_sec=600.0)
    thermal = ThermalStabilityResult(
        frame_rate_stability_pct=97.2,
        pass_threshold_97=True,
        clock_min_mhz=3400.0,
        clock_max_mhz=3500.0,
        samples=1200,
    )
    result = BenchmarkResult(
        system_info=info,
        config=cfg,
        start_time=info.timestamp,
        end_time=info.timestamp,
        thermal=thermal,
    )
    js = result.model_dump_json()
    back = BenchmarkResult.model_validate_json(js)
    assert back.thermal is not None
    assert back.thermal.pass_threshold_97 is True


def test_legacy_microbench_suite_json_still_validates():
    """Старые JSON без полей overall/preset/n_runs должны парситься."""
    info = _sample_system_info()
    raw = {
        "system_info": info.model_dump(mode="json"),
        "results": [],
        "start_time": info.timestamp.isoformat(),
        "end_time": info.timestamp.isoformat(),
        "duration_sec_per_test": 5.0,
    }
    suite = MicroBenchSuiteResult.model_validate(raw)
    assert suite.overall is None
    assert suite.preset is None
    assert suite.n_runs == 1


def test_legacy_benchmark_result_json_still_validates():
    """Старые BenchmarkResult без поля thermal должны парситься."""
    info = _sample_system_info()
    cfg = BenchmarkConfig(profile_name="cpu_heavy", duration_sec=10.0)
    raw = {
        "system_info": info.model_dump(mode="json"),
        "config": cfg.model_dump(mode="json"),
        "start_time": info.timestamp.isoformat(),
        "end_time": info.timestamp.isoformat(),
        "metrics_history": [],
        "stress_results": [],
        "final_score": 0.0,
    }
    result = BenchmarkResult.model_validate(raw)
    assert result.thermal is None
