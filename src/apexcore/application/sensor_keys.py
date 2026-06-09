"""Парсер legacy-ключей сенсоров → ``SensorReading``.

Существующие адаптеры публикуют данные в ``MetricSnapshot`` тремя
flat-словарями: ``temperatures``, ``voltages``, ``frequencies``. Ключи имеют
формат ``{prefix}/{name}`` (LHM-convention) — например ``cpu/p_core_1``,
``gpunvidia/gpu_hot_spot``, ``nvml/0/power_w``.

Эта модель парсинга:

- распознаёт `prefix` → `SensorGroup`;
- по имени datapoint'а определяет `label` (русскоязычный для UI) и `kind`
  (для NVML — переопределяется суффиксом: ``power_w`` → POWER, ``clock_*`` → FREQUENCY);
- неизвестные/ACPI-ключи возвращают ``None`` — они не должны попадать
  в раздел «Датчики».

Все таблицы в одном месте — единый источник истины для UI «Датчики» (M5)
и для будущей миграции `MetricSnapshot` → `SensorSnapshot`.
"""

from __future__ import annotations

import re

from apexcore.domain.sensor_models import (
    SensorGroup,
    SensorKind,
    SensorReading,
    SourceBackend,
)

# ─── Префикс → группа железа ─────────────────────────────────────────────

_GROUP_BY_PREFIX: dict[str, SensorGroup] = {
    "cpu": SensorGroup.CPU,
    "gpunvidia": SensorGroup.GPU,
    "gpuintel": SensorGroup.GPU,
    "gpuamd": SensorGroup.GPU,
    "motherboard": SensorGroup.MOTHERBOARD,
    "memory": SensorGroup.MEMORY,
    "storage": SensorGroup.STORAGE,
    "nvml": SensorGroup.GPU,
}

# Какой backend поставляет данные с этим префиксом (для diagnostics в UI).
_SOURCE_BY_PREFIX: dict[str, SourceBackend] = {
    "cpu": SourceBackend.LHM,
    "gpunvidia": SourceBackend.LHM,
    "gpuintel": SourceBackend.LHM,
    "gpuamd": SourceBackend.LHM,
    "motherboard": SourceBackend.LHM,
    "memory": SourceBackend.LHM,
    "storage": SourceBackend.LHM,
    "nvml": SourceBackend.NVML,
}

_UNIT_BY_KIND: dict[SensorKind, str] = {
    SensorKind.TEMPERATURE: "°C",
    SensorKind.VOLTAGE: "В",
    SensorKind.FREQUENCY: "МГц",
    SensorKind.POWER: "Вт",
    SensorKind.FAN_RPM: "об/мин",
    SensorKind.LOAD: "%",
    SensorKind.USAGE_BYTES: "ГБ",
}

# ─── Human-readable метки для известных datapoint-имён ────────────────────

# Используется когда subkey не индексный (не p_core_N/dimm_N/...).
# Если ключ не найден в карте — берём `name.replace('_', ' ').capitalize()`.
_LABEL_BY_NAME: dict[str, str] = {
    # CPU temps. Первичная температура — короткое «Температура» (в
    # карточке CPU понятно что речь про CPU). Технические имена
    # сенсоров (Tctl, Tdie, Tcore) встречаются только на Linux/AMD
    # где это primary temp; на Windows LHM пишет «cpu_package»
    # который уже мапится в «Package» рядом со «Среднее по ядрам».
    "cpu_package": "Package",
    "package": "Package",
    "core_average": "Среднее по ядрам",
    "core_max": "Макс. ядро",
    "cpu_socket": "Сокет",
    "tctl": "Температура",       # AMD k10temp primary
    "tdie": "Tdie",              # AMD k10temp secondary (offset-corrected)
    "tccd1": "CCD 1",            # Zen3+ per-CCD
    "tccd2": "CCD 2",
    # CPU voltages
    "cpu_core": "Vcore",
    "vid": "VID",
    # GPU
    "gpu_core": "GPU Core",
    "gpu_hot_spot": "Hot Spot",
    "gpu_memory_junction": "Memory",
    "gpu_core_voltage": "Vcore GPU",
    "temperature": "Температура",
    # Motherboard
    "cpu": "CPU socket",
    "m2_1": "M.2 слот 1",
    "m2_2": "M.2 слот 2",
    "pch": "Чипсет (PCH)",
    "pcie_x1": "PCIe x1",
    "system": "Система",
    "vrm_mos": "VRM MOS",
    "12v": "+12V",
    "5v": "+5V",
    "3.3v": "+3.3V",
    "dimm": "DIMM",
    "vcore": "Vcore",
    "vref": "Vref",
    "vsb": "VSB",
    "avcc3": "AVCC3",
    "avsb": "AVSB",
    "cmos_battery": "CMOS-батарея",
    "battery":      "Аккумулятор",
    # Generic ACPI / Super-IO temp1 chips — отображаются как «Температура»
    # в карточке «Материнская плата». На Astra без lm-sensors это
    # acpitz; при наличии Super-IO драйверов (nct6798d / it87) могут
    # быть дополнительные temp-каналы с осмысленными labels.
    "acpitz":       "Температура",
    "temp1":        "Температура",
    "cpu_i_o": "CPU I/O",
    "cpu_system_agent": "System Agent",
    "cpu_termination": "CPU Termination",
    "voltage_1": "Напряжение 1",
    "voltage_2": "Напряжение 2",
    # Storage (LHM legacy format)
    "composite_temperature": "Composite",
    "temperature_2": "Диск 2",
}

# Индексные имена: p_core_<N>, e_core_<N>, dimm_<N>
_P_CORE_RE = re.compile(r"^p_core_(\d+)$")
_E_CORE_RE = re.compile(r"^e_core_(\d+)$")
_CORE_RE = re.compile(r"^core_(\d+)$")
_DIMM_RE = re.compile(r"^dimm_(\d+)$")

# Зарезервированные ключи `frequencies`, не являющиеся показанием датчика
# (это метаданные cpu_base / cpu_max и т.п.). Не публикуем как Reading.
_FREQUENCY_META_KEYS = frozenset(
    {"cpu_avg", "cpu_min", "cpu_max", "cpu_base"}
)

# Шумные служебные motherboard-сенсоры, нерелевантные обычному пользователю
# (внутренние CPU IO/SA/Termination railsы, Vref). Скрываем из карточки
# «Системные сенсоры». Если кому-то понадобятся для отладки — добавим
# debug-режим или раздел «Все сенсоры» в M6.
_MOTHERBOARD_HIDDEN_NAMES = frozenset(
    {
        "cpu_i_o",
        "cpu_system_agent",
        "cpu_termination",
        "vref",
    }
)

# Outliers — то же эвристическое окно, что в render.py:_group_temps.
_TEMP_MIN = 15.0
_TEMP_MAX = 130.0


def parse_legacy_key(
    key: str,
    value: float,
    *,
    default_kind: SensorKind,
    cpu_device: str = "CPU",
    gpu_devices: dict[str, str] | None = None,
    thresholds: dict[str, float] | None = None,
    storage_lhm_names: dict[str, str] | None = None,
    storage_smartctl_info: dict[str, dict[str, str]] | None = None,
) -> SensorReading | None:
    """Преобразовать legacy-ключ `MetricSnapshot` в ``SensorReading``.

    :param key: исходный ключ, например ``cpu/p_core_1`` / ``nvml/0/power_w``.
    :param value: численное значение.
    :param default_kind: подсказка из исходного словаря (TEMPERATURE для
        ``temperatures``, VOLTAGE для ``voltages``, FREQUENCY для
        ``frequencies``). Для NVML-ключей может переопределяться суффиксом.
    :param cpu_device: модель CPU из `SystemInfo` для подписи карточки.
    :param gpu_devices: ``{"gpunvidia": "NVIDIA RTX 4070 Ti", "gpuintel": "Intel UHD 770"}``.
    :param thresholds: ``{key: tjmax_celsius}`` из ``read_lhm_tjmax()``,
        для подкраски температурных ячеек.
    :param storage_lhm_names: ``{normalized_sensor_name: device_name}`` из
        ``read_lhm_storage_names()`` — для подмены generic «Накопитель» на
        реальную модель в LHM-ключах вида `storage/composite_temperature`.
    :param storage_smartctl_info: ``{short_name: {"model": str, "type": str}}``
        из ``read_smartctl_devices_info()`` — для smartctl-ключей вида
        `storage/nvme0/temperature` подменяет device на «<model> · <type>».

    Возвращает ``None``, если ключ не распознан (ACPI thermal zone, psutil
    chip-имена, метаданные cpu_base/cpu_max). Это не ошибка — такие данные
    просто не должны попадать в раздел «Датчики».
    """
    # Метаданные частот без префикса. ``cpu_avg`` мы НЕ отбрасываем —
    # это live-средняя частота CPU, превращается в SensorReading чтобы
    # показать пользователю текущую частоту процессора в карточке CPU.
    if "/" not in key:
        if key == "cpu_avg":
            return _parse_cpu_avg_freq(value, cpu_device=cpu_device)
        if key in _FREQUENCY_META_KEYS:
            return None

    # Fan/RPM ключи — отдельная группа FANS.
    if key.startswith("fan/"):
        return _parse_fan(key, value)

    # CPU power ключи (cpu_power/package, cpu_power/cores) — отдельная
    # обработка чтобы создать SensorReading с kind=POWER в группе CPU.
    if key.startswith("cpu_power/"):
        return _parse_cpu_power(key, value, cpu_device=cpu_device)

    # LHM `gpunvidia/gpu_core` температура — дубль NVML temperature.
    # Скрываем чтобы карточка GPU не показывала две одинаковые строки
    # «GPU Core 54°» и «GPU температура 54°» рядом. Voltage (vcore) и
    # другие LHM-датчики (Hot Spot, Memory) остаются как есть.
    if (
        key == "gpunvidia/gpu_core"
        and default_kind is SensorKind.TEMPERATURE
    ):
        return None

    # NVML kind переопределяется суффиксом.
    if key.startswith("nvml/"):
        return _parse_nvml(key, value, gpu_devices=gpu_devices or {})

    # AMD GPU (amdgpu/radeon hwmon) — kind определяется суффиксом, как
    # для NVML. На Linux без LHM это единственный источник GPU-метрик:
    # temp1=edge, temp2=junction, temp3=mem, power1_average=PPT,
    # in0=vddgfx, in1=vddnb, freq1=sclk, fan1=GPU fan.
    if key.startswith("gpuamd/") or key.startswith("gpuintel/"):
        r = _parse_gpu_vendor(key, value, gpu_devices=gpu_devices or {})
        if r is not None:
            return r
        # Fallback в общий 2-segment handler если ключ не покрыт mapping'ом.

    # Storage может быть и `storage/temperature_2` (LHM) и
    # `storage/nvme0/temperature` (smartctl).
    if key.startswith("storage/"):
        return _parse_storage(
            key, value,
            default_kind=default_kind,
            lhm_names=storage_lhm_names or {},
            smartctl_info=storage_smartctl_info or {},
        )

    # Обычный 2-сегментный ключ {prefix}/{name}.
    prefix, _, name = key.partition("/")
    if not name:
        return None
    group = _GROUP_BY_PREFIX.get(prefix)
    if group is None:
        return None
    # Скрытые motherboard-датчики (CPU I/O, System Agent, Termination, Vref).
    if prefix == "motherboard" and name in _MOTHERBOARD_HIDDEN_NAMES:
        return None
    # Outlier-filter только для температур (для напряжений 0..12+ В норма,
    # для частот 200..6000 МГц норма).
    if default_kind is SensorKind.TEMPERATURE and not (
        _TEMP_MIN <= value <= _TEMP_MAX
    ):
        return None

    label = _resolve_label(name, group=group, default_kind=default_kind)
    device = _resolve_device(prefix, group=group, cpu_device=cpu_device, gpu_devices=gpu_devices or {})
    threshold_crit = (thresholds or {}).get(key) if default_kind is SensorKind.TEMPERATURE else None
    return SensorReading(
        group=group,
        device=device,
        sensor=name,
        label=label,
        kind=default_kind,
        value=value,
        unit=_UNIT_BY_KIND[default_kind],
        threshold_warn=None,
        threshold_crit=threshold_crit,
        source=_SOURCE_BY_PREFIX[prefix],
    )


# ─── helpers ──────────────────────────────────────────────────────────────


def _parse_cpu_avg_freq(value: float, *, cpu_device: str) -> SensorReading:
    """``cpu_avg`` (МГц) → SensorReading «Частота» в группе CPU.

    Источник определяется наличием LHM: WindowsAdapter ставит cpu_avg
    из ``read_lhm_cpu_clocks()`` (LHM) или из PDH (PSUTIL fallback).
    Здесь мы не знаем источник — помечаем как LHM, потому что без LHM
    cpu_avg равен базовой частоте и малоинформативен. Для UI важно
    лишь то что значение есть.

    Лейбл — просто «Частота»: значение live и обновляется каждый тик,
    «(средняя)» в скобках сбивает с толку (создаёт впечатление что
    это не сейчас, а агрегат за интервал).
    """
    return SensorReading(
        group=SensorGroup.CPU,
        device=cpu_device,
        sensor="cpu_avg",
        label="Частота",
        kind=SensorKind.FREQUENCY,
        value=value,
        unit=_UNIT_BY_KIND[SensorKind.FREQUENCY],
        source=SourceBackend.LHM,
    )


def _parse_cpu_power(
    key: str, value: float, *, cpu_device: str,
) -> SensorReading | None:
    """``cpu_power/<name>`` → SensorReading c kind=POWER в группе CPU.

    Имена: ``package`` (общее энергопотребление пакета), ``cores``,
    ``graphics``, ``memory``. Для UI показываем только ``package``
    как «Мощность» — аналогично GPU-карточке. Остальные имена
    отбрасываются как не информативные для рядового пользователя
    (cores/iGPU/memory subdomains).
    """
    parts = key.split("/", 1)
    if len(parts) != 2:
        return None
    name = parts[1]
    if name != "package":
        return None
    return SensorReading(
        group=SensorGroup.CPU,
        device=cpu_device,
        sensor="cpu_package_power",
        label="Мощность",
        kind=SensorKind.POWER,
        value=value,
        unit=_UNIT_BY_KIND[SensorKind.POWER],
        source=SourceBackend.LHM,
    )


def _resolve_label(
    name: str,
    *,
    group: SensorGroup,
    default_kind: SensorKind,
) -> str:
    """Подобрать русское имя для UI."""
    # Индексные имена ядер
    m = _P_CORE_RE.match(name)
    if m:
        idx = int(m.group(1))
        return f"VID P{idx}" if default_kind is SensorKind.VOLTAGE else f"Ядро P{idx}"
    m = _E_CORE_RE.match(name)
    if m:
        idx = int(m.group(1))
        return f"VID E{idx}" if default_kind is SensorKind.VOLTAGE else f"Ядро E{idx}"
    m = _CORE_RE.match(name)
    if m:
        return f"Ядро {m.group(1)}"
    m = _DIMM_RE.match(name)
    if m:
        return f"DIMM {m.group(1)}"
    # Явные карты — для motherboard `cpu` стоит подпись «CPU socket», а
    # не «Cpu» из default-формата. Lookup case-insensitive: hwmon labels
    # на разных kernel приходят в разном регистре (`Tctl` vs `tctl`,
    # `edge` vs `Edge`).
    if name in _LABEL_BY_NAME:
        return _LABEL_BY_NAME[name]
    lower = name.lower()
    if lower in _LABEL_BY_NAME:
        return _LABEL_BY_NAME[lower]
    # Fallback: заменить подчёркивания на пробелы + capitalize первого слова.
    return name.replace("_", " ").capitalize()


def _resolve_device(
    prefix: str,
    *,
    group: SensorGroup,
    cpu_device: str,
    gpu_devices: dict[str, str],
) -> str:
    """Имя устройства для группировки в UI-карточке."""
    if group is SensorGroup.CPU:
        return cpu_device
    if group is SensorGroup.GPU:
        # Приоритет: явный override для префикса → fallback.
        if prefix in gpu_devices:
            return gpu_devices[prefix]
        return {
            "gpunvidia": "NVIDIA GPU",
            "gpuintel": "Intel GPU",
            "gpuamd": "AMD GPU",
        }.get(prefix, "GPU")
    if group is SensorGroup.MOTHERBOARD:
        return "Материнская плата"
    if group is SensorGroup.MEMORY:
        return "RAM"
    if group is SensorGroup.STORAGE:
        return "Накопитель"
    return "—"


def _parse_nvml(
    key: str,
    value: float,
    *,
    gpu_devices: dict[str, str],
) -> SensorReading | None:
    """Распарсить `nvml/<idx>/<metric>` — kind определяется суффиксом."""
    parts = key.split("/")
    if len(parts) != 3:
        return None
    _, idx_str, metric = parts
    try:
        int(idx_str)
    except ValueError:
        return None

    if metric == "temperature":
        kind = SensorKind.TEMPERATURE
        # Просто «GPU температура» — раньше был суффикс «(NVML)» чтобы
        # отличить от LHM gpu_core, но теперь LHM gpu_core скрыт как
        # дубль (см. parse_legacy_key), и уточнять источник незачем.
        label = "GPU температура"
    elif metric == "power_w":
        kind = SensorKind.POWER
        label = "Мощность"
    elif metric in ("clock_graphics", "clock_sm"):
        kind = SensorKind.FREQUENCY
        label = "Graphics clock"
    elif metric == "clock_memory":
        kind = SensorKind.FREQUENCY
        label = "Memory clock"
    elif metric == "util_gpu":
        kind = SensorKind.LOAD
        label = "Загрузка GPU"
    elif metric == "util_mem":
        kind = SensorKind.LOAD
        label = "Загрузка памяти"
    else:
        return None

    if kind is SensorKind.TEMPERATURE and not (_TEMP_MIN <= value <= _TEMP_MAX):
        return None

    device = gpu_devices.get("nvml", gpu_devices.get("gpunvidia", f"GPU {idx_str}"))
    return SensorReading(
        group=SensorGroup.GPU,
        device=device,
        sensor=f"nvml_{idx_str}_{metric}",
        label=label,
        kind=kind,
        value=value,
        unit=_UNIT_BY_KIND[kind],
        source=SourceBackend.NVML,
    )


def _parse_gpu_vendor(
    key: str,
    value: float,
    *,
    gpu_devices: dict[str, str],
) -> SensorReading | None:
    """Распарсить ``gpuamd/<metric>`` или ``gpuintel/<metric>``.

    kind определяется semantic-именем metric'а (по аналогии с
    ``_parse_nvml``). Используется на Linux где LHM нет, и единственный
    источник GPU-метрик — kernel hwmon `amdgpu` / `i915` / `xe`:
    temp1=edge, temp2=junction/hot_spot, temp3=mem, power1_average=PPT,
    in0=vddgfx, in1=vddnb, freq1=sclk, freq2=mclk, fan1=GPU fan.

    Возвращает ``None`` для неизвестных metric — тогда parse_legacy_key
    падает обратно в общий 2-segment handler.
    """
    parts = key.split("/")
    if len(parts) < 2:
        return None
    prefix = parts[0]      # gpuamd / gpuintel
    # Lowercased lookup: hwmon labels могут быть в любом регистре
    # (например `power1_label=PPT` uppercase). Без нормализации
    # mapping["ppt"] не находил metric="PPT" → kind определялся
    # дефолтом (VOLTAGE из voltages-dict) и «Ppt 29.04 В» в UI
    # вместо корректного «Мощность 29.04 Вт».
    metric = parts[-1].lower()

    # Подобрать device-имя: NVML / sys_info берёт GPU model на Linux.
    if prefix == "gpuamd":
        device = gpu_devices.get("gpuamd", "AMD GPU")
    else:  # gpuintel
        device = gpu_devices.get("gpuintel", "Intel GPU")

    # Mapping metric → (kind, label). Покрывает Linux-hwmon имена:
    # /sys/class/hwmon/<n>/temp*_label, in*_label, freq*_label,
    # power*_label, fan*_label. LHM-имена (gpu_core / gpu_clock /
    # gpu_core_voltage) сюда не входят — на Windows LHM их обрабатывает
    # generic 2-segment handler, который и так отнесёт к группе GPU.
    mapping: dict[str, tuple[SensorKind, str]] = {
        # Температуры (hwmon). Первичная T° GPU — короткое «Температура»
        # (в карточке GPU так понятно), вторичные (Hot Spot / Memory)
        # сохраняют семантичные имена для различения.
        "edge":               (SensorKind.TEMPERATURE, "Температура"),
        "junction":           (SensorKind.TEMPERATURE, "Hot Spot"),
        "hot_spot":           (SensorKind.TEMPERATURE, "Hot Spot"),
        "mem":                (SensorKind.TEMPERATURE, "Memory"),
        "memory":             (SensorKind.TEMPERATURE, "Memory"),
        # Напряжения (амплитуда уже сконвертирована в Вольты в адаптере)
        "vddgfx":             (SensorKind.VOLTAGE, "Vcore GPU"),
        "vddnb":              (SensorKind.VOLTAGE, "VDDNB"),
        # Мощность (уже сконвертирована в Ватты в адаптере)
        "power_average":      (SensorKind.POWER, "Мощность"),
        "ppt":                (SensorKind.POWER, "Мощность"),
        # Частоты (уже сконвертированы в МГц в адаптере)
        "sclk":               (SensorKind.FREQUENCY, "Graphics clock"),
        "mclk":               (SensorKind.FREQUENCY, "Memory clock"),
    }
    if metric not in mapping:
        return None
    kind, label = mapping[metric]

    # Outlier-filter для температур (общая эвристика).
    if kind is SensorKind.TEMPERATURE and not (_TEMP_MIN <= value <= _TEMP_MAX):
        return None

    return SensorReading(
        group=SensorGroup.GPU,
        device=device,
        sensor=metric,
        label=label,
        kind=kind,
        value=value,
        unit=_UNIT_BY_KIND[kind],
        # На Linux hwmon — это **не** LHM источник, и не NVML. Используем
        # HWMON badge чтобы UI показывал реальный path данных.
        source=SourceBackend.HWMON,
    )


def _parse_storage(
    key: str,
    value: float,
    *,
    default_kind: SensorKind,
    lhm_names: dict[str, str],
    smartctl_info: dict[str, dict[str, str]],
) -> SensorReading | None:
    """Распарсить `storage/...` ключи.

    Источники:
    - LHM legacy: ``storage/composite_temperature``, ``storage/temperature_2``
      → device берётся из ``lhm_names[sensor_name]`` если доступно,
      иначе fallback «Накопитель».
    - smartctl: ``storage/nvme0/temperature`` → device = «<model> · <type>»
      из ``smartctl_info[short_name]`` если доступно, иначе short_name.

    Composite (на NVMe SSD) — это NVMe Composite Temperature из NVMe-Health
    спецификации: усреднённая температура контроллера и NAND-чипов.
    Самый репрезентативный показатель «здоровья» NVMe-диска.
    """
    if default_kind is not SensorKind.TEMPERATURE:
        return None
    if not (_TEMP_MIN <= value <= _TEMP_MAX):
        return None
    parts = key.split("/")
    if len(parts) == 2:
        # LHM legacy: storage/composite_temperature, storage/temperature_2.
        # На Linux сюда же попадают hwmon-температуры дисков:
        # storage/nvme_composite, storage/drivetemp_temp1 (см.
        # LinuxAdapter._read_hwmon — disk-чипы публикуют здесь, потому что
        # smartctl на kernel 6.1+ требует CAP_SYS_ADMIN для SMART log
        # и недоступен для unprivileged user).
        name = parts[1]
        label = _resolve_storage_label(name)
        device = lhm_names.get(name)
        source = SourceBackend.LHM
        if device is None:
            # Fallback для Linux hwmon: lhm_names всегда пуст (LHM нет).
            # Если smartctl --scan нашёл ровно одно устройство (типичный
            # single-NVMe laptop) — используем его model + type как device.
            # source помечаем HWMON чтобы badge в UI отражал реальный
            # источник (hwmon, не LHM/smartctl). Без этого fallback'а
            # generic «Накопитель» отбрасывался в sensors.js (см.
            # `enrichStorageDevice` — orphan-readings без inventory-матча
            # фильтруются), и Linux-пользователь не видел NVMe-карточки.
            source = SourceBackend.HWMON
            if len(smartctl_info) == 1:
                single = next(iter(smartctl_info.values()))
                model = single.get("model", "")
                dev_type = single.get("type", "")
                if model and dev_type:
                    device = f"{model} · {dev_type}"
                elif model:
                    device = model
                else:
                    device = "Накопитель"
            else:
                device = "Накопитель"
        return SensorReading(
            group=SensorGroup.STORAGE,
            device=device,
            sensor=name,
            label=label,
            kind=SensorKind.TEMPERATURE,
            value=value,
            unit="°C",
            source=source,
        )
    if len(parts) == 3:
        # smartctl: storage/nvme0/temperature
        _, dev, metric = parts
        if metric != "temperature":
            return None
        info = smartctl_info.get(dev) or {}
        model = info.get("model", "")
        dev_type = info.get("type", "")
        if model and dev_type:
            device = f"{model} · {dev_type}"
        elif model:
            device = model
        elif dev_type:
            device = f"{dev} · {dev_type}"
        else:
            device = dev
        return SensorReading(
            group=SensorGroup.STORAGE,
            device=device,
            sensor="temperature",
            label="Температура",
            kind=SensorKind.TEMPERATURE,
            value=value,
            unit="°C",
            source=SourceBackend.SMARTCTL,
        )
    return None


def _parse_fan(key: str, value: float) -> SensorReading | None:
    """Распарсить ``fan/...`` ключ → ``SensorReading`` группы FANS.

    Поддерживает форматы:
    - ``fan/cpu_fan`` (одна секция)
    - ``fan/<hw_prefix>/<sensor>`` (две секции, если LHM имел коллизию имён)

    Label определяется по эвристикам в ``_resolve_fan_label``.
    """
    parts = key.split("/")
    if len(parts) < 2:
        return None
    # Последний сегмент — имя сенсора, остальное — hardware-context.
    name = parts[-1]
    # 0 RPM — валидное состояние (idle GPU / zero-RPM mode), показываем.
    # Отрицательные значения и outlier'ы >20000 RPM — пропускаем (sensor error).
    if value < 0 or value > 20000:
        return None
    label = _resolve_fan_label(name)
    return SensorReading(
        group=SensorGroup.FANS,
        device="Вентиляторы",
        sensor=key.replace("/", "_"),
        label=label,
        kind=SensorKind.FAN_RPM,
        value=value,
        unit="об/мин",
        source=SourceBackend.LHM,
    )


def _resolve_fan_label(name: str) -> str:
    """LHM-имя вентилятора → русская метка для UI.

    LHM на ASUS/Gigabyte/MSI публикует имена вида:
    ``CPU Fan``, ``Chassis Fan #1``, ``Water Pump``, ``Fan #N``, ``GPU Fan``.
    Здесь маппим самые частые случаи. Неузнанные имена остаются как есть
    (с капитализацией).
    """
    n = name.lower()
    # Помпа/AIO.
    if "pump" in n or "помпа" in n or "aio" in n:
        return "Помпа"
    # CPU-кулер.
    if "cpu" in n and ("fan" in n or "cooler" in n):
        return "ЦП"
    # GPU-кулеры (часто несколько).
    if "gpu" in n:
        m = re.search(r"(\d+)", n)
        return f"Графический процессор {m.group(1)}" if m else "Графический процессор"
    # Корпусные вентиляторы — ищем номер.
    m = re.search(r"(?:chassis|case|sys)[_\s]*fan[_\s#]*(\d+)", n)
    if m:
        return f"Шасси {m.group(1)}"
    # Чистый Fan #N или fan_N — может быть и CPU, и chassis; считаем шасси.
    m = re.match(r"^fan[_\s#]*(\d+)$", n)
    if m:
        return f"Шасси {m.group(1)}"
    return name.replace("_", " ").strip().capitalize() or "Вентилятор"


def _resolve_storage_label(name: str) -> str:
    """Понятная метка для LHM-ключей storage.

    Все основные варианты (single primary temp на одном устройстве)
    мапятся в «Температура» — в Sensors-карточке уже понятно что
    это диск, технические имена сенсоров (`composite_temperature`,
    `nvme_composite`, `temp1`) только путают пользователя.
    Multi-disk LHM имена `temperature_2`, `temperature_3` оставляют
    индекс — он различает физические устройства внутри группы STORAGE.
    """
    lower = name.lower()
    # Primary NVMe Composite — на single-NVMe ноутбуке это единственная
    # T° диска. LHM-формат: `composite_temperature`. Linux hwmon:
    # `nvme_composite` (см. LinuxAdapter._read_hwmon → `storage/nvme_composite`).
    if lower in ("composite_temperature", "nvme_composite", "temperature", "temp1"):
        return "Температура"
    if lower.startswith("temperature_"):
        suffix = name.split("_", 1)[1]
        return f"Температура {suffix}"
    return _LABEL_BY_NAME.get(name, _LABEL_BY_NAME.get(lower, name.replace("_", " ").capitalize()))
