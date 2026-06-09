"""Оркестратор расширенного теста ОЗУ и кеша (Ram&Cache).

Запускает измерения «уровень × операция» и формирует :class:`RamCacheReport`.
Не сохраняет результаты в SQLite — это диагностический тест, аналог
``apexcore info`` или ``apexcore monitor``. Для сохранения используется
JSON-экспорт через CLI/меню.

Полный набор — 16 измерений: 4 уровня (DRAM, L3, L2, L1) × 4 операции
(read, write, copy, latency). Можно запросить любое подмножество
через ``selected_pairs``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from apexcore.domain.cache import (
    BackendName,
    CacheTopology,
    LevelName,
    OperationName,
    RamCacheMetric,
    RamCacheReport,
)
from apexcore.domain.ports import OSAdapter
from apexcore.infrastructure.microbench.base import CancelledError
from apexcore.infrastructure.microbench.ram_cache import (
    HAVE_NUMBA,
    RamCacheBench,
)

# Доли уровня, занимаемые тестовым буфером.
# Значение < 1.0 оставляет запас на стек, код, метаданные numpy.
CACHE_FILL_RATIO = 0.5

# Канонический порядок: уровни сверху вниз, операции слева направо.
LEVELS_ORDER: tuple[LevelName, ...] = ("DRAM", "L3", "L2", "L1")
OPERATIONS_ORDER: tuple[OperationName, ...] = ("read", "write", "copy", "latency")

ProgressCallback = Callable[[LevelName, OperationName, int, int], None]

# Карта строкового имени уровня (lowercase) → каноническое имя.
_LEVEL_BY_LOWER: dict[str, LevelName] = {
    "l1": "L1",
    "l2": "L2",
    "l3": "L3",
    "dram": "DRAM",
}


# ────────── helpers для CLI / меню ──────────


def bench_id(level: LevelName, operation: OperationName) -> str:
    """Канонический идентификатор теста: ``"l1_read"``, ``"dram_latency"`` и т.п."""
    return f"{level.lower()}_{operation}"


def all_test_names() -> list[str]:
    """Все 16 имён тестов в каноническом порядке (DRAM→L3→L2→L1 × R/W/C/L)."""
    return [bench_id(lvl, op) for lvl in LEVELS_ORDER for op in OPERATIONS_ORDER]


def all_test_pairs() -> list[tuple[LevelName, OperationName]]:
    """Все 16 пар (level, operation) в каноническом порядке."""
    return [(lvl, op) for lvl in LEVELS_ORDER for op in OPERATIONS_ORDER]


def parse_test_name(name: str) -> tuple[LevelName, OperationName] | None:
    """Распарсить ``"l1_read"`` / ``"DRAM_copy"`` в пару ``(level, operation)``.

    Регистр игнорируется. Возвращает ``None`` при невалидном имени.
    """
    parts = name.strip().lower().split("_")
    if len(parts) != 2:
        return None
    lvl_raw, op_raw = parts
    if lvl_raw not in _LEVEL_BY_LOWER:
        return None
    if op_raw not in OPERATIONS_ORDER:
        return None
    return _LEVEL_BY_LOWER[lvl_raw], op_raw  # type: ignore[return-value]


# ────────── service ──────────


def _buffer_bytes_for(level_size_bytes: int, level: LevelName) -> int:
    """Подобрать размер тестового буфера под уровень.

    Для cache-уровней — половина размера уровня. Для DRAM — весь объём
    (там DRAM_BUFFER_BYTES уже задан с запасом 256 МБ > LLC).
    """
    if level == "DRAM":
        return level_size_bytes
    return max(int(level_size_bytes * CACHE_FILL_RATIO), 4096)


class RamCacheService:
    """Application-сервис теста Ram&Cache."""

    def __init__(self, adapter: OSAdapter) -> None:
        self._adapter = adapter

    @property
    def backend_default(self) -> BackendName:
        return "numba" if HAVE_NUMBA else "numpy"

    def run(
        self,
        duration_sec_per_metric: float,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        selected_pairs: set[tuple[LevelName, OperationName]] | None = None,
    ) -> RamCacheReport:
        """Прогнать измерения и вернуть отчёт.

        - ``selected_pairs`` — подмножество пар ``(level, operation)``. Если
          ``None`` — гонится полный набор из 16 измерений. Иначе запускаются
          только указанные пары; невыбранные просто отсутствуют в
          ``report.metrics`` (рендер показывает «—» в этих ячейках таблицы).
        - ``cancel_token`` сработал — оставшиеся измерения помечаются как
          отменённые и сохраняются с ``error="отменено пользователем"``.
        - ``on_progress`` (если передан) вызывается перед каждым выбранным
          измерением: ``on_progress(level, operation, index, total)``.
        """
        sys_info = self._adapter.get_system_info()
        topology: CacheTopology = self._adapter.get_cache_topology()
        size_by_level = {lvl.name: lvl.size_bytes for lvl in topology.levels}

        # Список того, что реально нужно прогнать, в каноническом порядке.
        scheduled: list[tuple[LevelName, OperationName]] = [
            (lvl, op)
            for lvl in LEVELS_ORDER
            for op in OPERATIONS_ORDER
            if selected_pairs is None or (lvl, op) in selected_pairs
        ]

        started_at = datetime.now(timezone.utc)
        metrics: list[RamCacheMetric] = []
        cancelled = False
        total = len(scheduled)
        for index, (level, op) in enumerate(scheduled, start=1):
            if cancel_token is not None and cancel_token.is_set():
                cancelled = True
                metrics.append(_cancelled_metric(level, op, self.backend_default))
                continue
            if on_progress is not None:
                on_progress(level, op, index, total)
            buffer_bytes = _buffer_bytes_for(size_by_level[level], level)
            bench = RamCacheBench(level=level, operation=op, buffer_bytes=buffer_bytes)
            try:
                metric = bench.run(
                    duration_sec=duration_sec_per_metric,
                    cancel_token=cancel_token,
                )
            except CancelledError:
                cancelled = True
                metrics.append(_cancelled_metric(level, op, self.backend_default))
                continue
            metrics.append(metric)

        ended_at = datetime.now(timezone.utc)
        return RamCacheReport(
            system_info=sys_info,
            topology=topology,
            metrics=metrics,
            started_at=started_at,
            ended_at=ended_at,
            duration_sec_per_metric=duration_sec_per_metric,
            backend_default=self.backend_default,
            cancelled=cancelled,
        )


def _cancelled_metric(
    level: LevelName,
    op: OperationName,
    backend: BackendName,
) -> RamCacheMetric:
    return RamCacheMetric(
        level=level,
        operation=op,
        value=0.0,
        unit="ns" if op == "latency" else "MB/s",
        backend=backend,
        duration_actual_sec=0.0,
        iterations=0,
        error="отменено пользователем",
    )
