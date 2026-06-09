"""CLI-команда ``apexcore ram-cache`` — расширенный тест ОЗУ и кеша.

Подкоманды
----------
- ``apexcore ram-cache list`` — показать перечень из 16 измерений
  (4 уровня × 4 операции) с размерами буферов и единицами измерения.
- ``apexcore ram-cache run`` — прогнать набор и вывести таблицу 4×4 +
  сноску с описанием метрик. Через ``--tests`` можно ограничить набор.

Тест диагностический, не входит в общий балл (scoring v2). Результаты
не сохраняются в SQLite; для сохранения используется ``--export``,
которая пишет :class:`RamCacheReport` в JSON-файл.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    help="Расширенный тест ОЗУ и кеша (Ram&Cache).",
)


@app.command("list")
def list_tests() -> None:
    """Показать перечень тестов: 4 уровня × 4 операции = 16 измерений."""
    from rich.table import Table

    from apexcore.application.ram_cache_service import (
        LEVELS_ORDER,
        OPERATIONS_ORDER,
        _buffer_bytes_for,
        bench_id,
    )
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.microbench.ram_cache import HAVE_NUMBA
    from apexcore.interfaces.cli.render import console, format_buffer_size

    adapter = AdapterFactory.detect()
    topology = adapter.get_cache_topology()
    size_by_level = {lvl.name: lvl.size_bytes for lvl in topology.levels}
    source_by_level = {lvl.name: lvl.source for lvl in topology.levels}
    backend = "numba" if HAVE_NUMBA else "numpy"

    tbl = Table(title="Расширенный тест ОЗУ и кеша — список измерений")
    tbl.add_column("№", style="bold cyan", justify="right")
    tbl.add_column("Имя", style="bold cyan")
    tbl.add_column("Уровень")
    tbl.add_column("Операция")
    tbl.add_column("Единицы")
    tbl.add_column("Размер буфера", justify="right", style="dim")
    tbl.add_column("Источник", style="dim")
    tbl.add_column("Бэкенд", style="dim")

    i = 0
    for level in LEVELS_ORDER:
        buf = _buffer_bytes_for(size_by_level[level], level)
        for op in OPERATIONS_ORDER:
            i += 1
            unit = "ns" if op == "latency" else "MB/s"
            buffer_str = format_buffer_size(buf // 2 if op == "copy" else buf)
            tbl.add_row(
                str(i),
                bench_id(level, op),
                level,
                op,
                unit,
                buffer_str,
                source_by_level[level],
                backend,
            )
    console.print(tbl)
    console.print(
        "[dim]Запуск всех:    apexcore ram-cache run\n"
        "Выборочно:      apexcore ram-cache run --tests dram_read,l3_latency[/]"
    )


@app.command("run")
def run(
    duration: float = typer.Option(
        0.0, "--duration", "-d",
        help="Длительность одного измерения, секунд (0 — взять из настроек меню).",
    ),
    select: str = typer.Option(
        "", "--tests",
        help=(
            "Подмножество тестов через запятую: имена (dram_read, l1_latency) "
            "или номера из `apexcore ram-cache list`. Пусто = все 16."
        ),
    ),
    export: Path | None = typer.Option(
        None, "--export",
        help="Если задано — записать результат в этот JSON-файл.",
    ),
) -> None:
    """Прогнать тест Read/Write/Copy/Latency для DRAM и L1/L2/L3 кеша процессора.

    Под таблицей выводится сноска с описанием каждой метрики
    (¹ Read, ² Write, ³ Copy, ⁴ Latency). Ctrl+C отменяет прогон с
    сохранением уже собранных значений.
    """
    from apexcore.application.ram_cache_service import (
        RamCacheService,
        all_test_names,
        parse_test_name,
    )
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.interfaces.cli.menu import settings_store
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.render import (
        console,
        render_ram_cache_progress,
        render_ram_cache_report,
    )

    menu_settings = settings_store.load_menu_settings()
    if duration <= 0.0:
        duration = menu_settings.durations.ram_cache

    selected_pairs = None
    if select.strip():
        names = all_test_names()
        wanted: set = set()
        unknown: list[str] = []
        for raw in select.split(","):
            t = raw.strip()
            if not t:
                continue
            # Номер из списка (1..16)?
            if t.isdigit():
                idx = int(t) - 1
                if 0 <= idx < len(names):
                    pair = parse_test_name(names[idx])
                    if pair is not None:
                        wanted.add(pair)
                else:
                    unknown.append(t)
                continue
            # Имя?
            pair = parse_test_name(t)
            if pair is None:
                unknown.append(t)
            else:
                wanted.add(pair)
        if unknown:
            console.print(
                f"[red]Неизвестные тесты:[/] {', '.join(unknown)}\n"
                f"Доступны: {', '.join(names)}"
            )
            raise typer.Exit(code=2)
        if not wanted:
            console.print("[red]Не выбран ни один тест.[/]")
            raise typer.Exit(code=2)
        selected_pairs = wanted

    adapter = AdapterFactory.detect()
    service = RamCacheService(adapter)

    n = 16 if selected_pairs is None else len(selected_pairs)
    console.rule("[bold]Расширенный тест ОЗУ и кеша (Ram&Cache)[/]")
    console.print(
        f"[dim]{n} измерений, по {duration:.1f} с на каждое (~ {duration * n:.0f} с суммарно).[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — прервать с сохранением промежуточных значений.")
    console.print()

    progress, advance, finish = render_ram_cache_progress(n)
    with cancellable() as token, progress:
        report = service.run(
            duration_sec_per_metric=duration,
            cancel_token=token,
            on_progress=advance,
            selected_pairs=selected_pairs,
        )
        finish(report.cancelled)

    console.print()
    render_ram_cache_report(report)

    if export is not None:
        export.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print()
        console.print(f"[green]Сохранено в {export}[/]")
