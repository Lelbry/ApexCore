"""Интеграционный тест SqliteMicroRunRepository (scoring v2)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    MicroBenchSuiteResult,
    OverallScore,
    SystemInfo,
)
from apexcore.infrastructure.persistence import SqliteMicroRunRepository


def _make_suite(
    preset: str = "fast",
    overall_score: float = 1234.5,
    n_runs: int = 1,
) -> MicroBenchSuiteResult:
    info = SystemInfo(
        os_name="Linux",
        os_version="5",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=4, logical=8),
        ram_total_gb=16.0,
        timestamp=datetime.now(timezone.utc),
    )
    overall = OverallScore(
        overall_ratio=overall_score / 1000.0,
        overall_score=overall_score,
        subscores={"R_MEM": 0.8, "R_CPU_compute": 0.9},
        ci_lower=overall_score - 30.0,
        ci_upper=overall_score + 30.0,
        ci_method="t_logscale",
        n_runs=n_runs,
        scoring_version="2.0.0",
    )
    return MicroBenchSuiteResult(
        id=uuid4(),
        system_info=info,
        results=[
            MicroBenchResult(
                name="memory_read",
                category="memory",
                value=80000.0,
                unit="MB/s",
                duration_actual_sec=5.0,
            ),
        ],
        start_time=info.timestamp,
        end_time=info.timestamp,
        duration_sec_per_test=5.0,
        overall=overall,
        preset=preset,
        n_runs=n_runs,
    )


def test_save_get_list_delete(tmp_path):
    repo = SqliteMicroRunRepository(tmp_path / "test.sqlite")
    s1 = _make_suite(preset="fast", overall_score=1100.0)
    s2 = _make_suite(preset="accurate", overall_score=1200.0, n_runs=5)
    repo.save(s1)
    repo.save(s2)

    fetched = repo.get(s1.id)
    assert fetched is not None
    assert fetched.preset == "fast"
    assert fetched.overall is not None
    assert fetched.overall.overall_score == 1100.0
    assert fetched.overall.scoring_version == "2.0.0"

    all_runs = repo.list_runs(limit=10)
    assert len(all_runs) == 2

    accurate_only = repo.list_runs(limit=10, preset="accurate")
    assert len(accurate_only) == 1
    assert accurate_only[0].id == s2.id

    assert repo.delete(s1.id) is True
    assert repo.get(s1.id) is None
    repo.close()


def test_resolve_id_by_prefix(tmp_path):
    repo = SqliteMicroRunRepository(tmp_path / "test.sqlite")
    s = _make_suite()
    repo.save(s)

    full = repo.resolve_id(str(s.id)[:8])
    assert full == str(s.id)

    assert repo.resolve_id("nonexistent_xyz") is None
    repo.close()


def test_save_without_overall(tmp_path):
    """Suite без overall — save должен работать с NULL-индексами."""
    repo = SqliteMicroRunRepository(tmp_path / "test.sqlite")
    s = _make_suite()
    s.overall = None  # legacy/standalone runs могут не иметь overall
    s.preset = None
    repo.save(s)

    fetched = repo.get(s.id)
    assert fetched is not None
    assert fetched.overall is None
    assert fetched.preset is None
    repo.close()


def test_list_runs_ordered_by_start_time_desc(tmp_path):
    """Список — DESC по start_time."""
    repo = SqliteMicroRunRepository(tmp_path / "test.sqlite")
    info = SystemInfo(
        os_name="Linux",
        os_version="5",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=4, logical=8),
        ram_total_gb=16.0,
        timestamp=datetime.now(timezone.utc),
    )
    early = MicroBenchSuiteResult(
        id=uuid4(),
        system_info=info,
        results=[],
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_sec_per_test=5.0,
    )
    late = MicroBenchSuiteResult(
        id=uuid4(),
        system_info=info,
        results=[],
        start_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        duration_sec_per_test=5.0,
    )
    repo.save(early)
    repo.save(late)

    runs = repo.list_runs(limit=10)
    assert runs[0].id == late.id
    assert runs[1].id == early.id
    repo.close()
