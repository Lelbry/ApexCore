"""Smoke-тесты YAML-калибровки Winsat-аналога.

Проверяем, что файл найден, парсится, и пороги монотонны.
"""

from __future__ import annotations

from importlib import resources

import yaml


def test_yaml_resource_exists_and_is_parseable() -> None:
    text = resources.files("apexcore.data").joinpath(
        "winsat_thresholds.yaml"
    ).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    assert raw.get("version") == 1


def test_required_categories_present() -> None:
    text = resources.files("apexcore.data").joinpath(
        "winsat_thresholds.yaml"
    ).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    for name in ("cpu", "memory", "disk_sequential_read", "disk_random_read"):
        assert name in raw, f"отсутствует категория {name}"
        cat = raw[name]
        assert "metric" in cat
        assert "unit" in cat
        assert "points" in cat
        assert isinstance(cat["points"], list)
        assert len(cat["points"]) >= 2


def test_all_points_have_value_and_score_keys() -> None:
    text = resources.files("apexcore.data").joinpath(
        "winsat_thresholds.yaml"
    ).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    for name, cat in raw.items():
        if name == "version":
            continue
        for p in cat["points"]:
            assert "value" in p, f"{name}: точка без value"
            assert "score" in p, f"{name}: точка без score"
            assert isinstance(p["value"], (int, float))
            assert 1.0 <= p["score"] <= 9.9


def test_points_monotonic_in_value_and_score() -> None:
    text = resources.files("apexcore.data").joinpath(
        "winsat_thresholds.yaml"
    ).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    for name, cat in raw.items():
        if name == "version":
            continue
        values = [float(p["value"]) for p in cat["points"]]
        scores = [float(p["score"]) for p in cat["points"]]
        assert values == sorted(values), f"{name}: value не монотонен"
        assert scores == sorted(scores), f"{name}: score не монотонен"
