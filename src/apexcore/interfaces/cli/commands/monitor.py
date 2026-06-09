"""CLI-команда `apexcore monitor` — живой просмотр метрик с заданной частотой."""

from __future__ import annotations

import time

import typer


def monitor(
    duration: float = typer.Option(10.0, "--duration", "-d", help="Длительность мониторинга, секунд."),
    rate: float = typer.Option(0.5, "--rate", "-r", help="Интервал между отсчётами, секунд."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Не печатать каждый снимок (только сводку)."),
    table_rows: int = typer.Option(20, "--table", help="Сколько последних снимков показать в финальной таблице."),
) -> None:
    """Запустить семплер телеметрии на N секунд и вывести сводку."""
    from apexcore.application.telemetry_service import (
        InMemoryMetricsBus,
        TelemetryService,
    )
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.interfaces.cli.render import (
        console,
        render_metric_snapshot,
        render_metric_summary,
        render_metric_table,
    )

    adapter = AdapterFactory.detect()
    bus = InMemoryMetricsBus()

    if not quiet:
        bus.subscribe(render_metric_snapshot)

    service = TelemetryService(adapter=adapter, bus=bus, sampling_rate_sec=rate)
    console.print(
        f"[bold cyan]Мониторинг {duration:.1f} с, шаг {rate:.2f} с[/]"
    )
    service.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        console.print("[yellow]Прервано пользователем[/]")
    finally:
        history = service.stop()

    console.rule("[bold]Итоги мониторинга[/]")
    render_metric_table(history, max_rows=table_rows)
    render_metric_summary(history)
