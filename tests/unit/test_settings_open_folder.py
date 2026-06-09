"""Тесты `settings_store.open_settings_dir` + пункт «Открыть папку настроек».

Главные инварианты:
1. `settings_dir()` возвращает родителя `settings_path()` и существует на диске.
2. `open_settings_dir()` зовёт `os.startfile` на Windows и `xdg-open` на Linux,
   возвращает путь.
3. Любая ошибка ОС (`OSError` / `FileNotFoundError`) пробрасывается вверх —
   обрабатывает её UI-слой.
4. `SettingsScreen` содержит пункт «Открыть папку настроек» и обработчик
   корректно реагирует на ошибку (`stay` с red-сообщением) и на успех.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apexcore.interfaces.cli.menu import settings_store
from apexcore.interfaces.cli.menu.nav import NavAction
from apexcore.interfaces.cli.menu.screens import SettingsScreen

# ─── settings_store ─────────────────────────────────────────────────────────


def test_settings_dir_returns_parent_of_settings_path(tmp_path: Path, monkeypatch):
    """`settings_dir() == settings_path().parent` и существует на диске."""

    class _FakeSettings:
        data_dir = tmp_path / "apexcore"

    monkeypatch.setattr(settings_store, "load_settings", lambda: _FakeSettings())

    folder = settings_store.settings_dir()
    assert folder == tmp_path / "apexcore"
    assert folder.exists() and folder.is_dir()
    assert settings_store.settings_path().parent == folder


def test_open_settings_dir_uses_startfile_on_windows(tmp_path, monkeypatch):
    """На Windows вызывается `os.startfile(folder)`."""
    if sys.platform != "win32":
        pytest.skip("Windows-only test")

    class _FakeSettings:
        data_dir = tmp_path / "apexcore"

    monkeypatch.setattr(settings_store, "load_settings", lambda: _FakeSettings())

    import os

    startfile_mock = MagicMock()
    monkeypatch.setattr(os, "startfile", startfile_mock, raising=False)

    folder = settings_store.open_settings_dir()

    assert folder == tmp_path / "apexcore"
    startfile_mock.assert_called_once_with(str(folder))


def test_open_settings_dir_uses_xdg_open_on_linux(tmp_path, monkeypatch):
    """На Linux вызывается `subprocess.Popen(['xdg-open', ...])`."""
    if sys.platform == "win32":
        pytest.skip("Linux/macOS test")

    class _FakeSettings:
        data_dir = tmp_path / "apexcore"

    monkeypatch.setattr(settings_store, "load_settings", lambda: _FakeSettings())

    import subprocess

    popen_calls: list[list[str]] = []

    def _fake_popen(args, *a, **kw):
        popen_calls.append(args)
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    folder = settings_store.open_settings_dir()

    assert folder == tmp_path / "apexcore"
    assert len(popen_calls) == 1
    cmd = popen_calls[0]
    if sys.platform == "darwin":
        assert cmd[0] == "open"
    else:
        assert cmd[0] == "xdg-open"
    assert cmd[1] == str(folder)


def test_open_settings_dir_propagates_oserror(tmp_path, monkeypatch):
    """Ошибки ОС не глотаются — пробрасываются вверх, UI решает что показать."""

    class _FakeSettings:
        data_dir = tmp_path / "apexcore"

    monkeypatch.setattr(settings_store, "load_settings", lambda: _FakeSettings())

    if sys.platform == "win32":
        import os

        def _raise(_):
            raise OSError("simulated startfile failure")

        monkeypatch.setattr(os, "startfile", _raise, raising=False)
    else:
        import subprocess

        def _raise(*_a, **_kw):
            raise FileNotFoundError("xdg-open not installed")

        monkeypatch.setattr(subprocess, "Popen", _raise)

    with pytest.raises((OSError, FileNotFoundError)):
        settings_store.open_settings_dir()


# ─── SettingsScreen ─────────────────────────────────────────────────────────


def test_settings_screen_has_open_folder_item():
    """Пункт «Открыть папку настроек» присутствует в меню Настроек."""
    screen = SettingsScreen()
    items = screen.items()
    labels = [it.label for it in items]
    assert any(
        "Открыть папку настроек" in label and "ручного редактирования" in label
        for label in labels
    )


def test_settings_screen_open_folder_calls_helper(tmp_path, monkeypatch):
    """Хендлер `_open_folder` зовёт `open_settings_dir` и возвращает stay+green."""

    target = tmp_path / "apexcore"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        settings_store, "open_settings_dir", lambda: target
    )

    screen = SettingsScreen()
    result = screen._open_folder()

    assert result.action == NavAction.STAY
    assert result.flash is not None
    assert "Открыта папка настроек" in result.flash
    assert str(target) in result.flash


def test_settings_screen_open_folder_handles_error(tmp_path, monkeypatch):
    """При ошибке `_open_folder` возвращает stay с красным сообщением, не падает."""

    def _raise() -> Path:
        raise FileNotFoundError("xdg-open not installed")

    monkeypatch.setattr(settings_store, "open_settings_dir", _raise)
    monkeypatch.setattr(
        settings_store, "settings_dir", lambda: tmp_path / "apexcore"
    )

    screen = SettingsScreen()
    result = screen._open_folder()

    assert result.action == NavAction.STAY
    assert result.flash is not None
    assert "Не удалось открыть" in result.flash
    assert "red" in result.flash
