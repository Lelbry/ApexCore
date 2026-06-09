"""Smoke и unit-тесты теста Ram&Cache.

Проверяем:
- ``RamCacheBench`` отрабатывает за короткое время для каждой комбинации
  уровень × операция и возвращает осмысленные значения;
- ``RamCacheService`` собирает 16 метрик в порядке DRAM/L3/L2/L1 ×
  read/write/copy/latency;
- отмена через cancel_token сохраняет уже выполненные метрики;
- модели сериализуются/десериализуются через JSON.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from apexcore.application.ram_cache_service import (
    LEVELS_ORDER,
    OPERATIONS_ORDER,
    RamCacheService,
    all_test_names,
    all_test_pairs,
    bench_id,
    parse_test_name,
)
from apexcore.domain.cache import (
    CacheLevel,
    CacheTopology,
    RamCacheReport,
)
from apexcore.domain.models import CpuCores, MetricSnapshot, SystemInfo
from apexcore.infrastructure.adapters.cache import (
    DEFAULT_L1_BYTES,
    DEFAULT_L2_BYTES,
    DEFAULT_L3_BYTES,
    DRAM_BUFFER_BYTES,
)
from apexcore.infrastructure.microbench.ram_cache import RamCacheBench

DURATION = 0.1  # smoke-бюджет, как в test_microbench.py


# ────────── fake adapter ──────────


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.topology = CacheTopology(
            levels=[
                CacheLevel(name="L1", size_bytes=32 * 1024, source="fallback"),
                CacheLevel(name="L2", size_bytes=256 * 1024, source="fallback"),
                CacheLevel(name="L3", size_bytes=4 * 1024 * 1024, source="fallback"),
                CacheLevel(name="DRAM", size_bytes=DRAM_BUFFER_BYTES, source="fallback"),
            ]
        )

    def get_system_info(self) -> SystemInfo:
        return SystemInfo(
            os_name="Linux",
            os_version="test",
            cpu_model="Test CPU",
            cpu_cores=CpuCores(physical=4, logical=8),
            ram_total_gb=16.0,
            gpu_list=[],
            cpu_arch="x86_64",
            hostname="testhost",
            timestamp=datetime.now(timezone.utc),
        )

    def get_current_metrics(self) -> MetricSnapshot:  # pragma: no cover - не используется
        raise NotImplementedError

    def check_prerequisites(self) -> bool:  # pragma: no cover - не используется
        return True

    def get_available_temps(self):  # pragma: no cover - не используется
        return []

    def get_frequencies_mhz(self):  # pragma: no cover - не используется
        return {}

    def get_cache_topology(self) -> CacheTopology:
        return self.topology


# ────────── RamCacheBench smoke ──────────


@pytest.mark.parametrize("operation", ["read", "write", "copy", "latency"])
def test_bench_dram_runs(operation):
    """DRAM-измерения проходят за DURATION и дают положительное значение."""
    bench = RamCacheBench(level="DRAM", operation=operation, buffer_bytes=4 * 1024 * 1024)
    metric = bench.run(duration_sec=DURATION)
    assert metric.error is None
    assert metric.level == "DRAM"
    assert metric.operation == operation
    assert metric.value > 0
    assert metric.duration_actual_sec > 0
    assert metric.iterations >= 1
    if operation == "latency":
        assert metric.unit == "ns"
    else:
        assert metric.unit == "MB/s"


@pytest.mark.parametrize("level", ["L1", "L2", "L3"])
def test_bench_cache_levels_run(level):
    """Cache-уровни тоже отрабатывают; буферы — 50% от размера уровня."""
    sizes = {"L1": DEFAULT_L1_BYTES, "L2": DEFAULT_L2_BYTES, "L3": DEFAULT_L3_BYTES}
    half = sizes[level] // 2
    bench = RamCacheBench(level=level, operation="read", buffer_bytes=half)
    metric = bench.run(duration_sec=DURATION)
    assert metric.error is None
    assert metric.level == level
    assert metric.value > 0


def test_bench_cancel_immediately_yields_error():
    """cancel_token, выставленный до запуска, должен дать metric.error."""
    bench = RamCacheBench(level="L1", operation="read", buffer_bytes=4096)
    token = threading.Event()
    token.set()
    metric = bench.run(duration_sec=DURATION, cancel_token=token)
    assert metric.error is not None
    assert metric.value == 0.0


# ────────── RamCacheService ──────────


def test_service_returns_16_metrics_in_canonical_order():
    """Service выдаёт 4×4=16 метрик в порядке DRAM/L3/L2/L1 × read/write/copy/latency."""
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    report = service.run(duration_sec_per_metric=DURATION)
    assert isinstance(report, RamCacheReport)
    assert len(report.metrics) == 16
    expected_pairs = [(lvl, op) for lvl in LEVELS_ORDER for op in OPERATIONS_ORDER]
    actual_pairs = [(m.level, m.operation) for m in report.metrics]
    assert actual_pairs == expected_pairs


def test_service_progress_callback_called():
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    calls: list[tuple] = []

    def progress(level, op, idx, total):
        calls.append((level, op, idx, total))

    service.run(duration_sec_per_metric=DURATION, on_progress=progress)
    assert len(calls) == 16
    # Индексы возрастают с 1 до 16, total всегда 16.
    indices = [c[2] for c in calls]
    assert indices == list(range(1, 17))
    assert all(c[3] == 16 for c in calls)


def test_service_cancel_marks_remaining_metrics():
    """После set() cancel_token оставшиеся метрики получают error='отменено пользователем'."""
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    token = threading.Event()

    def progress(level, op, idx, total):
        # Отменяем после 5-й метрики.
        if idx == 5:
            token.set()

    report = service.run(
        duration_sec_per_metric=DURATION,
        cancel_token=token,
        on_progress=progress,
    )
    assert report.cancelled is True
    cancelled_metrics = [m for m in report.metrics if m.error == "отменено пользователем"]
    # Минимум 11 (16 - 5 успешных), может быть больше из-за гонки.
    assert len(cancelled_metrics) >= 11


def test_report_round_trip_json():
    """Pydantic-модель сериализуется и валидно парсится обратно."""
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    report = service.run(duration_sec_per_metric=DURATION)
    raw = report.model_dump_json()
    restored = RamCacheReport.model_validate_json(raw)
    assert restored.duration_sec_per_metric == report.duration_sec_per_metric
    assert len(restored.metrics) == 16
    assert restored.system_info.cpu_model == "Test CPU"


# ────────── helpers: имена тестов ──────────


def test_all_test_names_canonical_order():
    names = all_test_names()
    assert names == [
        "dram_read", "dram_write", "dram_copy", "dram_latency",
        "l3_read", "l3_write", "l3_copy", "l3_latency",
        "l2_read", "l2_write", "l2_copy", "l2_latency",
        "l1_read", "l1_write", "l1_copy", "l1_latency",
    ]


def test_all_test_pairs_aligns_with_names():
    pairs = all_test_pairs()
    assert len(pairs) == 16
    for (lvl, op), name in zip(pairs, all_test_names(), strict=True):
        assert bench_id(lvl, op) == name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("l1_read", ("L1", "read")),
        ("L1_read", ("L1", "read")),
        ("DRAM_latency", ("DRAM", "latency")),
        ("dram_latency", ("DRAM", "latency")),
        ("  l3_copy  ", ("L3", "copy")),
        ("L2_write", ("L2", "write")),
    ],
)
def test_parse_test_name_valid(raw, expected):
    assert parse_test_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "l4_read",          # неизвестный уровень
        "l1_unknown",       # неизвестная операция
        "just_one_word",    # формат не <level>_<op>
        "l1readwrite",      # без подчёркивания
        "l1_read_extra",    # лишний сегмент
    ],
)
def test_parse_test_name_invalid(raw):
    assert parse_test_name(raw) is None


# ────────── selected_pairs ──────────


def test_service_runs_only_selected_pairs():
    """Если задан selected_pairs — выполняются только указанные пары."""
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    selected = {("DRAM", "read"), ("L1", "latency")}
    report = service.run(
        duration_sec_per_metric=DURATION,
        selected_pairs=selected,
    )
    assert len(report.metrics) == 2
    actual = {(m.level, m.operation) for m in report.metrics}
    assert actual == selected
    # Канонический порядок сохраняется (DRAM раньше L1).
    assert report.metrics[0].level == "DRAM"
    assert report.metrics[1].level == "L1"


def test_service_progress_callback_for_selected_pairs():
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    selected = {("L2", "read"), ("L2", "write"), ("L3", "copy")}
    calls: list[tuple] = []

    def progress(level, op, idx, total):
        calls.append((level, op, idx, total))

    service.run(
        duration_sec_per_metric=DURATION,
        on_progress=progress,
        selected_pairs=selected,
    )
    assert len(calls) == 3
    assert all(c[3] == 3 for c in calls)
    assert [c[2] for c in calls] == [1, 2, 3]


def test_service_empty_selected_pairs_returns_no_metrics():
    """Пустой селектор → отчёт без измерений (но валидный)."""
    service = RamCacheService(FakeAdapter())  # type: ignore[arg-type]
    report = service.run(
        duration_sec_per_metric=DURATION,
        selected_pairs=set(),
    )
    assert report.metrics == []
    assert report.cancelled is False
