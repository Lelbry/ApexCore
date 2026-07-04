"""Тесты ``interfaces/cli/menu/gpu_screen.GpuScreen`` и пункта GPU в HomeScreen.

Главные инварианты:
1. HomeScreen содержит пункт «Оценка производительности GPU» и открывает
   GpuScreen через соответствующий handler.
2. Когда GPU/OpenCL доступен — GpuScreen показывает 3 действия (список /
   полный бенчмарк / одиночный замер) + b/q.
3. Когда GPU/OpenCL НЕ доступен — экран всё равно показывается, но остаётся
   только «Список устройств» + b/q (кнопки прогона скрыты), без исключений.
4. Ошибка загрузки бэкенда трактуется как «GPU недоступен» (экран не падает).
5. Глобальные шорткаты b/q всегда присутствуют.
"""

from __future__ import annotations

import pytest

from apexcore.interfaces.cli.menu.gpu_screen import GpuScreen
from apexcore.interfaces.cli.menu.nav import NavAction
from apexcore.interfaces.cli.menu.screens import HomeScreen


def _labels(items) -> list[str]:
    return [it.label for it in items]


def _keys(items) -> list[str]:
    return [it.key for it in items]


class _FakeBackend:
    """Мини-заглушка GpuComputeBackend для контроля is_available в тестах."""

    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _patch_backend(monkeypatch, *, available: bool) -> None:
    monkeypatch.setattr(
        "apexcore.infrastructure.gpu.build_default_gpu_backend",
        lambda: _FakeBackend(available),
    )


# ─── HomeScreen содержит пункт GPU ──────────────────────────────────────────


def test_home_screen_contains_gpu_item():
    screen = HomeScreen()
    labels = _labels(screen.items())
    assert any("Оценка производительности GPU" in label for label in labels)


def test_home_screen_gpu_handler_pushes_gpu_screen():
    screen = HomeScreen()
    res = screen._gpu()
    assert res.action == NavAction.PUSH
    assert isinstance(res.next_screen, GpuScreen)


# ─── GpuScreen: доступный GPU ───────────────────────────────────────────────


def test_gpu_screen_items_when_available(monkeypatch):
    _patch_backend(monkeypatch, available=True)
    screen = GpuScreen()
    items = screen.items()
    labels = _labels(items)
    # Четыре действия при наличии GPU (список / бенчмарк / стресс / замер).
    assert any("Список" in label for label in labels)
    assert any("Полный бенчмарк" in label for label in labels)
    assert any("Стресс-тест" in label for label in labels)
    assert any("Одиночный замер" in label for label in labels)
    # Глобальные шорткаты.
    keys = _keys(items)
    assert "b" in keys
    assert "q" in keys


def test_gpu_screen_has_stress_item_when_available(monkeypatch):
    """Пункт «Стресс-тест GPU (термостабильность)» есть и привязан к _run_stress."""
    _patch_backend(monkeypatch, available=True)
    screen = GpuScreen()
    stress_items = [
        it for it in screen.items() if "Стресс-тест" in it.label
    ]
    assert len(stress_items) == 1
    assert "термостабильность" in stress_items[0].label
    # Хэндлер — именно метод стресс-прогона экрана.
    assert stress_items[0].handler == screen._run_stress


def test_gpu_screen_stress_hidden_when_unavailable(monkeypatch):
    """Без GPU пункт стресс-теста скрыт (как и остальные прогоны)."""
    _patch_backend(monkeypatch, available=False)
    screen = GpuScreen()
    labels = _labels(screen.items())
    assert not any("Стресс-тест" in label for label in labels)


def test_gpu_screen_run_stress_graceful_without_gpu(monkeypatch):
    """`_run_stress` без GPU возвращает STAY с понятным сообщением, без падения."""
    _patch_backend(monkeypatch, available=False)
    screen = GpuScreen()
    res = screen._run_stress()
    assert res.action == NavAction.STAY
    assert res.flash is not None and "OpenCL" in res.flash


# ─── GpuScreen: GPU недоступен — экран не падает, только список + b/q ────────


def test_gpu_screen_items_when_unavailable(monkeypatch):
    _patch_backend(monkeypatch, available=False)
    screen = GpuScreen()
    items = screen.items()
    labels = _labels(items)
    # Пункт списка остаётся всегда.
    assert any("Список" in label for label in labels)
    # Кнопки прогона скрыты, когда GPU нет.
    assert not any("Полный бенчмарк" in label for label in labels)
    assert not any("Одиночный замер" in label for label in labels)
    keys = _keys(items)
    assert "b" in keys
    assert "q" in keys


def test_gpu_screen_backend_load_error_is_unavailable(monkeypatch):
    """Если фабрика бэкенда бросает — экран считает GPU недоступным, не падает."""

    def _boom():
        raise RuntimeError("нет OpenCL ICD")

    monkeypatch.setattr(
        "apexcore.infrastructure.gpu.build_default_gpu_backend", _boom
    )
    screen = GpuScreen()
    # Не должно быть исключения; кнопки прогона скрыты.
    items = screen.items()
    labels = _labels(items)
    assert not any("Полный бенчмарк" in label for label in labels)
    assert "b" in _keys(items)


def test_gpu_screen_run_full_graceful_without_gpu(monkeypatch):
    """`_run_full` без GPU возвращает STAY с понятным сообщением, без падения."""
    _patch_backend(monkeypatch, available=False)
    screen = GpuScreen()
    res = screen._run_full()
    assert res.action == NavAction.STAY
    assert res.flash is not None and "OpenCL" in res.flash


@pytest.mark.parametrize("available", [True, False])
def test_gpu_screen_back_and_quit_present(monkeypatch, available):
    _patch_backend(monkeypatch, available=available)
    screen = GpuScreen()
    keys = _keys(screen.items())
    assert "b" in keys
    assert "q" in keys
