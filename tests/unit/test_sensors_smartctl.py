"""Тесты `infrastructure/sensors/smartctl.py` — без реального smartctl."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from apexcore.infrastructure.sensors import smartctl


@pytest.fixture(autouse=True)
def _reset_smartctl_cache() -> None:
    smartctl._reset_cache_for_tests()
    yield
    smartctl._reset_cache_for_tests()


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], _FakeCompleted | Exception],
) -> list[list[str]]:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _FakeCompleted:
        captured.append(list(cmd))
        result = handler(cmd)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def _patch_smartctl_in_path(monkeypatch: pytest.MonkeyPatch, found: bool) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "smartctl" if found and name == "smartctl" else None)


# ────────── базовые ──────────


def test_returns_empty_when_smartctl_not_in_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=False)
    assert smartctl.read_smartctl_temperatures() == {}
    assert smartctl.is_available() is False


def test_returns_empty_when_scan_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    _patch_subprocess(monkeypatch, lambda _cmd: FileNotFoundError("not found"))
    assert smartctl.read_smartctl_temperatures() == {}


def test_returns_empty_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    _patch_subprocess(monkeypatch, lambda _cmd: _FakeCompleted(stdout="not-json"))
    assert smartctl.read_smartctl_temperatures() == {}


# ────────── успешные пути ──────────


def test_nvme_device_temperature_via_temperature_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Современный smartctl 7+ всегда заполняет `temperature.current` в °C."""
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/nvme0n1", "type": "nvme"}]})
    info_json = json.dumps(
        {
            "temperature": {"current": 48},
            "nvme_smart_health_information_log": {"temperature": 48},
        }
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    result = smartctl.read_smartctl_temperatures()
    assert result == {"storage/nvme0/temperature": 48.0}


def test_sata_device_via_temperature_current(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/sda", "type": "sat"}]})
    info_json = json.dumps({"temperature": {"current": 35}})

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    result = smartctl.read_smartctl_temperatures()
    assert result == {"storage/sda/temperature": 35.0}


def test_legacy_nvme_kelvin_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Старый smartctl мог отдавать NVMe temperature в Кельвинах (>200) — нормализуем."""
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/nvme0"}]})
    info_json = json.dumps(
        {
            # current отсутствует в `temperature` — берём nvme-блок
            "nvme_smart_health_information_log": {"temperature": 321},  # K
        }
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    result = smartctl.read_smartctl_temperatures()
    assert result == {"storage/nvme0/temperature": 48.0}


def test_windows_physical_drive_short_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": r"\\.\PhysicalDrive0"}]})
    info_json = json.dumps({"temperature": {"current": 42}})

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    result = smartctl.read_smartctl_temperatures()
    assert result == {"storage/drive0/temperature": 42.0}


def test_multiple_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps(
        {"devices": [{"name": "/dev/nvme0n1"}, {"name": "/dev/sda"}]}
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        if "/dev/nvme0n1" in cmd:
            return _FakeCompleted(stdout=json.dumps({"temperature": {"current": 48}}))
        return _FakeCompleted(stdout=json.dumps({"temperature": {"current": 35}}))

    _patch_subprocess(monkeypatch, handler)

    result = smartctl.read_smartctl_temperatures()
    assert result == {
        "storage/nvme0/temperature": 48.0,
        "storage/sda/temperature": 35.0,
    }


def test_short_name_unit_cases() -> None:
    assert smartctl._short_name("/dev/nvme0n1") == "nvme0"
    assert smartctl._short_name("/dev/nvme1") == "nvme1"
    assert smartctl._short_name("/dev/sda") == "sda"
    assert smartctl._short_name(r"\\.\PhysicalDrive0") == "drive0"
    assert smartctl._short_name(r"\\.\PhysicalDrive12") == "drive12"
    # Fallback — нормализуем спецсимволы
    assert smartctl._short_name("/dev/strange-name") == "strange_name"


def test_devices_info_nvme_m2(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/nvme0n1"}]})
    info_json = json.dumps(
        {
            "model_name": "Samsung SSD 980 PRO 1TB",
            "device": {"protocol": "NVMe"},
            "form_factor": {"name": "M.2"},
            "rotation_rate": 0,
        }
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    info = smartctl.read_smartctl_devices_info()
    assert info["nvme0"]["model"] == "Samsung SSD 980 PRO 1TB"
    assert info["nvme0"]["type"] == "SSD M.2 NVMe"


def test_devices_info_sata_ssd(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/sda"}]})
    info_json = json.dumps(
        {
            "model_name": "Crucial MX500 1TB",
            "device": {"protocol": "ATA"},
            "rotation_rate": 0,
        }
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    info = smartctl.read_smartctl_devices_info()
    assert info["sda"]["model"] == "Crucial MX500 1TB"
    assert info["sda"]["type"] == "SSD SATA"


def test_devices_info_hdd(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_json = json.dumps({"devices": [{"name": "/dev/sdb"}]})
    info_json = json.dumps(
        {
            "model_name": "WDC WD20EZBX-00AYRA0",
            "device": {"protocol": "ATA"},
            "rotation_rate": 7200,
        }
    )

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    info = smartctl.read_smartctl_devices_info()
    assert info["sdb"]["type"] == "HDD (7200 RPM)"


def test_devices_info_returns_empty_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smartctl_in_path(monkeypatch, found=False)
    assert smartctl.read_smartctl_devices_info() == {}


def test_scan_cache_avoids_redundant_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """В пределах TTL повторный вызов не дёргает `smartctl --scan` второй раз."""
    _patch_smartctl_in_path(monkeypatch, found=True)
    scan_calls = {"count": 0}
    scan_json = json.dumps({"devices": [{"name": "/dev/nvme0n1"}]})
    info_json = json.dumps({"temperature": {"current": 48}})

    def handler(cmd: list[str]) -> _FakeCompleted:
        if "--scan" in cmd:
            scan_calls["count"] += 1
            return _FakeCompleted(stdout=scan_json)
        return _FakeCompleted(stdout=info_json)

    _patch_subprocess(monkeypatch, handler)

    smartctl.read_smartctl_temperatures()
    smartctl.read_smartctl_temperatures()
    smartctl.read_smartctl_temperatures()
    assert scan_calls["count"] == 1
