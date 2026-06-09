"""Утилиты времени: монотонные таймеры и контекстный измеритель."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


def now() -> float:
    """Высокоточный монотонный таймер, секунды (float)."""
    return time.perf_counter()


@dataclass
class Stopwatch:
    """Простой секундомер для измерения интервалов."""

    started_at: float = field(default_factory=now)

    def reset(self) -> None:
        self.started_at = now()

    def elapsed(self) -> float:
        return now() - self.started_at


@contextmanager
def measure(label: str | None = None) -> Iterator[Stopwatch]:
    """Контекст для замера блока кода:

    >>> with measure("phase") as sw:
    ...     do_work()
    >>> sw.elapsed()
    """
    sw = Stopwatch()
    yield sw
    # label оставлен в API для совместимости и удобства логирования у пользователя.
    _ = label
