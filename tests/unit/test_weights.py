"""Юнит-тесты для weights module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apexcore.application import weights


def test_load_default_profile():
    """Профиль default загружается из bundled data."""
    profile = weights.load_weights("default")
    assert profile.name == "default"
    assert profile.subsystem_weights == {"R_MEM": 1.0, "R_CPU_compute": 1.0}
    assert profile.compute_category_weights == {
        "r_flops": 1.0,
        "r_integer": 1.0,
        "r_crypto": 1.0,
        "r_fractal": 1.0,
    }


def test_load_unknown_profile_raises():
    with pytest.raises(FileNotFoundError):
        weights.load_weights("nonexistent_profile_xyz")


def test_load_from_search_path(tmp_path: Path):
    """Профиль из произвольной папки имеет приоритет над bundled."""
    raw = {
        "name": "custom",
        "version": "1.0",
        "description": "Custom profile for tests",
        "subsystem_weights": {"R_MEM": 2.0, "R_CPU_compute": 1.0},
        "compute_category_weights": {
            "r_flops": 3.0,
            "r_integer": 1.0,
            "r_crypto": 1.0,
            "r_fractal": 1.0,
        },
    }
    file = tmp_path / "custom.yaml"
    file.write_text(yaml.safe_dump(raw), encoding="utf-8")

    profile = weights.load_weights("custom", search_paths=[tmp_path])
    assert profile.subsystem_weights["R_MEM"] == 2.0
    assert profile.compute_category_weights["r_flops"] == 3.0


def test_invalid_yaml_raises_value_error(tmp_path: Path):
    file = tmp_path / "bad.yaml"
    # Профиль без обязательного subsystem_weights — Pydantic должен бросить.
    file.write_text("name: bad\nversion: 1.0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        weights.load_weights("bad", search_paths=[tmp_path])


def test_extra_field_rejected(tmp_path: Path):
    """Контракт фиксирован: extra fields отвергаются."""
    raw = {
        "name": "x",
        "subsystem_weights": {"R_MEM": 1.0, "R_CPU_compute": 1.0},
        "unknown_extra": "oops",
    }
    file = tmp_path / "x.yaml"
    file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        weights.load_weights("x", search_paths=[tmp_path])


# ─── normalize_weights ──────────────────────────────────────────────────────


def test_normalize_weights_simple():
    assert weights.normalize_weights({"a": 1.0, "b": 1.0}) == {"a": 0.5, "b": 0.5}


def test_normalize_weights_uneven():
    norm = weights.normalize_weights({"a": 3.0, "b": 1.0})
    assert norm["a"] == pytest.approx(0.75)
    assert norm["b"] == pytest.approx(0.25)
    assert sum(norm.values()) == pytest.approx(1.0)


def test_normalize_weights_zero_sum_falls_back_to_equal():
    norm = weights.normalize_weights({"a": 0.0, "b": 0.0})
    assert norm == {"a": 0.5, "b": 0.5}


def test_normalize_weights_empty_dict():
    """Пустой dict не должен падать."""
    assert weights.normalize_weights({}) == {}
