"""Тесты `infrastructure/disk_inventory.py`.

Subprocess (PowerShell на Windows, lsblk на Linux) мокается через monkeypatch.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from apexcore.infrastructure import disk_inventory
from apexcore.infrastructure.disk_inventory import (
    PhysicalDisk,
    _coerce_enum,
)
from apexcore.interfaces.cli.render_sensors import _normalize_model


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


# ─── PhysicalDisk.display_type / display_title ─────────────────────────────


def test_display_type_nvme_ssd() -> None:
    d = PhysicalDisk(
        index=0,
        model="Kingston KC3000 2TB",
        bus_type="NVMe",
        media_type="SSD",
        size_gb=2048.0,
    )
    assert d.display_type == "SSD NVMe"


def test_display_type_sata_hdd() -> None:
    d = PhysicalDisk(
        index=1,
        model="ST2000NM0011",
        bus_type="SATA",
        media_type="HDD",
        size_gb=2000.0,
    )
    assert d.display_type == "HDD SATA"


def test_display_type_sata_ssd() -> None:
    d = PhysicalDisk(
        index=2,
        model="Samsung 860 EVO",
        bus_type="SATA",
        media_type="SSD",
        size_gb=500.0,
    )
    assert d.display_type == "SSD SATA"


def test_display_type_usb() -> None:
    d = PhysicalDisk(
        index=3, model="Generic Flash", bus_type="USB", media_type="SSD", size_gb=32.0
    )
    assert "USB" in d.display_type


def test_display_title_with_letters() -> None:
    d = PhysicalDisk(
        index=0,
        model="Kingston KC3000 2TB",
        bus_type="NVMe",
        media_type="SSD",
        size_gb=2048.0,
        letters=["C:", "D:"],
    )
    title = d.display_title
    assert "[C:, D:]" in title
    assert "Kingston KC3000 2TB" in title
    assert "SSD NVMe" in title


def test_display_title_without_letters() -> None:
    d = PhysicalDisk(
        index=0, model="Diskless RAID", bus_type="RAID", media_type="",
        size_gb=None, letters=[],
    )
    title = d.display_title
    assert "[" not in title  # без квадратных скобок если букв нет
    assert "Diskless RAID" in title


# ─── _coerce_enum ───────────────────────────────────────────────────────────


def test_coerce_enum_int_to_string() -> None:
    code_map = {17: "NVMe", 11: "SATA"}
    assert _coerce_enum(17, code_map) == "NVMe"
    assert _coerce_enum(11, code_map) == "SATA"


def test_coerce_enum_string_passthrough() -> None:
    """PowerShell на новых билдах возвращает строку — оставляем как есть."""
    assert _coerce_enum("NVMe", {}) == "NVMe"
    assert _coerce_enum("SATA", {}) == "SATA"


def test_coerce_enum_unknown_returns_empty() -> None:
    assert _coerce_enum(None, {}) == ""
    assert _coerce_enum("Unknown", {}) == ""
    assert _coerce_enum("Unspecified", {}) == ""
    assert _coerce_enum(0, {0: "Unspecified"}) == ""


# ─── _normalize_model ──────────────────────────────────────────────────────


def test_normalize_model_strips_punct_lower() -> None:
    assert _normalize_model("KINGSTON SKC3000D2048G") == "kingstonskc3000d2048g"
    assert _normalize_model("Samsung 980 PRO 1TB") == "samsung980pro1tb"
    assert _normalize_model("WDC WD20EZBX-00AYRA0") == "wdcwd20ezbx00ayra0"


# ─── Windows enumeration ───────────────────────────────────────────────────


def test_list_windows_disks_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    ps_output = json.dumps(
        {
            "Disks": [
                {
                    "DeviceId": 0,
                    "FriendlyName": "Kingston SKC3000D2048G",
                    "Model": "Kingston SKC3000D2048G",
                    "BusType": "NVMe",
                    "MediaType": "SSD",
                    "Size": 2_048_408_248_320,
                },
                {
                    "DeviceId": 1,
                    "FriendlyName": "ST2000NM0011",
                    "Model": "ST2000NM0011",
                    "BusType": "SATA",
                    "MediaType": "HDD",
                    "Size": 2_000_398_934_016,
                },
            ],
            "Partitions": [
                {"DiskNumber": 0, "DriveLetter": "C"},
                {"DiskNumber": 0, "DriveLetter": "D"},
                {"DiskNumber": 1, "DriveLetter": "E"},
            ],
        }
    )
    _patch_subprocess(monkeypatch, lambda _cmd: _FakeCompleted(stdout=ps_output))

    disks = disk_inventory._list_windows_disks()
    assert len(disks) == 2
    kingston = disks[0]
    assert kingston.model == "Kingston SKC3000D2048G"
    assert kingston.bus_type == "NVMe"
    assert kingston.media_type == "SSD"
    assert kingston.letters == ["C:", "D:"]
    assert kingston.display_type == "SSD NVMe"
    seagate = disks[1]
    assert seagate.letters == ["E:"]
    assert seagate.display_type == "HDD SATA"


def test_list_windows_disks_handles_int_busmedia(monkeypatch: pytest.MonkeyPatch) -> None:
    """Старые билды PowerShell сериализуют enum как int."""
    monkeypatch.setattr("sys.platform", "win32")
    ps_output = json.dumps(
        {
            "Disks": [
                {
                    "DeviceId": 0,
                    "FriendlyName": "Test SSD",
                    "Model": "Test SSD",
                    "BusType": 17,   # NVMe
                    "MediaType": 4,  # SSD
                    "Size": 1_000_000_000,
                }
            ],
            "Partitions": [],
        }
    )
    _patch_subprocess(monkeypatch, lambda _cmd: _FakeCompleted(stdout=ps_output))

    disks = disk_inventory._list_windows_disks()
    assert disks[0].bus_type == "NVMe"
    assert disks[0].media_type == "SSD"


def test_list_windows_disks_powershell_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    _patch_subprocess(monkeypatch, lambda _cmd: FileNotFoundError("powershell not found"))
    assert disk_inventory._list_windows_disks() == []


def test_list_windows_disks_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    _patch_subprocess(monkeypatch, lambda _cmd: _FakeCompleted(stdout="not-json"))
    assert disk_inventory._list_windows_disks() == []


# ─── Linux enumeration ────────────────────────────────────────────────────


def test_list_linux_disks_parses_lsblk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    base_json = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "model": "Samsung SSD 980 PRO 1TB",
                    "tran": "nvme",
                    "rota": False,
                    "size": 1_000_204_886_016,
                },
                {
                    "name": "sda",
                    "model": "WDC WD20EZBX-00AYRA0",
                    "tran": "sata",
                    "rota": True,
                    "size": 2_000_398_934_016,
                },
            ]
        }
    )
    mounts_json = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "children": [
                        {"name": "nvme0n1p1", "mountpoints": ["/boot"]},
                        {"name": "nvme0n1p2", "mountpoints": ["/"]},
                    ],
                },
                {"name": "sda", "children": [{"name": "sda1", "mountpoints": ["/home"]}]},
            ]
        }
    )
    calls = {"count": 0}

    def handler(cmd: list[str]) -> _FakeCompleted:
        calls["count"] += 1
        if "-d" in cmd:
            return _FakeCompleted(stdout=base_json)
        return _FakeCompleted(stdout=mounts_json)

    _patch_subprocess(monkeypatch, handler)

    disks = disk_inventory._list_linux_disks()
    assert len(disks) == 2
    nvme = disks[0]
    assert nvme.model == "Samsung SSD 980 PRO 1TB"
    assert nvme.bus_type == "NVMe"
    assert nvme.media_type == "SSD"
    assert "/" in nvme.letters
    sata = disks[1]
    assert sata.media_type == "HDD"
    assert "/home" in sata.letters


def test_list_linux_disks_lsblk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    _patch_subprocess(monkeypatch, lambda _cmd: FileNotFoundError("lsblk not found"))
    assert disk_inventory._list_linux_disks() == []
