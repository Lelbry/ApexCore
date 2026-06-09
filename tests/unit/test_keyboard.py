"""Тесты `interfaces/cli/keyboard.py:classify_key`.

Проверяем что hotkeys корректно классифицируются для EN и RU раскладок.
"""

from __future__ import annotations

import pytest

from apexcore.interfaces.cli.keyboard import KeyAction, classify_key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # QUIT
        ("q", KeyAction.QUIT),
        ("Q", KeyAction.QUIT),
        ("й", KeyAction.QUIT),
        ("Й", KeyAction.QUIT),
        ("\x1b", KeyAction.QUIT),  # Esc
        # BACK
        ("b", KeyAction.BACK),
        ("B", KeyAction.BACK),
        ("и", KeyAction.BACK),
        ("И", KeyAction.BACK),
        # FOCUS_CPU (новый бинд C — был T)
        ("c", KeyAction.FOCUS_CPU),
        ("C", KeyAction.FOCUS_CPU),
        ("с", KeyAction.FOCUS_CPU),
        ("С", KeyAction.FOCUS_CPU),
        # COLLAPSE_CORES (новый бинд E — был C)
        ("e", KeyAction.COLLAPSE_CORES),
        ("E", KeyAction.COLLAPSE_CORES),
        ("у", KeyAction.COLLAPSE_CORES),
        ("У", KeyAction.COLLAPSE_CORES),
        # FOCUS_GPU
        ("g", KeyAction.FOCUS_GPU),
        ("п", KeyAction.FOCUS_GPU),
        # FOCUS_SYSTEM
        ("m", KeyAction.FOCUS_SYSTEM),
        ("ь", KeyAction.FOCUS_SYSTEM),
        # FOCUS_FANS (новый бинд F)
        ("f", KeyAction.FOCUS_FANS),
        ("F", KeyAction.FOCUS_FANS),
        ("а", KeyAction.FOCUS_FANS),
        ("А", KeyAction.FOCUS_FANS),
        # OVERVIEW
        ("a", KeyAction.OVERVIEW),
        ("ф", KeyAction.OVERVIEW),
        ("0", KeyAction.OVERVIEW),
        # PAUSE
        ("p", KeyAction.PAUSE),
        ("з", KeyAction.PAUSE),
    ],
)
def test_classify_key_known_bindings(key: str, expected: KeyAction) -> None:
    assert classify_key(key) is expected


@pytest.mark.parametrize("key", ["", "x", "1", "?", "\t", " "])
def test_classify_key_unknown_returns_none(key: str) -> None:
    assert classify_key(key) is None


def test_rebind_c_no_longer_collapses_cores() -> None:
    """Регрессия: C должно фокусировать CPU, не сворачивать ядра."""
    assert classify_key("c") is KeyAction.FOCUS_CPU
    assert classify_key("c") is not KeyAction.COLLAPSE_CORES


def test_rebind_e_now_collapses_cores() -> None:
    """Регрессия: E теперь сворачивает/разворачивает ядра."""
    assert classify_key("e") is KeyAction.COLLAPSE_CORES


def test_t_no_longer_focuses_cpu() -> None:
    """Регрессия: T больше не зарезервирована (раньше — FOCUS_CPU)."""
    assert classify_key("t") is None
