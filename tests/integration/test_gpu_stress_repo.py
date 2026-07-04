"""Интеграционный тест SqliteGpuStressRepository (миграция v6).

Покрывает: CRUD, upgrade существующей v5-БД (additive, старые таблицы
целы, включая gpu_benchmark_runs), resolve_id (точное / префикс /
неоднозначный), порядок list_runs. По образцу
tests/integration/test_gpu_benchmark_repo.py.

Здесь у отчёта НЕТ балла — индексное поле для листинга это вердикт
PASS/WARN/FAIL/UNKNOWN.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from apexcore.domain.errors import RepositoryError
from apexcore.domain.gpu import (
    GpuDeviceInfo,
    GpuDeviceType,
    GpuStressReport,
    GpuStressSample,
    GpuStressVerdict,
)
from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.persistence import SqliteGpuStressRepository


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
    verdict: GpuStressVerdict = GpuStressVerdict.PASS,
    max_temp_c: float | None = 72.0,
    with_samples: bool = False,
) -> GpuStressReport:
    started = datetime.now(timezone.utc)
    samples = (
        [GpuStressSample(t_sec=float(i), temp_c=70.0 + i, clock_mhz=2600.0) for i in range(3)]
        if with_samples
        else []
    )
    return GpuStressReport(
        id=uuid4(),
        system_info=_sys_info(),
        device=_device(device_name),
        started_at=started,
        ended_at=started,
        duration_sec=120.0,
        requested_duration_sec=120.0,
        max_temp_c=max_temp_c,
        avg_temp_c=68.0,
        max_power_w=285.0,
        avg_power_w=270.0,
        min_clock_mhz=2400.0,
        avg_clock_mhz=2550.0,
        max_clock_mhz_observed=2610.0,
        avg_util_pct=99.0,
        verdict=verdict,
        samples=samples,
        samples_taken=len(samples),
    )


def test_save_get_list_delete(tmp_path):
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    r1 = _make_report(verdict=GpuStressVerdict.PASS, with_samples=True)
    r2 = _make_report(verdict=GpuStressVerdict.FAIL, device_name="Intel Iris Xe")

    repo.save(r1)
    repo.save(r2)

    got = repo.get(r1.id)
    assert got is not None
    assert got.verdict is GpuStressVerdict.PASS
    assert got.max_temp_c == 72.0
    assert got.device.name == "NVIDIA GeForce RTX 4070 Ti"
    assert len(got.samples) == 3
    assert got.samples[0].temp_c == 70.0

    all_runs = repo.list_runs(limit=10)
    assert len(all_runs) == 2

    assert repo.delete(r1.id) is True
    assert repo.get(r1.id) is None
    assert repo.delete(r1.id) is False  # уже удалён
    repo.close()


def test_get_missing_returns_none(tmp_path):
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    assert repo.get(uuid4()) is None
    repo.close()


def test_save_replaces_existing_id(tmp_path):
    """INSERT OR REPLACE: повторный save по тому же id обновляет, не дублирует."""
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    r = _make_report(verdict=GpuStressVerdict.WARN)
    repo.save(r)
    r.verdict = GpuStressVerdict.FAIL
    repo.save(r)

    assert len(repo.list_runs(limit=10)) == 1
    got = repo.get(r.id)
    assert got is not None
    assert got.verdict is GpuStressVerdict.FAIL
    repo.close()


def test_save_unknown_verdict_and_no_telemetry(tmp_path):
    """verdict=UNKNOWN + отсутствие телеметрии (None) сохраняется и читается."""
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    r = _make_report(verdict=GpuStressVerdict.UNKNOWN, max_temp_c=None)
    repo.save(r)

    got = repo.get(r.id)
    assert got is not None
    assert got.verdict is GpuStressVerdict.UNKNOWN
    assert got.max_temp_c is None
    repo.close()


def test_resolve_id_exact(tmp_path):
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    r = _make_report()
    repo.save(r)

    assert repo.resolve_id(str(r.id)) == str(r.id)
    repo.close()


def test_resolve_id_by_prefix(tmp_path):
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
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
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
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
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
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
    repo = SqliteGpuStressRepository(tmp_path / "test.sqlite")
    for _ in range(5):
        repo.save(_make_report())
    assert len(repo.list_runs(limit=3)) == 3
    repo.close()


def _seed_v5_db(db_path) -> None:
    """Создать «настоящую» v5-БД: старые таблицы есть (в т.ч. gpu_benchmark_runs),
    но gpu_stress_runs — нет.

    Открываем через gpu-benchmark-репо (это доводит схему до текущей), затем
    руками откатываем к состоянию v5: дропаем gpu_stress_runs и ставим
    schema_version = 5. Так apply_schema при следующем открытии выполнит
    именно ветку v5 → v6.
    """
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
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
    # gpu_benchmark_runs (v5) должна существовать до миграции v5 → v6.
    gpu_bench_repo = SqliteGpuBenchmarkRepository(db_path)
    gpu_bench_repo.close()

    # Откат к v5: убрать gpu-стресс-таблицу и понизить версию.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS gpu_stress_runs")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (5)")
    conn.commit()
    conn.close()


def test_migration_v5_to_v6_upgrades_and_keeps_old_tables(tmp_path):
    """v5 → v6 additive: версия становится 6, все старые таблицы целы."""
    db = tmp_path / "test.sqlite"
    _seed_v5_db(db)

    # Санити: перед миграцией это действительно v5 без gpu_stress-таблицы,
    # но с уже существующей gpu_benchmark_runs.
    conn = sqlite3.connect(str(db))
    ver_before = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    assert ver_before == 5
    has_stress_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_stress_runs'"
    ).fetchone()
    assert has_stress_before is None
    has_bench_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_benchmark_runs'"
    ).fetchone()
    assert has_bench_before is not None
    conn.close()

    # Открытие через gpu-стресс-репо запускает apply_schema → миграцию v5 → v6.
    repo = SqliteGpuStressRepository(db)
    r = _make_report()
    repo.save(r)
    assert repo.get(r.id) is not None
    repo.close()

    # После миграции: версия = 6 и все таблицы на месте.
    conn = sqlite3.connect(str(db))
    ver_after = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    assert ver_after == 6

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
        "gpu_stress_runs",
    ):
        assert expected in tables, f"таблица {expected} потеряна при миграции v5 → v6"
    conn.close()


def test_migration_v6_does_not_drop_existing_data(tmp_path):
    """Данные соседних таблиц (в т.ч. gpu_benchmark_runs) переживают
    открытие через gpu-стресс-репо."""
    from apexcore.domain.gpu import GpuBenchmarkReport
    from apexcore.infrastructure.persistence import SqliteGpuBenchmarkRepository

    db = tmp_path / "test.sqlite"
    _seed_v5_db(db)

    # Записать gpu-benchmark-прогон ПОСЛЕ отката к v5 (gpu_benchmark_runs уже есть).
    started = datetime.now(timezone.utc)
    gbr = GpuBenchmarkReport(
        id=uuid4(),
        system_info=_sys_info(),
        device=_device(),
        started_at=started,
        ended_at=started,
        fp32_gflops=40000.0,
        mem_bandwidth_gb_s=500.0,
        r_fp32=0.85,
        r_mem=0.85,
        score=8500.0,
        arch="nvidia_ada",
        peak_source="roofline",
    )
    gpu_bench_repo = SqliteGpuBenchmarkRepository(db)  # доведёт до v6, но данные не тронет
    gpu_bench_repo.save(gbr)
    gpu_bench_repo.close()

    # Открыть через gpu-стресс-репо (миграция уже была) и убедиться, что
    # gpu_benchmark-прогон цел.
    stress_repo = SqliteGpuStressRepository(db)
    stress_repo.save(_make_report())
    stress_repo.close()

    gpu_bench_repo2 = SqliteGpuBenchmarkRepository(db)
    got = gpu_bench_repo2.get(gbr.id)
    assert got is not None
    assert got.score == 8500.0
    gpu_bench_repo2.close()
