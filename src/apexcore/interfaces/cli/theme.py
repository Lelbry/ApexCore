"""Темы для Rich-консоли (CLI ApexCore).

Задача — настоящая светлая тема (белый фон окна терминала + чёрный текст +
тёмные акценты), нужная для печатных скриншотов в магистерскую и для работы
при ярком освещении. Тёмная тема (по умолчанию) — фон и текст определяет
профиль терминала, мы только подмешиваем акценты.

Светлая тема состоит из ДВУХ частей:

1. **Rich Theme** (`LIGHT_THEME`) — переопределяет имена цветов в markup
   (`[cyan]`, `[bold cyan]`, …) на тёмные оттенки, читабельные на белом.
   Покрывает только составные комбинации, реально встречающиеся в render.py
   (см. grep по `style="` и `[...]`); если появится новый паттерн — его
   нужно добавить в LIGHT_THEME, иначе он выведется ANSI-цветом.

2. **ANSI OSC-коды для фона/текста ОКНА терминала** (`_enter_light` /
   `_leave_light`). Rich сам по себе не управляет фоном — это делает
   профиль терминала. Но Windows Terminal / iTerm2 / xterm / kitty
   поддерживают OSC 10 (foreground) и OSC 11 (background) — escape-коды
   которые задают цвет окна на лету:
       \x1b]11;#FFFFFF\x07    # фон → белый
       \x1b]10;#000000\x07    # текст → чёрный
       \x1b]111\x07           # сброс фона к default из профиля
       \x1b]110\x07           # сброс текста к default из профиля
   Применяем при входе в light, восстанавливаем через atexit при выходе
   процесса (чтобы после `apexcore info --theme=light` пользователь не
   остался с белой консолью навсегда).

API:
    from apexcore.interfaces.cli.theme import apply_theme, current_theme
    apply_theme("light")           # светлая тема: белый фон + чёрный текст
    apply_theme("dark")            # дефолт: цвета из профиля терминала
    name = current_theme()         # 'dark' | 'light'

Selection priority:
    1. apply_theme(name) — явный вызов (например, из CLI `--theme` флага)
    2. ENV var APEXCORE_THEME=light|dark — для скриптов и dev-окружения
    3. По умолчанию — dark (обратная совместимость)
"""

from __future__ import annotations

import atexit
import contextlib
import os
import sys
from typing import Literal

from rich.theme import Theme

ThemeName = Literal["dark", "light"]
_VALID_NAMES: tuple[str, ...] = ("dark", "light")
_ENV_VAR = "APEXCORE_THEME"

# ─── DARK ─────────────────────────────────────────────────────────────
# Пустая тема = дефолтное поведение Rich. Cyan останется cyan, green —
# green, и т.д. Менять оттенки тёмной темы не нужно: текущая палитра
# отработала на сотнях скриншотов и привычна пользователям.
DARK_THEME = Theme({}, inherit=True)

# ─── LIGHT ────────────────────────────────────────────────────────────
# Палитра подобрана под печать на белой бумаге и просмотр на белом фоне
# терминала. Цвета синхронизированы с installer/WebUI light-палитрой
# (см. packaging/.../tokens-installer.css и webui/.../tokens.css):
#   accent = #2872d4 (saturated blue) — заголовки, основные элементы
#   ok     = #15a866 (green)         — успешные результаты
#   warn   = #b45309 (amber-brown)   — предупреждения
#   danger = #c0392b (dark red)      — ошибки и провалы
# Все цвета имеют contrast ratio ≥ 4.5:1 на белом фоне (WCAG AA для
# обычного текста).
#
# ВАЖНО — про составные стили (`[bold cyan]`, `[dim yellow]` и т.д.).
# Rich парсит markup `[bold cyan]` как ОДНУ строку style-имени. Сначала
# console ищет это полное имя в стеке тем, и только если не найдено —
# фоллбэкает на парсинг "bold" + Color.parse("cyan"). На этапе Color.parse
# тема НЕ просматривается — там жёстко мапится ANSI-имя cyan→код 6, что
# даёт стандартный терминальный cyan вместо нашего #2872d4.
#
# Поэтому здесь регистрируем КАЖДУЮ комбинацию, реально встречающуюся в
# render.py / menu/screens.py (источник списка — grep по `style=`, `[...]`).
# Если в render.py появится новый паттерн вроде `[dim magenta]` — его тоже
# нужно добавить сюда, иначе он выведется ANSI-цветом, а не нашим оттенком.
_LIGHT_BG       = "#ffffff"  # фон окна терминала (OSC 11)
_LIGHT_FG       = "#0f172a"  # основной текст (OSC 10)
_LIGHT_ACCENT   = "#2872d4"
_LIGHT_ACCENT_2 = "#1f5fb8"
_LIGHT_OK       = "#15a866"
_LIGHT_OK_2     = "#0e8b54"
_LIGHT_DANGER   = "#c0392b"
_LIGHT_DANGER_2 = "#a01f12"
_LIGHT_WARN     = "#b45309"
_LIGHT_WARN_2   = "#92400e"
_LIGHT_MUTED    = "#475569"
_LIGHT_PURPLE   = "#7c3aed"

# Каждый цвет дополнен `on #ffffff` (явная белая подложка), чтобы Rich-Style
# принудительно отрисовывал bg-цветом ячейки. Без этого Rich эмитит только
# foreground escape (`\x1b[38;2;...m`), и фон остаётся «тёмным» — если
# профиль терминала или OSC 11 ещё не успели его перекрасить, текст
# окрашивается на тёмном фоне → нечитаемо.
_ON_WHITE = f" on {_LIGHT_BG}"

LIGHT_THEME = Theme(
    {
        # ── Базовый стиль (всё что без явного цвета) ──────────────────
        # `none` — Rich-овский «дефолтный» стиль, применяется ко всему
        # тексту без явной разметки (заголовки таблиц без style, текст
        # ячеек, рамки и т.п.). Задаём чёрный на белом — это решает
        # 90% видимости в светлой теме.
        "none":  f"{_LIGHT_FG}{_ON_WHITE}",
        "reset": f"{_LIGHT_FG}{_ON_WHITE}",
        # ── Cyan (основной accent) ────────────────────────────────────
        "cyan":             f"{_LIGHT_ACCENT}{_ON_WHITE}",
        "bold cyan":        f"bold {_LIGHT_ACCENT}{_ON_WHITE}",
        "dim cyan":         f"dim {_LIGHT_ACCENT}{_ON_WHITE}",
        "bright_cyan":      f"{_LIGHT_ACCENT_2}{_ON_WHITE}",
        "bold bright_cyan": f"bold {_LIGHT_ACCENT_2}{_ON_WHITE}",
        # ── Green (PASS, итоговые баллы) ───────────────────────────────
        "green":             f"{_LIGHT_OK}{_ON_WHITE}",
        "bold green":        f"bold {_LIGHT_OK}{_ON_WHITE}",
        "bright_green":      f"{_LIGHT_OK_2}{_ON_WHITE}",
        "bold bright_green": f"bold {_LIGHT_OK_2}{_ON_WHITE}",
        # ── Red (FAIL, ошибки) ────────────────────────────────────────
        "red":        f"{_LIGHT_DANGER}{_ON_WHITE}",
        "bold red":   f"bold {_LIGHT_DANGER}{_ON_WHITE}",
        "dim red":    f"dim {_LIGHT_DANGER}{_ON_WHITE}",
        "bright_red": f"{_LIGHT_DANGER_2}{_ON_WHITE}",
        # ── Yellow (предупреждения, тротлинг) ─────────────────────────
        "yellow":        f"{_LIGHT_WARN}{_ON_WHITE}",
        "bold yellow":   f"bold {_LIGHT_WARN}{_ON_WHITE}",
        "dim yellow":    f"dim {_LIGHT_WARN}{_ON_WHITE}",
        "bright_yellow": f"{_LIGHT_WARN_2}{_ON_WHITE}",
        # ── Magenta (редкие акценты, deprecation alerts) ──────────────
        "magenta":      f"{_LIGHT_PURPLE}{_ON_WHITE}",
        "bold magenta": f"bold {_LIGHT_PURPLE}{_ON_WHITE}",
        # ── Dim (служебные пометки в скобках) ─────────────────────────
        # В тёмной теме dim = "цвет фона минус контраст"; в светлой —
        # средне-серый, читаемый на белом.
        "dim":       f"{_LIGHT_MUTED}{_ON_WHITE}",
        "bold dim":  f"bold {_LIGHT_MUTED}{_ON_WHITE}",
        "dim white": f"{_LIGHT_MUTED}{_ON_WHITE}",  # render_sensors использует
        # ── Rich built-in style names ─────────────────────────────────
        # Дефолтный `status.spinner` = "green" (см. rich.default_styles).
        "status.spinner": f"{_LIGHT_ACCENT}{_ON_WHITE}",
        # Progress bars (apexcore micro / ram-cache используют Rich
        # Progress с BarColumn). Дефолтные «complete=green, finished=
        # bright_green» — на белом ярко. Подкрашиваем в наш ok-цвет.
        "bar.complete": f"{_LIGHT_OK}{_ON_WHITE}",
        "bar.finished": f"{_LIGHT_OK_2}{_ON_WHITE}",
        "bar.pulse":    f"{_LIGHT_ACCENT}{_ON_WHITE}",
        "progress.percentage": f"{_LIGHT_ACCENT}{_ON_WHITE}",
        "progress.remaining":  f"{_LIGHT_MUTED}{_ON_WHITE}",
        "progress.elapsed":    f"{_LIGHT_MUTED}{_ON_WHITE}",
    },
    inherit=True,
)

_THEMES: dict[ThemeName, Theme] = {"dark": DARK_THEME, "light": LIGHT_THEME}

# Текущая активная тема. Внутреннее состояние модуля. Не изменять напрямую
# из других модулей — используйте apply_theme().
_current: ThemeName = "dark"


def current_theme() -> ThemeName:
    """Возвращает имя активной темы."""
    return _current


def detect_default_theme() -> ThemeName:
    """Тема по умолчанию: ENV APEXCORE_THEME, иначе dark."""
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    if raw in _VALID_NAMES:
        return raw  # type: ignore[return-value]
    return "dark"


# ─── OSC-коды для смены фона/текста окна терминала ────────────────────
# OSC 11 — установить background color, OSC 10 — foreground.
# OSC 111/110 — сброс к default из профиля терминала.
# `\x1b]` — OSC introducer, `\x07` (BEL) — терминатор (xterm BEL-форма
# поддерживается шире чем ST-форма `\x1b\\`). Работает в Windows Terminal,
# iTerm2, kitty, alacritty, xterm. Не работает в старом conhost.exe
# (Windows 8 и раньше) — там просто проигнорируется.
_OSC_BG_SET   = f"\x1b]11;{_LIGHT_BG}\x07"
_OSC_FG_SET   = f"\x1b]10;{_LIGHT_FG}\x07"
_OSC_BG_RESET = "\x1b]111\x07"
_OSC_FG_RESET = "\x1b]110\x07"

_atexit_registered = False


def _stdout_is_tty() -> bool:
    """OSC коды осмысленны только в реальном терминале. При redirect
    (`apexcore info > out.txt`) или pipe OSC байты попадут в файл и
    сделают его нечитаемым. isatty() надёжно отсекает оба случая."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _restore_terminal_colors() -> None:
    """Сбрасывает фон/текст окна терминала к default из профиля.

    Регистрируется через atexit при первом входе в light theme. Без этого
    `apexcore --theme=light info` оставил бы пользователя в белой консоли
    навсегда (OSC 11 действует до явного сброса или закрытия окна)."""
    if not _stdout_is_tty():
        return
    try:
        sys.stdout.write(_OSC_BG_RESET + _OSC_FG_RESET)
        sys.stdout.flush()
    except Exception:
        # Stream может быть закрыт во время shutdown — игнорим.
        pass


def _enter_light_terminal() -> None:
    """Красит окно терминала в белый фон + чёрный текст через OSC 11/10."""
    global _atexit_registered
    if not _stdout_is_tty():
        return
    try:
        sys.stdout.write(_OSC_BG_SET + _OSC_FG_SET)
        sys.stdout.flush()
    except Exception:
        return
    if not _atexit_registered:
        atexit.register(_restore_terminal_colors)
        _atexit_registered = True


def _leave_light_terminal() -> None:
    """Возвращает фон/текст к default. Вызывается при apply_theme('dark')."""
    if not _stdout_is_tty():
        return
    try:
        sys.stdout.write(_OSC_BG_RESET + _OSC_FG_RESET)
        sys.stdout.flush()
    except Exception:
        pass


def apply_theme(name: str) -> ThemeName:
    """Переключает Rich-console и окно терминала на указанную тему.

    Light = (Rich theme override) + (OSC 11/10 для фона/текста окна).
    Dark  = снимает overrides и сбрасывает OSC 11/10 → терминал возвращается
            к цветам из профиля.

    **Почему НЕ мутируем `console._style`**: вариант "global default bg=white"
    через `console._style` действительно убирает «лоскуты» тёмного фона в
    padding Panel/Table, НО ломает выделение текста в терминале — Windows
    Terminal при selection инвертирует cell bg/fg, и ячейки с явным
    bg=#ffffff превращаются в нечитаемый чёрно-оранжевый. Лучше «лоскутный»
    фон (профиль терминала + явный bg только у текстовых ячеек) который
    нормально выделяется, чем «гладкая» подложка с нечитаемой selection.

    Импортирует `console` лениво, чтобы избежать циклического импорта
    (render.py импортирует theme.py для инициализации).

    Если ``name`` неизвестна — silent fallback на dark с предупреждением
    в stderr (но не raise: тема — не critical-path, не должна валить CLI).
    """
    global _current
    target: ThemeName = name.lower() if name.lower() in _VALID_NAMES else "dark"  # type: ignore[assignment]
    if target != name.lower():
        print(
            f"apexcore: неизвестная тема '{name}', откат на 'dark'. "
            f"Доступные: {', '.join(_VALID_NAMES)}",
            file=sys.stderr,
        )

    from apexcore.interfaces.cli.render import console  # ленивый импорт

    # Снимаем предыдущую тему если была pushed. Rich держит стек тем; без
    # pop он растёт неограниченно при многократных переключениях. На
    # дефолтной теме стек пустой — pop кинет IndexError, suppress = no-op.
    if _current != "dark":
        with contextlib.suppress(Exception):
            console.pop_theme()

    if target == "light":
        console.push_theme(_THEMES["light"])
        _enter_light_terminal()
    else:  # dark
        if _current == "light":
            _leave_light_terminal()

    _current = target
    return target
