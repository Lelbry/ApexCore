"""Интеграционный тест SqliteGpuBenchmarkRepository (миграция v5).

Покрывает: CRUD, upgrade существующей v4-БД (additive, старые таблицы
целы), resolve_id (точное / префикс / неоднозначный), порядок list_runs.
По образцу tests/integration/test_winsat_repo.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from apexcore.domain.errors import RepositoryError
from apexcore.domain.gpu import GpuBenchmarkReport, GpuDeviceInfo, GpuDeviceType
from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.persistence import SqliteGpuBenchmarkRepository


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.26200",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        timestamp=datetime.now(timezone.utc),
    )


def _device(name: str = "NVIDIA GeForce RTX 4070 Ti") -> GpuDeviceInfo:
    return GpuDeviceInfo(
        index=0,
        name=name,
        vendor="NVIDIA",
        platform_name="NVIDIA CUDA",
        device_type=GpuDeviceType.DISCRETE,
        compute_units=60,
        max_clock_mhz=2610,
        global_mem_mb=12288,
        fp64_supported=False,
        arch="nvidia_ada",
    )


def _make_report(
    *,
    device_name: str = "NVIDIA GeForce RTX 4070 Ti",
    score: float | None = 8500.0,
    fp32: float | None = 40000.0,
    mem: float | None = 500.0,
) -> GpuBenchmarkReport:
    started = datetime.now(timezone.utc)
    return GpuBenchmarkReport(
        id=uuid4(),
        system_info=_sys_info(),
        device=_device(device_name),
        started_at=started,
        ended_at=started,
        fp32_gflops=fp32,
        mem_bandwidth_gb_s=mem,
        r_fp32=0.85,
        r_mem=0.85,
        score=score,
        arch="nvidia_ada",
        peak_source="roofline",
    )


def test_save_get_list_delete(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    r1 = _make_report(score=8500.0, fp32=40000.0)
    r2 = _make_report(score=3200.0, fp32=15000.0, device_name="Intel Iris Xe")

    repo.save(r1)
    repo.save(r2)

    got = repo.get(r1.id)
    assert got is not None
    assert got.score == 8500.0
    assert got.fp32_gflops == 40000.0
    assert got.device.name == "NVIDIA GeForce RTX 4070 Ti"

    all_runs = repo.list_runs(limit=10)
    assert len(all_runs) == 2

    assert repo.delete(r1.id) is True
    assert repo.get(r1.id) is None
    assert repo.delete(r1.id) is False  # уже удалён
    repo.close()


def test_get_missing_returns_none(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    assert repo.get(uuid4()) is None
    repo.close()


def test_save_replaces_existing_id(tmp_path):
    """INSERT OR REPLACE: повторный save по тому же id обновляет, не дублирует."""
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    r = _make_report(score=1000.0)
    repo.save(r)
    r.score = 9999.0
    repo.save(r)

    assert len(repo.list_runs(limit=10)) == 1
    got = repo.get(r.id)
    assert got is not None
    assert got.score == 9999.0
    repo.close()


def test_save_with_null_score(tmp_path):
    """score=None (фаза не посчиталась) сохраняется и читается корректно."""
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    r = _make_report(score=None, fp32=None, mem=None)
    repo.save(r)

    got = repo.get(r.id)
    assert got is not None
    assert got.score is None
    assert got.fp32_gflops is None
    repo.close()


def test_resolve_id_exact(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    r = _make_report()
    repo.save(r)

    assert repo.resolve_id(str(r.id)) == str(r.id)
    repo.close()


def test_resolve_id_by_prefix(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    r = _make_report()
    repo.save(r)

    full = repo.resolve_id(str(r.id)[:8])
    assert full == str(r.id)

    # Хвостовое многоточие/точка отсекаются (как в CLI-выводе "abcd1234…").
    assert repo.resolve_id(str(r.id)[:8] + "…") == str(r.id)

    assert repo.resolve_id("nonexistent_xyz") is None
    repo.close()


def test_resolve_id_ambiguous_prefix_raises(tmp_path):
    """Общий префикс у двух прогонов → RepositoryError (нужно уточнить)."""
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    shared = "abcdef01"
    r1 = _make_report()
    r1.id = uuid4()
    r2 = _make_report()
    r2.id = uuid4()
    # Подменяем id так, чтобы оба начинались с одного префикса.
    r1_id = shared + str(r1.id)[8:]
    r2_id = shared + str(r2.id)[8:]
    r1.id = type(r1.id)(r1_id)
    r2.id = type(r2.id)(r2_id)
    repo.save(r1)
    repo.save(r2)

    with pytest.raises(RepositoryError):
        repo.resolve_id(shared)
    repo.close()


def test_list_runs_ordered_by_started_at_desc(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
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


def test_list_runs_respects_limit(tmp_path):
    repo = SqliteGpuBenchmarkRepository(tmp_path / "test.sqlite")
    for _ in range(5):
        repo.save(_make_report())
    assert len(repo.list_runs(limit=3)) == 3
    repo.close()


def _seed_v4_db(db_path) -> None:
    """Создать «настоящую» v4-БД: старые таблицы есть, gpu_benchmark_runs — нет.

    Открываем через general-репо (это доводит схему до текущей), затем
    руками откатываем к состоянию v4: дропаем gpu_benchmark_runs и ставим
    schema_version = 4. Так apply_schema при следующем открытии выполнит
    именно ветку v4 → v5.
    """
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteMicroRunRepository,
        SqliteWinsatRepository,
    )

    # Наполним старые таблицы, чтобы проверить, что миграция их не тронет.
    gb_repo = SqliteGeneralBenchmarkRepository(db_path)
    gb_repo.close()
    micro_repo = SqliteMicroRunRepository(db_path)
    micro_repo.close()
    winsat_repo = SqliteWinsatRepository(db_path)
    winsat_repo.close()

    # Откат к v4: убрать gpu-таблицу и понизить версию.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS gpu_benchmark_runs")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (4)")
    conn.commit()
    conn.close()


def test_migration_v4_to_v5_upgrades_and_keeps_old_tables(tmp_path):
    """v4 → v5 additive: версия становится 5, все старые таблицы целы."""
    db = tmp_path / "test.sqlite"
    _seed_v4_db(db)

    # Санити: перед миграцией это действительно v4 без gpu-таблицы.
    conn = sqlite3.connect(str(db))
    ver_before = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    assert ver_before == 4
    has_gpu_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_benchmark_runs'"
    ).fetchone()
    assert has_gpu_before is None
    conn.close()

    # Открытие через gpu-репо запускает apply_schema → миграцию v4 → v5.
    repo = SqliteGpuBenchmarkRepository(db)
    r = _make_report()
    repo.save(r)
    assert repo.get(r.id) is not None
    repo.close()

    # После миграции: версия доведена до CURRENT_VERSION (>= 5, схема
    # прошла через ветку v4 → v5) и все таблицы на месте.
    conn = sqlite3.connect(str(db))
    ver_after = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    assert ver_after >= 5

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for expected in (
        "runs",
        "baselines",
        "micro_runs",
        "winsat_runs",
        "general_benchmark_runs",
        "gpu_benchmark_runs",
    ):
        assert expected in tables, f"таблица {expected} потеряна при миграции v4 → v5"
    conn.close()


def test_migration_v5_does_not_drop_existing_data(tmp_path):
    """Данные соседних таблиц переживают открытие через gpu-репо."""
    from apexcore.infrastructure.persistence import SqliteWinsatRepository

    db = tmp_path / "test.sqlite"
    _seed_v4_db(db)

    # Записать winsat-прогон ПОСЛЕ отката к v4 (winsat_runs уже существует).
    from apexcore.domain.winsat import WinsatReport, WinsatStatus, WinsatSubscore

    def _sub(cat, st=WinsatStatus.PASS, sc=8.0):
        return WinsatSubscore(
            category=cat, metric_name="m", metric_value=1.0,
            metric_unit="x", score=sc, status=st,
        )

    started = datetime.now(timezone.utc)
    wr = WinsatReport(
        id=uuid4(),
        system_info=_sys_info(),
        started_at=started,
        ended_at=started,
        cpu_score=_sub("cpu"),
        memory_score=_sub("memory"),
        disk_score=_sub("disk"),
        graphics_score=_sub("graphics", WinsatStatus.NA, 1.0),
        d3d_score=_sub("d3d", WinsatStatus.NA, 1.0),
        winspr_level=8.0,
    )
    winsat_repo = SqliteWinsatRepository(db)  # доведёт до v5, но данные не тронет
    winsat_repo.save(wr)
    winsat_repo.close()

    # Открыть через gpu-репо (миграция уже была) и убедиться, что winsat цел.
    gpu_repo = SqliteGpuBenchmarkRepository(db)
    gpu_repo.save(_make_report())
    gpu_repo.close()

    winsat_repo2 = SqliteWinsatRepository(db)
    got = winsat_repo2.get(wr.id)
    assert got is not None
    assert got.winspr_level == 8.0
    winsat_repo2.close()
