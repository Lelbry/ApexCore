"""Тесты `infrastructure/sensors/nvidia_ml.py` (без реального NVIDIA-драйвера).

Mock-стратегия: подменяем `sys.modules["pynvml"]` на синтетический модуль
с нужным API. Это позволяет тестировать парсинг даже на машинах без
NVIDIA-GPU (CI/Linux-без-NVIDIA/AstraLinux).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from apexcore.infrastructure.sensors import nvidia_ml


@pytest.fixture(autouse=True)
def _reset_nvml_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбрасываем singleton init-flag между тестами."""
    nvidia_ml._reset_for_tests()
    yield
    nvidia_ml._reset_for_tests()
    # Удаляем подменённый pynvml из sys.modules — иначе следующий тест
    # будет видеть мок предыдущего теста.
    sys.modules.pop("pynvml", None)


class _FakeNVMLError(Exception):
    """Симулирует pynvml.NVMLError для веток обработки исключений."""


def _make_fake_pynvml(
    *,
    init_raises: Exception | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> Any:
    """Сконструировать синтетический pynvml-модуль с заданными устройствами.

    Каждое устройство — dict с полями (по необходимости):
    name, temperature, power_mw, clock_graphics, clock_memory,
    util_gpu, util_mem, threshold_slowdown, threshold_shutdown,
    threshold_gpu_max.
    Поля могут быть None или отсутствовать — тогда соответствующий вызов
    бросает _FakeNVMLError или возвращает None (что и эмулирует поведение
    consumer-GPU когда драйвер не публикует метрику).
    """
    devices = devices or []

    handles = list(range(len(devices)))

    def nvmlInit() -> None:
        if init_raises is not None:
            raise init_raises

    def nvmlShutdown() -> None:
        return None

    def nvmlDeviceGetCount() -> int:
        return len(devices)

    def nvmlDeviceGetHandleByIndex(idx: int) -> int:
        return handles[idx]

    def nvmlDeviceGetName(handle: int) -> str:
        name = devices[handle].get("name")
        if name is None:
            raise _FakeNVMLError("name unavailable")
        return name

    def nvmlDeviceGetTemperature(handle: int, _sensor: int) -> int:
        value = devices[handle].get("temperature")
        if value is None:
            raise _FakeNVMLError("no temp")
        return int(value)

    def nvmlDeviceGetPowerUsage(handle: int) -> int:
        value = devices[handle].get("power_mw")
        if value is None:
            raise _FakeNVMLError("no power")
        return int(value)

    def nvmlDeviceGetClockInfo(handle: int, clock_id: int) -> int:
        key = {0: "clock_graphics", 1: "clock_sm", 2: "clock_memory"}.get(
            clock_id, "?"
        )
        value = devices[handle].get(key)
        if value is None:
            raise _FakeNVMLError("no clock")
        return int(value)

    def nvmlDeviceGetUtilizationRates(handle: int) -> Any:
        gpu = devices[handle].get("util_gpu")
        mem = devices[handle].get("util_mem")
        if gpu is None and mem is None:
            raise _FakeNVMLError("no util")
        return SimpleNamespace(gpu=gpu or 0, memory=mem or 0)

    def nvmlDeviceGetTemperatureThreshold(handle: int, thr_id: int) -> int:
        key = {0: "threshold_shutdown", 1: "threshold_slowdown", 4: "threshold_gpu_max"}.get(
            thr_id, "?"
        )
        value = devices[handle].get(key)
        if value is None:
            raise _FakeNVMLError("no threshold")
        return int(value)

    return SimpleNamespace(
        NVMLError=_FakeNVMLError,
        NVML_TEMPERATURE_GPU=0,
        NVML_CLOCK_GRAPHICS=0,
        NVML_CLOCK_MEM=2,
        NVML_TEMPERATURE_THRESHOLD_SHUTDOWN=0,
        NVML_TEMPERATURE_THRESHOLD_SLOWDOWN=1,
        NVML_TEMPERATURE_THRESHOLD_GPU_MAX=4,
        nvmlInit=nvmlInit,
        nvmlShutdown=nvmlShutdown,
        nvmlDeviceGetCount=nvmlDeviceGetCount,
        nvmlDeviceGetHandleByIndex=nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetName=nvmlDeviceGetName,
        nvmlDeviceGetTemperature=nvmlDeviceGetTemperature,
        nvmlDeviceGetPowerUsage=nvmlDeviceGetPowerUsage,
        nvmlDeviceGetClockInfo=nvmlDeviceGetClockInfo,
        nvmlDeviceGetUtilizationRates=nvmlDeviceGetUtilizationRates,
        nvmlDeviceGetTemperatureThreshold=nvmlDeviceGetTemperatureThreshold,
    )


# ────────── базовые случаи ──────────


def test_unavailable_when_pynvml_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если pynvml не импортируется — все read_* возвращают пустой dict."""
    # Запрещаем импорт pynvml через подмену __import__
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pynvml":
            raise ImportError("nvidia-ml-py не установлен")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert nvidia_ml.read_nvml_temperatures() == {}
    assert nvidia_ml.read_nvml_power() == {}
    assert nvidia_ml.read_nvml_frequencies() == {}
    assert nvidia_ml.is_available() is False


def test_init_failure_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """После одной неудачной инициализации повторные вызовы не пытаются re-init."""
    fake = _make_fake_pynvml(init_raises=_FakeNVMLError("driver not loaded"))
    sys.modules["pynvml"] = fake

    init_calls = {"count": 0}
    real_init = fake.nvmlInit

    def counting_init() -> None:
        init_calls["count"] += 1
        real_init()

    fake.nvmlInit = counting_init  # type: ignore[attr-defined]

    assert nvidia_ml.read_nvml_temperatures() == {}
    assert nvidia_ml.read_nvml_power() == {}
    assert nvidia_ml.read_nvml_frequencies() == {}
    # Init был вызван ровно один раз — флаг _init_failed закэшировал отказ.
    assert init_calls["count"] == 1


# ────────── успешные пути ──────────


def test_temperatures_single_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[{"name": "RTX 4070 Ti", "temperature": 53}]
    )

    result = nvidia_ml.read_nvml_temperatures()
    assert result == {"nvml/0/temperature": 53.0}


def test_power_converts_mw_to_w(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[{"power_mw": 211_662}]  # 211.662 W
    )

    result = nvidia_ml.read_nvml_power()
    assert result["nvml/0/power_w"] == pytest.approx(211.662, abs=0.01)


def test_frequencies_two_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[{"clock_graphics": 2910, "clock_memory": 10701}]
    )

    result = nvidia_ml.read_nvml_frequencies()
    assert result == {
        "nvml/0/clock_graphics": 2910.0,
        "nvml/0/clock_memory": 10701.0,
    }


def test_utilization(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[{"util_gpu": 73, "util_mem": 28}]
    )

    result = nvidia_ml.read_nvml_utilization()
    assert result == {"nvml/0/util_gpu": 73.0, "nvml/0/util_mem": 28.0}


def test_thresholds_consumer_gpu_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """На consumer-картах часть thresholds может отсутствовать — берём только не-None."""
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[
            {
                "threshold_slowdown": 96,
                "threshold_shutdown": 101,
                # threshold_gpu_max не задан — fake выбросит NVMLError
            }
        ]
    )

    result = nvidia_ml.read_nvml_thresholds()
    assert "nvml/0/threshold_slowdown" in result
    assert "nvml/0/threshold_shutdown" in result
    assert "nvml/0/threshold_gpu_max" not in result


def test_read_all_single_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_nvml_all не должен дублировать init / GetCount."""
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[
            {
                "temperature": 53,
                "power_mw": 200_000,
                "clock_graphics": 2910,
                "clock_memory": 10701,
            }
        ]
    )

    temps, power, freqs = nvidia_ml.read_nvml_all()
    assert temps == {"nvml/0/temperature": 53.0}
    assert power == {"nvml/0/power_w": 200.0}
    assert freqs["nvml/0/clock_graphics"] == 2910.0


def test_multiple_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Многоkartочная система: каждое устройство получает свой индекс."""
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[
            {"name": "RTX 4090", "temperature": 60},
            {"name": "RTX 3090", "temperature": 55},
        ]
    )

    result = nvidia_ml.read_nvml_temperatures()
    assert result == {"nvml/0/temperature": 60.0, "nvml/1/temperature": 55.0}


def test_device_names_returns_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[{"name": "NVIDIA GeForce RTX 4070 Ti"}]
    )

    result = nvidia_ml.read_nvml_device_names()
    assert result == {0: "NVIDIA GeForce RTX 4070 Ti"}


def test_individual_sensor_error_doesnt_break_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если одно устройство падает на температуре — другое всё равно должно
    отдать своё значение."""
    sys.modules["pynvml"] = _make_fake_pynvml(
        devices=[
            {"temperature": None},  # NVMLError на nvmlDeviceGetTemperature
            {"temperature": 55},
        ]
    )

    result = nvidia_ml.read_nvml_temperatures()
    assert result == {"nvml/1/temperature": 55.0}
