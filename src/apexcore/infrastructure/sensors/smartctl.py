"""Температуры NVMe/SATA-дисков через `smartctl -j`.

Использует пакет **smartmontools** (CLI `smartctl`). Реализован subprocess'ом
с JSON-выводом — без новых Python-зависимостей.

- Установка на Windows: `winget install smartmontools.smartmontools`
- Установка на AstraLinux: `sudo apt install smartmontools`

Поведение деградации: при отсутствии `smartctl` в PATH или любых ошибках
выполнения функции возвращают пустой словарь. Исключения наружу не идут —
это страховка, чтобы `MetricSnapshot` не упал из-за необязательного
бэкенда.

Ключи: `storage/<short_name>/temperature` (например `storage/nvme0/temperature`),
совместимы с группировкой в `interfaces/cli/render.py:_group_temps`
(префикс `storage/` уже учтён как группа `nvme`).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# Кэш списка устройств — `smartctl --scan` быстрый, но в hot-path 0.5с-тика
# избыточен. Обновляем не чаще раза в 60 секунд.
_SCAN_TTL_SEC = 60.0
_scan_cache: list[str] = []
_scan_cache_at: float = 0.0


def is_available() -> bool:
    """`smartctl` доступен (PATH или /usr/sbin как на Debian/Astra)."""
    from apexcore.infrastructure.sbin_lookup import has_sbin
    return has_sbin("smartctl")


def read_smartctl_temperatures() -> dict[str, float]:
    """Снять температуры всех видимых SMART-устройств.

    Возвращает ``{"storage/nvme0/temperature": 48.0, ...}``. При отсутствии
    smartctl или ошибках — пустой словарь.
    """
    if not is_available():
        return {}

    devices = _scan_devices()
    result: dict[str, float] = {}
    for dev in devices:
        temp = _read_device_temperature(dev)
        if temp is None:
            continue
        short = _short_name(dev)
        result[f"storage/{short}/temperature"] = temp
    return result


def _scan_devices() -> list[str]:
    """`smartctl --scan -j` → список путей устройств. Кэшируется на 60 с."""
    global _scan_cache, _scan_cache_at
    now = time.monotonic()
    if _scan_cache and now - _scan_cache_at < _SCAN_TTL_SEC:
        return _scan_cache

    out = _run_smartctl(["--scan", "-j"], timeout=5.0)
    if out is None:
        return []

    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("smartctl --scan returned non-JSON: %s", exc)
        return []

    devices: list[str] = []
    for entry in data.get("devices", []) or []:
        name = entry.get("name")
        if name:
            devices.append(str(name))

    _scan_cache = devices
    _scan_cache_at = now
    return devices


def _read_device_temperature(device: str) -> float | None:
    """Извлечь текущую температуру одного устройства из ``smartctl -a -j``.

    Поддерживает оба формата: NVMe (`nvme_smart_health_information_log.temperature`)
    и SATA (`temperature.current`). Возвращает float °C или None.
    """
    data = _read_device_json(device)
    if data is None:
        return None

    # Общий "temperature.current" — на новых версиях smartctl всегда в °C
    temp_block = data.get("temperature")
    if isinstance(temp_block, dict):
        cur = temp_block.get("current")
        if isinstance(cur, (int, float)) and cur > 0:
            return float(cur)

    # NVMe-специфичное поле
    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        nvme_temp = nvme.get("temperature")
        if isinstance(nvme_temp, (int, float)) and nvme_temp > 0:
            # На большинстве систем smartctl 7+ возвращает уже °C;
            # старые версии могли давать Kelvin (>200). Простая нормализация:
            return float(nvme_temp - 273) if nvme_temp > 200 else float(nvme_temp)

    return None


def _read_device_json(device: str) -> dict | None:
    """Запустить `smartctl -a -j <device>` и распарсить JSON. None при ошибке."""
    out = _run_smartctl(["-j", "-a", device], timeout=5.0)
    if out is None:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("smartctl -a returned non-JSON for %s: %s", device, exc)
        return None


def read_smartctl_devices_info() -> dict[str, dict[str, str]]:
    """Информация об устройствах: модель и тип подключения.

    Возвращает ``{short_name: {"model": str, "type": str}}``. Например::

        {
          "nvme0": {"model": "Samsung SSD 980 PRO 1TB", "type": "SSD M.2 NVMe"},
          "sda":   {"model": "WDC WD20EZBX-00AYRA0", "type": "HDD"},
        }

    Тип определяется по ``device.protocol`` (NVMe vs ATA) и
    ``rotation_rate`` (0 = SSD, >0 = HDD). Если smartctl не отдаёт нужных
    полей — возвращает то, что нашлось; ``type`` может быть «Диск».

    Кэширование такое же, как у ``read_smartctl_temperatures`` — 60с.
    """
    if not is_available():
        return {}

    devices = _scan_devices()
    result: dict[str, dict[str, str]] = {}
    for dev in devices:
        info = _read_device_info(dev)
        if info is None:
            continue
        result[_short_name(dev)] = info
    return result


def _read_device_info(device: str) -> dict[str, str] | None:
    """Распарсить model_name + тип подключения из ``smartctl -a -j``."""
    data = _read_device_json(device)
    if data is None:
        return None

    # Модель: предпочитаем model_name (полное имя), fallback на model_family.
    model = (
        data.get("model_name")
        or data.get("model_family")
        or data.get("scsi_model_name")
        or ""
    )
    if isinstance(model, str):
        model = model.strip()

    # Тип подключения.
    type_str = _classify_device_type(data, device)

    return {"model": model or "(без имени)", "type": type_str}


def _classify_device_type(data: dict, device: str) -> str:
    """Определить тип диска: NVMe / SSD / HDD.

    smartctl выдаёт:
    - ``device.protocol`` = "NVMe" / "ATA" / "SCSI"
    - ``rotation_rate`` = 0 (Solid State Device) или > 0 (RPM HDD)
    - ``form_factor.name`` = "M.2", "2.5 inches", "3.5 inches", ...

    Старые smartctl могут не давать всех полей — graceful degrade.
    """
    dev_block = data.get("device") or {}
    protocol = dev_block.get("protocol") if isinstance(dev_block, dict) else None
    rotation = data.get("rotation_rate")
    form_factor = ""
    ff_block = data.get("form_factor") or {}
    if isinstance(ff_block, dict):
        form_factor = ff_block.get("name") or ""

    # NVMe — почти всегда M.2 на потребительском железе.
    if (isinstance(protocol, str) and protocol.upper() == "NVME") or (
        isinstance(device, str) and "nvme" in device.lower()
    ):
        if isinstance(form_factor, str) and "M.2" in form_factor:
            return "SSD M.2 NVMe"
        return "SSD NVMe"

    # SATA: rotation_rate решает.
    if isinstance(rotation, (int, float)):
        if rotation == 0:
            # Solid state SATA.
            if isinstance(form_factor, str) and "M.2" in form_factor:
                return "SSD M.2 SATA"
            return "SSD SATA"
        if rotation > 0:
            return f"HDD ({int(rotation)} RPM)"

    # Текстовая зацепка — некоторые драйверы пишут «Solid State Device».
    rotation_str = data.get("rotation_rate")
    if isinstance(rotation_str, str) and "solid" in rotation_str.lower():
        return "SSD"

    return "Диск"


def _run_smartctl(args: list[str], timeout: float = 5.0) -> str | None:
    """Запустить smartctl с заданными аргументами и вернуть stdout.

    На ошибки/timeout — None. smartctl возвращает non-zero exit code,
    если устройство не отдаёт smart данных, но stdout всё равно содержит
    валидный JSON с разделом ошибок — поэтому игнорируем returncode.
    """
    # На Debian/Astra smartctl в /usr/sbin, который не в PATH у обычного
    # пользователя. which_with_sbin находит его как fallback.
    from apexcore.infrastructure.sbin_lookup import which_with_sbin
    smartctl_path = which_with_sbin("smartctl") or "smartctl"
    cmd = [smartctl_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # На Windows скрыть всплывающее окно консоли
            creationflags=_NO_WINDOW_FLAG,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("smartctl run failed (%s): %s", cmd, exc)
        return None
    return proc.stdout or None


_NO_WINDOW_FLAG = 0
if sys.platform == "win32":
    # CREATE_NO_WINDOW = 0x08000000 — без mig-консоли при subprocess
    _NO_WINDOW_FLAG = 0x08000000


def _short_name(device_path: str) -> str:
    """Сократить путь устройства до удобного ключа.

    Linux: ``/dev/nvme0n1`` → ``nvme0``, ``/dev/sda`` → ``sda``.
    Windows: ``\\.\\PhysicalDrive0`` → ``drive0``, ``/dev/sda`` (если smartctl
    нормализовал) → ``sda``.
    """
    # Windows-style: \\.\PhysicalDriveN
    m = re.search(r"PhysicalDrive(\d+)", device_path, re.IGNORECASE)
    if m:
        return f"drive{m.group(1)}"
    # Linux-style: /dev/nvme0n1 → nvme0; /dev/nvme1 → nvme1
    m = re.match(r"^/dev/(nvme\d+)(?:n\d+)?$", device_path)
    if m:
        return m.group(1)
    # Linux: /dev/sda, /dev/sdb...
    m = re.match(r"^/dev/(sd[a-z]+)$", device_path)
    if m:
        return m.group(1)
    # Fallback: basename без слешей и спец-символов
    return re.sub(r"[^A-Za-z0-9]+", "_", device_path.rsplit("/", 1)[-1]).strip("_").lower() or "dev"


def _reset_cache_for_tests() -> None:
    """Сбросить module-state — только для unit-тестов."""
    global _scan_cache, _scan_cache_at
    _scan_cache = []
    _scan_cache_at = 0.0
