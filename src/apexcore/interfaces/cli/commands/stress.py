"""CLI-команда `apexcore stress` — управление стресс-движками."""

from __future__ import annotations

import typer

app = typer.Typer(help="Запуск отдельного стресс-движка (CPU/RAM, builtin или внешний).")


@app.command("list")
def list_engines() -> None:
    """Показать список доступных стресс-движков."""
    from rich.table import Table

    from apexcore.infrastructure.stress import build_default_registry
    from apexcore.interfaces.cli.render import console

    registry = build_default_registry()
    tbl = Table(title="Стресс-движки")
    tbl.add_column("Имя", style="bold")
    tbl.add_column("Категория")
    tbl.add_column("Тип")
    tbl.add_column("Доступен")
    for engine in registry.all():
        kind = "external" if engine.is_external else "builtin"
        avail = "[green]да[/]" if engine.is_available() else "[red]нет[/]"
        tbl.add_row(engine.name, engine.category, kind, avail)
    console.print(tbl)


@app.command("run")
def run_engine(
    engine: str = typer.Option(..., "--engine", "-e", help="Имя стресс-движка (см. `stress list`)."),
    duration: float = typer.Option(15.0, "--duration", "-d", help="Длительность нагрузки, секунд."),
    threads: int = typer.Option(0, "--threads", "-t", help="Количество потоков (0 = все логические ядра)."),
    monitor: bool = typer.Option(True, "--monitor/--no-monitor", help="Параллельно собирать телеметрию."),
) -> None:
    """Запустить один стресс-движок и распечатать результат."""
    from apexcore.application.telemetry_service import InMemoryMetricsBus, TelemetryService
    from apexcore.domain.errors import StressEngineUnavailableError
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.stress import build_default_registry
    from apexcore.interfaces.cli.render import (
        console,
        render_metric_summary,
        render_stress_result,
    )

    registry = build_default_registry()
    eng = registry.get(engine)
    if eng is None:
        console.print(f"[red]Движок '{engine}' не найден.[/] Доступные: {', '.join(e.name for e in registry.all())}")
        raise typer.Exit(code=2)
    if not eng.is_available():
        raise StressEngineUnavailableError(f"Движок {engine} недоступен в текущей среде")

    history = []
    service: TelemetryService | None = None
    if monitor:
        adapter = AdapterFactory.detect()
        bus = InMemoryMetricsBus()
        service = TelemetryService(adapter=adapter, bus=bus, sampling_rate_sec=0.5)
        service.start()

    console.print(f"[bold cyan]Запуск {engine} на {duration:.1f} с, потоков={threads or 'auto'}[/]")
    result = eng.run(duration_sec=duration, threads=threads if threads > 0 else None)

    if service is not None:
        history = service.stop()

    render_stress_result(result)
    if history:
        console.rule("[bold]Телеметрия за время прогона[/]")
        render_metric_summary(history)
