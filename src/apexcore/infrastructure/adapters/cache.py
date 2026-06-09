"""Определение размеров L1/L2/L3 кеша + размер тестового буфера DRAM.

Платформенно-независимая часть. Платформенные адаптеры (Windows, Linux)
вызывают парсер для своей ОС и затем оборачивают результат в
:class:`CacheTopology`. При отсутствии данных используется
:func:`default_cache_topology` со стандартными значениями.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from apexcore.domain.cache import CacheLevel, CacheTopology

logger = logging.getLogger(__name__)

# Дефолты, если реальные размеры не удалось определить.
# Усреднённые значения по семействам Intel Coffee/Tiger Lake и AMD Zen 2/3.
DEFAULT_L1_BYTES = 32 * 1024
DEFAULT_L2_BYTES = 256 * 1024
DEFAULT_L3_BYTES = 8 * 1024 * 1024
# DRAM-«уровень» в этом тесте — это размер тестового буфера, заведомо больше
# любого современного LLC. Совпадает с BUFFER_MB в memory.py:30.
DRAM_BUFFER_BYTES = 256 * 1024 * 1024

SYSFS_CPU_CACHE_ROOT = Path("/sys/devices/system/cpu/cpu0/cache")


def default_cache_topology() -> CacheTopology:
    """Вернуть топологию со всеми уровнями из ``"fallback"``."""
    return CacheTopology(
        levels=[
            CacheLevel(name="L1", size_bytes=DEFAULT_L1_BYTES, source="fallback"),
            CacheLevel(name="L2", size_bytes=DEFAULT_L2_BYTES, source="fallback"),
            CacheLevel(name="L3", size_bytes=DEFAULT_L3_BYTES, source="fallback"),
            CacheLevel(name="DRAM", size_bytes=DRAM_BUFFER_BYTES, source="fallback"),
        ]
    )


def parse_size_string(raw: str) -> int | None:
    """Распарсить строку «32K», «1024 KB», «8M» и т.п. в байты.

    Поддерживает суффиксы K/KB/M/MB/G/GB (без учёта регистра). Возвращает
    ``None`` если строка пустая или не распознана.
    """
    if not raw:
        return None
    s = raw.strip().upper().replace(" ", "")
    if not s:
        return None
    m = re.match(r"^(\d+)([KMG]?)(B?)$", s)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2)
    multiplier = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}[unit]
    return value * multiplier


def detect_topology_from_sysfs(
    root: Path = SYSFS_CPU_CACHE_ROOT,
) -> CacheTopology | None:
    """Прочитать /sys/devices/system/cpu/cpu0/cache/index*/size и сопутствующие поля.

    Каждая директория ``indexN`` описывает один уровень кеша:
    - ``level`` — 1, 2 или 3
    - ``type`` — Data, Instruction, Unified
    - ``size`` — строка с размером (формат «32K», «1024K» и т.п.)

    Для L1 берётся размер data-кеша (Data или Unified). Если уровень не найден —
    возвращается ``None`` (вызывающий код использует fallback).

    Если ``root`` не существует — возвращает ``None`` (нелинуксовая система или
    урезанное ядро без sysfs cache).
    """
    if not root.exists() or not root.is_dir():
        return None
    sizes: dict[int, int] = {}
    try:
        for index_dir in sorted(root.iterdir()):
            if not index_dir.is_dir() or not index_dir.name.startswith("index"):
                continue
            level_file = index_dir / "level"
            size_file = index_dir / "size"
            type_file = index_dir / "type"
            if not (level_file.exists() and size_file.exists()):
                continue
            try:
                level = int(level_file.read_text().strip())
            except (OSError, ValueError):
                continue
            cache_type = (
                type_file.read_text().strip() if type_file.exists() else "Unified"
            )
            # Для L1 предпочитаем data-кеш; instruction-кеш игнорируем.
            if level == 1 and cache_type.lower() == "instruction":
                continue
            try:
                raw = size_file.read_text().strip()
            except OSError:
                continue
            size = parse_size_string(raw)
            if size is None:
                continue
            # Если для уровня уже есть значение — берём максимум (на всякий случай).
            sizes[level] = max(sizes.get(level, 0), size)
    except OSError as exc:  # pragma: no cover
        logger.debug("sysfs cache read failed: %s", exc)
        return None

    if not sizes:
        return None

    return CacheTopology(
        levels=[
            CacheLevel(
                name="L1",
                size_bytes=sizes.get(1, DEFAULT_L1_BYTES),
                source="sysfs" if 1 in sizes else "fallback",
            ),
            CacheLevel(
                name="L2",
                size_bytes=sizes.get(2, DEFAULT_L2_BYTES),
                source="sysfs" if 2 in sizes else "fallback",
            ),
            CacheLevel(
                name="L3",
                size_bytes=sizes.get(3, DEFAULT_L3_BYTES),
                source="sysfs" if 3 in sizes else "fallback",
            ),
            CacheLevel(name="DRAM", size_bytes=DRAM_BUFFER_BYTES, source="fallback"),
        ]
    )


def topology_from_wmi_kb(
    l1_kb: int | None,
    l2_kb: int | None,
    l3_kb: int | None,
) -> CacheTopology:
    """Собрать топологию из значений WMI Win32_Processor (всегда в КБ).

    Любое значение, равное ``None`` или 0, заменяется дефолтом и помечается как
    ``"fallback"``. Win32_Processor.L1CacheSize обычно отсутствует — пользователь
    получит fallback для L1 (32 КБ), что разумно для подавляющего большинства x86 CPU.
    """

    def _level(name: str, kb: int | None, default: int) -> CacheLevel:
        if kb is None or kb <= 0:
            return CacheLevel(name=name, size_bytes=default, source="fallback")  # type: ignore[arg-type]
        return CacheLevel(name=name, size_bytes=kb * 1024, source="wmi")  # type: ignore[arg-type]

    return CacheTopology(
        levels=[
            _level("L1", l1_kb, DEFAULT_L1_BYTES),
            _level("L2", l2_kb, DEFAULT_L2_BYTES),
            _level("L3", l3_kb, DEFAULT_L3_BYTES),
            CacheLevel(name="DRAM", size_bytes=DRAM_BUFFER_BYTES, source="fallback"),
        ]
    )
