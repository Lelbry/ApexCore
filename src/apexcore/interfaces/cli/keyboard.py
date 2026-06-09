"""Кросс-платформенный non-blocking keyboard input для интерактивных
TUI-экранов (раздел «Датчики» в M5).

Реализация:

- **Windows**: ``msvcrt.kbhit()`` + ``msvcrt.getwch()`` — non-blocking
  чтение wide-char (для unicode-раскладки).
- **Linux/macOS**: ``select.select([sys.stdin], ...)`` + ``termios`` для
  переключения tty в cbreak-mode (читаем по одному символу без Enter,
  но с обработкой Ctrl+C как KeyboardInterrupt).

Использование (см. ``commands/sensors.py``):

.. code-block:: python

    with KeyboardListener() as kb:
        with Live(...) as live:
            while True:
                if kb.has_key():
                    key = kb.read_key()
                    action = classify_key(key)
                    if action is KeyAction.QUIT:
                        break
                # ... update screen
                time.sleep(0.1)
"""

from __future__ import annotations

import contextlib
import logging
import sys
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class KeyAction(str, Enum):
    """Действия UI, на которые реагирует экран «Датчики»."""

    QUIT = "quit"
    BACK = "back"
    COLLAPSE_CORES = "collapse_cores"
    PAUSE = "pause"
    FOCUS_CPU = "focus_cpu"
    FOCUS_GPU = "focus_gpu"
    FOCUS_SYSTEM = "focus_system"
    FOCUS_FANS = "focus_fans"
    OVERVIEW = "overview"


# ─── Карта клавиш ──────────────────────────────────────────────────────────

# Все наборы клавиш содержат RU-эквиваленты на той же физической клавише —
# конвенция, согласованная с ``nav.py:BACK_KEYS/HOME_KEYS/QUIT_KEYS``.

# RU-эквиваленты на тех же физических клавишах поддерживаются на уровне
# функционала (раскладка пользователя), но в подсказке UI отображаются
# только EN-обозначения — чтобы footer не был перегружен дублями.

QUIT_KEYS = frozenset({"q", "Q", "й", "Й", "\x1b"})  # Esc = \x1b
BACK_KEYS = frozenset({"b", "B", "и", "И"})
FOCUS_CPU_KEYS = frozenset({"c", "C", "с", "С"})  # C = CPU
FOCUS_GPU_KEYS = frozenset({"g", "G", "п", "П"})  # G = GPU
FOCUS_MB_KEYS = frozenset({"m", "M", "ь", "Ь"})  # M = Memory/Motherboard (system)
FOCUS_FANS_KEYS = frozenset({"f", "F", "а", "А"})  # F = Fans (вентиляторы)
COLLAPSE_CORES_KEYS = frozenset({"e", "E", "у", "У"})  # E = Expand/collapse ядра
PAUSE_KEYS = frozenset({"p", "P", "з", "З"})
OVERVIEW_KEYS = frozenset({"a", "A", "ф", "Ф", "0"})


def classify_key(key: str) -> KeyAction | None:
    """Сопоставить введённый символ с действием UI. None если не распознан."""
    if not key:
        return None
    if key in QUIT_KEYS:
        return KeyAction.QUIT
    if key in BACK_KEYS:
        return KeyAction.BACK
    if key in COLLAPSE_CORES_KEYS:
        return KeyAction.COLLAPSE_CORES
    if key in PAUSE_KEYS:
        return KeyAction.PAUSE
    if key in FOCUS_CPU_KEYS:
        return KeyAction.FOCUS_CPU
    if key in FOCUS_GPU_KEYS:
        return KeyAction.FOCUS_GPU
    if key in FOCUS_MB_KEYS:
        return KeyAction.FOCUS_SYSTEM
    if key in FOCUS_FANS_KEYS:
        return KeyAction.FOCUS_FANS
    if key in OVERVIEW_KEYS:
        return KeyAction.OVERVIEW
    return None


# ─── KeyboardListener ──────────────────────────────────────────────────────


class KeyboardListener:
    """Контекст-менеджер для non-blocking keyboard input.

    Внутри ``__enter__`` переключает tty в cbreak-mode (только Linux/macOS;
    на Windows ``msvcrt`` работает без подготовки). ``__exit__``
    восстанавливает старый mode. Безопасно использовать как ``with``-блок —
    при любом исключении tty восстанавливается.
    """

    def __init__(self) -> None:
        self._impl: _KeyboardImpl = _make_impl()

    def __enter__(self) -> KeyboardListener:
        self._impl.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._impl.stop()

    def has_key(self) -> bool:
        """В буфере ввода есть необработанный символ?"""
        return self._impl.has_key()

    def read_key(self) -> str:
        """Прочитать один символ. Может блокироваться если буфер пуст —
        вызывайте только когда ``has_key()`` вернул True.
        """
        return self._impl.read_key()


# ─── Платформо-зависимая реализация ────────────────────────────────────────


class _KeyboardImpl:
    """Базовый интерфейс. Конкретная реализация — в подклассах."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def has_key(self) -> bool:
        return False

    def read_key(self) -> str:
        return ""


def _make_impl() -> _KeyboardImpl:
    """Фабрика: выбор реализации по платформе."""
    if sys.platform == "win32":
        return _WindowsImpl()
    return _PosixImpl()


class _WindowsImpl(_KeyboardImpl):
    """Windows-реализация через ``msvcrt`` (без подготовки tty)."""

    def __init__(self) -> None:
        try:
            import msvcrt
            self._msvcrt = msvcrt
        except ImportError:  # pragma: no cover — only on win32
            self._msvcrt = None

    def has_key(self) -> bool:
        if self._msvcrt is None:
            return False
        try:
            return bool(self._msvcrt.kbhit())
        except OSError:
            return False

    def read_key(self) -> str:
        if self._msvcrt is None:
            return ""
        try:
            ch = self._msvcrt.getwch()  # wide-char для RU/EN
        except OSError:
            return ""
        # Спецклавиши (стрелки/F-keys) приходят двумя байтами — сбрасываем
        # второй и возвращаем пустую строку (потребитель проигнорирует).
        if ch in ("\x00", "\xe0"):
            with contextlib.suppress(OSError):
                self._msvcrt.getwch()
            return ""
        return ch


class _PosixImpl(_KeyboardImpl):
    """Linux/macOS через termios cbreak-mode + select."""

    def __init__(self) -> None:
        self._old_settings: Any = None
        self._tty_available = sys.stdin.isatty()
        try:
            import select
            import termios
            import tty
            self._select = select
            self._termios = termios
            self._tty = tty
        except ImportError:  # pragma: no cover
            self._select = None
            self._termios = None
            self._tty = None
            self._tty_available = False

    def start(self) -> None:
        if not self._tty_available or self._termios is None or self._tty is None:
            return
        try:
            self._old_settings = self._termios.tcgetattr(sys.stdin.fileno())
            self._tty.setcbreak(sys.stdin.fileno())
        except (self._termios.error, OSError) as exc:
            logger.debug("termios setcbreak failed: %s", exc)
            self._old_settings = None

    def stop(self) -> None:
        if self._old_settings is None or self._termios is None:
            return
        try:
            self._termios.tcsetattr(
                sys.stdin.fileno(),
                self._termios.TCSADRAIN,
                self._old_settings,
            )
        except (self._termios.error, OSError) as exc:
            logger.debug("termios restore failed: %s", exc)
        finally:
            self._old_settings = None

    def has_key(self) -> bool:
        if not self._tty_available or self._select is None:
            return False
        try:
            r, _, _ = self._select.select([sys.stdin], [], [], 0)
            return bool(r)
        except (OSError, ValueError):
            return False

    def read_key(self) -> str:
        if not self._tty_available:
            return ""
        try:
            ch = sys.stdin.read(1)
        except (OSError, UnicodeDecodeError):
            return ""
        return ch
