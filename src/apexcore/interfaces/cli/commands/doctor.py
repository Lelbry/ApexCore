"""CLI-команда ``apexcore doctor`` — диагностика температурных датчиков.

Прогоняет все известные бэкенды чтения температуры (HWiNFO/CoreTemp
через SHM, LHM/psutil/WMI/hwmon/nvidia-smi), показывает что работает,
что нет, и даёт дифференцированные инструкции по ``DegradedReason``
(HVCI/SAC/Defender/no_admin/...). При флаге ``--repair`` предлагает
self-repair действия: скачать DLL через ``fetch_lhm.ps1``, активировать
bundled .NET 9 runtime.

Доступно также как пункт меню «Настройки → Диагностика датчиков».
"""

from __future__ import annotations

import logging
import platform
import shutil
import sqlite3
import subprocess
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

# Таблицы прогонов, которые ожидает увидеть объединённая лента
# (``runs.collect_unified_listing``). После ``quick_check = ok`` доктор
# дополнительно проверяет, что все они на месте.
_KNOWN_TABLES: tuple[str, ...] = (
    "runs",
    "micro_runs",
    "winsat_runs",
    "general_benchmark_runs",
)

app = typer.Typer(
    help="Диагностика источников температурных датчиков и других сенсоров.",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def doctor(
    repair: bool = typer.Option(
        False,
        "--repair",
        help=(
            "Предложить self-repair действия: скачать LHM DLL через "
            "scripts/fetch_lhm.ps1, активировать bundled .NET 9. "
            "Каждое действие требует подтверждения пользователя."
        ),
    ),
) -> None:
    """Проверить, какие источники температуры работают на этой системе."""
    from apexcore.application.diagnostics_sensors import diagnose_sensors
    from apexcore.interfaces.cli.render import render_sensor_diagnostics

    report = diagnose_sensors()
    render_sensor_diagnostics(report)

    _check_database_health()

    if not repair:
        return

    _run_self_repair(report)


def _check_database_health() -> None:
    """Проверить целостность файла SQLite-БД (``PRAGMA quick_check``).

    Проактивный аналог устойчивого чтения ленты
    (``runs.collect_unified_listing``): там повреждение ловится по факту
    при листинге, здесь — заранее, в диагностике. БД открывается строго
    read-only (URI ``mode=ro``), чтобы не создать пустой файл и не пытаться
    мигрировать/чинить схему (обычный ``_connect`` репозитория вызвал бы
    ``apply_schema`` с записью). ``quick_check`` быстрее полного
    ``integrity_check``; полный запускаем только когда quick_check уже
    что-то нашёл — ради деталей в выводе.
    """
    from apexcore.interfaces.cli.messages import db_corrupt_recovery_hint
    from apexcore.interfaces.cli.render import console
    from apexcore.shared.config import load_settings

    db_path = load_settings().db_path

    console.print()
    console.print("[bold]─── База данных ───[/]")

    if db_path is None or not Path(db_path).exists():
        console.print(
            f"[dim]Файл БД ещё не создан: {db_path}. "
            "История появится после первого прогона.[/]"
        )
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        console.print(f"[red]✗ Не удалось открыть БД:[/] {exc}")
        console.print("  " + db_corrupt_recovery_hint(db_path))
        return

    try:
        problems = [r[0] for r in conn.execute("PRAGMA quick_check;").fetchall() if r[0] != "ok"]
        if not problems:
            console.print(f"[green]✓ Цел[/] · [dim]{db_path}[/]")
            _report_known_tables(conn)
            return
        # quick_check нашёл проблемы — добираем детали полным integrity_check.
        console.print("[red]✗ БД повреждена (PRAGMA quick_check).[/]")
        try:
            detail = [r[0] for r in conn.execute("PRAGMA integrity_check;").fetchall()]
        except sqlite3.DatabaseError:
            detail = problems
        for line in detail[:5]:
            console.print(f"    [dim]{line}[/]")
        console.print("  " + db_corrupt_recovery_hint(db_path))
    except sqlite3.DatabaseError as exc:
        console.print(f"[red]✗ Проверка целостности БД упала:[/] {exc}")
        console.print("  " + db_corrupt_recovery_hint(db_path))
    finally:
        conn.close()


def _report_known_tables(conn: sqlite3.Connection) -> None:
    """После ``quick_check = ok`` проверить наличие 4 таблиц прогонов.

    Молчит, если все на месте (типичный случай) — сообщает только о
    недостающих, чтобы не зашумлять вывод доктора.
    """
    from apexcore.interfaces.cli.render import console

    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.DatabaseError:
        return
    missing = [t for t in _KNOWN_TABLES if t not in existing]
    if missing:
        console.print(
            "[yellow]  ⚠ Отсутствуют таблицы:[/] "
            + ", ".join(missing)
            + " [dim](создадутся при следующей миграции схемы)[/]"
        )


def _run_self_repair(report) -> None:  # type: ignore[no-untyped-def]
    """Интерактивный self-repair по ``SensorDiagnostics.backends``.

    Действует только при подтверждении пользователя (``typer.confirm``).
    Не пытается перерегистрировать WinRing0 — это требует admin и
    LHM-lib сама делает это при первом admin-старте.
    """
    from apexcore.domain.sensor_models import DegradedReason

    typer.echo("")
    typer.secho("─── Self-repair ───", fg=typer.colors.CYAN, bold=True)

    repaired_anything = False

    # 1. DLL отсутствует → предложить fetch_lhm.ps1.
    if DegradedReason.NO_LHM_DLL in report.degraded_reasons:
        if platform.system().lower() != "windows":
            typer.secho(
                "  • DLL LibreHardwareMonitor нужна только на Windows; "
                "на этой ОС ничего делать не надо.",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo(
                "\n  • LHM DLL не найдена в lib/. Запустить fetch_lhm.ps1 "
                "сейчас? Он скачает LHM v0.9.6 с GitHub (~700 КБ), "
                "проверит SHA256 и положит DLL в src/apexcore/"
                "infrastructure/sensors/lib/."
            )
            if typer.confirm("    Скачать?", default=True):
                ok = _run_fetch_lhm_script()
                if ok:
                    typer.secho(
                        "    ✓ LHM DLL установлена. Перезапустите apexcore.",
                        fg=typer.colors.GREEN,
                    )
                    repaired_anything = True
                else:
                    typer.secho(
                        "    ✗ Не удалось скачать DLL. Проверьте интернет "
                        "и корпоративный proxy.",
                        fg=typer.colors.RED,
                    )

    # 2. .NET runtime → подсказать о bundled dotnet/.
    if DegradedReason.NO_DOTNET_RUNTIME in report.degraded_reasons:
        bundled = _find_bundled_dotnet()
        if bundled is not None:
            typer.echo(
                f"\n  • Найден bundled .NET 9 в {bundled}. "
                "Установить env-vars и перезапустить apexcore?"
            )
            if typer.confirm("    Активировать?", default=True):
                typer.echo(
                    "\n    Установите следующие переменные окружения и "
                    "перезапустите apexcore:"
                )
                typer.secho(
                    f"      set APEXCORE_DOTNET_ROOT={bundled}",
                    fg=typer.colors.CYAN,
                )
                typer.secho(
                    f"      set DOTNET_ROOT={bundled}", fg=typer.colors.CYAN
                )
                typer.echo(
                    "    (В .ps1: $env:APEXCORE_DOTNET_ROOT = '...')"
                )
                repaired_anything = True
        else:
            typer.echo(
                "\n  • .NET runtime не найден и bundled-копия отсутствует "
                "в установке apexcore. Установите .NET 9 Desktop Runtime "
                "с https://dotnet.microsoft.com/download."
            )

    # 3. Прочие DegradedReason — self-repair не применим, всё уже в advice.
    if not repaired_anything:
        typer.secho(
            "  Self-repair не выполнен — следуйте советам выше.",
            fg=typer.colors.YELLOW,
        )


def _run_fetch_lhm_script() -> bool:
    """Запустить ``scripts/fetch_lhm.ps1`` через PowerShell.

    Returns ``True`` при rc=0 и появлении DLL в ``sensors/lib/``.
    """
    ps = shutil.which("powershell")
    if ps is None:
        typer.secho("    PowerShell не найден в PATH.", fg=typer.colors.RED)
        return False

    # fetch_lhm.ps1 лежит в scripts/ в корне репозитория. Путь от этого файла:
    #   commands[0] / cli[1] / interfaces[2] / apexcore[3] / src[4] / <root>[5]
    project_root = Path(__file__).resolve().parents[5]
    script = project_root / "scripts" / "fetch_lhm.ps1"
    if not script.exists():
        typer.secho(
            f"    Скрипт не найден: {script}", fg=typer.colors.RED
        )
        return False

    cmd = [
        ps,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    typer.echo(f"    Выполняем: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=False, timeout=120.0)
        if result.returncode != 0:
            typer.secho(
                f"    fetch_lhm.ps1 вернул код {result.returncode}",
                fg=typer.colors.RED,
            )
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.exception("fetch_lhm.ps1 упал")
        typer.secho(f"    Ошибка: {exc!r}", fg=typer.colors.RED)
        return False

    # Проверить что DLL появилась.
    dll = (
        new_app_root
        / "src"
        / "apexcore"
        / "infrastructure"
        / "sensors"
        / "lib"
        / "LibreHardwareMonitorLib.dll"
    )
    return dll.exists()


def _find_bundled_dotnet() -> Path | None:
    """Найти bundled .NET 9 runtime в installer-папке apexcore.

    После P0.8 build_windows.ps1 кладёт .NET 9 в ``<install>/dotnet/``,
    рядом с PyInstaller-исполняемым. На dev-машине (editable install)
    этой папки нет — return None.
    """
    import sys

    candidates = [
        Path(sys.executable).resolve().parent / "dotnet",
        Path(sys.executable).resolve().parents[1] / "dotnet",
    ]
    for c in candidates:
        if (c / "shared" / "Microsoft.NETCore.App").exists():
            return c
        if (c / "host" / "fxr").exists():
            return c
    return None
