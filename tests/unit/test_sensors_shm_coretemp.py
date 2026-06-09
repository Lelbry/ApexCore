"""Unit-тесты для CoreTemp Shared Memory reader."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from apexcore.infrastructure.sensors.shm import coretemp as coretemp_mod
from apexcore.infrastructure.sensors.shm.coretemp import (
    _CoreTempSharedDataEx,
    read_coretemp_sensors,
)


def _make_coretemp_blob(
    core_count: int,
    temps: list[float],
    *,
    fahrenheit: bool = False,
    delta_to_tjmax: bool = False,
    tjmax: int = 100,
) -> bytes:
    """Собрать синтетический CoreTemp SHM-блоб."""
    data = _CoreTempSharedDataEx()
    data.uiCoreCnt = core_count
    data.uiCPUCnt = 1
    for i in range(core_count):
        if i < len(temps):
            data.fTemp[i] = temps[i]
    data.uiTjMax[0] = tjmax
    data.ucFahrenheit = 1 if fahrenheit else 0
    data.ucDeltaToTjMax = 1 if delta_to_tjmax else 0
    data.ucTdpSupported = 0
    data.ucPowerSupported = 0
    data.fVID = 0.0
    return bytes(data)


def _patch_open_shm(monkeypatch: pytest.MonkeyPatch, blob: bytes | None) -> None:
    @contextmanager
    def fake_open_shm(name: str):
        yield blob

    monkeypatch.setattr(coretemp_mod, "open_shm", fake_open_shm)


def test_read_coretemp_no_shm(monkeypatch: pytest.MonkeyPatch) -> None:
    """CoreTemp не запущен — пустой dict."""
    _patch_open_shm(monkeypatch, None)
    assert read_coretemp_sensors() == {}


def test_read_coretemp_short_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    """Слишком короткий blob — пустой dict, без crash."""
    _patch_open_shm(monkeypatch, b"\x00" * 100)
    assert read_coretemp_sensors() == {}


def test_read_coretemp_per_core_temps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Восемь ядер — восемь ключей ``cpu/core_N`` + ``cpu/package`` = max."""
    blob = _make_coretemp_blob(8, [55.0, 56.5, 57.0, 56.0, 58.5, 57.5, 56.0, 55.5])
    _patch_open_shm(monkeypatch, blob)
    result = read_coretemp_sensors()
    assert result["cpu/core_0"] == pytest.approx(55.0)
    assert result["cpu/core_4"] == pytest.approx(58.5)
    assert result["cpu/core_7"] == pytest.approx(55.5)
    # package — max по core temps.
    assert result["cpu/package"] == pytest.approx(58.5)


def test_read_coretemp_fahrenheit_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если ``ucFahrenheit=1`` — конвертация °F → °C."""
    blob = _make_coretemp_blob(2, [131.0, 140.0], fahrenheit=True)
    _patch_open_shm(monkeypatch, blob)
    result = read_coretemp_sensors()
    # 131°F = 55°C, 140°F = 60°C
    assert result["cpu/core_0"] == pytest.approx(55.0, abs=0.1)
    assert result["cpu/core_1"] == pytest.approx(60.0, abs=0.1)


def test_read_coretemp_delta_to_tjmax(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если ``ucDeltaToTjMax=1`` — value = ``TjMax - delta``."""
    blob = _make_coretemp_blob(
        2, [40.0, 35.0], delta_to_tjmax=True, tjmax=100
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_coretemp_sensors()
    # 100 - 40 = 60, 100 - 35 = 65
    assert result["cpu/core_0"] == pytest.approx(60.0)
    assert result["cpu/core_1"] == pytest.approx(65.0)


def test_read_coretemp_zero_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если ``uiCoreCnt=0`` — пустой dict (graceful)."""
    blob = _make_coretemp_blob(0, [])
    _patch_open_shm(monkeypatch, blob)
    assert read_coretemp_sensors() == {}


def test_normalizer_keys_match_watchdog() -> None:
    """**Регрессия**: ключи ``cpu/core_N`` matchятся ``_is_cpu_temp_key``."""
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key

    for i in range(16):
        assert _is_cpu_temp_key(f"cpu/core_{i}"), (
            f"CoreTemp key cpu/core_{i} не matchится — watchdog не подхватит"
        )
    assert _is_cpu_temp_key("cpu/package")
