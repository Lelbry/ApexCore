"""CLI-команда `apexcore micro` — расширенное тестирование процессора.

Подкоманды
----------
- ``apexcore micro list``  — показать все доступные тесты.
- ``apexcore micro run``   — прогнать набор и вывести таблицу результатов.

Во время прогона показывается прогресс-бар с текущим тестом, ETA и
последним полученным значением. После завершения — итоговая таблица,
сгруппированная по категориям (Memory, FLOPS, IOPS, Crypto, Fractals).

Если ``--duration`` не передан — берётся значение из пользовательских
настроек меню (``menu_settings.yaml``). Это даёт согласованное поведение
между «прямым» вызовом из консоли и запуском того же теста из меню.
Во время выполнения Ctrl+C прерывает работу и выходит на ту же таблицу
с пометкой «отменено».
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Расширенное тестирование процессора (Memory, FLOPS, IOPS, AES, SHA-1, Fractals).",
)


@app.command("list")
def list_tests() -> None:
    """Показать перечень микробенчмарков и их доступность в текущей среде."""
    from rich.table import Table

    from apexcore.infrastructure.microbench import build_default_microbench_registry
    from apexcore.interfaces.cli.render import console

    tests = build_default_microbench_registry()
    tbl = Table(title="Расширенное тестирование процессора")
    tbl.add_column("Имя", style="bold cyan")
    tbl.add_column("Категория")
    tbl.add_column("Единицы")
    tbl.add_column("Доступен")
    for t in tests:
        avail = "[green]да[/]" if t.is_available() else "[red]нет[/]"
        tbl.add_row(t.name, t.category, t.unit, avail)
    console.print(tbl)
    console.print(
        "[dim]Каждый тест занимает несколько секунд. См. `apexcore micro run --help`[/]"
    )


@app.command("run")
def run(
    duration: float = typer.Option(
        0.0, "--duration", "-d",
        help="Длительность каждого теста, секунд (0 — взять из настроек меню).",
    ),
    threads: int = typer.Option(
        -1, "--threads", "-t",
        help="Количество потоков (0 = авто, -1 = взять из настроек меню).",
    ),
    select: str = typer.Option(
        "", "--tests",
        help="Подмножество тестов через запятую (по именам). Пусто = все.",
    ),
    preset: str = typer.Option(
        "", "--preset",
        help=(
            "Пресет точности для итогового балла: fast (1 прогон), "
            "standard (3 прогона, median-of-3), accurate (5 прогонов с CI). "
            "Пусто = старый режим (одиночный прогон без скоринга, для совместимости)."
        ),
    ),
) -> None:
    """Прогнать набор микробенчмарков и распечатать итоговую таблицу.

    С флагом ``--preset`` (fast/standard/accurate) используется scoring v2:
    - запускается N прогонов согласно пресету (1/3/5),
    - вычисляется итоговый балл по Roofline (1000 = 100% архитектурного пика),
    - для accurate-пресета считается 95% CI на лог-шкале по t-распределению,
    - результат сохраняется в БД (таблица micro_runs).

    Без ``--preset`` — старое поведение (один прогон, таблица результатов
    без итогового балла).
    """
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.microbench import build_default_microbench_registry
    from apexcore.interfaces.cli.menu import settings_store
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.menu.runners import run_microbench_suite
    from apexcore.interfaces.cli.render import console, render_microbench_suite

    menu_settings = settings_store.load_menu_settings()
    if duration <= 0.0:
        duration = menu_settings.durations.micro
    if threads < 0:
        threads = menu_settings.threads

    # Парсим селект.
    selected_names: set[str] | None = None
    if select.strip():
        all_tests = build_default_microbench_registry()
        wanted = {s.strip() for s in select.split(",") if s.strip()}
        unknown = wanted - {t.name for t in all_tests}
        if unknown:
            console.print(
                f"[red]Неизвестные тесты:[/] {', '.join(sorted(unknown))}\n"
                f"Доступны: {', '.join(t.name for t in all_tests)}"
            )
            raise typer.Exit(code=2)
        selected_names = wanted

    # Если пресет указан — путь scoring v2 через ScoringService.
    if preset.strip():
        if preset not in ("fast", "standard", "accurate"):
            console.print(
                f"[red]Неизвестный пресет: {preset!r}.[/] "
                f"Допустимы: fast / standard / accurate."
            )
            raise typer.Exit(code=2)
        _run_with_scoring(
            preset=preset,
            duration=duration,
            threads=threads,
            selected_names=selected_names,
        )
        return

    # Иначе — старое поведение (без скоринга, для совместимости).
    tests = build_default_microbench_registry()
    if selected_names is not None:
        tests = [t for t in tests if t.name in selected_names]
    if not tests:
        console.print("[red]Нет тестов для выполнения.[/]")
        raise typer.Exit(code=2)

    adapter = AdapterFactory.detect()
    sys_info = adapter.get_system_info()
    n = len(tests)
    estimated_total = duration * n
    console.print(
        f"[bold]Запуск {n} микробенчмарков[/]   "
        f"[dim]~ {estimated_total:.0f} с суммарно, по {duration:.1f} с на тест[/]"
    )
    console.print(
        f"[dim]CPU: {sys_info.cpu_model}, "
        f"физ. {sys_info.cpu_cores.physical} / лог. {sys_info.cpu_cores.logical}[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — отменить выполнение.")

    with cancellable() as token:
        suite = run_microbench_suite(
            tests=tests,
            duration_sec=duration,
            threads=threads,
            sys_info=sys_info,
            cancel_token=token,
        )

    console.print()
    render_microbench_suite(suite)
    if any(r.error == "отменено пользователем" for r in suite.results):
        console.print("[yellow]Прогон был отменён пользователем.[/]")


def _run_with_scoring(
    preset: str,
    duration: float,
    threads: int,
    selected_names: set[str] | None,
) -> None:
    """Прогон с подсчётом scoring v2 (общая оценка производительности)."""
    from apexcore.application.scoring_service import ScoringService
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.persistence import SqliteMicroRunRepository
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.menu.runners import run_microbench_suite
    from apexcore.interfaces.cli.render import (
        console,
        render_microbench_suite,
        render_overall_score,
    )
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteMicroRunRepository(settings.db_path)
    adapter = AdapterFactory.detect()

    n_runs_map = {"fast": 1, "standard": 3, "accurate": 5}
    n_runs = n_runs_map[preset]
    sys_info = adapter.get_system_info()
    n_tests = len(selected_names) if selected_names else 12
    estimated_total = duration * n_tests * n_runs
    console.print(
        f"[bold]Общая оценка производительности[/]   "
        f"[dim]preset={preset}, прогонов={n_runs}, "
        f"~ {estimated_total:.0f} с суммарно[/]"
    )
    console.print(
        f"[dim]CPU: {sys_info.cpu_model}, "
        f"физ. {sys_info.cpu_cores.physical} / лог. {sys_info.cpu_cores.logical}[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — отменить выполнение.")

    service = ScoringService(
        adapter=adapter,
        repo=repo,
        suite_runner=run_microbench_suite,
    )

    def progress_cb(idx: int, total: int) -> None:
        console.print(f"\n[bold cyan]Прогон {idx}/{total}[/]")

    with cancellable() as token:
        result = service.run_overall(
            preset=preset,
            duration_sec=duration,
            threads=threads,
            cancel_token=token,
            progress=progress_cb,
            save=True,
            selected_workloads=list(selected_names) if selected_names else None,
        )

    console.print()
    render_microbench_suite(result)
    if result.overall is not None:
        render_overall_score(result.overall, preset=preset)
        console.print()
        console.print(
            f"[green]Сохранено в {settings.db_path} (UUID: {result.id})[/]"
        )
    else:
        console.print("[yellow]Прогон отменён до завершения, балл не вычислен.[/]")
