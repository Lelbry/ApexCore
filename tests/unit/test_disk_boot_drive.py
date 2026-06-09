"""Тесты `infrastructure/disk_inventory.get_boot_drive*`.

Главные инварианты:
1. На Windows `get_boot_drive_path()` использует SystemDrive (с fallback на
   домашнюю букву и финальный fallback `C:\\`).
2. На Linux всегда `/`.
3. `get_boot_drive()` матчит букву диска с PhysicalDisk.letters
   (регистронезависимо).
4. Если ни один физический диск не содержит букву загрузочного — возвращает
   (boot_path, None).
"""

from __future__ import annotations

import sys

import pytest

from apexcore.infrastructure.disk_inventory import (
    PhysicalDisk,
    get_boot_drive,
    get_boot_drive_path,
)


@pytest.fixture
def fake_disks() -> list[PhysicalDisk]:
    return [
        PhysicalDisk(
            index=0,
            model="Kingston KC3000",
            bus_type="NVMe",
            media_type="SSD",
            size_gb=2000.0,
            letters=["C:"],
        ),
        PhysicalDisk(
            index=1,
            model="Seagate Barracuda 2TB",
            bus_type="SATA",
            media_type="HDD",
            size_gb=2000.0,
            letters=["D:", "E:"],
        ),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_boot_path_uses_system_drive(monkeypatch):
    monkeypatch.setenv("SystemDrive", "D:")
    assert get_boot_drive_path() == "D:\\"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_boot_path_fallback_c(monkeypatch):
    monkeypatch.delenv("SystemDrive", raising=False)
    # Path.home().drive обычно даёт "C:" — но мы не знаем точно. Просто
    # проверяем что вернулось что-то заканчивающееся на ":\\".
    result = get_boot_drive_path()
    assert result.endswith("\\")
    assert ":" in result


@pytest.mark.skipif(sys.platform == "win32", reason="Linux/Unix-only")
def test_boot_path_linux_is_root():
    assert get_boot_drive_path() == "/"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_get_boot_drive_matches_c_disk(monkeypatch, fake_disks):
    monkeypatch.setenv("SystemDrive", "C:")
    path, disk = get_boot_drive(disks=fake_disks)
    assert path == "C:\\"
    assert disk is not None
    assert disk.model == "Kingston KC3000"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_get_boot_drive_matches_d_when_system_drive_d(monkeypatch, fake_disks):
    monkeypatch.setenv("SystemDrive", "D:")
    path, disk = get_boot_drive(disks=fake_disks)
    assert path == "D:\\"
    assert disk is not None
    assert disk.model == "Seagate Barracuda 2TB"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_get_boot_drive_returns_none_when_no_match(monkeypatch, fake_disks):
    monkeypatch.setenv("SystemDrive", "Z:")
    path, disk = get_boot_drive(disks=fake_disks)
    assert path == "Z:\\"
    assert disk is None


@pytest.mark.skipif(sys.platform == "win32", reason="Linux/Unix-only")
def test_get_boot_drive_linux_matches_root():
    disks = [
        PhysicalDisk(
            index=0,
            model="nvme0n1",
            bus_type="NVMe",
            media_type="SSD",
            size_gb=1000.0,
            letters=["/", "/boot"],
        ),
    ]
    path, disk = get_boot_drive(disks=disks)
    assert path == "/"
    assert disk is not None
    assert disk.model == "nvme0n1"
