"""Низкоуровневые хелперы для чтения Shared Memory через ctypes.

Один источник правды для всех SHM-readers (``hwinfo.py``, ``coretemp.py``).
Принципы:

- Win32 API ``OpenFileMapping`` / ``MapViewOfFile`` / ``UnmapViewOfFile``
  / ``CloseHandle`` вызываются через ctypes; все handle'ы освобождаются
  даже при исключениях (context manager).
- На не-Windows ``open_shm`` всегда возвращает ``None`` — это норма,
  SHM-readers вообще не должны импортироваться на Linux в hot-path.
- Нормализатор ключей ``normalize_sensor_key`` приводит произвольные
  имена сенсоров от чужих утилит к apexcore-схеме (``cpu/package``,
  ``gpu/temperature``) — критично для совместимости с
  ``thermal_watchdog._is_cpu_temp_key``.

Mocking: тесты подменяют ``open_shm`` через monkeypatch, чтобы не
дёргать реальные Win32-вызовы. Сигнатура — функция, не класс, потому
что состояние не нужно.

См. план §3 и architectural review §3 (один общий ``_common.py``).
"""

from __future__ import annotations

import logging
import platform
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress

logger = logging.getLogger(__name__)

# Win32 константы.
_FILE_MAP_READ = 0x0004


@contextmanager
def open_shm(name: str) -> Iterator[bytes | None]:
    """Открыть Shared Memory под именем и вернуть его содержимое как bytes.

    Используется как context manager:

        with open_shm("Global\\HWiNFO_SENS_SM2") as raw:
            if raw is None:
                return {}
            # parse raw bytes

    На не-Windows / при отсутствии SHM yield'ит ``None``. Handle и
    view освобождаются автоматически.

    Размер маппинга считывается из ``VirtualQuery`` через
    ``MapViewOfFile(dwNumberOfBytesToMap=0)`` (0 = весь регион), затем
    через ``MEMORY_BASIC_INFORMATION.RegionSize``.
    """
    if platform.system().lower() != "windows":
        yield None
        return
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        yield None
        return

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:
        logger.debug("ctypes.windll.kernel32 недоступен: %s", exc)
        yield None
        return

    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.OpenFileMappingW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.MapViewOfFile.restype = wintypes.LPVOID
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    kernel32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = None
    view = None
    try:
        handle = kernel32.OpenFileMappingW(_FILE_MAP_READ, False, name)
        if not handle:
            yield None
            return
        view = kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, 0)
        if not view:
            yield None
            return
        # VirtualQuery — размер региона.
        region_size = _virtual_query_size(ctypes, wintypes, kernel32, view)
        if region_size <= 0:
            yield None
            return
        data = ctypes.string_at(view, region_size)
        yield data
    except OSError as exc:
        logger.debug("open_shm(%s) ошибка: %s", name, exc)
        yield None
    finally:
        if view:
            with suppress(OSError):
                kernel32.UnmapViewOfFile(view)
        if handle:
            with suppress(OSError):
                kernel32.CloseHandle(handle)


def _virtual_query_size(ctypes_mod, wintypes_mod, kernel32, address) -> int:
    """Определить размер маппинга через ``VirtualQuery``."""

    class MemoryBasicInformation(ctypes_mod.Structure):
        _fields_ = (
            ("BaseAddress", wintypes_mod.LPVOID),
            ("AllocationBase", wintypes_mod.LPVOID),
            ("AllocationProtect", wintypes_mod.DWORD),
            ("RegionSize", ctypes_mod.c_size_t),
            ("State", wintypes_mod.DWORD),
            ("Protect", wintypes_mod.DWORD),
            ("Type", wintypes_mod.DWORD),
        )

    info = MemoryBasicInformation()
    kernel32.VirtualQuery.restype = ctypes_mod.c_size_t
    kernel32.VirtualQuery.argtypes = [
        wintypes_mod.LPCVOID,
        ctypes_mod.POINTER(MemoryBasicInformation),
        ctypes_mod.c_size_t,
    ]
    try:
        kernel32.VirtualQuery(address, ctypes_mod.byref(info), ctypes_mod.sizeof(info))
        return int(info.RegionSize)
    except OSError as exc:
        logger.debug("VirtualQuery упал: %s", exc)
        return 0


# ─── Нормализатор имён ──────────────────────────────────────────────────────


# Паттерны для распознавания CPU/GPU сенсоров. Сначала проверяется CPU,
# потом GPU — порядок важен, потому что некоторые имена («GPU Core Temperature»
# на iGPU могут содержать «Core»).

_CPU_PATTERNS = [
    (re.compile(r"^\s*cpu\s+package", re.I), "cpu/package"),
    (re.compile(r"^\s*package\b", re.I), "cpu/package"),
    (re.compile(r"^\s*cpu\s+temp", re.I), "cpu/package"),
    (re.compile(r"^\s*cpu\s+diode\b", re.I), "cpu/package"),
    (re.compile(r"^\s*cpu\s+ia\s+cores?\b", re.I), "cpu/package"),
    (re.compile(r"^\s*cpu\s+\(\s*tctl", re.I), "cpu/tctl"),
    (re.compile(r"^\s*cpu\s+\(\s*tdie", re.I), "cpu/tdie"),
    (re.compile(r"^\s*tctl\b", re.I), "cpu/tctl"),
    (re.compile(r"^\s*tdie\b", re.I), "cpu/tdie"),
    (re.compile(r"^\s*ccd\s*(\d+)", re.I), "cpu/ccd_{0}"),
    (re.compile(r"^\s*cpu\s+core\s*#?\s*(\d+)", re.I), "cpu/core_{0}"),
    (re.compile(r"^\s*core\s*#?\s*(\d+)", re.I), "cpu/core_{0}"),
    (re.compile(r"^\s*p[\s\-_]*core\s*#?\s*(\d+)", re.I), "cpu/p_core_{0}"),
    (re.compile(r"^\s*e[\s\-_]*core\s*#?\s*(\d+)", re.I), "cpu/e_core_{0}"),
    # AIDA64 публикует package как label="CPU" (без "Package"). Должно идти
    # ПОСЛЕ "cpu core" / "cpu temp" / "cpu package" — иначе "CPU Core #1"
    # съест паттерн до core_N.
    (re.compile(r"^\s*cpu\s*$", re.I), "cpu/package"),
]

_GPU_PATTERNS = [
    (re.compile(r"^\s*gpu\s+hot[\s\-_]*spot", re.I), "gpu/hot_spot"),
    (re.compile(r"^\s*gpu\s+memory\s+junction", re.I), "gpu/memory_junction"),
    (re.compile(r"^\s*gpu\s+memory", re.I), "gpu/memory"),
    (re.compile(r"^\s*gpu\s+core", re.I), "gpu/core"),
    (re.compile(r"^\s*gpu\b", re.I), "gpu/temperature"),
]

_OTHER_PATTERNS = [
    (re.compile(r"^\s*motherboard|^\s*system", re.I), "motherboard/system"),
    (re.compile(r"^\s*vrm", re.I), "motherboard/vrm"),
    (re.compile(r"^\s*pch", re.I), "motherboard/pch"),
]


def normalize_sensor_key(source_name: str) -> str | None:
    """Привести имя сенсора от чужой утилиты к apexcore-схеме.

    Возвращает ``"cpu/package"``, ``"cpu/core_0"``, ``"gpu/hot_spot"`` и
    т.п. Если имя не распознано — возвращает ``None`` (сенсор будет
    проигнорирован — не подмешиваем сырые имена в snapshot, иначе
    ``thermal_watchdog._is_cpu_temp_key`` не подхватит и будут wrong
    matches).

    Это критическая точка совместимости — см. architectural review §1.1.
    """
    if not source_name:
        return None
    name = source_name.strip()
    for pattern, template in _CPU_PATTERNS:
        match = pattern.match(name)
        if match:
            return template.format(*match.groups()) if match.groups() else template
    for pattern, template in _GPU_PATTERNS:
        match = pattern.match(name)
        if match:
            return template.format(*match.groups()) if match.groups() else template
    for pattern, template in _OTHER_PATTERNS:
        match = pattern.match(name)
        if match:
            return template.format(*match.groups()) if match.groups() else template
    return None


# ─── Voltage normalizer (P1.5) ─────────────────────────────────────────────


# Паттерны для напряжений. Отдельно от ``normalize_sensor_key`` (температуры),
# потому что raw label'ы у voltage и temperature сенсоров не пересекаются:
# «CPU Core Voltage» vs «CPU Core». Без отдельного normalizer'а HWiNFO Vcore
# не попадал в результат (см. P1 §1.5).
#
# Ключи: ``cpu/vcore``, ``cpu/soc``, ``cpu/vid``, ``gpu/vcore``, ``gpu/memory``,
# ``ram/vdd``. Совпадает с тем, что использует LHM-нормализатор —
# совместимость для thermal_watchdog не нужна (voltages не идут в watchdog),
# но согласованность с HWiNFO/AIDA64/LHM полезна для render-слоя.

_VOLTAGE_PATTERNS = [
    # CPU voltages — пробуем самые специфичные паттерны первыми.
    (re.compile(r"^\s*cpu\s+core\s+voltage", re.I), "cpu/vcore"),
    (re.compile(r"^\s*cpu\s+vcore\b", re.I), "cpu/vcore"),
    (re.compile(r"^\s*vcore\b", re.I), "cpu/vcore"),
    (re.compile(r"^\s*cpu\s+vid\b", re.I), "cpu/vid"),
    (re.compile(r"^\s*cpu\s+soc\s+voltage", re.I), "cpu/soc"),
    (re.compile(r"^\s*soc\s+voltage", re.I), "cpu/soc"),
    (re.compile(r"^\s*cpu\s+vddcr[\s\-_]*soc", re.I), "cpu/soc"),
    (re.compile(r"^\s*cpu\s+vddcr[\s\-_]*cpu", re.I), "cpu/vcore"),
    # Общее «CPU ... Voltage» как catch-all (после специфичных).
    (re.compile(r"^\s*cpu\s+voltage\b", re.I), "cpu/vcore"),
    # GPU voltages.
    (re.compile(r"^\s*gpu\s+core\s+voltage", re.I), "gpu/vcore"),
    (re.compile(r"^\s*gpu\s+memory\s+voltage", re.I), "gpu/memory"),
    (re.compile(r"^\s*gpu\s+voltage\b", re.I), "gpu/vcore"),
    # RAM/DRAM voltages.
    (re.compile(r"^\s*dram\s+voltage", re.I), "ram/vdd"),
    (re.compile(r"^\s*ram\s+voltage", re.I), "ram/vdd"),
    (re.compile(r"^\s*memory\s+voltage", re.I), "ram/vdd"),
    # +12V / +5V / +3.3V rails — нумеруем по «12v», «5v» и т.д.
    (re.compile(r"^\s*\+?\s*12\s*v\b", re.I), "motherboard/12v"),
    (re.compile(r"^\s*\+?\s*5\s*v\b", re.I), "motherboard/5v"),
    (re.compile(r"^\s*\+?\s*3\.3\s*v\b", re.I), "motherboard/3v3"),
]


def normalize_voltage_key(source_name: str) -> str | None:
    """Привести voltage-label к apexcore-схеме.

    Возвращает ``cpu/vcore``, ``cpu/soc``, ``gpu/vcore`` и т.п. Если имя
    не распознано — ``None`` (rail отбрасывается, не подмешиваем сырые
    имена в snapshot).

    Используется ``shm/hwinfo.py`` и ``shm/aida64.py`` для voltage-readings
    вместо общего ``normalize_sensor_key`` (тот покрывает только temp/GPU
    температуры).
    """
    if not source_name:
        return None
    name = source_name.strip()
    for pattern, template in _VOLTAGE_PATTERNS:
        match = pattern.match(name)
        if match:
            return template.format(*match.groups()) if match.groups() else template
    return None


__all__ = [
    "normalize_sensor_key",
    "normalize_voltage_key",
    "open_shm",
]
