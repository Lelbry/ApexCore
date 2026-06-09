"""Юнит-тесты ядра scoring v2 (`apexcore.application.scoring`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apexcore.application import scoring, weights
from apexcore.application.references import ReferenceSet, ReferenceValue
from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    MicroBenchSuiteResult,
    SystemInfo,
)

# ─── Фабрики тестовых данных ────────────────────────────────────────────────


def _make_sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Linux",
        os_version="6.0",
        cpu_model="Intel Core i7-12700K",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        cpu_arch="AMD64",
        timestamp=datetime.now(timezone.utc),
    )


def _ref_value(workload_id: str, value: float, unit: str, provisional: bool = False) -> ReferenceValue:
    return ReferenceValue(
        workload_id=workload_id,
        value=value,
        unit=unit,
        source="roofline",
        provisional=provisional,
    )


def _full_reference() -> ReferenceSet:
    """Полный reference с 12 workload-эталонами на «средние» значения."""
    return ReferenceSet(
        id="test-ref-v1",
        version="1.0.0",
        machine_description="Test reference",
        values={
            "memory_read":   _ref_value("memory_read",   100000.0, "MB/s"),
            "memory_write":  _ref_value("memory_write",  100000.0, "MB/s"),
            "memory_copy":   _ref_value("memory_copy",   200000.0, "MB/s"),
            "flops_sp":      _ref_value("flops_sp",      1600.0,   "GFLOPS"),
            "flops_dp":      _ref_value("flops_dp",      800.0,    "GFLOPS"),
            "int_iops_24":   _ref_value("int_iops_24",   200.0,    "GIOPS"),
            "int_iops_32":   _ref_value("int_iops_32",   200.0,    "GIOPS"),
            "int_iops_64":   _ref_value("int_iops_64",   200.0,    "GIOPS"),
            "aes_256":       _ref_value("aes_256",       4000.0,   "MB/s"),
            "sha1":          _ref_value("sha1",          1000.0,   "MB/s"),
            "julia_sp":      _ref_value("julia_sp",      80.0,     "FPS",     provisional=True),
            "mandelbrot_dp": _ref_value("mandelbrot_dp", 40.0,     "FPS",     provisional=True),
        },
    )


def _suite_at_factor(factor: float) -> MicroBenchSuiteResult:
    """Создаёт MicroBenchSuiteResult, где все measured = factor × reference."""
    info = _make_sys_info()
    ref = _full_reference()
    results: list[MicroBenchResult] = []
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
    for wid, (cat, unit) in workload_categories.items():
        ref_val = ref.values[wid].value
        results.append(
            MicroBenchResult(
                name=wid,
                category=cat,
                value=ref_val * factor,
                unit=unit,
                duration_actual_sec=5.0,
            )
        )
    return MicroBenchSuiteResult(
        system_info=info,
        results=results,
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
    )


def _default_weights() -> weights.WeightsProfile:
    return weights.load_weights("default")


# ─── Базовые средние ────────────────────────────────────────────────────────


def test_harmonic_mean_basic():
    assert scoring.harmonic_mean([1.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_harmonic_mean_weakest_link():
    """HM сильно проседает при одном маленьком значении (weakest-link sensitive)."""
    hm = scoring.harmonic_mean([1.0, 1.0, 0.1])
    # GM было бы около 0.464; AM около 0.7. HM значительно меньше:
    assert hm < 0.3
    assert hm == pytest.approx(3.0 / (1.0 + 1.0 + 10.0))  # = 3/12 = 0.25


def test_harmonic_mean_empty_raises():
    with pytest.raises(ValueError):
        scoring.harmonic_mean([])


def test_harmonic_mean_zero_raises():
    with pytest.raises(ValueError):
        scoring.harmonic_mean([1.0, 0.0])


def test_geometric_mean_basic():
    assert scoring.geometric_mean([2.0, 8.0]) == pytest.approx(4.0)


def test_geometric_mean_two_quarters_average():
    """0.5 и 2.0 имеют GM=1.0 (свойство симметрии: компенсация на лог-шкале)."""
    assert scoring.geometric_mean([0.5, 2.0]) == pytest.approx(1.0)


def test_geometric_mean_empty_raises():
    with pytest.raises(ValueError):
        scoring.geometric_mean([])


def test_weighted_gm_uniform_weights_equals_gm():
    """Равные веса → результат равен невзвешенному GM."""
    values = {"a": 4.0, "b": 16.0}
    assert scoring.weighted_geometric_mean(values, {"a": 1, "b": 1}) == pytest.approx(8.0)


def test_weighted_gm_skews_to_higher_weight():
    """Большой вес для большего значения → результат ближе к нему."""
    values = {"a": 1.0, "b": 100.0}
    result = scoring.weighted_geometric_mean(values, {"a": 1, "b": 99})
    assert result > 50.0  # сильно сдвинуто к 100


def test_weighted_gm_zero_weights_falls_back_to_gm():
    """Все веса нули → fallback к равновзвешенному GM."""
    values = {"a": 4.0, "b": 16.0}
    assert scoring.weighted_geometric_mean(values, {"a": 0, "b": 0}) == pytest.approx(8.0)


# ─── compute_workload_ratios ─────────────────────────────────────────────────


def test_compute_ratios_identity_machine():
    """measured == reference → все ratios = 1.0."""
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    ratios, notes = scoring.compute_workload_ratios(suite, ref)
    assert len(ratios) == 12
    for r in ratios.values():
        assert r == pytest.approx(1.0)
    assert notes == []


def test_compute_ratios_skip_workloads_with_error():
    suite = _suite_at_factor(1.0)
    suite.results[0].error = "недоступен в этой среде"
    ref = _full_reference()
    ratios, notes = scoring.compute_workload_ratios(suite, ref)
    assert len(ratios) == 11
    assert any("workload_error" in n for n in notes)


def test_compute_ratios_skip_zero_value():
    suite = _suite_at_factor(1.0)
    suite.results[0].value = 0.0
    ref = _full_reference()
    ratios, notes = scoring.compute_workload_ratios(suite, ref)
    assert len(ratios) == 11
    assert any("workload_zero_value" in n for n in notes)


def test_compute_ratios_no_reference_for_workload():
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    del ref.values["sha1"]  # удаляем reference для одного теста
    ratios, notes = scoring.compute_workload_ratios(suite, ref)
    assert "sha1" not in ratios
    assert any("workload_no_reference:sha1" in n for n in notes)


# ─── geomean_score: ключевые свойства ────────────────────────────────────────


def test_geomean_score_identity_returns_ratio_1():
    """Идентичная reference machine → R_overall=1.0 (100% пика)."""
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    assert score.overall_ratio == pytest.approx(1.0)
    # overall_score (единый балл 1000·R) удалён в 0.9.x — не заполняется.
    assert score.overall_score is None
    # Все категории = 1.0
    for cat_key in ("r_memory", "r_flops", "r_integer", "r_crypto", "r_fractal"):
        assert score.subscores[cat_key] == pytest.approx(1.0)
    # Подсистемы тоже 1.0
    assert score.subscores["R_MEM"] == pytest.approx(1.0)
    assert score.subscores["R_CPU_compute"] == pytest.approx(1.0)


def test_geomean_score_double_returns_ratio_2():
    """measured = 2× reference → overall_ratio = 2.0."""
    suite = _suite_at_factor(2.0)
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    assert score.overall_ratio == pytest.approx(2.0)


def test_geomean_score_half_returns_ratio_half():
    """measured = 0.5× reference → overall_ratio = 0.5."""
    suite = _suite_at_factor(0.5)
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    assert score.overall_ratio == pytest.approx(0.5)


def test_geomean_score_provisional_flag():
    """Если использованы provisional reference (julia/mandelbrot) — флаг True."""
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    # У нас julia_sp и mandelbrot_dp provisional=True в _full_reference.
    assert score.provisional is True
    assert "provisional_reference_used" in score.notes


def test_geomean_score_metadata_filled():
    """OverallScore содержит правильные метаданные."""
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w, n_runs=3)
    assert score.scoring_version == "2.0.0"
    assert score.reference_id == "test-ref-v1"
    assert score.weights_profile == "default"
    assert score.n_runs == 3


def test_geomean_score_partial_reference():
    """Если part of reference отсутствует — балл считается по доступным категориям
    с пометкой 'partial_reference'."""
    suite = _suite_at_factor(1.0)
    ref = _full_reference()
    # Удаляем все memory-эталоны.
    del ref.values["memory_read"]
    del ref.values["memory_write"]
    del ref.values["memory_copy"]
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    # R_MEM пропущен.
    assert "R_MEM" not in score.subscores
    assert "r_memory" not in score.subscores
    # R_CPU_compute посчитан.
    assert "R_CPU_compute" in score.subscores
    # Заметки — partial_reference и single_subsystem_only.
    assert "partial_reference" in score.notes
    assert "single_subsystem_only" in score.notes
    # Доля пика всё равно != 0.
    assert score.overall_ratio > 0


def test_geomean_score_empty_suite():
    """Пустой suite → overall_ratio=0, provisional=True, заметка no_valid_ratios."""
    info = _make_sys_info()
    suite = MicroBenchSuiteResult(
        system_info=info,
        results=[],
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
    )
    ref = _full_reference()
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    assert score.overall_ratio == 0.0
    assert score.provisional is True
    assert "no_valid_ratios" in score.notes


def test_geomean_score_hm_within_category():
    """HM внутри категории действительно weakest-link sensitive.

    Делаем 2 нормальных теста + 1 проседающий в категории memory; HM должен
    отразить это сильнее, чем GM.
    """
    info = _make_sys_info()
    ref = _full_reference()
    # 3 тестов memory: 2 идут на 1.0, 1 на 0.1.
    results = [
        MicroBenchResult(
            name="memory_read", category="memory",
            value=100000.0, unit="MB/s", duration_actual_sec=5.0,
        ),
        MicroBenchResult(
            name="memory_write", category="memory",
            value=100000.0, unit="MB/s", duration_actual_sec=5.0,
        ),
        MicroBenchResult(
            name="memory_copy", category="memory",
            value=200000.0 * 0.1, unit="MB/s", duration_actual_sec=5.0,
        ),
    ]
    suite = MicroBenchSuiteResult(
        system_info=info,
        results=results,
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
    )
    w = _default_weights()
    score = scoring.geomean_score(suite, ref, w)
    # HM([1.0, 1.0, 0.1]) ≈ 0.25 (значительно меньше AM=0.7 и GM=0.464)
    assert score.subscores["r_memory"] == pytest.approx(0.25)
