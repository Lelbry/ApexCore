"""Тесты `interfaces/cli/menu/benchmark_screen.BenchmarkScreen`.

Главные инварианты:
1. На Windows BenchmarkScreen содержит 2 главных пункта (комплексный + winsat).
   На Linux Winsat-пункт скрывается, остаётся 1 пункт.
2. Глобальные шорткаты b/q всегда присутствуют.
3. HomeScreen теперь содержит пункт «Общая оценка производительности системы».
4. HomeScreen больше не содержит Winsat прямой ссылкой.
5. История прогонов комплексного бенчмарка живёт в общем HistoryScreen,
   в BenchmarkScreen отдельного пункта истории нет.
"""

from __future__ import annotations

import sys

import pytest

from apexcore.interfaces.cli.menu.benchmark_screen import BenchmarkScreen
from apexcore.interfaces.cli.menu.screens import HomeScreen


def _labels(items) -> list[str]:
    return [it.label for it in items]


def test_home_screen_contains_benchmark_item():
    screen = HomeScreen()
    items = screen.items()
    labels = _labels(items)
    assert any("Общая оценка производительности системы" in label for label in labels)


def test_home_screen_no_longer_has_winsat_directly():
    """Winsat переехал в BenchmarkScreen, в главном меню его быть не должно."""
    screen = HomeScreen()
    items = screen.items()
    labels = _labels(items)
    assert not any("Winsat" in label for label in labels)


def test_home_screen_menu_order():
    """Главное меню должно идти в порядке, согласованном с пользователем:
    1 Инфо · 2 Датчики · 3 Стресс · 4 Общая оценка · 5 CPU · 6 RAM&Cache ·
    7 История ваших тестов · 8 Web UI · 9 Настройки · q Выход.
    """
    screen = HomeScreen()
    items = screen.items()
    by_key = {it.key: it.label for it in items}
    assert by_key["1"].startswith("Информация о системе")
    assert "Датчики" in by_key["2"]
    assert "мониторинг в реальном времени" in by_key["2"]
    assert by_key["3"] == "Стресс-тест системы"
    assert by_key["4"] == "Общая оценка производительности системы"
    assert by_key["5"] == "Расширенное тестирование процессора"
    assert "Расширенный тест оперативной памяти и кеша" in by_key["6"]
    assert "Ram & CPU Cache" in by_key["6"]
    assert by_key["7"] == "История ваших тестов"


def test_benchmark_screen_has_general_benchmark_item():
    screen = BenchmarkScreen()
    items = screen.items()
    labels = _labels(items)
    assert any("Комплексный бенчмарк" in label for label in labels)


def test_benchmark_screen_no_history_item():
    """История комплексного бенчмарка переехала в общий HistoryScreen."""
    screen = BenchmarkScreen()
    items = screen.items()
    labels = _labels(items)
    assert not any("История" in label for label in labels)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Winsat")
def test_benchmark_screen_on_windows_includes_winsat():
    screen = BenchmarkScreen()
    items = screen.items()
    labels = _labels(items)
    assert any("Winsat" in label for label in labels)


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-only check")
def test_benchmark_screen_on_linux_hides_winsat():
    screen = BenchmarkScreen()
    items = screen.items()
    labels = _labels(items)
    assert not any("Winsat" in label for label in labels)


def test_benchmark_screen_back_and_quit_present():
    screen = BenchmarkScreen()
    items = screen.items()
    keys = [it.key for it in items]
    assert "b" in keys
    assert "q" in keys
