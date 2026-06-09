"""Интеграционный тест SqliteWinsatRepository (миграция v3)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.domain.winsat import WinsatReport, WinsatStatus, WinsatSubscore
from apexcore.infrastructure.persistence import SqliteWinsatRepository


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.26200",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        timestamp=datetime.now(timezone.utc),
    )


def _pass(category, score: float) -> WinsatSubscore:
    return WinsatSubscore(
        category=category,
        metric_name="m",
        metric_value=42.0,
        metric_unit="MB/s",
        score=score,
        status=WinsatStatus.PASS,
    )


def _na(category) -> WinsatSubscore:
    return WinsatSubscore(
        category=category,
        metric_name="-",
        metric_value=0.0,
        metric_unit="-",
        score=1.0,
        status=WinsatStatus.NA,
    )


def _make_report(*, cpu: float = 9.5, mem: float = 9.5, disk: float = 8.7) -> WinsatReport:
    started = datetime.now(timezone.utc)
    return WinsatReport(
        id=uuid4(),
        system_info=_sys_info(),
        started_at=started,
        ended_at=started,
        cpu_score=_pass("cpu", cpu),
        memory_score=_pass("memory", mem),
        disk_score=_pass("disk", disk),
        graphics_score=_na("graphics"),
        d3d_score=_na("d3d"),
        winspr_level=min(cpu, mem, disk),
    )


def test_save_get_list_delete(tmp_path):
    repo = SqliteWinsatRepository(tmp_path / "test.sqlite")
    r1 = _make_report(cpu=9.5, mem=9.5, disk=8.7)
    r2 = _make_report(cpu=7.0, mem=8.0, disk=6.5)

    repo.save(r1)
    repo.save(r2)

    got = repo.get(r1.id)
    assert got is not None
    assert got.cpu_score.score == 9.5
    assert got.winspr_level == 8.7

    all_runs = repo.list_runs(limit=10)
    assert len(all_runs) == 2

    assert repo.delete(r1.id) is True
    assert repo.get(r1.id) is None
    assert repo.delete(r1.id) is False  # уже удалён
    repo.close()


def test_resolve_id_by_prefix(tmp_path):
    repo = SqliteWinsatRepository(tmp_path / "test.sqlite")
    r = _make_report()
    repo.save(r)

    full = repo.resolve_id(str(r.id)[:8])
    assert full == str(r.id)

    assert repo.resolve_id("nonexistent_xyz") is None
    repo.close()


def test_list_runs_ordered_by_started_at_desc(tmp_path):
    repo = SqliteWinsatRepository(tmp_path / "test.sqlite")
    early = _make_report()
    early.started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    early.ended_at = early.started_at
    late = _make_report()
    late.started_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    late.ended_at = late.started_at

    repo.save(early)
    repo.save(late)

    runs = repo.list_runs(limit=10)
    assert runs[0].id == late.id
    assert runs[1].id == early.id
    repo.close()


def test_migration_v3_does_not_drop_existing_tables(tmp_path):
    """v3-миграция additive: micro_runs остаётся целым."""
    from apexcore.infrastructure.persistence import SqliteMicroRunRepository

    db = tmp_path / "test.sqlite"
    # Открыть как micro-репо — создаст micro_runs.
    micro_repo = SqliteMicroRunRepository(db)
    micro_repo.close()

    # Открыть тот же файл как winsat-репо — миграция v3 добавит таблицу.
    winsat_repo = SqliteWinsatRepository(db)
    r = _make_report()
    winsat_repo.save(r)
    winsat_repo.close()

    # Снова micro-репо: таблица micro_runs должна остаться (sanity).
    micro_repo2 = SqliteMicroRunRepository(db)
    runs = micro_repo2.list_runs()
    assert isinstance(runs, list)  # таблица существует, list пустой OK
    micro_repo2.close()
