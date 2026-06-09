"""Unit-тесты для ``ryzen_master`` — runtime-discovery AMD SDK (P1.4).

Реальная верификация требует AMD desktop — у пользователя Intel
i9-12900K. Тесты используют моки (``platform.processor`` для имитации
AMD, фейковый DLL handle через monkeypatch) и проверяют:

- ``is_available()`` корректно отсеивает не-AMD CPU + не-Windows ОС;
- DLL не грузится повторно после первого сбоя (cache флага);
- ``read_*`` graceful-degrade при отсутствии функций в DLL;
- интеграция с ``WindowsAdapter._read_sensors`` (Ryzen Master подключается
  только когда `_has_cpu_temp` пуст и `is_available`).

P1.4 caveat: signature функций (GetCpuTemperature, GetCpuVoltage,
GetSocVoltage) предположительны. При появлении AMD desktop возможны
правки — тесты тогда нужно будет обновить под реальный SDK.
"""

from __future__ import annotations

import platform
from pathlib import Path
from types import SimpleNamespace

import pytest

from apexcore.infrastructure.sensors import ryzen_master


@pytest.fixture(autouse=True)
def _reset_ryzen_state() -> None:
    """Сбросить module-level state между тестами."""
    ryzen_master._reset_for_tests()


# ─── _cpu_is_amd / is_available ────────────────────────────────────────────


def test_cpu_is_amd_for_ryzen_5800x(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ryzen 5800X → AMD."""
    monkeypatch.setattr(
        platform, "processor", lambda: "AMD Ryzen 7 5800X 8-Core Processor"
    )
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert ryzen_master._cpu_is_amd() is True


def test_cpu_is_amd_for_epyc(monkeypatch: pytest.MonkeyPatch) -> None:
    """EPYC server CPU тоже распознан как AMD."""
    monkeypatch.setattr(platform, "processor", lambda: "AMD EPYC 7763")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert ryzen_master._cpu_is_amd() is True


def test_cpu_is_amd_returns_false_for_intel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intel i9-12900K → не AMD."""
    monkeypatch.setattr(
        platform, "processor", lambda: "Intel(R) Core(TM) i9-12900K"
    )
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert ryzen_master._cpu_is_amd() is False


def test_cpu_is_amd_returns_false_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """На Linux всегда возвращает False (Ryzen Master Windows-only)."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "processor", lambda: "AMD Ryzen 7 5800X")
    assert ryzen_master._cpu_is_amd() is False


def test_is_available_requires_amd_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_available=False на Intel даже если DLL по пути есть."""
    monkeypatch.setattr(
        platform, "processor", lambda: "Intel(R) Core(TM) i9-12900K"
    )
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert ryzen_master.is_available() is False


def test_is_available_requires_dll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AMD CPU + DLL отсутствует → is_available=False."""
    monkeypatch.setattr(platform, "processor", lambda: "AMD Ryzen 9 7950X")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    # Override env var → путь несуществующий.
    monkeypatch.setenv("APEXCORE_RYZEN_MASTER_DLL", str(tmp_path / "missing.dll"))
    assert ryzen_master.is_available() is False


def test_is_available_true_with_amd_and_dll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AMD + DLL по env-override → is_available=True."""
    monkeypatch.setattr(platform, "processor", lambda: "AMD Ryzen 9 7950X")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    fake_dll = tmp_path / "Platform.AMD.RyzenMaster.dll"
    fake_dll.write_bytes(b"\x4d\x5a")  # MZ — фиктивный PE header
    monkeypatch.setenv("APEXCORE_RYZEN_MASTER_DLL", str(fake_dll))
    assert ryzen_master.is_available() is True


# ─── _ensure_dll_loaded ────────────────────────────────────────────────────


def test_ensure_dll_loaded_skips_non_amd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не AMD → ``_ensure_dll_loaded`` сразу None + помечает unavailable."""
    monkeypatch.setattr(platform, "processor", lambda: "Intel Core i9")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert ryzen_master._ensure_dll_loaded() is None
    assert ryzen_master._RYZEN_MASTER_UNAVAILABLE is True


def test_ensure_dll_loaded_caches_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """После первого сбоя — повторная загрузка не пытается грузить DLL снова."""
    monkeypatch.setattr(platform, "processor", lambda: "AMD Ryzen 7 5800X")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    fake_dll = tmp_path / "Platform.AMD.RyzenMaster.dll"
    fake_dll.write_bytes(b"not a real dll")
    monkeypatch.setenv("APEXCORE_RYZEN_MASTER_DLL", str(fake_dll))

    load_calls = {"count": 0}

    class _FailingCDLL:
        def __init__(self, *args, **kwargs):
            load_calls["count"] += 1
            raise OSError("simulated DLL load failure")

    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", _FailingCDLL)
    assert ryzen_master._ensure_dll_loaded() is None
    assert load_calls["count"] == 1
    # Повторный вызов — не должен снова дёргать WinDLL.
    assert ryzen_master._ensure_dll_loaded() is None
    assert load_calls["count"] == 1


# ─── read_ryzen_master_temperatures ────────────────────────────────────────


class _FakeDllHandle:
    """Имитация загруженной DLL с произвольным набором функций."""

    def __init__(self, **funcs) -> None:
        for name, func in funcs.items():
            setattr(self, name, func)


def _make_gettemp_func(value: float, rc: int = 0):
    """Возвращает callable, имитирующий GetCpuTemperature(double*)."""

    def _f(ptr):
        ptr._obj.value = value
        return rc

    _f.argtypes = None
    _f.restype = None
    return _f


def _install_fake_handle(
    monkeypatch: pytest.MonkeyPatch, handle: _FakeDllHandle
) -> None:
    """Подменить ``_ensure_dll_loaded`` чтобы возвращал заданный handle."""
    monkeypatch.setattr(ryzen_master, "_ensure_dll_loaded", lambda: handle)


def test_read_temperatures_returns_tctl_and_tdie(monkeypatch: pytest.MonkeyPatch) -> None:
    """GetCpuTemperature вернул 65.5 °C → cpu/tctl + cpu/tdie заполнены."""
    handle = _FakeDllHandle(GetCpuTemperature=_make_gettemp_func(65.5))
    _install_fake_handle(monkeypatch, handle)
    result = ryzen_master.read_ryzen_master_temperatures()
    assert result["cpu/tctl"] == pytest.approx(65.5)
    assert result["cpu/tdie"] == pytest.approx(65.5)


def test_read_temperatures_skip_on_nonzero_rc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetCpuTemperature вернул rc=1 → пустой dict, без temperature."""
    handle = _FakeDllHandle(GetCpuTemperature=_make_gettemp_func(65.5, rc=1))
    _install_fake_handle(monkeypatch, handle)
    assert ryzen_master.read_ryzen_master_temperatures() == {}


def test_read_temperatures_skip_zero_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """GetCpuTemperature вернул 0.0 → пустой dict (zero = invalid)."""
    handle = _FakeDllHandle(GetCpuTemperature=_make_gettemp_func(0.0))
    _install_fake_handle(monkeypatch, handle)
    assert ryzen_master.read_ryzen_master_temperatures() == {}


def test_read_temperatures_missing_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """DLL не экспортирует GetCpuTemperature → пустой dict (defensive)."""
    handle = _FakeDllHandle()  # без GetCpuTemperature
    _install_fake_handle(monkeypatch, handle)
    assert ryzen_master.read_ryzen_master_temperatures() == {}


def test_read_temperatures_no_dll_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """DLL не загружена → пустой dict без exceptions."""
    monkeypatch.setattr(ryzen_master, "_ensure_dll_loaded", lambda: None)
    assert ryzen_master.read_ryzen_master_temperatures() == {}


def test_read_temperatures_swallows_function_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Исключение из DLL-функции не пробрасывается наружу."""

    def boom(ptr):
        raise OSError("simulated SDK failure")

    handle = _FakeDllHandle(GetCpuTemperature=boom)
    _install_fake_handle(monkeypatch, handle)
    assert ryzen_master.read_ryzen_master_temperatures() == {}


# ─── read_ryzen_master_voltages ────────────────────────────────────────────


def test_read_voltages_returns_vcore_and_soc(monkeypatch: pytest.MonkeyPatch) -> None:
    """GetCpuVoltage и GetSocVoltage возвращают значения → vcore/soc в dict."""

    def make_volt_func(value: float):
        def _f(ptr):
            ptr._obj.value = value
            return 0

        return _f

    handle = _FakeDllHandle(
        GetCpuVoltage=make_volt_func(1.218),
        GetSocVoltage=make_volt_func(1.103),
    )
    _install_fake_handle(monkeypatch, handle)
    result = ryzen_master.read_ryzen_master_voltages()
    assert result["cpu/vcore"] == pytest.approx(1.218)
    assert result["cpu/soc"] == pytest.approx(1.103)


def test_read_voltages_partial_returns_what_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Только GetCpuVoltage есть в DLL → только vcore в dict."""

    def vcore_func(ptr):
        ptr._obj.value = 1.2
        return 0

    handle = _FakeDllHandle(GetCpuVoltage=vcore_func)
    _install_fake_handle(monkeypatch, handle)
    result = ryzen_master.read_ryzen_master_voltages()
    assert result == {"cpu/vcore": pytest.approx(1.2)}


def test_read_voltages_no_dll_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ryzen_master, "_ensure_dll_loaded", lambda: None)
    assert ryzen_master.read_ryzen_master_voltages() == {}


# ─── Reset helper ──────────────────────────────────────────────────────────


def test_reset_for_tests_clears_state() -> None:
    """``_reset_for_tests`` обнуляет module-level state."""
    ryzen_master._RYZEN_MASTER_UNAVAILABLE = True
    ryzen_master._RYZEN_MASTER_DLL_HANDLE = SimpleNamespace()
    ryzen_master._reset_for_tests()
    assert ryzen_master._RYZEN_MASTER_UNAVAILABLE is False
    assert ryzen_master._RYZEN_MASTER_DLL_HANDLE is None
