"""Главная точка входа CLI: ``apexcore``.

Подключает все подкоманды (info, monitor, stress, bench, runs, export,
webui). Запуск:

    python -m apexcore
    apexcore info
"""

from __future__ import annotations

# CP1251-fix: на Windows дефолтный stdout-кодек cp1251 не умеет ✓/✗/⚠/✔/…,
# которые rich.console печатает в наших таблицах. Без этого `apexcore info`
# из дефолтного cmd.exe падает с UnicodeEncodeError при первом юникод-символе.
# reconfigure доступен с Python 3.7+; errors="replace" гарантирует, что в крайнем
# случае символ становится "?", а не traceback'ом. Сделано в самом начале модуля,
# до любых импортов которые могут писать в stdout.
import sys as _sys
if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import logging
import platform

import typer

from apexcore import __version__
from apexcore.interfaces.cli.commands import bench as bench_cmd
from apexcore.interfaces.cli.commands import micro as micro_cmd
from apexcore.interfaces.cli.commands import ram_cache as ram_cache_cmd
from apexcore.interfaces.cli.commands import runs as runs_cmd
from apexcore.interfaces.cli.commands import stress as stress_cmd
from apexcore.interfaces.cli.commands import winsat as winsat_cmd
from apexcore.interfaces.cli.commands.doctor import doctor as doctor_fn
from apexcore.interfaces.cli.commands.export import export as export_fn
from apexcore.interfaces.cli.commands.info import info as info_fn
from apexcore.interfaces.cli.commands.monitor import monitor as monitor_fn
from apexcore.interfaces.cli.commands.repair import repair_drivers as repair_drivers_fn
from apexcore.interfaces.cli.commands.sensors import sensors as sensors_fn
from apexcore.interfaces.cli.commands.setup import setup as setup_fn
from apexcore.interfaces.cli.commands.webui import webui as webui_fn
from apexcore.interfaces.cli.menu import run_menu
from apexcore.shared.logging_setup import configure_logging

app = typer.Typer(
    name="apexcore",
    help="Кроссплатформенная оценка производительности компьютера (Windows / Astra Linux).",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)

def _monitor_deprecated(*args, **kwargs):
    """Alias `apexcore monitor` — оставлен как deprecated на 1-2 релиза.

    Старые скрипты пользователей продолжают работать; в выводе появляется
    короткое предупреждение с указанием новой команды.
    """
    from rich.panel import Panel

    from apexcore.interfaces.cli.render import console

    console.print(
        Panel(
            "[bold yellow]`apexcore monitor`[/] устарел и будет удалён в следующем "
            "milestone.\nИспользуйте [bold]`apexcore sensors`[/] — новый раздел "
            "«Датчики» с группировкой и hotkeys.",
            border_style="yellow",
            expand=False,
        )
    )
    return monitor_fn(*args, **kwargs)


# Команды сгруппированы через rich_help_panel — порядок групп в --help
# определяется первым появлением каждой панели. Согласовано с боковым
# меню Web UI / TUI: сначала «entry-points», потом разделы тестов в
# порядке полезности, потом история/экспорт, потом служебное, внизу —
# deprecated.
_PANEL_ENTRY = "Запуск"
_PANEL_TESTS = "Тесты и измерения"
_PANEL_HISTORY = "История и экспорт"
_PANEL_UTIL = "Диагностика и обслуживание"
_PANEL_DEPRECATED = "Устаревшее"

# ── Запуск ──
app.command(
    name="menu",
    help="Интерактивное меню apexcore (по умолчанию при запуске без аргументов).",
    rich_help_panel=_PANEL_ENTRY,
)(run_menu)
app.command(
    name="webui",
    help="Запуск локального Web UI (графический интерфейс в браузере).",
    rich_help_panel=_PANEL_ENTRY,
)(webui_fn)
app.command(
    name="info",
    help="Сведения о системе и доступных стресс-утилитах.",
    rich_help_panel=_PANEL_ENTRY,
)(info_fn)

# ── Тесты и измерения (по порядку Web UI меню) ──
app.command(
    name="sensors",
    help="Раздел «Датчики»: live-просмотр CPU/GPU/Memory/MB/Storage с группировкой.",
    rich_help_panel=_PANEL_TESTS,
)(sensors_fn)
app.add_typer(stress_cmd.app, name="stress", rich_help_panel=_PANEL_TESTS)
app.add_typer(bench_cmd.app, name="bench", rich_help_panel=_PANEL_TESTS)
app.add_typer(micro_cmd.app, name="micro", rich_help_panel=_PANEL_TESTS)
app.add_typer(ram_cache_cmd.app, name="ram-cache", rich_help_panel=_PANEL_TESTS)
app.add_typer(winsat_cmd.app, name="winsat", rich_help_panel=_PANEL_TESTS)

# ── История и экспорт ──
app.add_typer(runs_cmd.app, name="runs", rich_help_panel=_PANEL_HISTORY)
app.command(
    name="export",
    help="Экспорт прогона в JSON или CSV.",
    rich_help_panel=_PANEL_HISTORY,
)(export_fn)

# ── Диагностика и обслуживание ──
app.command(
    name="doctor",
    help="Диагностика температурных датчиков (LHM/WMI/hwmon/nvidia-smi).",
    rich_help_panel=_PANEL_UTIL,
)(doctor_fn)
app.command(
    name="setup",
    help="Первый-запуск wizard в браузере (обычно для Astra после .deb).",
    rich_help_panel=_PANEL_UTIL,
)(setup_fn)
app.command(
    name="repair-drivers",
    help="Переустановить PawnIO + apexcore_sensord (UAC, видимое окно). Лечит CPU temp X.",
    rich_help_panel=_PANEL_UTIL,
)(repair_drivers_fn)

# ── Устаревшее (удалить в v0.10.0) ──
app.command(
    name="monitor",
    help="[DEPRECATED] Старая телеметрия. Используйте `apexcore sensors`.",
    rich_help_panel=_PANEL_DEPRECATED,
)(_monitor_deprecated)


def _warn_if_lhm_dll_missing(invoked_subcommand: str | None) -> None:
    """Жёлтый баннер на старте CLI, если на Windows нет LHM-DLL.

    Защита от тихого регресса issue #20: dev-пользователь забыл запустить
    `fetch_lhm.ps1` после `pip install -e ".[dev]"`, и без DLL у
    него нет ни CPU-температуры, ни watchdog'а, ни корректного финального
    отчёта стресса. Текущий путь — заметить это только дойдя до стресса;
    с этим баннером — сразу при любом первом запуске `apexcore`.

    Не срабатывает:
    - не на Windows (DLL вообще не нужна);
    - при `apexcore doctor` — он сам печатает подробный отчёт;
    - при `--version` — выходит раньше через is_eager.
    """
    if invoked_subcommand == "doctor":
        return
    if platform.system().lower() != "windows":
        return
    try:
        from apexcore.infrastructure.sensors.lhm import _LIB_DLL
    except Exception:
        return
    if _LIB_DLL.exists():
        return
    try:
        from rich.panel import Panel

        from apexcore.interfaces.cli.render import console

        console.print(
            Panel(
                "[bold yellow]LibreHardwareMonitorLib.dll не найдена[/]\n\n"
                "Без неё apexcore не сможет считывать температуру CPU "
                "(LHM/WinRing0 не запустится). Скачайте DLL одной командой "
                "из корня репозитория (без админа):\n\n"
                "    [bold]powershell -ExecutionPolicy Bypass "
                "-File \scripts\\fetch_lhm.ps1[/]\n\n"
                "Подробности и полная диагностика: [bold]apexcore doctor[/]",
                border_style="yellow",
                expand=False,
            )
        )
    except Exception:
        # Rich/console недоступны — это не фатально, просто молча выходим.
        # Сама диагностика сработает позже в pre-flight стресса.
        pass


@app.callback()
def root(
    ctx: typer.Context,
    log_level: str = typer.Option(
        "INFO", "--log-level", help="Уровень логирования: DEBUG/INFO/WARN/ERROR.",
    ),
    theme: str = typer.Option(
        "",
        "--theme",
        help="Тема Rich-консоли: dark (по умолчанию) или light (для скриншотов/печати). "
             "Альтернативно — переменная окружения APEXCORE_THEME.",
    ),
    version: bool = typer.Option(False, "--version", help="Показать версию и выйти.", is_eager=True),
) -> None:
    """Глобальные опции."""
    # Deprecation alert первым делом — даже для --version, чтобы пользователи
    # старой команды `benchkit` видели подсказку в любом сценарии.
    # Console-script entry point указывает напрямую на Typer-app, минуя main(),
    # поэтому проверку делаем в callback (root() вызывается всегда).
    _warn_if_invoked_as_benchkit()
    if version:
        typer.echo(f"apexcore {__version__}")
        raise typer.Exit()
    # Тема: priority chain
    #   --theme флаг > APEXCORE_THEME env > menu_settings.yaml cli_theme > dark
    # render.py при импорте уже применил env-уровень (dark или ENV-значение).
    # Здесь — либо явный --theme флаг (перекрывает всё), либо fallback на YAML.
    # apply_saved_theme сам no-op'ит если current_theme() уже не 'dark' —
    # т.е. сохраняет приоритет ENV над YAML.
    if theme:
        from apexcore.interfaces.cli.theme import apply_theme
        apply_theme(theme)
    else:
        from apexcore.interfaces.cli.menu.settings_store import (
            apply_saved_theme,
            load_menu_settings,
        )
        apply_saved_theme(load_menu_settings().cli_theme)
    configure_logging(level=log_level)
    logging.getLogger(__name__).debug("CLI стартовал, версия %s", __version__)
    _warn_if_lhm_dll_missing(ctx.invoked_subcommand)
    # Без подкоманды — запускаем интерактивное меню.
    if ctx.invoked_subcommand is None:
        run_menu()


def _warn_if_invoked_as_benchkit() -> None:
    """Deprecation: команда ``benchkit`` оставлена как alias до v0.10.0.

    Если пользователь явно запустил `benchkit ...` (старое имя), печатаем
    short-warning в stderr один раз за процесс. Сам вызов всё равно
    обрабатывается через apexcore (один и тот же entry point).
    """
    import os as _os
    if not _sys.argv:
        return
    basename = _os.path.basename(_sys.argv[0]).lower()
    # На Windows может быть "benchkit.exe", на Linux — "benchkit"
    if basename in {"benchkit", "benchkit.exe", "benchkit-script.py"}:
        print(
            "apexcore: команда `benchkit` устарела и будет удалена в v0.10.0. "
            "Используйте `apexcore` (тот же функционал).",
            file=_sys.stderr,
        )


def main() -> None:
    """Точка входа консольного скрипта (см. pyproject.toml)."""
    _warn_if_invoked_as_benchkit()
    app()


if __name__ == "__main__":
    main()
