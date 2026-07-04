"""Тесты `application/gpu_benchmark_score`.

Главные инварианты (``docs/gpu_benchmark.md`` §2):
1. ``compute_gpu_benchmark_score`` — pure (одинаковые входы → одинаковый выход).
2. ``GM(r_fp32, r_mem) × 10 000`` — ровно два множителя (FP64/PCIe вне балла).
3. Любой None или ≤0 из двух ratio → score = None.
4. Clamp к 1.0 — boost/разгон не задирают GM выше шкалы.
5. Симметричный случай (0.7, 0.7) → 7000; асимметричный — по GM.
"""

from __future__ import annotations

import math

from apexcore.application.gpu_benchmark_score import (
    GPU_BENCHMARK_SCALE,
    _clamp_ratio,
    compute_gpu_benchmark_score,
)


def test_scale_is_10000():
    assert GPU_BENCHMARK_SCALE == 10_000.0


def test_score_symmetric_case():
    """r_fp32 = r_mem = 0.70 → R = 0.70 → 7000 из 10 000 (спека §11)."""
    score = compute_gpu_benchmark_score(0.70, 0.70)
    assert score is not None
    assert math.isclose(score, 7000.0, rel_tol=1e-9)


def test_score_asymmetric_case():
    """r_fp32 = 0.55, r_mem = 0.77 → R = √(0.55·0.77) ≈ 0.651 → ≈ 6510 (§11)."""
    score = compute_gpu_benchmark_score(0.55, 0.77)
    assert score is not None
    expected = GPU_BENCHMARK_SCALE * math.sqrt(0.55 * 0.77)
    assert math.isclose(score, expected, rel_tol=1e-9)
    # Санити: ~6500 попугаев из документа.
    assert 6400 < score < 6600


def test_score_full_inputs_matches_geomean():
    score = compute_gpu_benchmark_score(0.30, 0.60)
    assert score is not None
    expected = GPU_BENCHMARK_SCALE * math.exp(
        (math.log(0.30) + math.log(0.60)) / 2.0
    )
    assert math.isclose(score, expected, rel_tol=1e-9)


def test_score_none_if_any_missing():
    assert compute_gpu_benchmark_score(None, 0.7) is None
    assert compute_gpu_benchmark_score(0.7, None) is None
    assert compute_gpu_benchmark_score(None, None) is None


def test_score_none_if_any_zero_or_negative():
    assert compute_gpu_benchmark_score(0.0, 0.7) is None
    assert compute_gpu_benchmark_score(0.7, 0.0) is None
    assert compute_gpu_benchmark_score(-0.1, 0.7) is None
    assert compute_gpu_benchmark_score(0.7, -0.5) is None


def test_score_deterministic():
    a = compute_gpu_benchmark_score(0.61, 0.42)
    b = compute_gpu_benchmark_score(0.61, 0.42)
    assert a == b


def test_score_clamps_above_unity():
    """r_fp32 = 1.5 (измеренная FP32 выше табличного пика из-за boost) → clamp
    к 1.0, GM не задирается выше шкалы (§3.4)."""
    score = compute_gpu_benchmark_score(1.5, 0.5)
    # После clamp: GM(1.0, 0.5) = √0.5 ≈ 0.707.
    expected = GPU_BENCHMARK_SCALE * math.sqrt(1.0 * 0.5)
    assert score is not None
    assert math.isclose(score, expected, rel_tol=1e-9)


def test_score_both_clamped():
    """Оба ratio > 1 → GM(1.0, 1.0) = 1 → ровно 10 000 (потолок)."""
    score = compute_gpu_benchmark_score(1.3, 2.0)
    assert score is not None
    assert math.isclose(score, GPU_BENCHMARK_SCALE)


def test_score_at_unity():
    """Оба ratio = 1.0 (архитектурный потолок) → GM = 1 → 10 000."""
    score = compute_gpu_benchmark_score(1.0, 1.0)
    assert score is not None
    assert math.isclose(score, GPU_BENCHMARK_SCALE)


def test_clamp_ratio_passthrough():
    assert _clamp_ratio(0.5) == 0.5
    assert _clamp_ratio(0.999) == 0.999


def test_clamp_ratio_clamps_above_one():
    assert _clamp_ratio(1.5) == 1.0
    assert _clamp_ratio(2.0) == 1.0


def test_clamp_ratio_none_and_zero():
    assert _clamp_ratio(None) is None
    assert _clamp_ratio(0.0) is None
    assert _clamp_ratio(-0.5) is None
