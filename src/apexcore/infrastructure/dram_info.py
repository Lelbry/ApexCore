"""Реальная конфигурация DRAM для idle-превью UI (объём / тип / частота / каналы).

Цель — отдать UI **реальные** данные о памяти вместо hardcoded mock. Источники
по платформам и привилегиям:

- **Объём** (`total_gb`): всегда доступен через psutil (передаётся из SystemInfo),
  не требует ничего.
- **Тип/частота/модули** (`type` / `speed_mts` / `modules`):
  - Windows: WMI ``Win32_PhysicalMemory`` через PowerShell CIM — **без admin**.
  - Linux: ``dmidecode -t 17`` — требует root. Пробуем цепочку
    ``dmidecode`` → ``sudo -n dmidecode`` → ``pkexec dmidecode``. Первый
    успешный. Если все провалились (нет прав) — поля ``None``, ``available=False``.
- **Каналы** (`channels`): эвристика по модели CPU (`roofline._max_dram_channels`).
- **Тайминги (CL)**: НЕ определяются ни на одной платформе без чтения SPD —
  поэтому не возвращаются вовсе (раньше это был чистый mock «CL32-38-38-76»).

Результат кешируется на весь lifetime процесса: pkexec/sudo/WMI вызывается
**один раз** при первом запросе, дальше отдаётся из кеша (idle-превью может
дёргать endpoint часто — не плодим UAC-промпты и subprocess-и).
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] | None = None
_LOCK = threading.Lock()

# Карта SMBIOSMemoryType (Win32_PhysicalMemory) → человекочитаемый тип.
# Полная таблица — DMTF SMBIOS spec; берём актуальные DDR-поколения.
_SMBIOS_MEM_TYPE: dict[int, str] = {
    20: "DDR",
    21: "DDR2",
    24: "DDR3",
    26: "DDR4",
    34: "DDR5",
    35: "DDR5",  # LPDDR5 иногда репортится так
}


def get_dram_info(total_gb: float | None, cpu_model: str | None) -> dict[str, Any]:
    """Вернуть конфигурацию DRAM (с кешем).

    :param total_gb: объём RAM из SystemInfo (psutil) — всегда показывается.
    :param cpu_model: модель CPU для эвристики каналов.
    :returns: dict с полями total_gb / type / speed_mts / modules / channels /
        available / source. Поля type/speed/modules = None если недоступны.
    """
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            # Объём мог уточниться — обновляем из свежего total_gb.
            out = dict(_CACHE)
            if total_gb is not None:
                out["total_gb"] = round(total_gb, 1)
            return out

        info: dict[str, Any] = {
            "total_gb": round(total_gb, 1) if total_gb is not None else None,
            "type": None,
            "speed_mts": None,
            "modules": None,
            "channels": None,
            "available": False,
            "source": "psutil-only",
        }

        detail = _read_windows() if sys.platform == "win32" else _read_linux()
        if detail is not None:
            info.update(detail)
            info["available"] = True

        # Каналы — эвристика по CPU (не требует привилегий).
        if cpu_model:
            try:
                from apexcore.application.roofline import _max_dram_channels
                # _max_dram_channels отдаёт платформенный МАКСИМУМ каналов
                # (desktop=2, HEDT=4, server=6/8/12) либо None для нераспознанного
                # CPU → тогда channels останется None и UI покажет «н/д»
                # (лучше честное «н/д», чем неверная догадка на чужом железе).
                ch = _max_dram_channels(cpu_model)
                if ch:
                    # Реально заселено каналов ≤ числа модулей: одна планка =
                    # 1 канал, даже если платформа поддерживает 2; 4 планки на
                    # 2-канальной платформе = 2 (min(2,4)). Не завышаем для
                    # single-stick конфигов.
                    mods = info.get("modules")
                    info["channels"] = (
                        min(ch, mods) if isinstance(mods, int) and mods > 0 else ch
                    )
            except Exception as exc:  # pragma: no cover
                logger.debug("dram channels heuristic failed: %s", exc)

        _CACHE = dict(info)
        return info


def reset_cache() -> None:
    """Сбросить кеш (для тестов / принудительного перечитывания)."""
    global _CACHE
    with _LOCK:
        _CACHE = None


# ─── Windows ────────────────────────────────────────────────────────────────


def _read_windows() -> dict[str, Any] | None:
    """WMI Win32_PhysicalMemory — type/speed/modules без admin."""
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        ("Get-CimInstance Win32_PhysicalMemory | ForEach-Object { "
         "Write-Output \"$($_.SMBIOSMemoryType)|$($_.ConfiguredClockSpeed)|$($_.Speed)\" }"),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    types: set[int] = set()
    speeds: list[float] = []
    modules = 0
    for line in out.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        modules += 1
        try:
            t = int(parts[0]) if parts[0].strip().isdigit() else 0
            if t:
                types.add(t)
        except ValueError:
            pass
        configured = int(parts[1]) if parts[1].strip().isdigit() else 0
        jedec = int(parts[2]) if parts[2].strip().isdigit() else 0
        speed = configured if configured > 0 else jedec
        if speed > 0:
            speeds.append(float(speed))
    if modules == 0:
        return None
    dram_type = None
    for t in sorted(types, reverse=True):  # предпочитаем новейший тип
        if t in _SMBIOS_MEM_TYPE:
            dram_type = _SMBIOS_MEM_TYPE[t]
            break
    return {
        "type": dram_type,
        "speed_mts": max(speeds) if speeds else None,
        "modules": modules,
        "source": "wmi",
    }


# ─── Linux ────────────────────────────────────────────────────────────────


def _read_linux() -> dict[str, Any] | None:
    """dmidecode -t 17: type/speed/modules. Цепочка прав: direct → sudo -n → pkexec.

    Возвращает None если dmidecode недоступен или нет прав (тогда UI покажет
    только объём + «н/д» для деталей).
    """
    raw = _run_dmidecode()
    if raw is None:
        return None
    return _parse_dmidecode(raw)


def _run_dmidecode() -> str | None:
    """Получить вывод ``dmidecode -t 17`` пробуя нарастающие привилегии."""
    # 1) Напрямую (вдруг процесс уже root).
    # 2) sudo -n (passwordless sudo, без промпта — сработает если настроен).
    # 3) pkexec (GUI polkit-промпт — для десктопной сессии пользователя).
    attempts = [
        ["dmidecode", "-t", "17"],
        ["sudo", "-n", "dmidecode", "-t", "17"],
        ["pkexec", "dmidecode", "-t", "17"],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and "Memory Device" in out.stdout:
            logger.info("dram_info: dmidecode via %s", cmd[0])
            return out.stdout
    logger.info("dram_info: dmidecode недоступен (нет прав) — только объём RAM")
    return None


def _parse_dmidecode(text: str) -> dict[str, Any]:
    """Распарсить ``dmidecode -t 17`` → type/speed/modules.

    Каждый «Memory Device» блок описывает один слот; пустые слоты (Size:
    No Module Installed) пропускаем.
    """
    blocks = re.split(r"\n(?=Memory Device)", text)
    types: set[str] = set()
    speeds: list[float] = []
    modules = 0
    for block in blocks:
        if "Memory Device" not in block:
            continue
        size_m = re.search(r"^\s*Size:\s*(.+)$", block, re.MULTILINE)
        if size_m and ("No Module" in size_m.group(1) or "Unknown" in size_m.group(1)):
            continue  # пустой слот
        if not size_m:
            continue
        modules += 1
        type_m = re.search(r"^\s*Type:\s*(DDR\d|LPDDR\d)", block, re.MULTILINE | re.IGNORECASE)
        if type_m:
            types.add(type_m.group(1).upper())
        # Configured Memory Speed предпочтительнее (фактическая), иначе Speed.
        sp = re.search(r"Configured Memory Speed:\s*(\d+)\s*MT/s", block, re.IGNORECASE)
        if not sp:
            sp = re.search(r"Speed:\s*(\d+)\s*MT/s", block, re.IGNORECASE)
        if sp:
            try:
                v = float(sp.group(1))
                if v > 0:
                    speeds.append(v)
            except ValueError:
                pass
    if modules == 0:
        return {"type": None, "speed_mts": None, "modules": None, "source": "dmidecode-empty"}
    dram_type = None
    # Предпочитаем DDR5 > DDR4 > ... если разнотип (редко).
    for pref in ("DDR5", "LPDDR5", "DDR4", "LPDDR4", "DDR3"):
        if pref in types:
            dram_type = pref
            break
    if dram_type is None and types:
        dram_type = sorted(types)[0]
    return {
        "type": dram_type,
        "speed_mts": max(speeds) if speeds else None,
        "modules": modules,
        "source": "dmidecode",
    }


__all__ = ["get_dram_info", "reset_cache"]
