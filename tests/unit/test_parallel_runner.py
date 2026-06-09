"""Юнит-тесты ``application/parallel_runner.py``."""

from __future__ import annotations

import threading
import time

from apexcore.application.parallel_runner import (
    EngineProgress,
    EngineSpec,
    ParallelStressRunner,
)
from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine


class _FakeEngine(StressEngine):
    """Тестовый stub-движок: возвращает заданный throughput за указанное время."""

    is_external = False

    def __init__(
        self,
        name: str,
        category: str = "cpu_int",
        throughput: float = 1.0,
        sleep_for: float = 0.05,
        raise_exc: bool = False,
        respects_cancel: bool = True,
    ) -> None:
        self.name = name
        self.category = category
        self._tput = throughput
        self._sleep = sleep_for
        self._raise = raise_exc
        self._respects_cancel = respects_cancel

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        if self._raise:
            raise RuntimeError(f"{self.name} sabotage")
        deadline = time.monotonic() + min(self._sleep, duration_sec)
        while time.monotonic() < deadline:
            if self._respects_cancel and cancel_token is not None and cancel_token.is_set():
                break
            time.sleep(0.01)
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=time.monotonic() - (deadline - self._sleep),
            throughput=self._tput,
            throughput_unit="ops/s",
            threads=threads or 1,
        )


def test_runs_engines_in_parallel():
    """Два движка по 0.3s каждый должны завершиться за ~0.3s суммарно (а не 0.6)."""
    eng_a = _FakeEngine("eng_a", sleep_for=0.3)
    eng_b = _FakeEngine("eng_b", sleep_for=0.3)
    plan = [EngineSpec(eng_a), EngineSpec(eng_b)]
    runner = ParallelStressRunner()
    started = time.monotonic()
    result = runner.run(plan, duration_sec=1.0)
    elapsed = time.monotonic() - started
    assert len(result.results) == 2
    assert all(r.throughput == 1.0 for r in result.results)
    # Параллельный запуск: два по 0.3s должны уложиться значимо быстрее, чем 0.6s.
    assert elapsed < 0.55


def test_respects_cancel_token():
    eng = _FakeEngine("slow", sleep_for=10.0)
    plan = [EngineSpec(eng)]
    token = threading.Event()
    runner = ParallelStressRunner()

    def cancel_after_short_pause() -> None:
        time.sleep(0.1)
        token.set()

    threading.Thread(target=cancel_after_short_pause, daemon=True).start()
    started = time.monotonic()
    result = runner.run(plan, duration_sec=10.0, cancel_token=token)
    assert time.monotonic() - started < 1.5
    assert result.cancelled is True


def test_engine_failure_isolated():
    """Падение одного движка не валит остальные."""
    eng_ok = _FakeEngine("ok", sleep_for=0.1)
    eng_fail = _FakeEngine("fail", raise_exc=True)
    plan = [EngineSpec(eng_ok), EngineSpec(eng_fail)]
    runner = ParallelStressRunner()
    result = runner.run(plan, duration_sec=1.0)
    assert len(result.results) == 2
    # Первая запись — ok-движок (throughput=1.0). Вторая — fail (throughput=0).
    ok_result = next(r for r in result.results if r.engine == "ok")
    fail_result = next(r for r in result.results if r.engine == "fail")
    assert ok_result.throughput == 1.0
    assert fail_result.error_count == 1
    assert 1 in result.errors


def test_progress_callback_emits_events():
    """on_progress получает события для каждого движка (starting/running/done)."""
    eng = _FakeEngine("eng", sleep_for=0.1)
    plan = [EngineSpec(eng)]
    events: list[EngineProgress] = []
    runner = ParallelStressRunner()
    runner.run(plan, duration_sec=1.0, on_progress=events.append)
    assert any(e.state == "starting" for e in events)
    assert any(e.state == "done" for e in events)
    done_event = next(e for e in events if e.state == "done")
    assert done_event.result is not None
    assert done_event.result.throughput == 1.0


def test_empty_plan_returns_immediately():
    runner = ParallelStressRunner()
    started = time.monotonic()
    result = runner.run(plan=[], duration_sec=1.0)
    assert result.results == []
    assert time.monotonic() - started < 0.1
