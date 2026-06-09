"""Тесты `application/single_multi_compare.py` — Single/Multi runner."""

from __future__ import annotations

import threading

import pytest

from apexcore.application import single_multi_compare as smc
from apexcore.domain.models import MicroBenchResult


class FakeBench:
    """Простой мок-бенчмарк, фиксирующий вызовы и возвращающий заданный value."""

    name = "fake_int"
    category = "integer"
    unit = "GIOPS"

    def __init__(self, single_value: float = 2.0, multi_value: float = 30.0) -> None:
        self._single = single_value
        self._multi = multi_value
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        # Считаем сколько параллельных вызовов запущено сейчас — для проверки
        # что multi реально запускает несколько потоков одновременно.
        self._in_flight = 0
        self.max_in_flight = 0

    def is_available(self) -> bool:
        return True

    def run(self, duration_sec, threads=None, cancel_token=None):
        # Имитируем работу + фиксируем concurrency.
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.calls.append(
                {"duration": duration_sec, "threads": threads, "tid": threading.get_ident()}
            )
        try:
            # Поспать чуть-чуть, чтобы потоки реально пересеклись.
            import time as _time
            _time.sleep(0.05)
        finally:
            with self._lock:
                self._in_flight -= 1
        # Возвращаем разное значение в зависимости от того, как нас вызвали:
        # ориентируемся на текущее число параллельных. Это позволяет проверить
        # что Multi реально запустил несколько потоков.
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=self._single if self.max_in_flight == 1 else self._multi,
            unit=self.unit,
            duration_actual_sec=duration_sec,
            iterations=100,
            threads=threads or 1,
            extra={"backend": "fake"},
        )


# ─────────────────────────── choose_pinned_cpu ────────────────────────────


def test_choose_pinned_cpu_uses_hybrid_p_cpus(monkeypatch):
    """На гибридном CPU pinned = первый P-cpu."""
    from apexcore.infrastructure import cpu_topology

    monkeypatch.setattr(smc, "is_supported", lambda: True)
    monkeypatch.setattr(
        smc,
        "detect_hybrid_topology",
        lambda: cpu_topology.HybridTopology(
            p_cores=8,
            e_cores=8,
            p_threads=16,
            e_threads=8,
            p_cpus=tuple(range(16)),
            e_cpus=tuple(range(16, 24)),
        ),
    )
    cpu, kind = smc.choose_pinned_cpu()
    assert cpu == 0
    assert kind == "P-core"


def test_choose_pinned_cpu_non_hybrid_returns_cpu0(monkeypatch):
    monkeypatch.setattr(smc, "is_supported", lambda: True)
    monkeypatch.setattr(smc, "detect_hybrid_topology", lambda: None)
    cpu, kind = smc.choose_pinned_cpu()
    assert cpu == 0
    assert kind is None


def test_choose_pinned_cpu_unsupported_returns_none(monkeypatch):
    monkeypatch.setattr(smc, "is_supported", lambda: False)
    cpu, kind = smc.choose_pinned_cpu()
    assert cpu is None
    assert kind is None


# ─────────────────────────── run_single_multi_compare ────────────────────────


def test_run_single_multi_compare_basic(monkeypatch):
    """Проверка: оба замера выполняются, в Multi запущено N воркеров."""
    monkeypatch.setattr(smc, "is_supported", lambda: False)
    monkeypatch.setattr(smc, "detect_hybrid_topology", lambda: None)

    bench = FakeBench(single_value=2.0, multi_value=30.0)
    result = smc.run_single_multi_compare(
        bench=bench, duration_sec=0.1, total_threads=4
    )

    assert result.bench_name == "fake_int"
    assert result.cores_used_multi == 4
    assert result.single.value == 2.0
    assert result.multi.value > 2.0  # сумма по 4 потокам
    assert result.multi.threads == 4

    # Проверим что воркеры реально пересекались (max_in_flight ≥ 2).
    assert bench.max_in_flight >= 2


def test_run_single_multi_progress_callback(monkeypatch):
    """progress_cb получает 'Single-Core' и затем 'Multi-Core'."""
    monkeypatch.setattr(smc, "is_supported", lambda: False)
    monkeypatch.setattr(smc, "detect_hybrid_topology", lambda: None)

    received: list[str] = []
    bench = FakeBench()
    smc.run_single_multi_compare(
        bench=bench, duration_sec=0.05, total_threads=2, progress_cb=received.append
    )
    assert received == ["Single-Core", "Multi-Core"]


def test_run_single_multi_pinned_cpu_recorded(monkeypatch):
    """Если детектор вернул pinned_cpu — он сохраняется в результате."""
    from apexcore.infrastructure import cpu_topology

    monkeypatch.setattr(smc, "is_supported", lambda: True)
    monkeypatch.setattr(
        smc,
        "detect_hybrid_topology",
        lambda: cpu_topology.HybridTopology(
            p_cores=2,
            e_cores=2,
            p_threads=4,
            e_threads=2,
            p_cpus=(0, 1, 2, 3),
            e_cpus=(4, 5),
        ),
    )

    bench = FakeBench()
    result = smc.run_single_multi_compare(
        bench=bench, duration_sec=0.05, total_threads=2
    )
    assert result.pinned_cpu == 0
    assert result.pinned_kind == "P-core"


def test_run_single_multi_speedup_efficiency():
    """SingleMultiResult.speedup и efficiency считаются из values."""
    from apexcore.domain.models import MicroBenchResult, SingleMultiResult

    single = MicroBenchResult(
        name="x", category="c", value=2.0, unit="GIOPS",
        duration_actual_sec=1.0, threads=1,
    )
    multi = MicroBenchResult(
        name="x", category="c", value=30.0, unit="GIOPS",
        duration_actual_sec=1.0, threads=24,
    )
    r = SingleMultiResult(
        bench_name="x", duration_sec_per_test=1.0,
        single=single, multi=multi, cores_used_multi=24,
    )
    assert r.speedup == pytest.approx(15.0)
    assert r.efficiency == pytest.approx(15.0 / 24)


def test_run_single_multi_zero_single_value():
    """Если Single вернул 0 — speedup/efficiency None, не ZeroDivisionError."""
    from apexcore.domain.models import MicroBenchResult, SingleMultiResult

    single = MicroBenchResult(
        name="x", category="c", value=0.0, unit="GIOPS",
        duration_actual_sec=1.0, threads=1,
    )
    multi = MicroBenchResult(
        name="x", category="c", value=30.0, unit="GIOPS",
        duration_actual_sec=1.0, threads=24,
    )
    r = SingleMultiResult(
        bench_name="x", duration_sec_per_test=1.0,
        single=single, multi=multi, cores_used_multi=24,
    )
    assert r.speedup is None
    assert r.efficiency is None


def test_run_multi_aggregates_throughput(monkeypatch):
    """Multi-замер агрегирует value (сумма потоков), iterations (тоже)."""
    monkeypatch.setattr(smc, "is_supported", lambda: False)
    monkeypatch.setattr(smc, "detect_hybrid_topology", lambda: None)

    class TrivialBench:
        name = "trivial"
        category = "c"
        unit = "GIOPS"

        def is_available(self) -> bool:
            return True

        def run(self, duration_sec, threads=None, cancel_token=None):
            return MicroBenchResult(
                name=self.name, category=self.category,
                value=3.0, unit=self.unit,
                duration_actual_sec=duration_sec,
                iterations=10, threads=1, extra={"backend": "fake"},
            )

    result = smc.run_single_multi_compare(
        bench=TrivialBench(), duration_sec=0.05, total_threads=4
    )
    # 4 воркера × 3.0 = 12.0
    assert result.multi.value == pytest.approx(12.0)
    # 4 × 10 = 40
    assert result.multi.iterations == 40
    assert result.multi.threads == 4
