"""Протокол микробенчмарка и общие утилиты замера.

Каждый микротест реализует ``MicroBench`` — три атрибута (``name``,
``category``, ``unit``) и два метода (``is_available``, ``run``).
Сам цикл «выполняй пока не кончилось время» вынесен в ``time_loop``,
чтобы не дублировать timing-логику в каждом файле.

Поддержка отмены
----------------
``time_loop`` принимает опциональный ``cancel_token`` (``threading.Event``).
Если флаг выставлен — цикл прерывается между итерациями. Внутри одной
итерации (matmul, JIT-ядро) прерывание невозможно, гранулярность отмены
≈ длительности одной итерации (порядка 10–50 мс). При срабатывании
отмены ``time_loop`` возвращает то, что успел сделать; вызывающий код
сам решает, помечать ли результат как отменённый.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol


class CancelledError(Exception):
    """Тест был прерван пользователем (Ctrl+C / меню)."""


class MicroBench(Protocol):
    """Контракт микробенчмарка."""

    name: str
    category: str
    unit: str

    def is_available(self) -> bool:
        """Доступен ли тест в текущей среде (зависимости/ОС)."""
        ...

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ):
        """Прогнать тест и вернуть ``MicroBenchResult``.

        Если передан ``cancel_token`` и он становится set во время работы
        теста — реализация должна корректно прерваться (бросить
        ``CancelledError`` либо вернуть частичный результат с error).
        """
        ...


def time_loop(
    work_fn: Callable[[], None],
    duration_sec: float,
    warmup_calls: int = 1,
    cancel_token: threading.Event | None = None,
) -> tuple[int, float]:
    """Запустить ``work_fn()`` в цикле не менее ``duration_sec`` секунд.

    Перед измерением выполняется ``warmup_calls`` прогревочных вызовов
    (кэш CPU, JIT, первая аллокация и т.п.) — их время не считается.

    Если ``cancel_token`` выставлен между итерациями — цикл прерывается
    и бросается ``CancelledError``.

    Возвращает ``(iterations, elapsed_sec)``.
    """
    for _ in range(max(warmup_calls, 0)):
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("отменено до измерения")
        work_fn()

    iterations = 0
    started = time.perf_counter()
    deadline = started + duration_sec
    while time.perf_counter() < deadline:
        if cancel_token is not None and cancel_token.is_set():
            elapsed = time.perf_counter() - started
            if iterations == 0:
                raise CancelledError("отменено пользователем")
            # Уже что-то намерили — отдаём частичный результат, чтобы
            # вызывающий код мог корректно посчитать throughput на том,
            # что успел.
            return iterations, max(elapsed, 1e-9)
        work_fn()
        iterations += 1
    elapsed = time.perf_counter() - started
    # Если по какой-то причине цикл вообще не отработал (work_fn слишком
    # длинный или отрицательный duration) — гарантируем хотя бы одну итерацию,
    # чтобы не делить на ноль при подсчёте throughput.
    if iterations == 0:
        work_fn()
        iterations = 1
        elapsed = max(time.perf_counter() - started, 1e-9)
    return iterations, elapsed
