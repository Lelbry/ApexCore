"""Unit-тесты для HWiNFO Shared Memory reader.

Mockaem ``shm._common.open_shm`` чтобы подменить реальный
``OpenFileMapping``/``MapViewOfFile`` синтетическим bytes-блобом.
Это даёт детерминированную проверку парсинга struct без зависимости
от наличия HWiNFO на CI.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager

import pytest

from apexcore.infrastructure.sensors.shm import hwinfo as hwinfo_mod
from apexcore.infrastructure.sensors.shm.hwinfo import (
    _HwinfoHeader,
    _HwinfoReadingElement,
    read_hwinfo_sensors,
    read_hwinfo_temperatures_and_voltages,
)

_HWINFO_MAGIC = 0x53696857
_SENSOR_TYPE_TEMP = 1
_SENSOR_TYPE_VOLT = 2


def _make_hwinfo_blob(readings: list[tuple[int, str, float]]) -> bytes:
    """Собрать синтетический HWiNFO SHM-блоб для тестов.

    Args:
        readings: список ``(type, label, value)`` — type=1 для temp, 2 для voltage.
    """
    reading_size = ctypes.sizeof(_HwinfoReadingElement)
    header = _HwinfoHeader()
    header.dwSignature = _HWINFO_MAGIC
    header.dwVersion = 2
    header.dwRevision = 0
    header.pollTime = 0
    header.dwOffsetOfSensorSection = ctypes.sizeof(_HwinfoHeader)
    header.dwSizeOfSensorElement = 0
    header.dwNumSensorElements = 0
    header.dwOffsetOfReadingSection = ctypes.sizeof(_HwinfoHeader)
    header.dwSizeOfReadingElement = reading_size
    header.dwNumReadingElements = len(readings)

    blob = bytes(header)
    for typ, label, value in readings:
        r = _HwinfoReadingElement()
        r.tReading = typ
        r.dwSensorIndex = 0
        r.dwReadingID = 0
        label_bytes = label.encode("utf-8")[:127]
        r.szLabelOrig = label_bytes + b"\x00" * (128 - len(label_bytes))
        r.szLabelUser = r.szLabelOrig
        r.szUnit = b"\xb0C\x00" + b"\x00" * 13
        r.Value = value
        r.ValueMin = value
        r.ValueMax = value
        r.ValueAvg = value
        blob += bytes(r)
    return blob


def _patch_open_shm(monkeypatch: pytest.MonkeyPatch, blob: bytes | None) -> None:
    """Подменить ``open_shm`` синтетическим blob (или ``None`` если SHM нет)."""

    @contextmanager
    def fake_open_shm(name: str):
        yield blob

    monkeypatch.setattr(hwinfo_mod, "open_shm", fake_open_shm)


def test_read_hwinfo_no_shm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если HWiNFO не запущен (``open_shm`` → ``None``) — пустой dict."""
    _patch_open_shm(monkeypatch, None)
    assert read_hwinfo_sensors() == {}


def test_read_hwinfo_empty_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    """Слишком короткий blob — пустой dict."""
    _patch_open_shm(monkeypatch, b"\x00" * 8)
    assert read_hwinfo_sensors() == {}


def test_read_hwinfo_wrong_magic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Magic не совпадает — пустой dict (graceful, не crash)."""
    header = _HwinfoHeader()
    header.dwSignature = 0xDEADBEEF
    header.dwSizeOfReadingElement = ctypes.sizeof(_HwinfoReadingElement)
    header.dwOffsetOfReadingSection = ctypes.sizeof(_HwinfoHeader)
    _patch_open_shm(monkeypatch, bytes(header) + b"\x00" * 1000)
    assert read_hwinfo_sensors() == {}


def test_read_hwinfo_cpu_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """«CPU Package» → ключ ``cpu/package``."""
    blob = _make_hwinfo_blob([(_SENSOR_TYPE_TEMP, "CPU Package", 65.5)])
    _patch_open_shm(monkeypatch, blob)
    result = read_hwinfo_sensors()
    assert result == {"cpu/package": pytest.approx(65.5)}


def test_read_hwinfo_per_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Core #0», «Core #1» → ``cpu/core_0``, ``cpu/core_1``."""
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_TEMP, "Core #0", 60.0),
            (_SENSOR_TYPE_TEMP, "Core #1", 62.5),
            (_SENSOR_TYPE_TEMP, "Core #2", 58.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_hwinfo_sensors()
    assert result["cpu/core_0"] == pytest.approx(60.0)
    assert result["cpu/core_1"] == pytest.approx(62.5)
    assert result["cpu/core_2"] == pytest.approx(58.0)


def test_read_hwinfo_gpu_temps(monkeypatch: pytest.MonkeyPatch) -> None:
    """«GPU Hot Spot» → ``gpu/hot_spot``."""
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_TEMP, "GPU Hot Spot", 80.0),
            (_SENSOR_TYPE_TEMP, "GPU Core Temperature", 70.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_hwinfo_sensors()
    assert result["gpu/hot_spot"] == pytest.approx(80.0)
    assert result["gpu/core"] == pytest.approx(70.0)


def test_read_hwinfo_voltages_separated_from_temps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voltage-сенсоры не попадают в temps + Vcore попадает в voltages (P1.5).

    P1.5: ``normalize_voltage_key`` нормализует voltage-labels («CPU Core
    Voltage» → ``cpu/vcore``). До P1.5 нормализатор не покрывал voltages
    и Vcore терялся.
    """
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_TEMP, "CPU Package", 65.0),
            (_SENSOR_TYPE_VOLT, "CPU Core Voltage", 1.250),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    temps, voltages = read_hwinfo_temperatures_and_voltages()
    assert temps == {"cpu/package": pytest.approx(65.0)}
    # P1.5: voltage теперь нормализуется в `cpu/vcore` и попадает в voltages.
    assert voltages == {"cpu/vcore": pytest.approx(1.250)}
    # temp-словарь не содержит voltage-ключи.
    assert "cpu/vcore" not in temps


def test_read_hwinfo_voltages_p1_5_full_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1.5: расширенный набор voltage-labels нормализуется.

    Покрывает CPU Vcore + CPU SoC + GPU Core voltage. На multi-CPU/GPU
    системах эти voltages — единственный источник Vcore без LHM.
    """
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_VOLT, "CPU Core Voltage", 1.218),
            (_SENSOR_TYPE_VOLT, "CPU SoC Voltage", 1.103),
            (_SENSOR_TYPE_VOLT, "GPU Core Voltage", 1.075),
            (_SENSOR_TYPE_VOLT, "DRAM Voltage", 1.350),
            # +3.3V/+5V/+12V — общая мат-плата.
            (_SENSOR_TYPE_VOLT, "+12V", 12.05),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    _temps, voltages = read_hwinfo_temperatures_and_voltages()
    assert voltages["cpu/vcore"] == pytest.approx(1.218)
    assert voltages["cpu/soc"] == pytest.approx(1.103)
    assert voltages["gpu/vcore"] == pytest.approx(1.075)
    assert voltages["ram/vdd"] == pytest.approx(1.350)
    assert voltages["motherboard/12v"] == pytest.approx(12.05)


def test_read_hwinfo_voltage_unknown_label_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voltage с нераспознанным label'ом отбрасывается (не попадает в voltages)."""
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_TEMP, "CPU Package", 65.0),
            (_SENSOR_TYPE_VOLT, "Some Custom Rail XYZ", 0.85),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    _temps, voltages = read_hwinfo_temperatures_and_voltages()
    # Нераспознанный voltage не попал в voltages.
    assert voltages == {}


def test_read_hwinfo_unknown_label_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нераспознанные label'ы тихо отбрасываются (контракт normalize)."""
    blob = _make_hwinfo_blob(
        [
            (_SENSOR_TYPE_TEMP, "Some Vendor-Specific Zone XYZ", 55.0),
            (_SENSOR_TYPE_TEMP, "CPU Package", 65.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_hwinfo_sensors()
    assert "cpu/package" in result
    # Vendor-specific сенсор не должен попасть.
    assert all("xyz" not in k.lower() for k in result)


def test_normalizer_compatibility_with_thermal_watchdog() -> None:
    """**Регрессия**: ключи от HWiNFO matchятся ``_is_cpu_temp_key``.

    Architectural review §1.1: критическая совместимость. Без неё
    watchdog молчит даже когда HWiNFO даёт корректные данные.
    """
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key
    from apexcore.infrastructure.sensors.shm._common import normalize_sensor_key

    cpu_labels = [
        "CPU Package",
        "CPU (Tctl/Tdie)",
        "Core #0",
        "Core #15",
        "CPU Core 7",
        "P-Core 0",
        "E-Core 4",
        "CCD0",
    ]
    for label in cpu_labels:
        key = normalize_sensor_key(label)
        assert key is not None, f"label '{label}' не нормализуется"
        assert _is_cpu_temp_key(key), (
            f"HWiNFO label '{label}' → '{key}' не matchится "
            f"_is_cpu_temp_key — watchdog не подхватит"
        )
