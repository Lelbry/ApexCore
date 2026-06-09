"""Юнит-тесты multi-run агрегации с пресетами и CI."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from apexcore.application import multi_run, weights
from apexcore.application.references import ReferenceSet, ReferenceValue
from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    MicroBenchSuiteResult,
    SystemInfo,
)

# ─── Фабрики ────────────────────────────────────────────────────────────────


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Linux",
        os_version="6.0",
        cpu_model="Intel Core i7-12700K",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        cpu_arch="AMD64",
        timestamp=datetime.now(timezone.utc),
    )


def _rv(wid: str, value: float, unit: str, *, prov: bool = False) -> ReferenceValue:
    src = "empirical_proxy" if prov else "roofline"
    return ReferenceValue(
        workload_id=wid, value=value, unit=unit, source=src, provisional=prov,
    )


def _full_reference() -> ReferenceSet:
    return ReferenceSet(
        id="test-ref",
        version="1.0",
        machine_description="Test ref",
        values={
            "memory_read":   _rv("memory_read",   100000.0, "MB/s"),
            "memory_write":  _rv("memory_write",  100000.0, "MB/s"),
            "memory_copy":   _rv("memory_copy",   200000.0, "MB/s"),
            "flops_sp":      _rv("flops_sp",      1600.0,   "GFLOPS"),
            "flops_dp":      _rv("flops_dp",      800.0,    "GFLOPS"),
            "int_iops_24":   _rv("int_iops_24",   200.0,    "GIOPS"),
            "int_iops_32":   _rv("int_iops_32",   200.0,    "GIOPS"),
            "int_iops_64":   _rv("int_iops_64",   200.0,    "GIOPS"),
            "aes_256":       _rv("aes_256",       4000.0,   "MB/s"),
            "sha1":          _rv("sha1",          1000.0,   "MB/s"),
            "julia_sp":      _rv("julia_sp",      80.0,     "FPS",  prov=True),
            "mandelbrot_dp": _rv("mandelbrot_dp", 40.0,     "FPS",  prov=True),
        },
    )


def _make_suite(factor: float, run_idx: int = 0) -> MicroBenchSuiteResult:
    """Suite где measured = factor × reference; run_idx нужен только для разнообразия."""
    info = _sys_info()
    ref = _full_reference()
    workload_categories = {
        "memory_read":   ("memory", "MB/s"),
        "memory_write":  ("memory", "MB/s"),
        "memory_copy":   ("memory", "MB/s"),
        "flops_sp":      ("flops", "GFLOPS"),
        "flops_dp":      ("flops", "GFLOPS"),
        "int_iops_24":   ("integer", "GIOPS"),
        "int_iops_32":   ("integer", "GIOPS"),
        "int_iops_64":   ("integer", "GIOPS"),
        "aes_256":       ("crypto", "MB/s"),
        "sha1":          ("crypto", "MB/s"),
        "julia_sp":      ("fractal", "FPS"),
        "mandelbrot_dp": ("fractal", "FPS"),
    }
    results = [
        MicroBenchResult(
            name=wid,
            category=cat,
            value=ref.values[wid].value * factor,
            unit=unit,
            duration_actual_sec=5.0,
        )
        for wid, (cat, unit) in workload_categories.items()
    ]
    return MicroBenchSuiteResult(
        system_info=info,
        results=results,
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
    )


# ─── trimmed_mean ────────────────────────────────────────────────────────────


def test_trimmed_mean_below_10_uses_simple_mean():
    """При n<10 trim не применяется."""
    values = [1.0, 100.0, 2.0]  # outlier 100
    result = multi_run.trimmed_mean(values, trim_frac=0.1)
    # Простое среднее = 34.33 (с outlier'ом)
    assert result == pytest.approx((1.0 + 100.0 + 2.0) / 3.0)


def test_trimmed_mean_n10_trims_extremes():
    """При n=10 trim_frac=0.1 → отбрасываем 1 наибольшее и 1 наименьшее."""
    values = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 100.0]
    result = multi_run.trimmed_mean(values, trim_frac=0.1)
    # После trim: [1, 1, 1, 1, 1, 1, 1, 1] → 1.0
    assert result == pytest.approx(1.0)


def test_trimmed_mean_empty_raises():
    with pytest.raises(ValueError):
        multi_run.trimmed_mean([])


# ─── median_per_workload / mean_per_workload ─────────────────────────────────


def test_median_per_workload_3_runs():
    suites = [_make_suite(1.0), _make_suite(2.0), _make_suite(0.5)]
    medians = multi_run.median_per_workload(suites)
    # Для memory_read: ref=100000, factors=[1.0, 2.0, 0.5] → values=[100k, 200k, 50k]
    # median = 100k
    assert medians["memory_read"] == pytest.approx(100000.0)


def test_median_per_workload_skips_errors():
    suites = [_make_suite(1.0), _make_suite(2.0)]
    suites[0].results[0].error = "broken"
    medians = multi_run.median_per_workload(suites)
    # Для memory_read остался только 1 валидный run с factor=2.0 → 200000
    assert medians["memory_read"] == pytest.approx(200000.0)


def test_mean_per_workload_uses_trimmed_for_n10(monkeypatch):
    """При use_trimmed=True результат отличается от обычного mean."""
    suites = [_make_suite(1.0) for _ in range(10)]
    # Один outlier: первый прогон имеет 10× для memory_read.
    suites[0].results[0].value = 1000000.0  # очень большое
    # mean без trim
    no_trim = multi_run.mean_per_workload(suites, use_trimmed=False)
    # mean с trim
    with_trim = multi_run.mean_per_workload(suites, use_trimmed=True)
    # mean без trim больше (outlier тянет среднее вверх)
    assert no_trim["memory_read"] > with_trim["memory_read"]


# ─── compute_ci_logscale ────────────────────────────────────────────────────


def test_ci_constant_inputs_returns_point():
    """Для постоянной выборки CI вырождается в точку (sd=0)."""
    low, high, method = multi_run.compute_ci_logscale([1.0, 1.0, 1.0, 1.0, 1.0])
    assert low == pytest.approx(1.0)
    assert high == pytest.approx(1.0)
    assert method == "t_logscale"


def test_ci_with_variance():
    """Для разнообразной выборки CI имеет ненулевую ширину."""
    ratios = [0.95, 1.00, 1.05, 1.02, 0.98]
    low, high, method = multi_run.compute_ci_logscale(ratios)
    assert low is not None and high is not None
    assert low < high
    assert method == "t_logscale"


def test_ci_n_too_small():
    """n<2 → None."""
    low, high, method = multi_run.compute_ci_logscale([1.0])
    assert low is None and high is None and method is None


def test_ci_zero_value_returns_none():
    """Если есть 0 — лог невычислим, возвращается None."""
    low, high, _ = multi_run.compute_ci_logscale([0.0, 1.0])
    assert low is None and high is None


# ─── aggregate_multi_run ─────────────────────────────────────────────────────


def test_aggregate_fast_single_run():
    """Fast preset: 1 прогон, overall_ratio = 1.0 для identity, ci=None."""
    suites = [_make_suite(1.0)]
    ref = _full_reference()
    w = weights.load_weights("default")
    aggregated = multi_run.aggregate_multi_run(suites, ref, w, preset="fast")
    assert aggregated.overall is not None
    assert aggregated.overall.overall_ratio == pytest.approx(1.0)
    assert aggregated.overall.ci_lower is None
    assert aggregated.overall.ci_method == "no_ci_n1"
    assert aggregated.preset == "fast"
    assert aggregated.n_runs == 1


def test_aggregate_standard_median_of_3():
    """Standard preset: median-of-3 → выбирает средний прогон."""
    # Три прогона: factor=0.5, 1.0, 2.0 → median=1.0 → ratio=1.0
    suites = [_make_suite(0.5), _make_suite(1.0), _make_suite(2.0)]
    ref = _full_reference()
    w = weights.load_weights("default")
    aggregated = multi_run.aggregate_multi_run(suites, ref, w, preset="standard")
    assert aggregated.overall is not None
    assert aggregated.overall.overall_ratio == pytest.approx(1.0)
    assert aggregated.overall.ci_method == "median_of_n"
    assert aggregated.n_runs == 3


def test_aggregate_accurate_with_ci():
    """Accurate preset: mean + CI на лог-шкале."""
    # 5 прогонов с factor близкими к 1.0
    factors = [0.95, 0.98, 1.00, 1.02, 1.05]
    suites = [_make_suite(f) for f in factors]
    ref = _full_reference()
    w = weights.load_weights("default")
    aggregated = multi_run.aggregate_multi_run(suites, ref, w, preset="accurate")
    assert aggregated.overall is not None
    # mean factors ~ 1.0 → overall_ratio ~ 1.0
    assert aggregated.overall.overall_ratio == pytest.approx(1.0, rel=0.05)
    assert aggregated.overall.ci_lower is not None
    assert aggregated.overall.ci_upper is not None
    # CI теперь в ratio-шкале (overall_score удалён в 0.9.x).
    assert aggregated.overall.ci_lower < aggregated.overall.overall_ratio
    assert aggregated.overall.ci_upper > aggregated.overall.overall_ratio
    assert aggregated.overall.ci_method == "t_logscale"
    # CI должен быть в разумных пределах (не больше ±15% от ratio)
    width = aggregated.overall.ci_upper - aggregated.overall.ci_lower
    assert width < aggregated.overall.overall_ratio * 0.30


def test_aggregate_accurate_constant_runs_zero_width_ci():
    """Если все прогоны идентичны — CI имеет нулевую ширину."""
    suites = [_make_suite(1.0) for _ in range(5)]
    ref = _full_reference()
    w = weights.load_weights("default")
    aggregated = multi_run.aggregate_multi_run(suites, ref, w, preset="accurate")
    assert aggregated.overall is not None
    assert aggregated.overall.ci_lower == pytest.approx(1.0)
    assert aggregated.overall.ci_upper == pytest.approx(1.0)


def test_aggregate_unknown_preset_raises():
    suites = [_make_suite(1.0)]
    ref = _full_reference()
    w = weights.load_weights("default")
    with pytest.raises(ValueError):
        multi_run.aggregate_multi_run(suites, ref, w, preset="invalid")  # type: ignore[arg-type]


def test_aggregate_empty_raises():
    ref = _full_reference()
    w = weights.load_weights("default")
    with pytest.raises(ValueError):
        multi_run.aggregate_multi_run([], ref, w, preset="fast")


def test_preset_runs_constants():
    """PRESET_RUNS соответствует docs/scoring_v2.md §6.1."""
    assert multi_run.PRESET_RUNS["fast"] == 1
    assert multi_run.PRESET_RUNS["standard"] == 3
    assert multi_run.PRESET_RUNS["accurate"] == 5


def test_ci_log_scale_symmetric_in_log():
    """CI на лог-шкале симметричен в логарифмах: log(high) - log(mean) == log(mean) - log(low)."""
    ratios = [0.9, 0.95, 1.0, 1.05, 1.1]
    low, high, _ = multi_run.compute_ci_logscale(ratios)
    assert low is not None and high is not None
    # Симметрия в логарифмах:
    # mean_log должна быть ровно посередине между log(low) и log(high)
    log_mean_data = sum(math.log(r) for r in ratios) / len(ratios)
    log_mid_ci = (math.log(low) + math.log(high)) / 2
    assert log_mean_data == pytest.approx(log_mid_ci)
