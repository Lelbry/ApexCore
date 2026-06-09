"""Чтение базовой частоты CPU per-logical-cpu (для разделения P/E на гибридах).

На гибридных Intel (12th Gen+) P-cores и E-cores имеют разные паспортные
базовые частоты — например, 12900K даёт 3.2 ГГц для P и 2.4 ГГц для E.
``psutil.cpu_freq()`` отдаёт одно среднее значение и теряет разделение,
поэтому читаем сырые источники:

- **Windows:** реестр ``HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\<N>\\~MHz``.
  Поле существует с Windows NT, не требует админ-прав, заполняется ядром при
  старте системы из CPUID/SMBIOS. На 12900K реально различается между CPU 0
  (P-core) и CPU 16 (E-core).
- **Linux:** ``/sys/devices/system/cpu/cpu<N>/cpufreq/base_frequency`` —
  присутствует на ядрах с драйвером ``intel_pstate``. Fallback —
  ``cpuinfo_max_freq`` (на AMD это max-boost, не идеально, но это лучшее,
  что доступно).

Возвращаем словарь ``{logical_cpu_index: frequency_mhz}``. На любую ошибку
(нет прав, отсутствует ключ, неожиданный тип значения) — пустой словарь;
вызывающий код должен корректно работать с этим.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


def read_base_frequencies_by_cpu() -> dict[int, float]:
    """Вернуть базовую частоту в МГц для каждого логического CPU.

    Пустой dict при недоступности источника или ошибке.
    """
    system = platform.system().lower()
    try:
        if system == "windows":
            return _read_windows()
        if system == "linux":
            return _read_linux()
    except Exception:
        logger.debug("base frequency detection failed", exc_info=True)
    return {}


# ─────────────────────────── Windows ────────────────────────────


def _read_windows() -> dict[int, float]:
    """Итерировать `HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\*`.

    Под каждым подключом N (имя — десятичный номер логического CPU) лежит
    значение ``~MHz`` (REG_DWORD). На гибридных Intel значения для P-cores
    и E-cores различаются (например, 3192 МГц vs 2419 МГц на 12900K).
    """
    import winreg

    result: dict[int, float] = {}
    try:
        root = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor",
        )
    except OSError:
        return result

    try:
        idx = 0
        while True:
            try:
                subname = winreg.EnumKey(root, idx)
            except OSError:
                break
            idx += 1
            try:
                cpu_index = int(subname)
            except ValueError:
                continue
            try:
                with winreg.OpenKey(root, subname) as sub:
                    value, _kind = winreg.QueryValueEx(sub, "~MHz")
                    if isinstance(value, int) and value > 0:
                        result[cpu_index] = float(value)
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)

    return result


# ─────────────────────────── Linux ────────────────────────────


def _read_linux(
    cpu_root: Path = Path("/sys/devices/system/cpu"),
) -> dict[int, float]:
    if not cpu_root.is_dir():
        return {}
    result: dict[int, float] = {}
    for sub in cpu_root.iterdir():
        if not sub.is_dir():
            continue
        name = sub.name
        if not (name.startswith("cpu") and name[3:].isdigit()):
            continue
        cpu_index = int(name[3:])
        freq = _read_sysfs_freq(sub / "cpufreq")
        if freq is not None:
            result[cpu_index] = freq
    return result


def _read_sysfs_freq(cpufreq_dir: Path) -> float | None:
    """``base_frequency`` (intel_pstate) → fallback ``cpuinfo_max_freq``.

    ``base_frequency`` уже в кГц. ``cpuinfo_max_freq`` тоже в кГц. Возвращаем
    МГц для единообразия с Windows-источником.
    """
    for fname in ("base_frequency", "cpuinfo_max_freq"):
        path = cpufreq_dir / fname
        try:
            value = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if value > 0:
            return value / 1000.0  # кГц → МГц
    return None


# ─────────────────────────── агрегация ────────────────────────────


def average_mhz(
    freqs: dict[int, float], cpu_indices: tuple[int, ...] | list[int]
) -> float | None:
    """Среднее по заданному подмножеству CPU. None если ни одного не нашлось."""
    values = [freqs[c] for c in cpu_indices if c in freqs]
    if not values:
        return None
    return sum(values) / len(values)
