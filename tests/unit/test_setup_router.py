"""Регрессионные тесты для webui/setup_router.py.

Покрывает:
  - probe_environment не падает при любой среде, возвращает ожидаемые ключи;
  - probe_gpu (Linux/Windows) корректно классифицирует через моки;
  - is_setup_completed / _mark_setup_completed: marker-файл создаётся и читается;
  - make_setup_router возвращает router с ожидаемыми маршрутами.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# extras 'webui' — fastapi/uvicorn нужны для setup_router. Если их нет —
# skip всех тестов в этом файле (не блокируем CI на минимальной установке).
fastapi = pytest.importorskip("fastapi")

from apexcore.interfaces.webui import setup_router as sr


def test_probe_environment_returns_expected_keys():
    env = sr._probe_environment()
    assert isinstance(env, dict)
    for key in ("platform", "python", "blas", "sensors", "smartctl",
                "stress_ng", "dmidecode", "pkexec", "lspci"):
        assert key in env, f"missing key: {key}"


def test_probe_environment_platform_value():
    env = sr._probe_environment()
    assert env["platform"] in {"windows", "linux", "other"}


def test_marker_file_lifecycle(tmp_path, monkeypatch):
    """Проверяем что _mark_setup_completed → is_setup_completed возвращает True."""
    fake_marker = tmp_path / "config" / "setup_completed"
    monkeypatch.setattr(sr, "SETUP_MARKER_PATH", fake_marker)

    assert sr.is_setup_completed() is False
    sr._mark_setup_completed()
    assert sr.is_setup_completed() is True
    assert fake_marker.exists()
    # Файл содержит версию
    content = fake_marker.read_text(encoding="utf-8").strip()
    assert content  # любая непустая строка


def test_marker_write_failure_is_logged_at_warning(tmp_path, monkeypatch, caplog):
    """OSError при записи маркера должен попадать в лог как WARNING.

    Регрессия: раньше код тихо валился в logger.debug — на production
    уровне это значило, что не записанный маркер приводил к повторному
    показу wizard'а без видимой причины в логе. См. также F-15 в
    docs/Astra/problems_fixes.md — аналогичный паттерн silent-fallback
    в /api/history.
    """
    import logging

    # Невозможный путь — родительская директория = существующий файл.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    fake_marker = blocker / "setup_completed"
    monkeypatch.setattr(sr, "SETUP_MARKER_PATH", fake_marker)

    caplog.set_level(logging.WARNING, logger="apexcore.interfaces.webui.setup_router")
    sr._mark_setup_completed()  # OSError проглатывается, но лог должен быть

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "Ожидался WARNING-лог при OSError на записи маркера"
    assert any("setup_completed" in r.getMessage() for r in warnings)


def test_make_setup_router_has_expected_routes():
    router = sr.make_setup_router()
    paths = [getattr(r, "path", None) for r in router.routes]
    # Точки на GET / WebSocket — должны быть в роутере
    assert "/setup" in paths
    assert "/setup/" in paths
    assert "/api/setup/status" in paths
    assert "/api/setup/probe-gpu" in paths
    assert "/api/setup/probe-env" in paths
    # WebSocket путь тоже отражён как route
    assert any(p == "/ws/setup" for p in paths)


def test_probe_gpu_windows_handles_missing_powershell(monkeypatch):
    """Если PowerShell нет в PATH — probe возвращает reason, не падает."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda cmd: None if cmd == "powershell" else "/usr/bin/" + cmd)
    result = sr._probe_gpu_windows()
    assert isinstance(result, dict)
    assert result["nvidia"] is None
    assert result["amd"] is None
    assert result["reason"] is not None


# ─── launch_cli (Linux finish-шаг: чекбокс «Запустить CLI») ─────────────────

def test_launch_cli_no_display_skips(monkeypatch):
    """Без графической сессии ($DISPLAY/$WAYLAND_DISPLAY пусты) — не спавним."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    called = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda *a, **k: called.append((a, k)))
    msg = sr._launch_cli_terminal_linux()
    assert "пропуск" in msg
    assert not called, "Popen не должен вызываться без графической сессии"


def test_launch_cli_spawns_first_available_terminal(monkeypatch):
    """Находит первый доступный терминал и спавнит его с apexcore detached."""
    monkeypatch.setenv("DISPLAY", ":0")

    def fake_which(cmd):
        return {
            "apexcore": "/usr/bin/apexcore",
            "x-terminal-emulator": "/usr/bin/x-terminal-emulator",
        }.get(cmd)

    monkeypatch.setattr(sr.shutil, "which", fake_which)

    spawned = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append((argv, kwargs))

    monkeypatch.setattr(sr.subprocess, "Popen", _FakePopen)

    msg = sr._launch_cli_terminal_linux()
    assert spawned, "ожидался спавн терминала"
    argv, kwargs = spawned[0]
    assert argv == ["/usr/bin/x-terminal-emulator", "-e", "/usr/bin/apexcore"]
    assert kwargs.get("start_new_session") is True
    assert "x-terminal-emulator" in msg


def test_launch_cli_no_terminal_found(monkeypatch):
    """$DISPLAY есть, apexcore есть, но ни одного терминала — мягкое сообщение."""
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        sr.shutil, "which",
        lambda cmd: "/usr/bin/apexcore" if cmd == "apexcore" else None,
    )
    called = []
    monkeypatch.setattr(sr.subprocess, "Popen", lambda *a, **k: called.append(1))
    msg = sr._launch_cli_terminal_linux()
    assert not called
    assert "не найден" in msg
