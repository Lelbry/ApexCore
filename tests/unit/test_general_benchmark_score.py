"""Тесты `application/general_benchmark_score`.

Главные инварианты:
1. ``compute_general_benchmark_score`` — pure (одинаковые входы → одинаковый выход).
2. Любой None или ≤0 ratio → score = None.
3. Clamp к 1.0 — топовое железо не задирает GM выше шкалы.
4. ``_disk_ratio_from_components`` — None если хоть один компонент отсутствует.
"""

from __future__ import annotations

import math

from apexcore.application.general_benchmark_score import (
    GENERAL_BENCHMARK_SCALE,
    _clamp_ratio,
    _disk_ratio_from_components,
    compute_general_benchmark_score,
)


def test_score_full_inputs():
    # GM(0.30, 0.50, 0.70) ≈ 0.473 → ~4730 при шкале ×10000.
    score = compute_general_benchmark_score(0.30, 0.50, 0.70)
    assert score is not None
    expected = GENERAL_BENCHMARK_SCALE * math.exp(
        (math.log(0.30) + math.log(0.50) + math.log(0.70)) / 3.0
    )
    assert math.isclose(score, expected, rel_tol=1e-9)


def test_score_none_if_any_missing():
    assert compute_general_benchmark_score(None, 0.4, 0.7) is None
    assert compute_general_benchmark_score(0.3, None, 0.7) is None
    assert compute_general_benchmark_score(0.3, 0.4, None) is None


def test_score_none_if_any_zero_or_negative():
    assert compute_general_benchmark_score(0.0, 0.4, 0.7) is None
    assert compute_general_benchmark_score(0.3, 0.0, 0.7) is None
    assert compute_general_benchmark_score(0.3, 0.4, 0.0) is None
    assert compute_general_benchmark_score(-0.1, 0.4, 0.7) is None


def test_score_deterministic():
    a = compute_general_benchmark_score(0.31, 0.42, 0.71)
    b = compute_general_benchmark_score(0.31, 0.42, 0.71)
    assert a == b


def test_score_clamps_above_unity():
    """Если r_dgemm = 1.5 (топовый CPU, наш peak занижен) — clamp к 1.0,
    GM не задирается."""
    score = compute_general_benchmark_score(1.5, 0.5, 0.5)
    # После clamp: GM(1.0, 0.5, 0.5) = 0.63
    expected = GENERAL_BENCHMARK_SCALE * math.exp(
        (math.log(1.0) + math.log(0.5) + math.log(0.5)) / 3.0
    )
    assert score is not None
    assert math.isclose(score, expected, rel_tol=1e-9)


def test_score_at_unity():
    """Если все ratio = 1.0 (теоретический потолок) → GM = 1, score = 10 000."""
    score = compute_general_benchmark_score(1.0, 1.0, 1.0)
    assert score is not None
    assert math.isclose(score, GENERAL_BENCHMARK_SCALE)


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


def test_disk_ratio_from_components_full():
    """GM(0.6, 0.4, 0.8) ≈ 0.586."""
    r = _disk_ratio_from_components(0.6, 0.4, 0.8)
    assert r is not None
    expected = math.exp((math.log(0.6) + math.log(0.4) + math.log(0.8)) / 3.0)
    assert math.isclose(r, expected, rel_tol=1e-9)


def test_disk_ratio_from_components_clamps_each():
    """seq_read = 1.5 (NVMe Gen5 vs peak 3500) → clamp к 1.0."""
    r = _disk_ratio_from_components(1.5, 0.5, 0.5)
    expected = math.exp((math.log(1.0) + math.log(0.5) + math.log(0.5)) / 3.0)
    assert r is not None
    assert math.isclose(r, expected, rel_tol=1e-9)


def test_disk_ratio_none_if_any_missing():
    assert _disk_ratio_from_components(None, 0.4, 0.5) is None
    assert _disk_ratio_from_components(0.6, None, 0.5) is None
    assert _disk_ratio_from_components(0.6, 0.4, None) is None


def test_disk_ratio_none_if_any_zero():
    assert _disk_ratio_from_components(0.0, 0.4, 0.5) is None
