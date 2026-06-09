"""Запуск тестов и стресс-движков с отображением прогресса и поддержкой отмены.

Модуль изолирует логику «прокрутить N тестов с прогресс-баром, ловить
``cancel_token``, корректно собрать результаты». Используется и из меню,
и из CLI-команды ``apexcore micro run``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from apexcore.domain.models import (
    MicroBenchResult,
    MicroBenchSuiteResult,
    SystemInfo,
)
from apexcore.infrastructure.microbench import CancelledError
from apexcore.infrastructure.microbench.base import MicroBench
from apexcore.interfaces.cli.render import console


def _fmt_quick(value: float) -> str:
    """Краткое форматирование значения для прогресс-строки."""
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 100:
        return f"{value:.1f}"
    if value >= 10:
        return f"{value:.2f}"
    return f"{value:.3f}"


def run_microbench_suite(
    tests: list[MicroBench],
    duration_sec: float,
    threads: int,
    sys_info: SystemInfo,
    cancel_token: threading.Event | None = None,
) -> MicroBenchSuiteResult:
    """Прогнать набор микротестов и вернуть собранный suite-результат.

    Прогресс-бар показывает текущий тест, ETA и последнее измеренное
    значение. В строке статуса видна подсказка об отмене. Если
    ``cancel_token`` срабатывает — оставшиеся тесты отмечаются как
    «отменены» (без выполнения), уже частично прошедшие — сохраняются
    с пометкой.
    """
    n = len(tests)
    results: list[MicroBenchResult] = []
    start_time = datetime.now(timezone.utc)
    cancel_token = cancel_token if cancel_token is not None else threading.Event()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}[/]"),
        BarColumn(bar_width=20),
        TextColumn("{task.fields[status]}"),
        TextColumn("[dim]прошло[/]"),
        TimeElapsedColumn(),
        TextColumn("[dim]осталось[/]"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            description=f"[1/{n}] подготовка",
            total=n,
            status="[dim]Ctrl+C — отмена[/]",
        )
        for idx, test in enumerate(tests, start=1):
            if cancel_token.is_set():
                # Помечаем оставшиеся тесты как пропущенные.
                results.append(
                    MicroBenchResult(
                        name=test.name,
                        category=test.category,
                        value=0.0,
                        unit=test.unit,
                        duration_actual_sec=0.0,
                        error="отменено пользователем",
                    )
                )
                progress.update(task, advance=1, status="[yellow]отменено[/]")
                continue

            progress.update(
                task,
                description=f"[{idx}/{n}] {test.name}",
                status="[dim]выполняется… (Ctrl+C — отмена)[/]",
            )
            if not test.is_available():
                results.append(
                    MicroBenchResult(
                        name=test.name,
                        category=test.category,
                        value=0.0,
                        unit=test.unit,
                        duration_actual_sec=0.0,
                        error="недоступен в этой среде",
                    )
                )
                progress.update(task, advance=1, status="[yellow]пропущен[/]")
                continue

            try:
                res = test.run(
                    duration_sec=duration_sec,
                    threads=threads if threads > 0 else None,
                    cancel_token=cancel_token,
                )
                results.append(res)
                if cancel_token.is_set():
                    progress.update(
                        task,
                        advance=1,
                        status=f"[yellow]частично: {_fmt_quick(res.value)} {res.unit}[/]",
                    )
                else:
                    progress.update(
                        task,
                        advance=1,
                        status=f"[green]{_fmt_quick(res.value)} {res.unit}[/]",
                    )
            except CancelledError:
                results.append(
                    MicroBenchResult(
                        name=test.name,
                        category=test.category,
                        value=0.0,
                        unit=test.unit,
                        duration_actual_sec=0.0,
                        error="отменено пользователем",
                    )
                )
                progress.update(task, advance=1, status="[yellow]отменено[/]")
            except Exception as e:
                results.append(
                    MicroBenchResult(
                        name=test.name,
                        category=test.category,
                        value=0.0,
                        unit=test.unit,
                        duration_actual_sec=0.0,
                        error=str(e),
                    )
                )
                progress.update(
                    task,
                    advance=1,
                    status=f"[red]ошибка: {e}[/]"[:60],
                )

    end_time = datetime.now(timezone.utc)
    return MicroBenchSuiteResult(
        system_info=sys_info,
        results=results,
        start_time=start_time,
        end_time=end_time,
        duration_sec_per_test=duration_sec,
        threads=threads,
    )


__all__ = ["run_microbench_suite"]
