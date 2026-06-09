"""Юнит-тесты ScoringService с заглушкой suite_runner."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from apexcore.application.scoring_service import ScoringService
from apexcore.application.weights import load_weights
from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    MicroBenchSuiteResult,
    SystemInfo,
)

# ─── Фейковые компоненты ────────────────────────────────────────────────────


class _FakeAdapter:
    """Адаптер, возвращающий фиксированный SystemInfo."""

    def get_system_info(self) -> SystemInfo:
        return SystemInfo(
            os_name="Linux",
            os_version="6.0",
            cpu_model="Intel Core i7-12700K",
            cpu_cores=CpuCores(physical=8, logical=16),
            ram_total_gb=32.0,
            cpu_arch="AMD64",
            timestamp=datetime.now(timezone.utc),
        )

    def get_current_metrics(self):  # для интерфейса OSAdapter
        raise NotImplementedError

    def check_prerequisites(self) -> bool:
        return True

    def get_available_temps(self) -> list[str]:
        return []

    def get_frequencies_mhz(self) -> dict[str, float]:
        return {}


class _FakeRepo:
    """In-memory micro repo для проверки save()."""

    def __init__(self):
        self.saved: list[MicroBenchSuiteResult] = []

    def save(self, suite: MicroBenchSuiteResult) -> None:
        self.saved.append(suite)

    def get(self, run_id):
        for s in self.saved:
            if str(s.id) == str(run_id):
                return s
        return None

    def list_runs(self, limit=50, preset=None):
        runs = self.saved
        if preset:
            runs = [s for s in runs if s.preset == preset]
        return runs[-limit:]

    def delete(self, run_id):
        before = len(self.saved)
        self.saved = [s for s in self.saved if str(s.id) != str(run_id)]
        return len(self.saved) < before


def _fake_suite_runner_factory(factor: float = 1.0):
    """Создаёт suite_runner, возвращающий идентичные результаты с заданным factor."""

    def runner(tests, duration_sec, threads, sys_info, cancel_token):
        from apexcore.application.references import build_reference
        ref = build_reference(sys_info)
        results = []
        for t in tests:
            ref_val = ref.values.get(t.name)
            if ref_val is None:
                # Тест без reference — оставляем нулевое значение с error,
                # как это сделал бы реальный run при отсутствии fallback.
                results.append(
                    MicroBenchResult(
                        name=t.name, category=t.category,
                        value=0.0, unit=t.unit,
                        duration_actual_sec=duration_sec,
                        error="no_reference",
                    )
                )
                continue
            results.append(
                MicroBenchResult(
                    name=t.name, category=t.category,
                    value=ref_val.value * factor,
                    unit=t.unit,
                    duration_actual_sec=duration_sec,
                )
            )
        ts = datetime.now(timezone.utc)
        return MicroBenchSuiteResult(
            system_info=sys_info,
            results=results,
            start_time=ts,
            end_time=ts,
            duration_sec_per_test=duration_sec,
            threads=threads,
        )

    return runner


# ─── Тесты ──────────────────────────────────────────────────────────────────


def test_run_overall_fast_preset(monkeypatch: pytest.MonkeyPatch):
    """Fast preset: 1 прогон, score = 1000 для identity."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    repo = _FakeRepo()
    service = ScoringService(
        adapter=_FakeAdapter(),
        repo=repo,
        suite_runner=_fake_suite_runner_factory(factor=1.0),
        weights=load_weights("default"),
    )
    result = service.run_overall(preset="fast")
    assert result.overall is not None
    assert result.overall.overall_ratio == pytest.approx(1.0)
    assert result.preset == "fast"
    assert result.n_runs == 1
    # Сохранён в repo
    assert len(repo.saved) == 1


def test_run_overall_standard_3_runs(monkeypatch: pytest.MonkeyPatch):
    """Standard preset: 3 прогона, median-of-3."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    progress_calls = []

    def progress_cb(run_idx, total):
        progress_calls.append((run_idx, total))

    service = ScoringService(
        adapter=_FakeAdapter(),
        repo=None,
        suite_runner=_fake_suite_runner_factory(factor=0.8),
    )
    result = service.run_overall(preset="standard", progress=progress_cb)
    assert result.n_runs == 3
    assert progress_calls == [(1, 3), (2, 3), (3, 3)]
    # 80% от Roofline → overall_ratio ≈ 0.8
    assert result.overall.overall_ratio == pytest.approx(0.8)


def test_run_overall_accurate_with_ci(monkeypatch: pytest.MonkeyPatch):
    """Accurate preset: 5 прогонов с identical factor → CI вырождается в точку."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    service = ScoringService(
        adapter=_FakeAdapter(),
        suite_runner=_fake_suite_runner_factory(factor=1.0),
    )
    result = service.run_overall(preset="accurate")
    assert result.n_runs == 5
    # CI теперь в ratio-шкале (overall_score удалён в 0.9.x).
    assert result.overall.ci_lower == pytest.approx(1.0)
    assert result.overall.ci_upper == pytest.approx(1.0)
    assert result.overall.ci_method == "t_logscale"


def test_run_overall_cancel_before_first(monkeypatch: pytest.MonkeyPatch):
    """Если cancel_token уже set до первого прогона — возвращается пустой suite."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    token = threading.Event()
    token.set()

    service = ScoringService(
        adapter=_FakeAdapter(),
        suite_runner=_fake_suite_runner_factory(),
    )
    result = service.run_overall(preset="standard", cancel_token=token)
    # До первого прогона отмена → возвращается empty suite без overall.
    assert result.overall is None
    assert result.n_runs == 0


def test_run_overall_no_suite_runner_raises():
    service = ScoringService(adapter=_FakeAdapter(), suite_runner=None)
    with pytest.raises(RuntimeError, match="suite_runner"):
        service.run_overall(preset="fast")


def test_run_overall_save_false_skips_repo(monkeypatch: pytest.MonkeyPatch):
    """save=False не вызывает repo.save()."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    repo = _FakeRepo()
    service = ScoringService(
        adapter=_FakeAdapter(),
        repo=repo,
        suite_runner=_fake_suite_runner_factory(),
    )
    service.run_overall(preset="fast", save=False)
    assert len(repo.saved) == 0


def test_run_overall_selected_workloads(monkeypatch: pytest.MonkeyPatch):
    """Можно ограничить прогон только выбранными тестами."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")

    service = ScoringService(
        adapter=_FakeAdapter(),
        suite_runner=_fake_suite_runner_factory(),
    )
    result = service.run_overall(
        preset="fast",
        selected_workloads=["memory_read", "memory_write", "memory_copy"],
    )
    assert len(result.results) == 3
    assert all(r.category == "memory" for r in result.results)


def test_run_overall_unknown_workload_raises():
    service = ScoringService(
        adapter=_FakeAdapter(),
        suite_runner=_fake_suite_runner_factory(),
    )
    with pytest.raises(ValueError, match="не найден"):
        service.run_overall(preset="fast", selected_workloads=["nonexistent_test"])
