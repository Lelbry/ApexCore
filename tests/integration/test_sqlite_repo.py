"""Интеграционный тест SQLite-репозитория и круговорота BenchmarkResult."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    CpuCores,
    SystemInfo,
)
from apexcore.infrastructure.persistence import SqliteResultRepository


def _make_result(profile: str = "cpu_heavy") -> BenchmarkResult:
    info = SystemInfo(
        os_name="Linux",
        os_version="5",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=4, logical=8),
        ram_total_gb=16.0,
        gpu_list=[],
        timestamp=datetime.now(timezone.utc),
    )
    cfg = BenchmarkConfig(profile_name=profile, duration_sec=5)
    return BenchmarkResult(
        system_info=info,
        config=cfg,
        start_time=info.timestamp,
        end_time=info.timestamp,
        final_score=0.42,
    )


def test_save_get_list_delete(tmp_path):
    repo = SqliteResultRepository(tmp_path / "test.sqlite")
    r1 = _make_result("cpu_heavy")
    r2 = _make_result("ram_heavy")
    repo.save(r1)
    repo.save(r2)

    fetched = repo.get(r1.id)
    assert fetched is not None
    assert fetched.final_score == r1.final_score

    runs = repo.list_runs(limit=10)
    assert len(runs) == 2

    only_cpu = repo.list_runs(limit=10, profile_name="cpu_heavy")
    assert len(only_cpu) == 1
    assert only_cpu[0].id == r1.id

    full_id = repo.resolve_id(str(r1.id)[:8])
    assert full_id == str(r1.id)

    assert repo.delete(UUID(str(r1.id))) is True
    assert repo.get(r1.id) is None
    repo.close()


def test_resolve_unique_prefix(tmp_path):
    repo = SqliteResultRepository(tmp_path / "u.sqlite")
    r = _make_result()
    repo.save(r)
    full = repo.resolve_id(str(r.id)[:6])
    assert full == str(r.id)
    assert repo.resolve_id("nonexistent_prefix") is None
    repo.close()
