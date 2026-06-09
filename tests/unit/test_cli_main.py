"""Юнит-тесты bootstrap-логики CLI ``apexcore`` (interfaces/cli/main.py).

Здесь только то, что можно проверить изолированно от Typer'а — например,
баннер о пропавшей LHM-DLL, который защищает dev-пользователей от тихого
регресса issue #20 (DLL не скачана → температура CPU не считывается).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apexcore.interfaces.cli import main as cli_main


def _force_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заставить ``platform.system()`` вернуть Windows независимо от хоста."""
    monkeypatch.setattr(cli_main.platform, "system", lambda: "Windows")


def test_warn_if_dll_missing_prints_banner_when_dll_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """На Windows без DLL баннер появляется с упоминанием fetch_lhm.ps1."""
    _force_windows(monkeypatch)
    fake_dll = tmp_path / "LibreHardwareMonitorLib.dll"
    assert not fake_dll.exists()
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.lhm._LIB_DLL", fake_dll
    )

    cli_main._warn_if_lhm_dll_missing(invoked_subcommand=None)

    out = capsys.readouterr().out
    assert "LibreHardwareMonitorLib.dll" in out
    assert "fetch_lhm.ps1" in out
    assert "apexcore doctor" in out


def test_warn_if_dll_missing_silent_when_dll_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Если DLL на месте — никакого баннера не печатается."""
    _force_windows(monkeypatch)
    fake_dll = tmp_path / "LibreHardwareMonitorLib.dll"
    fake_dll.write_bytes(b"fake")
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.lhm._LIB_DLL", fake_dll
    )

    cli_main._warn_if_lhm_dll_missing(invoked_subcommand=None)

    assert capsys.readouterr().out == ""


def test_warn_if_dll_missing_skipped_for_doctor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`apexcore doctor` сам показывает полный отчёт — баннер избыточен."""
    _force_windows(monkeypatch)
    fake_dll = tmp_path / "LibreHardwareMonitorLib.dll"
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.lhm._LIB_DLL", fake_dll
    )

    cli_main._warn_if_lhm_dll_missing(invoked_subcommand="doctor")

    assert capsys.readouterr().out == ""


def test_warn_if_dll_missing_silent_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """На Linux/Astra DLL вообще не нужна — баннер не печатается."""
    monkeypatch.setattr(cli_main.platform, "system", lambda: "Linux")
    fake_dll = tmp_path / "LibreHardwareMonitorLib.dll"
    monkeypatch.setattr(
        "apexcore.infrastructure.sensors.lhm._LIB_DLL", fake_dll
    )

    cli_main._warn_if_lhm_dll_missing(invoked_subcommand=None)

    assert capsys.readouterr().out == ""
