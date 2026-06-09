"""Базовые утилиты стресс-движков: многопоточный запуск и параметры.

Каждый встроенный движок реализует короткую функцию ``_run_chunk`` (или работает
с заранее выделенными буферами), а общая логика запуска N потоков и сбора суммарного
throughput живёт здесь, чтобы не дублировать код.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass

from apexcore.shared.timing import now


@dataclass
class WorkerStat:
    """Статистика одного потока: сколько единиц работы выполнено и за какое время."""

    work_units: float = 0.0
    duration_sec: float = 0.0


def resolve_threads(threads: int | None) -> int:
    """Привести параметр threads к фактическому числу: None/<=0 → все логические ядра."""
    if threads is None or threads <= 0:
        return os.cpu_count() or 1
    return threads


def run_threaded_loop(
    work_fn: Callable[[float], float],
    duration_sec: float,
    threads: int,
    cancel_token: threading.Event | None = None,
) -> tuple[float, float]:
    """Запустить ``work_fn(deadline_left_sec)`` в N потоках.

    Каждый поток в цикле вызывает ``work_fn`` и накапливает возвращённое количество
    «единиц работы», пока не истечёт общий дедлайн. Возвращает (сумма единиц, фактическая длительность).

    Если передан внешний ``cancel_token`` (``threading.Event``) и он становится
    set — все воркеры корректно завершаются на следующей проверке (после текущей
    итерации ``work_fn``). Гранулярность отмены — длительность одного куска работы
    (≤0.5 с по умолчанию).
    """
    deadline = now() + duration_sec
    stop_event = threading.Event()
    stats: list[WorkerStat] = [WorkerStat() for _ in range(threads)]

    def cancelled() -> bool:
        return stop_event.is_set() or (cancel_token is not None and cancel_token.is_set())

    def worker(idx: int) -> None:
        local_units = 0.0
        local_start = now()
        while not cancelled():
            remaining = deadline - now()
            if remaining <= 0:
                break
            try:
                local_units += work_fn(min(remaining, 0.5))
            except Exception:
                stop_event.set()
                break
        stats[idx] = WorkerStat(
            work_units=local_units,
            duration_sec=now() - local_start,
        )

    workers = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    started = now()
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=duration_sec + 5.0)
    elapsed = now() - started
    total_units = sum(s.work_units for s in stats)
    return total_units, elapsed
