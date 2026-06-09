"""Тесты миграции data-dir benchkit → apexcore (Phase C rebrand).

Контракт миграции (см. shared/config.py):
- если новая директория apexcore пустая И старая benchkit существует с данными
  → копируем содержимое; оставляем marker `.migrated_to_apexcore` в старой
- если marker уже есть → skip
- если новая не пуста → skip (не перетираем user data)
- если старая не существует → skip
- ошибки миграции не падают, печатают warning
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_migration_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбросить module-level флаги между тестами."""
    from apexcore.shared import config
    monkeypatch.setattr(config, "_data_dir_migrated", False, raising=False)
    monkeypatch.setattr(config, "_env_warned", False, raising=False)


def _setup_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Подменить platformdirs.user_data_dir на tmp-папки для apexcore и benchkit."""
    new_dir = tmp_path / "apexcore"
    legacy_dir = tmp_path / "benchkit"

    def fake_user_data_dir(name: str, appauthor: bool = False, **_: object) -> str:
        if name == "apexcore":
            return str(new_dir)
        if name == "benchkit":
            return str(legacy_dir)
        return str(tmp_path / name)

    from apexcore.shared import config
    monkeypatch.setattr(config, "user_data_dir", fake_user_data_dir)
    return new_dir, legacy_dir


def test_no_legacy_dir_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Старой папки нет → ничего не происходит, новая создаётся пустой."""
    new_dir, legacy_dir = _setup_dirs(tmp_path, monkeypatch)
    assert not legacy_dir.exists()

    from apexcore.shared.config import default_data_dir
    result = default_data_dir()

    assert result == new_dir
    assert new_dir.exists()
    assert list(new_dir.iterdir()) == []


def test_legacy_dir_with_data_migrates_to_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Старая папка с данными → переносим в новую + marker в старой."""
    new_dir, legacy_dir = _setup_dirs(tmp_path, monkeypatch)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "benchkit.sqlite3").write_text("fake db", encoding="utf-8")
    (legacy_dir / "menu_settings.yaml").write_text("key: value\n", encoding="utf-8")
    (legacy_dir / "runs").mkdir()
    (legacy_dir / "runs" / "x.json").write_text("{}", encoding="utf-8")

    from apexcore.shared.config import (
        LEGACY_MIGRATION_MARKER,
        default_data_dir,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result = default_data_dir()

    assert result == new_dir
    assert (new_dir / "benchkit.sqlite3").read_text(encoding="utf-8") == "fake db"
    assert (new_dir / "menu_settings.yaml").exists()
    assert (new_dir / "runs" / "x.json").read_text(encoding="utf-8") == "{}"
    assert (legacy_dir / LEGACY_MIGRATION_MARKER).exists()


def test_marker_present_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если marker уже есть в старой → не перетираем."""
    new_dir, legacy_dir = _setup_dirs(tmp_path, monkeypatch)
    legacy_dir.mkdir(parents=True)
    from apexcore.shared.config import LEGACY_MIGRATION_MARKER
    (legacy_dir / LEGACY_MIGRATION_MARKER).write_text("done", encoding="utf-8")
    (legacy_dir / "benchkit.sqlite3").write_text("old", encoding="utf-8")

    from apexcore.shared.config import default_data_dir
    default_data_dir()

    assert not (new_dir / "benchkit.sqlite3").exists()


def test_populated_new_dir_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если новая папка не пуста → ничего не делаем (защита от перетирания)."""
    new_dir, legacy_dir = _setup_dirs(tmp_path, monkeypatch)
    new_dir.mkdir(parents=True)
    (new_dir / "apexcore.sqlite3").write_text("new", encoding="utf-8")
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "benchkit.sqlite3").write_text("old", encoding="utf-8")

    from apexcore.shared.config import default_data_dir
    default_data_dir()

    assert (new_dir / "apexcore.sqlite3").read_text(encoding="utf-8") == "new"
    assert not (new_dir / "benchkit.sqlite3").exists()
