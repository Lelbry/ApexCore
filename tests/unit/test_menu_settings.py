"""Юнит-тесты для модуля настроек меню (``cli.menu.settings_store``).

Проверяем три сценария:
- дефолты, когда YAML-файла ещё нет;
- круговой round-trip: записали значение → прочли → совпало;
- защита от мусорного YAML (битые значения должны откатываться к дефолту,
  а не падать).

Файл настроек живёт в ``data_dir`` apexcore, который мы переопределяем
через монки на ``load_settings``, чтобы не трогать реальную домашнюю
директорию пользователя.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apexcore.interfaces.cli.menu import settings_store


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Перенаправить ``data_dir`` apexcore во временную папку pytest."""

    class _FakeSettings:
        data_dir = tmp_path

    monkeypatch.setattr(
        "apexcore.interfaces.cli.menu.settings_store.load_settings",
        lambda: _FakeSettings(),
    )
    return tmp_path


def test_defaults_when_file_missing(isolated_settings: Path) -> None:
    s = settings_store.load_menu_settings()
    assert s.durations.micro == 5.0
    assert s.durations.monitor == 10.0
    assert s.durations.stress_engine == 15.0
    assert s.durations.bench == 30.0
    assert s.threads == 0


def test_update_duration_round_trip(isolated_settings: Path) -> None:
    settings_store.update_duration("micro", 7.5)
    s = settings_store.load_menu_settings()
    assert s.durations.micro == 7.5
    # Остальные значения остались дефолтными.
    assert s.durations.monitor == 10.0


def test_update_duration_below_minimum_rejected(isolated_settings: Path) -> None:
    with pytest.raises(ValueError):
        settings_store.update_duration("micro", 0.01)


def test_update_duration_above_maximum_rejected(isolated_settings: Path) -> None:
    with pytest.raises(ValueError):
        settings_store.update_duration("micro", 1e9)


def test_update_unknown_program(isolated_settings: Path) -> None:
    with pytest.raises(ValueError):
        settings_store.update_duration("nonexistent", 5.0)


def test_garbage_yaml_falls_back_to_defaults(isolated_settings: Path) -> None:
    path = settings_store.settings_path()
    path.write_text("not: [valid: yaml: at: all:\n", encoding="utf-8")
    s = settings_store.load_menu_settings()
    # Битый YAML — возвращаем дефолты, не падаем.
    assert s.durations.micro == 5.0


def test_partially_invalid_values_use_defaults(isolated_settings: Path) -> None:
    path = settings_store.settings_path()
    path.write_text(
        "durations:\n"
        "  micro: -1.0\n"      # ниже минимума → откат к default
        "  monitor: not_a_number\n"  # мусор → default
        "  stress_engine: 25.0\n"    # валидно
        "  bench: 9999999999\n"      # выше максимума → default
        "threads: 4\n",
        encoding="utf-8",
    )
    s = settings_store.load_menu_settings()
    assert s.durations.micro == 5.0  # default
    assert s.durations.monitor == 10.0  # default
    assert s.durations.stress_engine == 25.0  # сохранено
    assert s.durations.bench == 30.0  # default
    assert s.threads == 4


def test_reset_to_defaults(isolated_settings: Path) -> None:
    settings_store.update_duration("micro", 12.0)
    settings_store.reset_to_defaults()
    s = settings_store.load_menu_settings()
    assert s.durations.micro == 5.0


def test_program_descriptors_match_dataclass_fields() -> None:
    """Каждый PROGRAMS.field должен существовать в DurationSettings."""
    defaults = settings_store.DurationSettings()
    for p in settings_store.PROGRAMS:
        assert hasattr(defaults, p.field), f"Поле {p.field} нет в DurationSettings"


def test_full_run_duration_micro() -> None:
    """Микробенчмарк CPU: 5.0 с × 12 тестов = 60 с."""
    result = settings_store.full_run_duration("micro", 5.0)
    assert result is not None
    total, count, unit = result
    assert total == 60.0
    assert count == 12
    assert unit == "тестов"


def test_full_run_duration_single_multi() -> None:
    """Single/Multi: 5.0 с × 2 замера = 10 с."""
    result = settings_store.full_run_duration("single_multi", 5.0)
    assert result == (10.0, 2, "замера")


def test_full_run_duration_ram_cache() -> None:
    """Ram&Cache: 8.0 с × 16 измерений = 128 с."""
    result = settings_store.full_run_duration("ram_cache", 8.0)
    assert result == (128.0, 16, "измерений")


def test_full_run_duration_no_multiplier_returns_none() -> None:
    """Программы без full_run_count > 1 не имеют 'полного прогона'."""
    assert settings_store.full_run_duration("stress_engine", 15.0) is None
    assert settings_store.full_run_duration("bench", 30.0) is None


def test_full_run_duration_unknown_field_returns_none() -> None:
    assert settings_store.full_run_duration("nonexistent", 1.0) is None
