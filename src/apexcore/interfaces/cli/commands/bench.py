"""CLI-команда `apexcore bench` — полные прогоны бенчмарка."""

from __future__ import annotations

import typer

app = typer.Typer(help="Полный прогон бенчмарка с сохранением в БД.")


@app.command("run")
def run_bench(
    profile: str = typer.Option("cpu_heavy", "--profile", "-p", help="Имя профиля нагрузки."),
    duration: float = typer.Option(30.0, "--duration", "-d", help="Длительность каждой стресс-фазы, секунд."),
    rate: float = typer.Option(0.5, "--rate", "-r", help="Интервал телеметрии, секунд."),
    threads: int = typer.Option(0, "--threads", "-t", help="Потоков на стресс-фазу (0 = auto)."),
    baseline: str = typer.Option("", "--baseline", help="Имя baseline для нормализации (опц.)."),
) -> None:
    """Запустить полный прогон бенчмарка и сохранить результат."""
    from apexcore.application.benchmark_service import BenchmarkService
    from apexcore.domain.models import BenchmarkConfig
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.persistence import (
        SqliteBaselineRepository,
        SqliteResultRepository,
    )
    from apexcore.infrastructure.stress import build_default_registry
    from apexcore.interfaces.cli.render import console, render_bench_result
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)
    baseline_repo = SqliteBaselineRepository(settings.db_path)
    adapter = AdapterFactory.detect()
    registry = build_default_registry()

    baseline_id = None
    if baseline:
        b = baseline_repo.find_by_name(baseline)
        if b is None:
            console.print(f"[yellow]Baseline '{baseline}' не найден, прогон без сравнения[/]")
        else:
            baseline_id = b.id

    config = BenchmarkConfig(
        profile_name=profile,
        duration_sec=duration,
        sampling_rate_sec=rate,
        threads=threads if threads > 0 else None,
        baseline_id=baseline_id,
    )

    service = BenchmarkService(
        adapter=adapter,
        registry=registry,
        repo=repo,
        baseline_repo=baseline_repo,
    )
    console.print(f"[bold cyan]Старт прогона профиля '{profile}'…[/]")
    result = service.run(config)
    console.rule("[bold]Готово[/]")
    render_bench_result(result)
    console.print(f"[green]Сохранено в {settings.db_path} (UUID: {result.id})[/]")
