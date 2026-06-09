"""Тесты `infrastructure/persistence/general_benchmark_repo`.

Главные инварианты:
1. apply_schema создаёт таблицу general_benchmark_runs.
2. CRUD: save → get → list → delete.
3. score = None корректно сохраняется и читается.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from apexcore.domain.general_benchmark import GeneralBenchmarkReport
from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.persistence import SqliteGeneralBenchmarkRepository


def _make_sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0",
        cpu_model="Intel i7",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        gpu_list=["RTX 4070"],
        cpu_arch="x86_64",
        hostname="host",
        cpu_base_mhz=3600.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_report(score: float | None = 5500.0) -> GeneralBenchmarkReport:
    now = datetime.now(timezone.utc)
    return GeneralBenchmarkReport(
        system_info=_make_sys_info(),
        started_at=now,
        ended_at=now,
        dgemm_gflops=600.0,
        stream_gb_s=30.0,
        disk_seq_read_mb_s=2500.0,
        disk_random_read_mb_s=500.0,
        disk_seq_write_mb_s=1800.0,
        dgemm_peak_gflops=2000.0,
        stream_peak_gb_s=50.0,
        disk_seq_read_peak_mb_s=3500.0,
        disk_random_read_peak_mb_s=600.0,
        disk_seq_write_peak_mb_s=2500.0,
        r_dgemm=0.30,
        r_stream=0.60,
        r_disk=0.65,
        score=score,
        boot_drive_path="C:\\",
        disk_model="Kingston KC3000",
        disk_media_type="SSD",
        disk_bus_type="NVMe",
        disk_media_label="NVMe",
        notes=[],
        cancelled=False,
    )


@pytest.fixture
def repo(tmp_path: Path):
    db = tmp_path / "test.db"
    r = SqliteGeneralBenchmarkRepository(db)
    yield r
    r.close()


def test_save_and_get(repo):
    rep = _make_report(score=5500.0)
    repo.save(rep)
    got = repo.get(rep.id)
    assert got is not None
    assert got.id == rep.id
    assert got.score == pytest.approx(5500.0)
    assert got.disk_model == "Kingston KC3000"


def test_list_returns_newest_first(repo):
    import time

    rep1 = _make_report(score=4000.0)
    repo.save(rep1)
    time.sleep(0.01)
    rep2 = _make_report(score=6000.0)
    repo.save(rep2)

    runs = repo.list_runs(limit=10)
    assert len(runs) == 2
    assert runs[0].id == rep2.id
    assert runs[1].id == rep1.id


def test_delete(repo):
    rep = _make_report()
    repo.save(rep)
    assert repo.delete(rep.id) is True
    assert repo.get(rep.id) is None
    assert repo.delete(rep.id) is False  # повторное удаление = False


def test_save_with_none_score(repo):
    rep = _make_report(score=None)
    repo.save(rep)
    got = repo.get(rep.id)
    assert got is not None
    assert got.score is None


def test_resolve_id_exact_and_prefix(repo):
    rep = _make_report()
    repo.save(rep)
    full_id = str(rep.id)
    # Точное совпадение.
    assert repo.resolve_id(full_id) == full_id
    # Префикс (первые 8 символов).
    assert repo.resolve_id(full_id[:8]) == full_id


def test_resolve_id_returns_none_when_not_found(repo):
    assert repo.resolve_id("deadbeef-cafe-babe-1234-567890abcdef") is None
