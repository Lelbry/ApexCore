"""Чтение CoreTemp Shared Memory (``CoreTempMappingObjectEx``).

CoreTemp — лёгкий freeware CPU-monitor (~3 МБ, https://www.alcpu.com/
CoreTemp/). Поддерживает Intel и AMD через MSR-чтение из собственного
драйвера. **Публично документирован** SDK на coretemp.org/developers/
— формат SHM стабилен с 2013 г.

Покрытие: **только CPU temp/load/power**, без VRM/GPU/мат-платы.
Это и есть основной интерес apexcore, поэтому CoreTemp — отличный
P0-fallback после HWiNFO. Юридически чисто: чтение SHM из MIT-приложения
EULA CoreTemp не нарушает (см. ресерч §3.3 и прецедент psutil PR #1952).

Структура C-API (из ``GetCoreTempInfo.h``, alcpu.com SDK):

    struct CoreTempSharedDataEx {
        unsigned int   uiLoad[256];
        unsigned int   uiTjMax[128];
        unsigned int   uiCoreCnt;
        unsigned int   uiCPUCnt;
        float          fTemp[256];
        float          fVID;
        float          fCPUSpeed;
        float          fFSBSpeed;
        float          fMultiplier;
        char           sCPUName[100];
        unsigned char  ucFahrenheit;
        unsigned char  ucDeltaToTjMax;
        unsigned char  ucTdpSupported;
        unsigned char  ucPowerSupported;
        unsigned int   uiStructVersion;
        unsigned int   uiTdp[128];
        float          fPower[128];
        float          fMultipliers[256];
    };

См. ``docs/research`` §3.3 для дополнительного контекста. Имена сенсоров
``cpu/core_0``…``cpu/core_N`` — стандартная apexcore-схема.
"""

from __future__ import annotations

import ctypes
import logging

from apexcore.infrastructure.sensors.shm._common import open_shm

logger = logging.getLogger(__name__)

# Имя SHM-объекта (CoreTempMappingObjectEx). См. docs/research §3.3.
_CORETEMP_SHM_NAME = "CoreTempMappingObjectEx"

# Размеры массивов внутри CoreTempSharedDataEx.
_MAX_CORES = 256
_MAX_CPUS = 128


class _CoreTempSharedDataEx(ctypes.Structure):
    """Структура SHM CoreTemp (см. ``GetCoreTempInfo.h`` alcpu SDK)."""

    _fields_ = (
        ("uiLoad", ctypes.c_uint32 * _MAX_CORES),
        ("uiTjMax", ctypes.c_uint32 * _MAX_CPUS),
        ("uiCoreCnt", ctypes.c_uint32),
        ("uiCPUCnt", ctypes.c_uint32),
        ("fTemp", ctypes.c_float * _MAX_CORES),
        ("fVID", ctypes.c_float),
        ("fCPUSpeed", ctypes.c_float),
        ("fFSBSpeed", ctypes.c_float),
        ("fMultiplier", ctypes.c_float),
        ("sCPUName", ctypes.c_char * 100),
        ("ucFahrenheit", ctypes.c_ubyte),
        ("ucDeltaToTjMax", ctypes.c_ubyte),
        ("ucTdpSupported", ctypes.c_ubyte),
        ("ucPowerSupported", ctypes.c_ubyte),
        ("uiStructVersion", ctypes.c_uint32),
        ("uiTdp", ctypes.c_uint32 * _MAX_CPUS),
        ("fPower", ctypes.c_float * _MAX_CPUS),
        ("fMultipliers", ctypes.c_float * _MAX_CORES),
    )


def read_coretemp_sensors() -> dict[str, float]:
    """Прочитать температуры CoreTemp и привести к apexcore-схеме ключей.

    Возвращает ``{"cpu/core_0": 56.0, "cpu/core_1": 58.5, ...}``. Если
    ``ucFahrenheit == 1`` — конвертируется в Celsius. Если
    ``ucDeltaToTjMax == 1`` — значения публикуются как дельта до TjMax,
    мы пересчитываем обратно в absolute через ``TjMax - delta``.

    При недоступности SHM или формате — пустой словарь.
    """
    with open_shm(_CORETEMP_SHM_NAME) as raw:
        if raw is None:
            return {}
        min_size = ctypes.sizeof(_CoreTempSharedDataEx)
        if len(raw) < min_size:
            logger.debug(
                "CoreTemp SHM: размер %d < ожидаемого %d", len(raw), min_size
            )
            return {}
        try:
            return _parse_coretemp(raw)
        except Exception as exc:
            logger.debug("CoreTemp SHM parse upal: %s", exc)
            return {}


def _parse_coretemp(raw: bytes) -> dict[str, float]:
    """Парсинг CoreTempSharedDataEx → словарь ``cpu/core_N → °C``."""
    data = _CoreTempSharedDataEx.from_buffer_copy(
        raw[: ctypes.sizeof(_CoreTempSharedDataEx)]
    )
    core_count = int(data.uiCoreCnt)
    cpu_count = max(1, int(data.uiCPUCnt))
    if core_count <= 0 or core_count > _MAX_CORES:
        return {}

    fahrenheit = bool(data.ucFahrenheit)
    delta_to_tjmax = bool(data.ucDeltaToTjMax)

    result: dict[str, float] = {}
    for i in range(core_count):
        try:
            value = float(data.fTemp[i])
        except (TypeError, ValueError):
            continue
        if fahrenheit:
            value = (value - 32.0) * (5.0 / 9.0)
        if delta_to_tjmax:
            # value публикуется как «сколько градусов до TjMax».
            # Реальная температура = TjMax - delta. TjMax публикуется
            # per-CPU; на multi-socket системах берём TjMax своего сокета.
            cpu_index = i // max(1, core_count // cpu_count) if cpu_count > 1 else 0
            cpu_index = min(cpu_index, _MAX_CPUS - 1)
            try:
                tjmax = float(data.uiTjMax[cpu_index])
            except (TypeError, ValueError, IndexError):
                tjmax = 100.0
            value = tjmax - value
        result[f"cpu/core_{i}"] = value

    # CoreTemp не публикует package — но если есть TjMax и core temps,
    # «package» естественнее всего — это max по core temps. Это даёт
    # совместимость с _CPU_KEY_TOKENS (содержит "package") и с UI.
    if result:
        result["cpu/package"] = max(result.values())
    return result


__all__ = [
    "read_coretemp_sensors",
]
