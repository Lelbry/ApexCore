"""Чтение HWiNFO Shared Memory (``Global\\HWiNFO_SENS_SM2``).

HWiNFO — индустриальный референс мониторинга железа на Windows
(подписанный драйвер ``HWiNFO64A.SYS`` совместим с HVCI/Memory
Integrity Win11 24H2/25H2). Если у пользователя HWiNFO запущен в трее
с включённой Shared Memory Support — apexcore получает данные
силиконового качества **без admin** и **без зависимости** от
WinRing0 / PawnIO.

Формат SHM публично документирован с HWiNFO v7.0 (March 2021). Лимит
free-версии — 12 ч непрерывной работы SHM, после чего пользователь
должен перезапустить HWiNFO. Pro Engineer / Corporate — без лимитов.

Структуры C (из gist namazso, c47-dev/hwinfo-overlay, warbou/
hwinfo-oled-monitor):

- ``HWiNFO_SENSORS_SHARED_MEM2`` — заголовок (магия ``'HWiS'``);
- ``HWiNFO_SENSORS_SENSOR_ELEMENT`` — sensor group (CPU/GPU/MB);
- ``HWiNFO_SENSORS_READING_ELEMENT`` — одно показание (temp/voltage/...).

См. ``docs/research`` §3.1 для полной таблицы покрытия. Имена сенсоров
от HWiNFO приводятся к apexcore-схеме через
``shm._common.normalize_sensor_key`` — это критично для совместимости
с ``thermal_watchdog._is_cpu_temp_key``.
"""

from __future__ import annotations

import ctypes
import logging

from apexcore.infrastructure.sensors.shm._common import (
    normalize_sensor_key,
    normalize_voltage_key,
    open_shm,
)

logger = logging.getLogger(__name__)

# Имя SHM-объекта (Global\HWiNFO_SENS_SM2). См. docs/research §3.1.
_HWINFO_SHM_NAME = "Global\\HWiNFO_SENS_SM2"

# Магия заголовка: 'HWiS' → little-endian DWORD 0x53696857.
_HWINFO_MAGIC = 0x53696857

# Тип reading'а: 0=none, 1=temp, 2=voltage, 3=fan, 4=current, 5=power, 6=clock, 7=usage, 8=other.
_SENSOR_TYPE_TEMP = 1
_SENSOR_TYPE_VOLT = 2


class _HwinfoHeader(ctypes.Structure):
    """Заголовок Shared Memory HWiNFO v2."""

    _fields_ = (
        ("dwSignature", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwRevision", ctypes.c_uint32),
        ("pollTime", ctypes.c_int64),  # __time64_t (8 bytes)
        ("dwOffsetOfSensorSection", ctypes.c_uint32),
        ("dwSizeOfSensorElement", ctypes.c_uint32),
        ("dwNumSensorElements", ctypes.c_uint32),
        ("dwOffsetOfReadingSection", ctypes.c_uint32),
        ("dwSizeOfReadingElement", ctypes.c_uint32),
        ("dwNumReadingElements", ctypes.c_uint32),
    )


class _HwinfoSensorElement(ctypes.Structure):
    """Sensor group — например «CPU [#0]: Core (Ryzen 5800X)»."""

    _fields_ = (
        ("dwSensorID", ctypes.c_uint32),
        ("dwSensorInst", ctypes.c_uint32),
        ("szSensorNameOrig", ctypes.c_char * 128),
        ("szSensorNameUser", ctypes.c_char * 128),
    )


class _HwinfoReadingElement(ctypes.Structure):
    """Одно показание — temperature/voltage/fan-rpm/...

    Размер фиксированный, идёт массивом сразу за sensor-section.
    """

    _fields_ = (
        ("tReading", ctypes.c_uint32),  # SENSOR_READING_TYPE
        ("dwSensorIndex", ctypes.c_uint32),
        ("dwReadingID", ctypes.c_uint32),
        ("szLabelOrig", ctypes.c_char * 128),
        ("szLabelUser", ctypes.c_char * 128),
        ("szUnit", ctypes.c_char * 16),
        ("Value", ctypes.c_double),
        ("ValueMin", ctypes.c_double),
        ("ValueMax", ctypes.c_double),
        ("ValueAvg", ctypes.c_double),
    )


def read_hwinfo_sensors() -> dict[str, float]:
    """Прочитать температуры HWiNFO и привести к apexcore-схеме ключей.

    Возвращает ``{"cpu/package": 65.4, "cpu/core_0": 60.0, ...}``. Любые
    ошибки → пустой словарь (graceful degrade).

    Voltage в P0 **не извлекается** — план §3.3 говорит: voltage из
    SHM-источников — это P1, потому что текущий ``WindowsAdapter.
    _read_sensors`` ожидает voltages из LHM (он публикует Vcore только
    на CPU-узле). HWiNFO публикует Vcore как отдельный sensor — это
    можно интегрировать позже без breaking changes.
    """
    return read_hwinfo_temperatures_and_voltages()[0]


def read_hwinfo_temperatures_and_voltages() -> tuple[dict[str, float], dict[str, float]]:
    """Прочитать температуры **и** напряжения HWiNFO в одном проходе.

    Используется ``WindowsAdapter._read_sensors`` если HWiNFO активен —
    он покрывает и Vcore CPU (raw label «CPU Core Voltage» / «Vcore»).
    Voltages нормализуются через тот же ``normalize_sensor_key``, что
    и температуры (CPU/GPU паттерны включают voltage-формы).

    При недоступности SHM или формате — пара пустых словарей.
    """
    with open_shm(_HWINFO_SHM_NAME) as raw:
        if raw is None or len(raw) < ctypes.sizeof(_HwinfoHeader):
            return {}, {}
        try:
            return _parse_hwinfo(raw)
        except Exception as exc:
            logger.debug("HWiNFO SHM parse upal: %s", exc)
            return {}, {}


def _parse_hwinfo(raw: bytes) -> tuple[dict[str, float], dict[str, float]]:
    """Парсинг HWiNFO SHM: header → sensor-section → reading-section.

    Возвращает ``(temps, voltages)``. Нераспознанные имена сенсоров
    отбрасываются (см. ``normalize_sensor_key`` контракт).
    """
    header = _HwinfoHeader.from_buffer_copy(raw[: ctypes.sizeof(_HwinfoHeader)])
    if header.dwSignature != _HWINFO_MAGIC:
        logger.debug(
            "HWiNFO SHM: неверная магия 0x%08x (ждали 0x%08x)",
            header.dwSignature,
            _HWINFO_MAGIC,
        )
        return {}, {}

    reading_size = int(header.dwSizeOfReadingElement)
    if reading_size <= 0:
        return {}, {}

    # Sensor section — массив SensorElement'ов для маппинга
    # «dwSensorIndex → имя группы» (например «CPU [#0]: Core»). В P0
    # apexcore'у это не нужно — мы используем только label каждого
    # reading'а через normalize_sensor_key. Сохранили offset на будущее.

    reading_offset = int(header.dwOffsetOfReadingSection)
    num_readings = int(header.dwNumReadingElements)

    temps: dict[str, float] = {}
    voltages: dict[str, float] = {}

    for i in range(num_readings):
        start = reading_offset + i * reading_size
        end = start + reading_size
        if end > len(raw):
            break
        # Копируем — не делаем from_buffer (raw — immutable bytes).
        reading = _HwinfoReadingElement.from_buffer_copy(
            raw[start : start + ctypes.sizeof(_HwinfoReadingElement)]
        )
        if reading.tReading not in (_SENSOR_TYPE_TEMP, _SENSOR_TYPE_VOLT):
            continue
        label = _decode_cstring(reading.szLabelOrig)
        value = float(reading.Value)
        if reading.tReading == _SENSOR_TYPE_TEMP:
            # HWiNFO иногда публикует voltage с tReading=TEMP (старые билды
            # при кастомных user-label'ах) — отсеиваем по тексту label'а.
            if "voltage" in label.lower() or "vcore" in label.lower():
                continue
            key = normalize_sensor_key(label)
            if key is None:
                continue
            temps[key] = value
        else:
            # P1.5: voltages используют отдельный normalizer — у них свой
            # набор raw label'ов («CPU Core Voltage», «CPU SoC Voltage»),
            # который не пересекается с temperature-паттернами.
            key = normalize_voltage_key(label)
            if key is None:
                continue
            voltages[key] = value
    return temps, voltages


def _decode_cstring(buf: bytes) -> str:
    """Декодировать null-terminated C-string из ctypes c_char array."""
    if not buf:
        return ""
    end = buf.find(b"\x00")
    raw = buf if end < 0 else buf[:end]
    try:
        return raw.decode("utf-8", errors="replace").strip()
    except (UnicodeDecodeError, AttributeError):
        # utf-8 с errors="replace" не должен бросать, но на корявых
        # latin-1 байтах подстрахуемся.
        return raw.decode("latin-1", errors="replace").strip()


__all__ = [
    "read_hwinfo_sensors",
    "read_hwinfo_temperatures_and_voltages",
]
