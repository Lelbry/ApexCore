"""CLI-команда `apexcore export` — выгрузить прогон в JSON/CSV."""

from pathlib import Path

import typer


def export(
    run_id: str = typer.Argument(..., help="UUID/префикс прогона."),
    fmt: str = typer.Option("json", "--format", "-f", help="json | csv."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Путь сохранения. По умолчанию — рядом."),
) -> None:
    """Экспортировать прогон по UUID."""
    from apexcore.infrastructure.exporters import export_run_csv, export_run_json
    from apexcore.infrastructure.persistence import SqliteResultRepository
    from apexcore.interfaces.cli.render import console
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)
    rid = repo.resolve_id(run_id)
    if rid is None:
        console.print(f"[red]Прогон '{run_id}' не найден[/]")
        raise typer.Exit(code=2)
    result = repo.get(rid)
    if result is None:
        console.print(f"[red]Прогон '{run_id}' не найден[/]")
        raise typer.Exit(code=2)

    if fmt == "json":
        target = out or Path(f"apexcore_run_{result.id}.json")
        export_run_json(result, target)
    elif fmt == "csv":
        target = out or Path(f"apexcore_run_{result.id}.csv")
        export_run_csv(result, target)
    else:
        console.print(f"[red]Неизвестный формат '{fmt}', допустимы: json, csv[/]")
        raise typer.Exit(code=2)
    console.print(f"[green]Экспортировано → {target}[/]")
