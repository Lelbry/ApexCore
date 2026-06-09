"""Тесты статистического движка."""

from __future__ import annotations

import numpy as np

from apexcore.application.statistics import compare_metric_series


def test_compare_identical_samples_not_significant():
    rng = np.random.default_rng(0)
    a = rng.normal(50, 5, size=100).tolist()
    b = rng.normal(50, 5, size=100).tolist()
    cmp = compare_metric_series(a, b)
    assert cmp.is_significant is False
    assert cmp.direction == "same"


def test_compare_clearly_different_significant():
    rng = np.random.default_rng(1)
    baseline = rng.normal(50, 2, size=200).tolist()
    current = rng.normal(70, 2, size=200).tolist()
    cmp = compare_metric_series(baseline, current, higher_is_better=False)
    assert cmp.is_significant is True
    assert cmp.direction == "worse"
    assert cmp.p_value < 0.05
    assert abs(cmp.effect_size) > 0.5


def test_compare_higher_is_better_improvement():
    rng = np.random.default_rng(2)
    baseline = rng.normal(1000, 30, size=100).tolist()
    current = rng.normal(1200, 30, size=100).tolist()
    cmp = compare_metric_series(baseline, current, higher_is_better=True)
    assert cmp.is_significant is True
    assert cmp.direction == "better"


def test_compare_insufficient_samples():
    cmp = compare_metric_series([1.0], [2.0])
    assert cmp.test_name == "insufficient"
    assert cmp.is_significant is False


def test_compare_non_normal_falls_back_to_mannwhitney():
    rng = np.random.default_rng(3)
    # Сильно скошённые выборки.
    a = (rng.exponential(1.0, size=80)).tolist()
    b = (rng.exponential(2.5, size=80)).tolist()
    cmp = compare_metric_series(a, b, higher_is_better=False)
    assert cmp.test_name in {"Mann-Whitney U", "Welch t-test"}
