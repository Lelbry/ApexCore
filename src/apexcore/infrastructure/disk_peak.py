"""Типовые пики пропускной способности накопителей для Roofline-нормировки.

Используется в ``application/general_benchmark_score`` для расчёта
``r_disk = GM(seq_read/peak_read, random_read/peak_random_read,
seq_write/peak_write)``.

Пики намеренно детерминированы по ``media_type`` (NVMe / SSD / HDD) — это
повторяет инвариант Roofline-балла: одинаковая система → одинаковый
знаменатель → одинаковый балл. Тонкое различение поколений (NVMe Gen3 vs
Gen4 vs Gen5) не делаем — clamp ratio к 1.0 в формуле спасает топовое
железо от просадки балла из-за того, что наш встроенный disk-bench даёт
синхронный sync I/O с queue depth 1 (см. ``microbench/disk.py``).

Значения — типовые datasheet-показатели:
- NVMe (Gen3 SATA): ~3500 MB/s sequential read, 600 MB/s random 4K, 2500 MB/s write
- SATA SSD: предел SATA III ~600 MB/s, рабочий 550 MB/s
- HDD 7200 RPM: 200 MB/s sequential, ~5 MB/s random (seek-killer), 180 MB/s write
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiskPeakProfile:
    """Roofline-пики накопителя для трёх паттернов IO (MB/s)."""

    media_label: str        # Человекочитаемая метка для UI ("NVMe", "SATA SSD", "HDD")
    seq_read_mb_s: float
    random_read_mb_s: float
    seq_write_mb_s: float


# Базовая таблица. Ключ — нормализованный кортеж (media_type, bus_type),
# где значения берутся из PhysicalDisk.media_type/bus_type (см.
# infrastructure/disk_inventory.py). bus_type=="" → fallback по media_type.
NVME_PROFILE = DiskPeakProfile(
    media_label="NVMe",
    seq_read_mb_s=3500.0,
    random_read_mb_s=600.0,
    seq_write_mb_s=2500.0,
)
SATA_SSD_PROFILE = DiskPeakProfile(
    media_label="SATA SSD",
    seq_read_mb_s=550.0,
    random_read_mb_s=400.0,
    seq_write_mb_s=500.0,
)
HDD_PROFILE = DiskPeakProfile(
    media_label="HDD",
    seq_read_mb_s=200.0,
    random_read_mb_s=5.0,
    seq_write_mb_s=180.0,
)
UNKNOWN_PROFILE = HDD_PROFILE  # консервативный fallback


def lookup_disk_peak(
    media_type: str | None,
    bus_type: str | None,
) -> DiskPeakProfile:
    """Подобрать ``DiskPeakProfile`` по ``(media_type, bus_type)``.

    ``media_type`` и ``bus_type`` приходят из ``PhysicalDisk``. Регистр
    нормализуется. Логика:

    - ``bus_type == "NVMe"`` (любой media_type) → ``NVME_PROFILE``
    - ``media_type == "SSD"`` (любая шина кроме NVMe) → ``SATA_SSD_PROFILE``
    - ``media_type == "HDD"`` → ``HDD_PROFILE``
    - всё остальное → ``UNKNOWN_PROFILE`` (тот же, что HDD — консервативно).
    """
    media = (media_type or "").strip().upper()
    bus = (bus_type or "").strip().upper()

    if bus == "NVME":
        return NVME_PROFILE
    if media == "SSD":
        return SATA_SSD_PROFILE
    if media == "HDD":
        return HDD_PROFILE
    return UNKNOWN_PROFILE


__all__ = [
    "HDD_PROFILE",
    "NVME_PROFILE",
    "SATA_SSD_PROFILE",
    "UNKNOWN_PROFILE",
    "DiskPeakProfile",
    "lookup_disk_peak",
]
