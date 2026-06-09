"""Тесты scoring-логики Winsat-аналога."""

from __future__ import annotations

from math import isclose

import pytest

from apexcore.application.winsat_scoring import (
    SCORE_MAX,
    SCORE_MIN,
    _Point,
    _thresholds,
    compute_cpu_score,
    compute_disk_score,
    compute_memory_score,
    compute_winspr_level,
    error_subscore,
    harmonic_mean_pair,
    na_subscore,
    score_from_metric,
)
from apexcore.domain.winsat import WinsatStatus


# ─── score_from_metric — clamp и интерполяция ──────────────────────────────


def test_score_clamped_below_minimum() -> None:
    pts = (_Point(100, 1.0), _Point(200, 2.0), _Point(800, 5.0))
    assert score_from_metric(50, pts) == SCORE_MIN
    assert score_from_metric(0, pts) == SCORE_MIN


def test_score_clamped_above_maximum() -> None:
    pts = (_Point(100, 1.0), _Point(200, 2.0), _Point(800, 5.0))
    assert score_from_metric(10000, pts) == SCORE_MAX


def test_score_at_threshold_point_is_exact() -> None:
    pts = (_Point(100, 1.0), _Point(800, 5.0), _Point(6400, 9.0))
    # Точка ровно на пороге — попадаем в нижний интервал, log_lo = log_hi не для нашей точки.
    # Граница 800 → 5.0.
    assert isclose(score_from_metric(800, pts), 5.0, abs_tol=1e-9)


def test_score_log_interpolation_midpoint() -> None:
    # Между 100 (1.0) и 400 (3.0) геометрический mid = 200 → score = 2.0.
    pts = (_Point(100, 1.0), _Point(400, 3.0))
    result = score_from_metric(200, pts)
    assert isclose(result, 2.0, abs_tol=1e-6)


def test_empty_points_raises() -> None:
    with pytest.raises(ValueError):
        score_from_metric(100, ())


# ─── Гармоническое среднее ─────────────────────────────────────────────────


def test_harmonic_mean_balanced() -> None:
    assert isclose(harmonic_mean_pair(100, 100), 100.0, abs_tol=1e-9)


def test_harmonic_mean_imbalanced() -> None:
    # HM(10, 1000) ≈ 19.8 — сильно ближе к меньшему.
    hm = harmonic_mean_pair(10, 1000)
    assert isclose(hm, 19.8019, abs_tol=1e-3)


def test_harmonic_mean_zero_value_returns_zero() -> None:
    assert harmonic_mean_pair(0, 100) == 0.0
    assert harmonic_mean_pair(100, 0) == 0.0


# ─── Калибровка YAML — контрольные точки ───────────────────────────────────


def test_yaml_thresholds_loaded_with_known_categories() -> None:
    th = _thresholds()
    assert "cpu" in th
    assert "memory" in th
    assert "disk_sequential_read" in th
    assert "disk_random_read" in th


def test_yaml_thresholds_monotonic_by_value_and_score() -> None:
    th = _thresholds()
    for name, cat in th.items():
        values = [p.value for p in cat.points]
        scores = [p.score for p in cat.points]
        assert values == sorted(values), f"{name} value не монотонен"
        assert scores == sorted(scores), f"{name} score не монотонен"


def test_calibration_cpu_76800_maps_to_9_5() -> None:
    # Контрольная точка из YAML: 76800 MB/s → 9.5 (типичный Ryzen 5/7).
    cpu_pts = _thresholds()["cpu"].points
    assert isclose(score_from_metric(76800, cpu_pts), 9.5, abs_tol=0.05)


def test_calibration_disk_seq_3500_maps_to_8_7() -> None:
    # Контрольная точка из YAML: 3500 MB/s → 8.7 (Gen3 NVMe).
    pts = _thresholds()["disk_sequential_read"].points
    assert isclose(score_from_metric(3500, pts), 8.7, abs_tol=0.05)


def test_calibration_disk_random_1800_maps_to_8_7() -> None:
    pts = _thresholds()["disk_random_read"].points
    assert isclose(score_from_metric(1800, pts), 8.7, abs_tol=0.05)


# ─── compute_*_score ───────────────────────────────────────────────────────


def test_compute_cpu_score_balanced() -> None:
    sub = compute_cpu_score(aes_mbps=80000, sha1_mbps=80000)
    assert sub.status == WinsatStatus.PASS
    assert sub.category == "cpu"
    assert sub.score >= 9.0


def test_compute_memory_score_high_bandwidth() -> None:
    sub = compute_memory_score(memory_read_mbps=55000)
    assert sub.status == WinsatStatus.PASS
    assert isclose(sub.score, 9.5, abs_tol=0.05)


def test_compute_disk_score_takes_min_of_two() -> None:
    # seq=3500 → 8.7; random=50 → 4.0; min = 4.0.
    sub = compute_disk_score(seq_read_mbps=3500, random_read_mbps=50)
    assert sub.status == WinsatStatus.PASS
    assert isclose(sub.score, 4.0, abs_tol=0.05)


# ─── compute_winspr_level ───────────────────────────────────────────────────


def test_winspr_level_takes_min_of_pass_subscores() -> None:
    pass_subs = [
        compute_cpu_score(80000, 80000),  # ~9.5
        compute_memory_score(55000),  # ~9.5
        compute_disk_score(3500, 1800),  # ~8.7
    ]
    na = na_subscore("graphics")
    err = error_subscore("d3d", note="нет данных")
    level = compute_winspr_level([*pass_subs, na, err])
    # Минимум среди PASS = ~8.7. NA/ERROR игнорируются.
    assert isclose(level, 8.7, abs_tol=0.1)


def test_winspr_level_no_pass_returns_minimum() -> None:
    na1 = na_subscore("cpu")
    na2 = na_subscore("memory")
    level = compute_winspr_level([na1, na2])
    assert level == SCORE_MIN


def test_na_subscore_status() -> None:
    sub = na_subscore("graphics")
    assert sub.status == WinsatStatus.NA
    assert sub.score == SCORE_MIN


def test_error_subscore_status() -> None:
    sub = error_subscore("disk", note="отменено")
    assert sub.status == WinsatStatus.ERROR
    assert sub.note == "отменено"
