"""Интерактивное TUI-меню apexcore.

Модуль реализует навигацию по экранам, настройки длительности тестов и
поддержку отмены активных прогонов (Ctrl+C → cancel_event).

Точка входа — функция ``run_menu()``.
"""

from apexcore.interfaces.cli.menu.app import run_menu

__all__ = ["run_menu"]
