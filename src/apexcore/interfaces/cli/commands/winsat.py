"""CLI-команда ``apexcore winsat`` — Аналог Windows System Assessment Tool.

Подкоманды
----------
- ``apexcore winsat run``    — запустить полную оценку (CPU + Memory + Disk).
- ``apexcore winsat formal`` — алиас для ``run`` (как у настоящего ``winsat formal``).
- ``apexcore winsat query``  — показать последний сохранённый отчёт.
- ``apexcore winsat list``   — последние N прогонов из локальной БД.

Доступно только на Windows. На Linux вызов любой подкоманды печатает
сообщение «недоступно на этой ОС» и выходит с кодом 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(
    help="Аналог Windows Winsat — оценка по шкале 1.0–9.9 (только Windows).",
)


def _ensure_windows() -> None:
    """Прервать команду, если ОС не Windows."""
    if sys.platform != "win32":
        from apexcore.interfaces.cli.render import console

        console.print(
            "[red]Аналог Winsat недоступен на этой ОС.[/] "
            "Модуль работает только на Windows. На Linux используйте "
            "`apexcore micro run` (общий scoring v2)."
        )
        raise typer.Exit(code=2)


@app.command("run")
def run(
    duration: float = typer.Option(
        5.0,
        "--duration",
        "-d",
        help="Длительность каждого подтеста, секунд (по умолчанию 5.0 — стандарт Winsat).",
    ),
    export: Path | None = typer.Option(
        None,
        "--export",
        help="Если задано — записать WinsatReport в этот JSON-файл.",
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        help="Не сохранять результат в локальную БД.",
    ),
) -> None:
    """Запустить полную Winsat-оценку: CPU + Memory + Disk.

    Graphics/D3D в MVP помечены как N/A («Будет в следующем релизе»).
    Итоговый WinSPRLevel считается как минимум среди PASS-подскоров.
    Ctrl+C прерывает прогон с пометкой ``cancelled=True``.
    """
    _ensure_windows()

    from apexcore.application.winsat_service import WinsatService
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.persistence import SqliteWinsatRepository
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.render import (
        console,
        render_winsat_progress,
        render_winsat_report,
        render_winsat_welcome,
    )
    from apexcore.shared.config import load_settings

    settings = load_settings()
    adapter = AdapterFactory.detect()
    repo = None if no_save else SqliteWinsatRepository(settings.db_path)
    service = WinsatService(adapter, repo=repo)

    render_winsat_welcome()
    console.print(
        f"[dim]По {duration:.1f} с на тест × 5 подтестов ≈ {duration * 5:.0f} с суммарно. "
        f"CPU: {adapter.get_system_info().cpu_model}.[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — прервать с сохранением промежуточных значений.")
    console.print()

    progress, advance = render_winsat_progress()
    with cancellable() as token, progress:
        report = service.run_formal(
            duration_sec_per_test=duration,
            cancel_token=token,
            on_progress=advance,
            save=not no_save,
        )

    console.print()
    render_winsat_report(report)

    if export is not None:
        export.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"\n[green]Сохранено в {export}[/]")

    if not no_save and not report.cancelled:
        console.print(
            f"\n[dim]Сохранено в БД: {report.id}. "
            f"Используйте `apexcore winsat query` для просмотра.[/]"
        )


@app.command("formal")
def formal(
    duration: float = typer.Option(
        5.0, "--duration", "-d",
        help="Длительность каждого подтеста, секунд.",
    ),
) -> None:
    """Алиас для ``run`` — в стиле настоящего ``winsat formal``."""
    _ensure_windows()
    run(duration=duration, export=None, no_save=False)


@app.command("query")
def query() -> None:
    """Показать последний сохранённый Winsat-отчёт."""
    _ensure_windows()
    from apexcore.infrastructure.persistence import SqliteWinsatRepository
    from apexcore.interfaces.cli.render import console, render_winsat_report
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteWinsatRepository(settings.db_path)
    runs = repo.list_runs(limit=1)
    if not runs:
        console.print(
            "[yellow]Нет сохранённых winsat-прогонов.[/] "
            "Запустите `apexcore winsat run` для первой оценки."
        )
        raise typer.Exit(code=1)
    render_winsat_report(runs[0])


@app.command("list")
def list_runs_cmd(
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Сколько последних прогонов показать.",
    ),
) -> None:
    """Список последних winsat-прогонов из локальной БД."""
    _ensure_windows()
    from rich.table import Table

    from apexcore.infrastructure.persistence import SqliteWinsatRepository
    from apexcore.interfaces.cli.render import console
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteWinsatRepository(settings.db_path)
    rows = repo.list_runs(limit=limit)
    if not rows:
        console.print("[yellow]Нет сохранённых winsat-прогонов.[/]")
        raise typer.Exit(code=1)

    table = Table(title=f"Последние {len(rows)} winsat-прогонов")
    table.add_column("ID", style="dim cyan", no_wrap=True)
    table.add_column("Дата", style="dim")
    table.add_column("CPU", justify="right")
    table.add_column("Mem", justify="right")
    table.add_column("Disk", justify="right")
    table.add_column("WinSPR", justify="right", style="bold")
    table.add_column("CPU модель", style="dim")
    for r in rows:
        table.add_row(
            str(r.id)[:8],
            r.started_at.strftime("%Y-%m-%d %H:%M"),
            f"{r.cpu_score.score:.1f}",
            f"{r.memory_score.score:.1f}",
            f"{r.disk_score.score:.1f}",
            f"{r.winspr_level:.1f}",
            r.system_info.cpu_model[:30],
        )
    console.print(table)
