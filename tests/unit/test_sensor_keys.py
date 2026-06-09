"""Тесты `application/sensor_keys.py` — парсер LHM/NVML/smartctl-ключей."""

from __future__ import annotations

import pytest

from apexcore.application.sensor_keys import parse_legacy_key
from apexcore.domain.sensor_models import SensorGroup, SensorKind, SourceBackend

# ─── CPU ─────────────────────────────────────────────────────────────────


def test_cpu_package_temperature() -> None:
    r = parse_legacy_key(
        "cpu/cpu_package", 65.0,
        default_kind=SensorKind.TEMPERATURE,
        cpu_device="Intel i9-12900K",
    )
    assert r is not None
    assert r.group is SensorGroup.CPU
    assert r.device == "Intel i9-12900K"
    assert r.sensor == "cpu_package"
    assert r.label == "Package"
    assert r.kind is SensorKind.TEMPERATURE
    assert r.value == 65.0
    assert r.unit == "°C"
    assert r.source is SourceBackend.LHM


def test_cpu_p_core_label_indexed() -> None:
    r = parse_legacy_key("cpu/p_core_3", 70.0, default_kind=SensorKind.TEMPERATURE)
    assert r is not None
    assert r.label == "Ядро P3"


def test_cpu_e_core_label_indexed() -> None:
    r = parse_legacy_key("cpu/e_core_5", 55.0, default_kind=SensorKind.TEMPERATURE)
    assert r is not None
    assert r.label == "Ядро E5"


def test_cpu_p_core_voltage_label() -> None:
    """Для напряжений per-core LHM имена — это VID, label отражает это."""
    r = parse_legacy_key("cpu/p_core_3", 1.32, default_kind=SensorKind.VOLTAGE)
    assert r is not None
    assert r.label == "VID P3"
    assert r.kind is SensorKind.VOLTAGE
    assert r.unit == "В"


def test_cpu_cpu_core_is_vcore_in_voltages() -> None:
    """cpu/cpu_core в voltages dict — общий Vcore."""
    r = parse_legacy_key("cpu/cpu_core", 1.34, default_kind=SensorKind.VOLTAGE)
    assert r is not None
    assert r.label == "Vcore"


def test_temperature_outlier_filtered() -> None:
    """11°C (motherboard/pcie_x1 на Z690) — битый сенсор, отсекается."""
    r = parse_legacy_key(
        "motherboard/pcie_x1", 11.0, default_kind=SensorKind.TEMPERATURE
    )
    assert r is None


def test_temperature_above_max_filtered() -> None:
    r = parse_legacy_key("cpu/cpu_package", 200.0, default_kind=SensorKind.TEMPERATURE)
    assert r is None


# ─── GPU ─────────────────────────────────────────────────────────────────


def test_gpu_nvidia_hot_spot() -> None:
    r = parse_legacy_key(
        "gpunvidia/gpu_hot_spot", 63.5,
        default_kind=SensorKind.TEMPERATURE,
        gpu_devices={"gpunvidia": "NVIDIA RTX 4070 Ti"},
    )
    assert r is not None
    assert r.group is SensorGroup.GPU
    assert r.device == "NVIDIA RTX 4070 Ti"
    assert r.label == "Hot Spot"


def test_gpu_memory_junction() -> None:
    r = parse_legacy_key(
        "gpunvidia/gpu_memory_junction", 44.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    # Раньше было «Memory Junction», переименовано в «Memory» для
    # компактности GPU-карточки.
    assert r.label == "Memory"


def test_gpu_intel_fallback_device_name() -> None:
    r = parse_legacy_key(
        "gpuintel/gpu_core", 35.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.device == "Intel GPU"  # без override


def test_gpu_amd_voltage() -> None:
    r = parse_legacy_key(
        "gpuamd/gpu_core", 1.0,
        default_kind=SensorKind.VOLTAGE,
    )
    assert r is not None
    assert r.kind is SensorKind.VOLTAGE
    assert r.unit == "В"


# ─── NVML ────────────────────────────────────────────────────────────────


def test_nvml_temperature() -> None:
    r = parse_legacy_key(
        "nvml/0/temperature", 53.0,
        default_kind=SensorKind.TEMPERATURE,
        gpu_devices={"nvml": "NVIDIA RTX 4070 Ti"},
    )
    assert r is not None
    assert r.group is SensorGroup.GPU
    assert r.kind is SensorKind.TEMPERATURE
    assert r.device == "NVIDIA RTX 4070 Ti"
    assert r.source is SourceBackend.NVML


def test_nvml_power_overrides_default_kind() -> None:
    """nvml/0/power_w из voltages-dict должно стать POWER, не VOLTAGE."""
    r = parse_legacy_key(
        "nvml/0/power_w", 211.0,
        default_kind=SensorKind.VOLTAGE,  # из voltages-dict в M3
    )
    assert r is not None
    assert r.kind is SensorKind.POWER
    assert r.unit == "Вт"
    assert r.label == "Мощность"


def test_nvml_clock_graphics() -> None:
    r = parse_legacy_key(
        "nvml/0/clock_graphics", 2910.0,
        default_kind=SensorKind.FREQUENCY,
    )
    assert r is not None
    assert r.kind is SensorKind.FREQUENCY
    assert r.label == "Graphics clock"


def test_nvml_util_gpu_is_load() -> None:
    r = parse_legacy_key(
        "nvml/0/util_gpu", 73.0,
        default_kind=SensorKind.LOAD,
    )
    assert r is not None
    assert r.kind is SensorKind.LOAD
    assert r.unit == "%"


def test_nvml_unknown_metric_returns_none() -> None:
    r = parse_legacy_key(
        "nvml/0/strange", 1.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is None


# ─── Storage ─────────────────────────────────────────────────────────────


def test_storage_lhm_legacy_with_device_name() -> None:
    r = parse_legacy_key(
        "storage/temperature_2", 48.0,
        default_kind=SensorKind.TEMPERATURE,
        storage_lhm_names={"temperature_2": "Samsung SSD 980 PRO 1TB"},
    )
    assert r is not None
    assert r.group is SensorGroup.STORAGE
    assert r.device == "Samsung SSD 980 PRO 1TB"
    assert r.source is SourceBackend.LHM


def test_storage_lhm_legacy_fallback_device_name() -> None:
    """Если LHM не дал имя — fallback на «Накопитель»."""
    r = parse_legacy_key(
        "storage/temperature_2", 48.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.device == "Накопитель"


def test_storage_composite_label_normalised() -> None:
    """`composite_temperature` подписан как «Температура».

    Раньше label был «Composite (среднее NVMe)» — техническое название.
    Пользовательский фидбэк (Astra-итерация F-14): для primary T°
    диска в карточке STORAGE label должен быть простым «Температура»,
    карточка уже подписана «Накопители» + device — model+letter.
    Технические имена сенсоров (composite/temp1) только путают.
    """
    r = parse_legacy_key(
        "storage/composite_temperature", 42.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.label == "Температура"


def test_storage_nvme_hwmon_label_normalised() -> None:
    """Linux hwmon публикует ключ `storage/nvme_composite` — тоже
    должен мапиться в «Температура» (см. LinuxAdapter._read_hwmon)."""
    r = parse_legacy_key(
        "storage/nvme_composite", 35.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.label == "Температура"


def test_storage_smartctl_format_with_metadata() -> None:
    r = parse_legacy_key(
        "storage/nvme0/temperature", 48.0,
        default_kind=SensorKind.TEMPERATURE,
        storage_smartctl_info={
            "nvme0": {
                "model": "Samsung SSD 980 PRO 1TB",
                "type": "SSD M.2 NVMe",
            }
        },
    )
    assert r is not None
    assert r.group is SensorGroup.STORAGE
    assert "Samsung" in r.device
    assert "NVMe" in r.device  # тип в device-имени
    assert r.source is SourceBackend.SMARTCTL


def test_storage_smartctl_fallback_without_metadata() -> None:
    """Без smartctl_info — device = short name (nvme0)."""
    r = parse_legacy_key(
        "storage/nvme0/temperature", 48.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.device == "nvme0"


def test_storage_voltage_kind_returns_none() -> None:
    """Storage у нас только температуры."""
    r = parse_legacy_key(
        "storage/nvme0/temperature", 48.0,
        default_kind=SensorKind.VOLTAGE,
    )
    assert r is None


# ─── Memory ──────────────────────────────────────────────────────────────


def test_memory_dimm_indexed() -> None:
    r = parse_legacy_key("memory/dimm_3", 39.5, default_kind=SensorKind.TEMPERATURE)
    assert r is not None
    assert r.group is SensorGroup.MEMORY
    assert r.label == "DIMM 3"


# ─── Motherboard ─────────────────────────────────────────────────────────


def test_motherboard_vrm_mos() -> None:
    r = parse_legacy_key(
        "motherboard/vrm_mos", 40.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is not None
    assert r.group is SensorGroup.MOTHERBOARD
    assert r.label == "VRM MOS"


def test_motherboard_12v_voltage() -> None:
    r = parse_legacy_key(
        "motherboard/12v", 12.096,
        default_kind=SensorKind.VOLTAGE,
    )
    assert r is not None
    assert r.kind is SensorKind.VOLTAGE
    assert r.label == "+12V"
    assert r.unit == "В"


def test_motherboard_unknown_name_default_label() -> None:
    r = parse_legacy_key(
        "motherboard/some_rare_sensor", 1.5,
        default_kind=SensorKind.VOLTAGE,
    )
    assert r is not None
    assert r.label == "Some rare sensor"


@pytest.mark.parametrize(
    ("key", "expected_label"),
    [
        ("fan/cpu_fan", "ЦП"),
        ("fan/chassis_fan_1", "Шасси 1"),
        ("fan/chassis_fan_6", "Шасси 6"),
        ("fan/water_pump", "Помпа"),
        ("fan/aio_pump", "Помпа"),
        ("fan/gpu_fan", "Графический процессор"),
        ("fan/gpu_fan_2", "Графический процессор 2"),
        ("fan/fan_3", "Шасси 3"),
    ],
)
def test_fan_parser_recognizes_common_names(key: str, expected_label: str) -> None:
    r = parse_legacy_key(key, 1058.0, default_kind=SensorKind.VOLTAGE)
    assert r is not None
    assert r.group is SensorGroup.FANS
    assert r.kind is SensorKind.FAN_RPM
    assert r.unit == "об/мин"
    assert r.value == 1058.0
    assert r.label == expected_label


def test_fan_unknown_name_capitalized_fallback() -> None:
    r = parse_legacy_key(
        "fan/custom_thing", 800.0, default_kind=SensorKind.VOLTAGE,
    )
    assert r is not None
    assert r.label == "Custom thing"


def test_fan_zero_rpm_is_valid_idle_state() -> None:
    """**Регрессия v0.5.3**: 0 RPM — валидный idle (zero-RPM mode), не отбрасывается.

    Раньше 0 RPM отсекались как «выключенный fan», и при idle GPU карточка
    «Вентиляторы» рисовала «нет данных от LHM». См. fix/lhm-fan-zero-rpm.
    """
    r = parse_legacy_key("fan/gpu_fan_1", 0.0, default_kind=SensorKind.VOLTAGE)
    assert r is not None
    assert r.value == 0.0
    assert r.label == "Графический процессор 1"


def test_fan_outlier_rpm_rejected() -> None:
    """Отрицательные значения и >20000 = битый сенсор, отбрасываем."""
    assert parse_legacy_key("fan/cpu_fan", -1.0, default_kind=SensorKind.VOLTAGE) is None
    assert parse_legacy_key("fan/cpu_fan", 99999.0, default_kind=SensorKind.VOLTAGE) is None


@pytest.mark.parametrize(
    "hidden_name",
    ["cpu_i_o", "cpu_system_agent", "cpu_termination", "vref"],
)
def test_motherboard_hidden_sensors_filtered(hidden_name: str) -> None:
    """Шумные служебные railы (CPU I/O, SA, Termination, Vref) не показываются."""
    r = parse_legacy_key(
        f"motherboard/{hidden_name}", 1.0,
        default_kind=SensorKind.VOLTAGE,
    )
    assert r is None


# ─── Unknown / ACPI / metadata ───────────────────────────────────────────


def test_acpi_thermal_zone_returns_none() -> None:
    """Ключи без распознаваемого префикса (ACPI) — не Reading."""
    r = parse_legacy_key(
        r"\thermal zone information(_total)\temperature", 30.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is None


def test_psutil_chip_label_returns_none() -> None:
    """psutil-формат `chip.label` без `/` — не должен публиковаться."""
    r = parse_legacy_key(
        "coretemp.Core 0", 45.0,
        default_kind=SensorKind.TEMPERATURE,
    )
    assert r is None


def test_cpu_avg_becomes_frequency_reading() -> None:
    """cpu_avg — live-средняя частота, превращается в SensorReading
    «Частота (средняя)» в группе CPU. Остальные cpu_min/max/base —
    всё ещё метаданные."""
    r = parse_legacy_key("cpu_avg", 4500.0, default_kind=SensorKind.FREQUENCY)
    assert r is not None
    assert r.kind is SensorKind.FREQUENCY
    assert r.group.value == "cpu"
    assert "Частота" in r.label

    for key in ("cpu_min", "cpu_max", "cpu_base"):
        r = parse_legacy_key(key, 3200.0, default_kind=SensorKind.FREQUENCY)
        assert r is None, f"{key} should be filtered as metadata"


def test_core_indexed_frequency_passes() -> None:
    """core_N в frequencies — обычное per-core значение, не метаданные."""
    r = parse_legacy_key("core_0", 4500.0, default_kind=SensorKind.FREQUENCY)
    # core_0 БЕЗ префикса — мы его не парсим (нет prefix), вернёт None.
    # Per-core клоки от LHM приходят с префиксом cpu/p_core_N или cpu/e_core_N.
    assert r is None


def test_tjmax_threshold_attached_to_temperature() -> None:
    r = parse_legacy_key(
        "cpu/p_core_1", 65.0,
        default_kind=SensorKind.TEMPERATURE,
        thresholds={"cpu/p_core_1": 100.0},
    )
    assert r is not None
    assert r.threshold_crit == 100.0


def test_tjmax_ignored_for_non_temperature() -> None:
    r = parse_legacy_key(
        "cpu/p_core_1", 1.3,
        default_kind=SensorKind.VOLTAGE,
        thresholds={"cpu/p_core_1": 100.0},
    )
    assert r is not None
    assert r.threshold_crit is None
