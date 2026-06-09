"""Тесты обёртки над LibreHardwareMonitorLib (без реального CLR/pythonnet)."""

from __future__ import annotations

from typing import Any

import pytest

from apexcore.infrastructure.sensors import lhm


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Перед каждым тестом сбрасываем глобальное состояние singleton.

    Дополнительно изолируем тесты от живого `apexcore_sensord` сервиса:
    если он запущен на машине разработчика, `read_lhm_*` функции через
    shm-first логику отдадут реальные данные вместо тестовых моков и
    assertion'ы упадут с непонятным diff. Принудительно отключаем
    оба shm-пути — тесты должны тестировать только прямой LHM-collector.
    """
    monkeypatch.setattr(lhm, "_computer", None)
    monkeypatch.setattr(lhm, "_init_failed", False)
    monkeypatch.setattr(lhm, "_runtime_configured", False)
    monkeypatch.setattr(lhm, "_try_shm_temperatures_and_voltages", lambda: None)
    monkeypatch.setattr(lhm, "_try_shm_reader", lambda _name: None)


# ────────── _normalize_name ──────────


def test_normalize_name_simple() -> None:
    assert lhm._normalize_name("CPU Package") == "cpu_package"


def test_normalize_name_with_punctuation() -> None:
    assert lhm._normalize_name("CPU Core #1") == "cpu_core_1"


def test_normalize_name_collapses_separators() -> None:
    assert lhm._normalize_name("GPU Hot Spot (peak)") == "gpu_hot_spot_peak"


def test_normalize_name_empty_falls_back() -> None:
    assert lhm._normalize_name("") == "unknown"
    assert lhm._normalize_name("---") == "unknown"


# ────────── _is_instantaneous_temp ──────────


def test_is_instantaneous_keeps_real_temperatures() -> None:
    """Реальные температуры должны проходить фильтр."""
    real = [
        "cpu_package",
        "p_core_1",
        "e_core_8",
        "core_max",
        "core_average",
        "gpu_core",
        "gpu_hot_spot",
        "gpu_memory_junction",
        "dimm_1",
        "vrm_mos",
        "pch",
        "composite_temperature",
        "temperature_2",
        "cpu_socket",
    ]
    for name in real:
        assert lhm._is_instantaneous_temp(name), f"должно пройти: {name}"


def test_is_instantaneous_drops_thresholds_and_params() -> None:
    """Константы-пороги и параметры датчика должны отбрасываться."""
    fake = [
        "thermal_sensor_low_limit",
        "thermal_sensor_high_limit",
        "thermal_sensor_critical_low_limit",
        "thermal_sensor_critical_high_limit",
        "temperature_sensor_resolution",
        "warning_temperature",
        "critical_temperature",
        "p_core_1_distance_to_tjmax",
        "e_core_8_distance_to_tjmax",
    ]
    for name in fake:
        assert not lhm._is_instantaneous_temp(name), f"должно отвалиться: {name}"


# ────────── graceful degrade ──────────


def test_read_lhm_returns_empty_when_dll_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Без DLL функция должна молча вернуть {} и поднять _init_failed."""
    # На dev-машине после scripts/fetch_lhm.ps1 DLL может уже лежать в lib/,
    # но в CI и в чистом репозитории её нет. Заводим тест заведомо в режим
    # «DLL отсутствует», подменяя путь.
    fake_lib = tmp_path / "lib"
    fake_lib.mkdir()
    monkeypatch.setattr(lhm, "_LIB_DIR", fake_lib)
    monkeypatch.setattr(lhm, "_LIB_DLL", fake_lib / "LibreHardwareMonitorLib.dll")
    assert not lhm._LIB_DLL.exists()

    result = lhm.read_lhm_temperatures()

    assert result == {}
    assert lhm._init_failed is True


def test_read_lhm_does_not_retry_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Один раз провалившись, ленивый init не дёргает CLR повторно."""
    calls = {"open": 0}

    def fake_open() -> Any:
        calls["open"] += 1
        raise RuntimeError("simulated init failure")

    monkeypatch.setattr(lhm, "_open_computer", fake_open)

    assert lhm.read_lhm_temperatures() == {}
    assert lhm.read_lhm_temperatures() == {}
    assert lhm.read_lhm_temperatures() == {}
    assert calls["open"] == 1  # инициализация попробована ровно раз


def test_read_lhm_uses_collected_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """При успешной инициализации возвращаем то, что собрал combined-сборщик."""
    sentinel_computer = object()
    monkeypatch.setattr(lhm, "_open_computer", lambda: sentinel_computer)
    monkeypatch.setattr(
        lhm,
        "_collect_temperatures_and_voltages",
        lambda comp: (
            {"cpu/package": 65.0, "gpu/temperature": 51.0},
            {"cpu/cpu_core": 1.250},
        ),
    )

    result = lhm.read_lhm_temperatures()
    assert result == {"cpu/package": 65.0, "gpu/temperature": 51.0}

    # Повторный вызов использует тот же объект — без re-init.
    second = lhm.read_lhm_temperatures()
    assert second == {"cpu/package": 65.0, "gpu/temperature": 51.0}


def test_read_lhm_voltages_uses_combined_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_lhm_voltages`` достаёт второй элемент кортежа combined-сбора."""
    sentinel_computer = object()
    monkeypatch.setattr(lhm, "_open_computer", lambda: sentinel_computer)
    monkeypatch.setattr(
        lhm,
        "_collect_temperatures_and_voltages",
        lambda comp: (
            {"cpu/cpu_package": 60.0},
            {"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95},
        ),
    )

    voltages = lhm.read_lhm_voltages()
    assert voltages == {"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95}


def test_read_lhm_combined_no_double_update_per_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один вызов combined-функции = ровно один проход по hardware.

    Регрессионный для требования «добавление вольтажа не удваивает цену
    LHM-опроса» (`hardware.Update()` — самая дорогая операция, ~10–50 мс
    на узел). Считаем количество ``Update`` вызовов на фейковом графе.
    """

    class _FakeSensor:
        def __init__(self, sensor_type: Any, name: str, value: float) -> None:
            self.SensorType = sensor_type
            self.Name = name
            self.Value = value

    class _FakeHardware:
        def __init__(self, ht_name: str, sensors: list[_FakeSensor]) -> None:
            self.HardwareType = ht_name
            self.Sensors = sensors
            self.SubHardware: list[Any] = []
            self.update_calls = 0

        def Update(self) -> None:  # повторяет имя метода LHM API
            self.update_calls += 1

    class _FakeComputer:
        def __init__(self, hardware: list[_FakeHardware]) -> None:
            self.Hardware = hardware

    # Реализуем минимальный двойник SensorType с .Temperature / .Voltage,
    # чтобы не тащить настоящий .NET enum.
    class _SensorTypes:
        Temperature = "Temperature"
        Voltage = "Voltage"

    import sys
    import types

    fake_module = types.ModuleType("LibreHardwareMonitor.Hardware")
    fake_module.SensorType = _SensorTypes
    monkeypatch.setitem(sys.modules, "LibreHardwareMonitor.Hardware", fake_module)

    cpu = _FakeHardware(
        "Cpu",
        [
            _FakeSensor("Temperature", "CPU Package", 67.0),
            _FakeSensor("Voltage", "CPU Core", 1.275),
            _FakeSensor("Voltage", "CPU SoC", 1.10),
            _FakeSensor("Voltage", "VID", None),  # игнорируется
            _FakeSensor("Clock", "CPU Core #1", 4800.0),  # другой тип — пропуск
        ],
    )
    gpu = _FakeHardware(
        "GpuNvidia",
        [
            _FakeSensor("Temperature", "GPU Core", 51.0),
            _FakeSensor("Voltage", "GPU Core", 0.95),
        ],
    )
    computer = _FakeComputer([cpu, gpu])

    temps, voltages = lhm._collect_temperatures_and_voltages(computer)

    assert cpu.update_calls == 1, "Update() на CPU должен вызываться ровно раз"
    assert gpu.update_calls == 1, "Update() на GPU должен вызываться ровно раз"

    assert temps == {"cpu/cpu_package": 67.0, "gpunvidia/gpu_core": 51.0}
    assert voltages == {
        "cpu/cpu_core": 1.275,
        "cpu/cpu_soc": 1.10,
        "gpunvidia/gpu_core": 0.95,
    }


def test_read_lhm_swallows_collect_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_computer = object()
    monkeypatch.setattr(lhm, "_open_computer", lambda: sentinel_computer)

    def boom(_computer: Any) -> tuple[dict[str, float], dict[str, float]]:
        raise RuntimeError("CLR exploded mid-iteration")

    monkeypatch.setattr(lhm, "_collect_temperatures_and_voltages", boom)

    # Не должно ронять процесс — graceful degrade до {} / ({}, {}).
    assert lhm.read_lhm_temperatures() == {}
    assert lhm.read_lhm_voltages() == {}
    assert lhm.read_lhm_temperatures_and_voltages() == ({}, {})


# ────────── _warmup_cpu_sensors (issue #20) ──────────


def test_warmup_returns_immediately_when_cpu_temp_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если CPU-температура уже доступна — прогрев останавливается на 1-й попытке."""
    calls = {"n": 0}

    def fake_collect(_comp: Any) -> dict[str, float]:
        calls["n"] += 1
        return {"cpu/package": 60.0, "gpu/temperature": 51.0}

    monkeypatch.setattr(lhm, "_collect_temperatures", fake_collect)
    # delay=0 чтобы тест не залипал на time.sleep даже в худшем случае.
    monkeypatch.setattr(lhm, "_WARMUP_DELAY_SEC", 0.0)

    lhm._warmup_cpu_sensors(object())

    assert calls["n"] == 1


def test_warmup_polls_until_cpu_temp_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Холодный LHM: первые опросы без cpu/*, прогрев ждёт пока появится."""
    calls = {"n": 0}

    def fake_collect(_comp: Any) -> dict[str, float]:
        calls["n"] += 1
        if calls["n"] < 3:
            return {"gpu/temperature": 51.0}  # пока без cpu/*
        return {"cpu/package": 60.0, "gpu/temperature": 51.0}

    monkeypatch.setattr(lhm, "_collect_temperatures", fake_collect)
    monkeypatch.setattr(lhm, "_WARMUP_DELAY_SEC", 0.0)

    lhm._warmup_cpu_sensors(object())

    assert calls["n"] == 3


def test_warmup_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если cpu/* так и не появилось — прогрев не зацикливается, выходит молча."""
    calls = {"n": 0}

    def fake_collect(_comp: Any) -> dict[str, float]:
        calls["n"] += 1
        return {"gpu/temperature": 51.0}

    monkeypatch.setattr(lhm, "_collect_temperatures", fake_collect)
    monkeypatch.setattr(lhm, "_WARMUP_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(lhm, "_WARMUP_DELAY_SEC", 0.0)

    lhm._warmup_cpu_sensors(object())

    assert calls["n"] == 4


def test_warmup_swallows_collect_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка _collect_temperatures не должна ронять init — выходим тихо."""
    def boom(_comp: Any) -> dict[str, float]:
        raise RuntimeError("CLR exploded mid-iteration")

    monkeypatch.setattr(lhm, "_collect_temperatures", boom)
    monkeypatch.setattr(lhm, "_WARMUP_DELAY_SEC", 0.0)

    # Не должно бросать.
    lhm._warmup_cpu_sensors(object())


# ────────── _configure_runtime ──────────


def test_configure_runtime_no_env_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEXCORE_DOTNET_ROOT", raising=False)
    # Не должно бросать ничего.
    lhm._configure_runtime()
    assert lhm._runtime_configured is True


def test_configure_runtime_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEXCORE_DOTNET_ROOT", raising=False)
    lhm._configure_runtime()
    lhm._configure_runtime()
    lhm._configure_runtime()
    # Идемпотентность — без побочных эффектов.
    assert lhm._runtime_configured is True


def test_configure_runtime_skips_when_runtimeconfig_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Если APEXCORE_DOTNET_ROOT указан, но runtimeconfig.json не лежит — не падаем."""
    monkeypatch.setenv("APEXCORE_DOTNET_ROOT", str(tmp_path))
    # Файла apexcore.runtimeconfig.json там нет — функция должна тихо вернуться.
    lhm._configure_runtime()
    assert lhm._runtime_configured is True


# ────────── read_lhm_fans: фильтрация RPM (регрессия idle GPU) ──────────


class _FakeSensor:
    def __init__(self, name: str, value: float | None, sensor_type: Any) -> None:
        self.Name = name
        self.Value = value
        self.SensorType = sensor_type


class _FakeHardware:
    def __init__(self, name: str, sensors: list[_FakeSensor]) -> None:
        self.Name = name
        self.HardwareType = "GpuNvidia"  # любой, для _hardware_prefix
        self.Sensors = sensors
        self.SubHardware: list[_FakeHardware] = []

    def Update(self) -> None:
        return None


def _setup_fake_lhm(monkeypatch: pytest.MonkeyPatch, sensors: list[_FakeSensor]) -> None:
    """Подменить LHM-singleton фейковыми sensors одного hardware-узла."""
    # Подменяем SensorType.Fan: используем строку как уникальный sentinel,
    # чтобы избежать импорта реальной CLR-сборки в тесте.
    fan_sentinel = "FAN_TYPE_SENTINEL"

    class _FakeSensorType:
        Fan = fan_sentinel

    # У _FakeSensor.sensor_type должен быть тот же sentinel — иначе фильтр в
    # read_lhm_fans отвалится. Перебиваем здесь, чтобы тесту не приходилось
    # импортировать LibreHardwareMonitor.Hardware.
    for s in sensors:
        s.SensorType = fan_sentinel

    sentinel_computer = type("C", (), {"Hardware": [_FakeHardware("GPU", sensors)]})()
    monkeypatch.setattr(lhm, "_open_computer", lambda: sentinel_computer)

    # Подменяем sys.modules чтобы `from LibreHardwareMonitor.Hardware import
    # SensorType` внутри read_lhm_fans возвращал наш sentinel.
    import sys
    import types

    fake_mod = types.ModuleType("LibreHardwareMonitor.Hardware")
    fake_mod.SensorType = _FakeSensorType
    monkeypatch.setitem(sys.modules, "LibreHardwareMonitor.Hardware", fake_mod)
    monkeypatch.setitem(
        sys.modules,
        "LibreHardwareMonitor",
        types.ModuleType("LibreHardwareMonitor"),
    )


def test_read_lhm_fans_includes_zero_rpm_idle_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Регрессия v0.5.3**: idle GPU с 0 RPM — валидный fan-датчик.

    До фикса фильтр ``rpm <= 0`` отсекал idle GPU fans, и при остановленных
    вентиляторах карточка «Вентиляторы» рисовала misleading «нет данных от
    LHM (без админа или нет fan-датчиков)». Теперь 0 RPM показываются
    как есть (stopped fan = валидное состояние zero-RPM mode).
    """
    sensors = [
        _FakeSensor("GPU Fan 1", 0.0, None),
        _FakeSensor("GPU Fan 2", 0.0, None),
    ]
    _setup_fake_lhm(monkeypatch, sensors)

    result = lhm.read_lhm_fans()

    assert result == {"fan/gpu_fan_1": 0.0, "fan/gpu_fan_2": 0.0}


def test_read_lhm_fans_drops_negative_rpm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отрицательные RPM — sensor error, отсекаются (в отличие от 0)."""
    sensors = [
        _FakeSensor("CPU Fan", 1500.0, None),
        _FakeSensor("Broken Fan", -1.0, None),
    ]
    _setup_fake_lhm(monkeypatch, sensors)

    result = lhm.read_lhm_fans()

    assert "fan/cpu_fan" in result
    assert "fan/broken_fan" not in result


def test_read_lhm_fans_drops_none_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """sensor.Value == None (datasheet/uninitialized) — пропускается."""
    sensors = [
        _FakeSensor("Chassis Fan 1", 1000.0, None),
        _FakeSensor("Unknown Fan", None, None),
    ]
    _setup_fake_lhm(monkeypatch, sensors)

    result = lhm.read_lhm_fans()

    assert "fan/chassis_fan_1" in result
    assert "fan/unknown_fan" not in result


def test_read_lhm_fans_mixed_running_and_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смешанный сценарий: одни fan'ы крутятся, другие остановлены."""
    sensors = [
        _FakeSensor("CPU Fan", 820.0, None),
        _FakeSensor("Chassis Fan 1", 1445.0, None),
        _FakeSensor("GPU Fan 1", 0.0, None),  # idle GPU
        _FakeSensor("GPU Fan 2", 0.0, None),
    ]
    _setup_fake_lhm(monkeypatch, sensors)

    result = lhm.read_lhm_fans()

    assert result["fan/cpu_fan"] == 820.0
    assert result["fan/chassis_fan_1"] == 1445.0
    assert result["fan/gpu_fan_1"] == 0.0
    assert result["fan/gpu_fan_2"] == 0.0
