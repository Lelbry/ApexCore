"""Настройка логирования с rich-форматированием."""

from __future__ import annotations

import logging

from rich.logging import RichHandler


def configure_logging(level: str = "INFO") -> None:
    """Настроить корневой логгер apexcore (rich-вывод)."""
    handler = RichHandler(rich_tracebacks=True, show_time=True, show_path=False)
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
        force=True,
    )
    # Понижаем шум сторонних библиотек.
    for noisy in ("numba", "asyncio", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
