"""Regression-тесты для фильтра «ACPI fake zone» (25-30 °C).

OEM на ноутбуках часто публикует декоративные thermal zones, отдающие
константы 25-30 °C даже под полной нагрузкой (см. ресерч §2.5).
Фильтр в ``windows.py::_is_acpi_fake_zone`` помечает такие источники
как ``approximate`` для UX (но не отбрасывает данные — на ноутбуках
это единственный источник).
"""

from __future__ import annotations

import sys

import pytest

if sys.platform != "win32":  # pragma: no cover
    pytest.skip("ACPI filter тестируется только на Win32", allow_module_level=True)

from apexcore.infrastructure.adapters.windows import (
    _has_cpu_temp,
    _is_acpi_fake_zone,
)


def test_fake_zone_all_28c() -> None:
    """Все значения в [25, 30] → fake zone."""
    temps = {"thermal_zone_0": 28.0, "thermal_zone_1": 27.5}
    assert _is_acpi_fake_zone(temps) is True


def test_fake_zone_boundary_25c() -> None:
    """Точно 25.0 и 30.0 — на границах включаются."""
    temps = {"zone_0": 25.0, "zone_1": 30.0}
    assert _is_acpi_fake_zone(temps) is True


def test_real_temperature_under_load() -> None:
    """Если хотя бы одно значение > 30°C → не fake zone."""
    temps = {"thermal_zone_0": 28.0, "cpu/package": 65.0}
    assert _is_acpi_fake_zone(temps) is False


def test_one_below_25() -> None:
    """Если значение < 25 (idle/охлаждённый) → не fake zone."""
    temps = {"thermal_zone_0": 22.0}
    assert _is_acpi_fake_zone(temps) is False


def test_empty_dict() -> None:
    """Пустой dict → False (нечего фильтровать)."""
    assert _is_acpi_fake_zone({}) is False


def test_has_cpu_temp_real_keys() -> None:
    """``_has_cpu_temp`` распознаёт реальные CPU-ключи."""
    assert _has_cpu_temp({"cpu/package": 65.0}) is True
    assert _has_cpu_temp({"cpu/core_0": 60.0}) is True
    assert _has_cpu_temp({"cpu/tctl": 55.0}) is True
    assert _has_cpu_temp({"coretemp/core_1": 58.0}) is True


def test_has_cpu_temp_ignores_gpu_storage() -> None:
    """``_has_cpu_temp`` НЕ matchится на GPU/storage."""
    assert _has_cpu_temp({"gpu/temperature": 70.0}) is False
    assert _has_cpu_temp({"nvme/temperature": 45.0}) is False
    assert _has_cpu_temp({"storage/composite": 50.0}) is False
    assert _has_cpu_temp({"wifi/temperature": 40.0}) is False


def test_has_cpu_temp_thermal_zone_not_cpu() -> None:
    """**Регрессия**: ``thermal_zone_N`` — НЕ CPU (ACPI fake zone, см. ARCHITECTURE.md)."""
    assert _has_cpu_temp({"thermal_zone_0": 28.0}) is False
    assert _has_cpu_temp({"thermal_zone_1": 30.0}) is False


def test_has_cpu_temp_mixed() -> None:
    """Среди GPU и thermal_zone есть один CPU → True."""
    assert _has_cpu_temp(
        {"gpu/temp": 70.0, "thermal_zone_0": 28.0, "cpu/package": 65.0}
    ) is True
