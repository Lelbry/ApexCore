"""Sparkline для значений в строке таблицы (M5 «Датчики»).

Три шкалы:

- **``blocks``** (default с v0.9.0): 3-уровневая ``_ ▯ ▮`` — низкая
  (<30%) / средняя (30-70%) / высокая (>70%) нагрузка. Семантически
  однозначно для usage-метрик (CPU% / RAM% / нагрузка ядер).
  Идеальна для «Стресс-тест системы» и «Датчики».
- **``unicode``**: 8-уровневая ``▁▂▃▄▅▆▇█`` (U+2581..U+2588) — плотный
  визуал для непрерывных значений (температура / частота), плавная
  динамика. Windows Terminal, modern Linux/macOS.
- **``ascii``** ``.,-=+*#@`` — fallback для старых шрифтов и classic
  Windows ``conhost.exe`` (PS 5.1) где блок-символы рендерятся как ▯.

Стиль выбирается через:

1. ENV ``APEXCORE_SPARKLINE`` — ``"blocks"`` / ``"unicode"`` / ``"ascii"`` / ``"auto"``.
2. Иначе ``"auto"``: на Windows без ``WT_SESSION`` (запуск из conhost) →
   ASCII; во всех прочих случаях → blocks.

Settings-store (``MenuSettings.sparkline_style``) проставляет значение в
``os.environ`` при старте TUI, поэтому здесь читаем только env — без
циклических зависимостей с меню.

Семантика blocks:
- Для usage-метрик (значения 0..100): пороги 30/70 по абсолютной величине.
- Для произвольных значений (температуры / частоты): нормализация по
  min/max окна и пороги 33/66 — даёт визуальную динамику без привязки
  к абсолютной шкале.

Helper'ы:

- ``sparkline(values, width)`` — последовательность баров.
- ``sparkline_with_range(values, width)`` — то же + подпись ``min..max``.

Никаких новых зависимостей, чистый stdlib.
"""

from __future__ import annotations

import os
import sys

_BARS_UNICODE: tuple[str, ...] = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
_BARS_ASCII: tuple[str, ...] = (".", ",", "-", "=", "+", "*", "#", "@")
# 3-уровневая шкала: low / mid / high.
# `_` (underscore, U+005F) — низкая нагрузка
# `▯` (white vertical rectangle, U+25AF) — средняя
# `▮` (black vertical rectangle, U+25AE) — высокая
_BARS_BLOCKS: tuple[str, ...] = ("_", "▯", "▮")
_EMPTY_CHAR_UNICODE = "·"   # U+00B7 — отсутствующий отсчёт в unicode-режиме
_EMPTY_CHAR_ASCII = " "     # в ASCII-режиме точка-разделитель «·» тоже не моноширинная
_EMPTY_CHAR_BLOCKS = " "    # в blocks-режиме промежуток (rectangles широкие, точка не нужна)


def _is_classic_conhost() -> bool:
    """Эвристика: Windows без Windows Terminal (``WT_SESSION``).

    Classic ``conhost.exe`` (PS 5.1, cmd.exe) с DejaVu/Consolas-подобными
    шрифтами часто рендерит U+2581..U+2588 как ▯. Windows Terminal
    выставляет ``WT_SESSION`` — там Unicode корректен.
    """
    return sys.platform == "win32" and not os.environ.get("WT_SESSION")


def _resolve_style() -> str:
    """Вернуть один из ``"blocks"`` / ``"unicode"`` / ``"ascii"``."""
    raw = (os.environ.get("APEXCORE_SPARKLINE") or "auto").strip().lower()
    if raw in ("ascii", "unicode", "blocks"):
        return raw
    # auto: blocks (3-уровневые прямоугольники _ ▯ ▮) — default с v0.9.0.
    # Эти символы — базовые геометрические (U+005F, U+25AF, U+25AE), их
    # рендерит даже classic conhost (Consolas/Cascadia). Раньше тут была
    # ветка для conhost → ASCII (.,-=+*#@), но новый blocks-стиль
    # читаемее даже на старых шрифтах. Кто хочет старый ASCII — ставит
    # APEXCORE_SPARKLINE=ascii явно.
    return "blocks"


def _resolve_bars() -> tuple[tuple[str, ...], str]:
    """Вернуть (bars, empty_char) согласно текущему стилю."""
    style = _resolve_style()
    if style == "ascii":
        return _BARS_ASCII, _EMPTY_CHAR_ASCII
    if style == "unicode":
        return _BARS_UNICODE, _EMPTY_CHAR_UNICODE
    # blocks (default)
    return _BARS_BLOCKS, _EMPTY_CHAR_BLOCKS


def sparkline(values: list[float], width: int = 12) -> str:
    """Сжать список значений в строку из ``width`` баров.

    Берёт ``values[-width:]`` (последние N) и масштабирует по локальным
    min/max этого окна. Это даёт визуально различимую динамику даже на
    плоских значениях.

    Если значений меньше чем width, левая часть строки — empty-char,
    правая — реальные бары. Пустой список → строка из empty-char.

    Семантика для 3-уровневой шкалы blocks:
    - Если все значения в диапазоне 0..100 (usage-метрики) → пороги 30/70
      по абсолютной величине: <30% = _, 30-70% = ▯, >70% = ▮.
    - Иначе (произвольные значения: температуры, частоты) → нормализация
      по min/max окна, пороги 33/66.
    """
    if width <= 0:
        return ""
    bars, empty = _resolve_bars()
    if not values:
        return empty * width

    tail = values[-width:]
    vmin = min(tail)
    vmax = max(tail)
    is_three_level = len(bars) == 3

    if vmax - vmin < 1e-9:
        # Плоская линия. Семантика бар-уровня:
        # - всё нулевое (idle fan / нет данных) → нижний бар, визуально «пустой».
        #   Без этой ветки 0 RPM выводился как ▄▄▄▄ и пользователь видел
        #   «заполненную» полоску при 0 оборотах (регрессия v0.5.3).
        # - ненулевое стабильное значение → средний уровень — «активность
        #   без изменений», в отличие от пустоты.
        if is_three_level and 0.0 <= vmax <= 100.0:
            # Usage-метрика: используем абсолютные пороги даже для плоской линии.
            bar = bars[0] if vmax < 30 else (bars[1] if vmax < 70 else bars[2])
        else:
            bar = bars[0] if abs(vmax) < 1e-9 else bars[len(bars) // 2]
        line = bar * len(tail)
    elif is_three_level and 0.0 <= vmin and vmax <= 100.0:
        # 3-уровневая blocks-шкала для usage-метрик (0..100%):
        # абсолютные пороги 30/70. Однозначно: пользователь видит
        # «нагрузка ниже 30 / средняя / выше 70» без необходимости
        # пересчитывать на min/max окна.
        def _bucket(v: float) -> str:
            if v < 30.0:
                return bars[0]
            if v < 70.0:
                return bars[1]
            return bars[2]
        line = "".join(_bucket(v) for v in tail)
    else:
        # Стандартная нормализация по min/max окна для непрерывных значений
        # (температуры, частоты) или для unicode/ascii-шкал с многими уровнями.
        span = vmax - vmin
        line = "".join(
            bars[
                min(
                    len(bars) - 1,
                    int((v - vmin) / span * (len(bars) - 1) + 0.5),
                )
            ]
            for v in tail
        )

    if len(line) < width:
        line = empty * (width - len(line)) + line
    return line


def sparkline_with_range(
    values: list[float],
    width: int = 10,
    fmt: str = "{vmin:.0f}..{vmax:.0f}",
) -> str:
    """Sparkline + подпись диапазона ``min..max`` для контекста.

    На широких карточках добавляем под графиком эту строку, чтобы
    пользователь видел абсолютные значения без необходимости считать
    высоту блоков.
    """
    if not values:
        return sparkline([], width)
    tail = values[-width:]
    vmin = min(tail)
    vmax = max(tail)
    return f"{sparkline(values, width)} {fmt.format(vmin=vmin, vmax=vmax)}"
