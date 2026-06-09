"""Тесты `infrastructure/disk_peak`.

Главные инварианты:
1. Lookup детерминированный (одинаковые входы → одинаковый профиль).
2. NVMe → NVME_PROFILE независимо от media_type ("SSD" / "" / неизвестный).
3. SATA SSD → SATA_SSD_PROFILE; HDD → HDD_PROFILE.
4. Unknown / пустое → UNKNOWN_PROFILE (= HDD-консерватив).
"""

from __future__ import annotations

from apexcore.infrastructure.disk_peak import (
    HDD_PROFILE,
    NVME_PROFILE,
    SATA_SSD_PROFILE,
    UNKNOWN_PROFILE,
    lookup_disk_peak,
)


def test_nvme_matches_regardless_of_media():
    assert lookup_disk_peak("SSD", "NVMe") is NVME_PROFILE
    assert lookup_disk_peak("", "NVMe") is NVME_PROFILE
    assert lookup_disk_peak(None, "NVMe") is NVME_PROFILE
    # Регистронезависимо.
    assert lookup_disk_peak("ssd", "nvme") is NVME_PROFILE


def test_sata_ssd_matches():
    assert lookup_disk_peak("SSD", "SATA") is SATA_SSD_PROFILE
    assert lookup_disk_peak("SSD", "") is SATA_SSD_PROFILE
    assert lookup_disk_peak("SSD", None) is SATA_SSD_PROFILE


def test_hdd_matches():
    assert lookup_disk_peak("HDD", "SATA") is HDD_PROFILE
    assert lookup_disk_peak("HDD", "") is HDD_PROFILE


def test_unknown_returns_fallback():
    assert lookup_disk_peak(None, None) is UNKNOWN_PROFILE
    assert lookup_disk_peak("", "") is UNKNOWN_PROFILE
    assert lookup_disk_peak("Unspecified", "USB") is UNKNOWN_PROFILE


def test_nvme_profile_has_higher_peaks_than_sata():
    assert NVME_PROFILE.seq_read_mb_s > SATA_SSD_PROFILE.seq_read_mb_s
    assert NVME_PROFILE.random_read_mb_s > SATA_SSD_PROFILE.random_read_mb_s


def test_hdd_random_is_killer():
    """HDD random ~5 MB/s — seek-killer-показатель."""
    assert HDD_PROFILE.random_read_mb_s <= 10.0
    assert HDD_PROFILE.random_read_mb_s < SATA_SSD_PROFILE.random_read_mb_s


def test_media_label_present():
    assert NVME_PROFILE.media_label == "NVMe"
    assert SATA_SSD_PROFILE.media_label == "SATA SSD"
    assert HDD_PROFILE.media_label == "HDD"
