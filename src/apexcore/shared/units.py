"""Единицы измерения и конвертации."""

from __future__ import annotations

BYTES_PER_GB = 1024 ** 3
BYTES_PER_MB = 1024 ** 2


def bytes_to_gb(value: float) -> float:
    """Перевести байты в гигабайты (двоичные)."""
    return value / BYTES_PER_GB


def bytes_to_mb(value: float) -> float:
    """Перевести байты в мегабайты (двоичные)."""
    return value / BYTES_PER_MB


def humanize_throughput(value: float, unit: str) -> str:
    """Отформатировать throughput с разумной точностью."""
    if unit in {"GB/s", "MB/s"}:
        return f"{value:.2f} {unit}"
    if unit == "ns/access":
        return f"{value:.1f} {unit}"
    if unit == "ops/s":
        if value >= 1e9:
            return f"{value / 1e9:.2f} Gops/s"
        if value >= 1e6:
            return f"{value / 1e6:.2f} Mops/s"
        if value >= 1e3:
            return f"{value / 1e3:.2f} Kops/s"
        return f"{value:.0f} ops/s"
    return f"{value:.3g} {unit}"
