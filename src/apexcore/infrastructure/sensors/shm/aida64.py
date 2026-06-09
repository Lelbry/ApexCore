"""Чтение AIDA64 Shared Memory (``AIDA64_SensorValues``).

AIDA64 — коммерческий ($40 Extreme / $200 Engineer) hardware-monitor с
подписанным драйвером ``kerneld.x64``. v7.60 (March 2025) обновлён в
сотрудничестве с Microsoft под HVCI/Memory Integrity. Покрытие: CPU,
GPU, материнка, RAM, диски, fan'ы, мощность, напряжения — на уровне
silicon (через MSR/IPMI/SMU).

SHM-интерфейс публично документирован на aida64.co.uk и helpmax.net.
В отличие от HWiNFO (бинарный struct) формат AIDA64 — длинная
**нуль-терминированная строка с XML-тегами** (но не валидный XML —
нет корневого элемента, encoding-deklaration'а и pre-amble'а). По этой
причине используем regex-парсер, а **не** ``xml.etree`` — последний
требует well-formed документа и упадёт на первом же sensor'е.

Формат одной записи (повторяется по всем сенсорам):

.. code-block:: text

    <temp><id>SCPU</id><label>CPU</label><value>56.0</value></temp>
    <temp><id>SCC-1</id><label>CPU Core #1</label><value>54.0</value></temp>
    <volt><id>VCPU</id><label>CPU Core</label><value>1.21</value></volt>
    <fan><id>FCPU</id><label>CPU</label><value>1234</value></fan>

Корневые теги: ``sys``, ``temp``, ``fan``, ``duty``, ``volt``, ``pwr``,
``curr``. Каждая запись имеет ``<id>``, ``<label>``, ``<value>`` —
порядок стабилен, но мы парсим robustly через group-named regex.

Лицензия. Сам AIDA64 платный, **SHM-интерфейс описан в публичной
документации**, использование чтения из MIT-приложения разрешено без
SDK-лицензирования (как и для HWiNFO/CoreTemp). Единственное условие —
у пользователя должна быть своя лицензия AIDA64 и AIDA64 должен быть
запущен в трее. См. ``docs/research`` §3.2.

ARM64 не поддерживается AIDA64 — на ARM64 reader просто вернёт пустой
словарь (probe не обнаружит SHM).
"""

from __future__ import annotations

import logging
import re

from apexcore.infrastructure.sensors.shm._common import (
    normalize_sensor_key,
    normalize_voltage_key,
    open_shm,
)

logger = logging.getLogger(__name__)

# Имя SHM-объекта (см. docs/research §3.2).
_AIDA64_SHM_NAME = "AIDA64_SensorValues"

# Корневые теги AIDA64. ``sys`` — общие system-метрики (FreeMemory и пр.),
# их не парсим. ``duty`` — fan duty %, ``curr`` — токи (А), ``pwr`` — Вт.
# В P1 берём только temp/volt; fan/pwr/curr — для будущих расширений
# (SensorSnapshot в M4 откроет полноценный SensorReading).
_AIDA64_TAG_TEMP = "temp"
_AIDA64_TAG_VOLT = "volt"

# Regex для одной записи. AIDA64 не экранирует амперсанды/кавычки внутри
# label'ов в современной версии, но на старых сборках встречались
# `&amp;` / `&lt;` — заменяем при decode. ``(?:...)`` non-capturing для
# выбора корневого тега, чтобы не плодить группы. ``re.DOTALL`` нужен
# в редких случаях когда label содержит \n (battery-метрики).
_AIDA64_ENTRY_RE = re.compile(
    r"<(?P<root>temp|volt)>"
    r"\s*<id>(?P<id>[^<]*)</id>"
    r"\s*<label>(?P<label>[^<]*)</label>"
    r"\s*<value>(?P<value>[^<]*)</value>"
    r"\s*</(?P=root)>",
    re.IGNORECASE | re.DOTALL,
)


def read_aida64_sensors() -> dict[str, float]:
    """Прочитать температуры AIDA64 и привести к apexcore-схеме ключей.

    Возвращает ``{"cpu/package": 56.0, "cpu/core_1": 54.0, ...}``. При
    недоступности SHM, формате или ошибках парсинга — пустой словарь
    (graceful degrade). Voltages парсятся отдельно через
    ``read_aida64_temperatures_and_voltages``.
    """
    return read_aida64_temperatures_and_voltages()[0]


def read_aida64_temperatures_and_voltages() -> tuple[dict[str, float], dict[str, float]]:
    """Прочитать температуры **и** напряжения AIDA64 в одном проходе.

    Используется ``WindowsAdapter._read_sensors`` если AIDA64 активен —
    AIDA64 публикует Vcore CPU (raw label «CPU VCORE» / «CPU Core») и
    другие напряжения мат-платы. Voltages нормализуются через тот же
    ``normalize_sensor_key``, что и температуры.

    При недоступности SHM или ошибках — пара пустых словарей.
    """
    with open_shm(_AIDA64_SHM_NAME) as raw:
        if raw is None:
            return {}, {}
        try:
            return _parse_aida64(raw)
        except Exception as exc:
            logger.debug("AIDA64 SHM parse upal: %s", exc)
            return {}, {}


def _parse_aida64(raw: bytes) -> tuple[dict[str, float], dict[str, float]]:
    """Парсинг AIDA64 SHM строки → (temps, voltages).

    AIDA64 пишет в SHM нуль-терминированную UTF-8 строку (на старых
    версиях — Windows-1251, поэтому используем errors=«replace»). Длина
    маппинга 64-128 КБ, реальный контент обрывается на ``\\x00``.

    Нераспознанные label'ы отбрасываются (контракт
    ``normalize_sensor_key`` — см. _common.py).
    """
    # Декодируем до первого null-byte. AIDA64 пишет нуль-терминированную
    # строку, остальное пространство SHM-маппинга — мусор/нули.
    end = raw.find(b"\x00")
    if end > 0:
        raw = raw[:end]
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return {}, {}

    temps: dict[str, float] = {}
    voltages: dict[str, float] = {}

    for match in _AIDA64_ENTRY_RE.finditer(text):
        root = match.group("root").lower()
        label = match.group("label").strip()
        value_str = match.group("value").strip()
        if not label or not value_str:
            continue
        try:
            value = float(value_str)
        except ValueError:
            # AIDA64 на некоторых сенсорах может писать «N/A» вместо числа.
            continue
        if root == _AIDA64_TAG_TEMP:
            # Защита: voltage-сенсор с «temp» тегом не должен попасть
            # (бывает на старых билдах при кастомных user-label'ах).
            if "voltage" in label.lower() or "vcore" in label.lower():
                continue
            key = normalize_sensor_key(label)
            if key is None:
                continue
            temps[key] = value
        elif root == _AIDA64_TAG_VOLT:
            # P1.5: voltages — отдельный normalizer (см. shm/_common.py).
            key = normalize_voltage_key(label)
            if key is None:
                continue
            voltages[key] = value
    return temps, voltages


__all__ = [
    "read_aida64_sensors",
    "read_aida64_temperatures_and_voltages",
]
