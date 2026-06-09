"""Точка входа TUI-меню apexcore.

Запускается через ``apexcore menu`` или автоматически при ``apexcore``
без аргументов (см. ``cli/main.py``).
"""

from __future__ import annotations

from rich.panel import Panel

from apexcore.interfaces.cli.menu.nav import MenuLoop
from apexcore.interfaces.cli.menu.screens import HomeScreen
from apexcore.interfaces.cli.menu.settings_store import (
    apply_sparkline_env,
    load_menu_settings,
)
from apexcore.interfaces.cli.render import console


def _print_welcome() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]apexcore[/]\n"
            "Кроссплатформенная оценка производительности компьютера\n"
            "[dim]Windows / Astra Linux · интерактивное меню[/]\n\n"
            "Подсказки: [bold]?[/] — помощь, [bold]b[/] — назад, "
            "[bold]h[/] — главный экран, [bold]q[/] — выход.\n"
            "Во время теста [bold]Ctrl+C[/] отменит выполнение и вернёт в меню.",
            border_style="cyan",
            title="Добро пожаловать",
        )
    )


def run_menu() -> None:
    """Запустить главный цикл интерактивного меню."""
    # Прокинуть сохранённый sparkline-стиль в env до первого рендера —
    # sparkline.py читает только env, без зависимости от меню.
    # Тему YAML cli_theme применяет cli.main:root() — для ВСЕХ команд
    # apexcore, не только menu (важно для скриншотов в отчёт).
    apply_sparkline_env(load_menu_settings().sparkline_style)
    _print_welcome()
    loop = MenuLoop(HomeScreen())
    try:
        loop.run()
    except KeyboardInterrupt:
        # На случай, если Ctrl+C прилетел вне активного теста и не был
        # обработан в MenuLoop.
        console.print("\n[yellow]Прервано пользователем[/]")
    console.print("[dim]До встречи![/]")


__all__ = ["run_menu"]
