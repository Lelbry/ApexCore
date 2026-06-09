"""Чтение порогов температуры процессора из ``/sys/class/hwmon`` на Linux/Astra.

Для активного thermal-watchdog нужно знать критическую точку Tj_max каждого
датчика, чтобы заранее остановить нагрузку до достижения hardware-throttle.
hwmon публикует пороги в файлах ``temp*_crit`` (приоритет) и ``temp*_max``
(резерв). Значение в милли-°C, делится на 1000.

Контракт:

- ``read_hwmon_tjmax()`` возвращает ``dict`` ключей вида
  ``"<hwmon_name>/temp<n>"`` → значение Tj_max в °C. Учитываются только
  CPU-датчики (``coretemp``, ``k10temp``, ``zenpower``, ``cpu_thermal``).
- При любой ошибке (нет /sys, PermissionError на Astra SE с MAC,
  нечитаемые файлы) возвращается пустой словарь — потребитель должен
  применить fallback на фиксированный 100°C (Intel Doc 655258 [25]).
- Никаких исключений наружу не пробрасываем.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Имена hwmon-устройств, которые относятся к CPU. Остальные (например, nvme/wifi)
# фильтруем, чтобы не подмешивать к Tj_max лишние пороги.
_CPU_HWMON_NAMES = (
    "coretemp",  # Intel
    "k10temp",  # AMD ≥ K10 (Zen included)
    "zenpower",  # AMD Zen, отдельный модуль
    "cpu_thermal",  # ARM/SoC
    "fam15h_power",  # AMD Bulldozer/Piledriver
)

_HWMON_ROOT = Path("/sys/class/hwmon")

# Минимально вменяемое Tj_max: пороги < 60°C — заведомо неверны (PCH chipset
# или термистор корпуса). Игнорируем такие записи.
_MIN_PLAUSIBLE_TJMAX = 60.0
_MAX_PLAUSIBLE_TJMAX = 130.0


def read_hwmon_tjmax() -> dict[str, float]:
    """Собрать критические пороги температуры со всех CPU-датчиков hwmon.

    Возвращает: ``{"coretemp/temp1": 100.0, "k10temp/temp2": 95.0, ...}``.
    Пустой словарь означает «данных нет» → потребитель использует fallback.
    """
    if not _HWMON_ROOT.exists():
        return {}
    result: dict[str, float] = {}
    try:
        entries = sorted(_HWMON_ROOT.iterdir())
    except (OSError, PermissionError) as exc:
        logger.debug("hwmon: не удалось перечислить %s: %s", _HWMON_ROOT, exc)
        return {}
    for hwmon_dir in entries:
        try:
            name = (hwmon_dir / "name").read_text(encoding="utf-8").strip().lower()
        except (OSError, UnicodeDecodeError):
            continue
        if not any(cpu_name in name for cpu_name in _CPU_HWMON_NAMES):
            continue
        result.update(_read_thresholds_for(hwmon_dir, name))
    return result


def best_tjmax(thresholds: dict[str, float], fallback: float = 100.0) -> float:
    """Выбрать представительный Tj_max из набора порогов.

    Берём минимум из имеющихся (самый консервативный) — если у разных ядер
    разные пороги, watchdog должен ориентироваться на наименьший.
    """
    values = [v for v in thresholds.values() if _MIN_PLAUSIBLE_TJMAX <= v <= _MAX_PLAUSIBLE_TJMAX]
    if not values:
        return fallback
    return float(min(values))


def _read_thresholds_for(hwmon_dir: Path, name: str) -> dict[str, float]:
    """Прочитать temp{N}_crit / temp{N}_max для одного hwmon-устройства."""
    out: dict[str, float] = {}
    # hwmon перечисляет до 16 каналов (на серверах больше, но 16 хватит).
    for n in range(1, 17):
        crit = _read_milli_celsius(hwmon_dir / f"temp{n}_crit")
        if crit is None:
            crit = _read_milli_celsius(hwmon_dir / f"temp{n}_max")
        if crit is None:
            continue
        if not (_MIN_PLAUSIBLE_TJMAX <= crit <= _MAX_PLAUSIBLE_TJMAX):
            continue
        out[f"{name}/temp{n}"] = crit
    return out


def _read_milli_celsius(path: Path) -> float | None:
    """Прочитать файл с millidegrees Celsius. None если файла нет/не читается."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, PermissionError) as exc:
        logger.debug("hwmon: PermissionError на %s: %s", path, exc)
        return None
    try:
        return int(raw) / 1000.0
    except (ValueError, TypeError):
        return None


__all__ = ["best_tjmax", "read_hwmon_tjmax"]
