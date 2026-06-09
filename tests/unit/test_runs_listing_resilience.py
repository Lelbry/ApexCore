"""Устойчивость объединённой ленты прогонов к повреждённой БД.

Регрессия: до фикса битая страница в ОДНОЙ таблице (например, ``runs``)
бросала «сырой» ``sqlite3.DatabaseError`` из ``list_runs`` и обрывала весь
листинг — пользователь не видел даже здоровые micro/winsat/general-прогоны.

:func:`collect_unified_listing` теперь читает каждый репозиторий в своём
try/except: сбойный пропускается с предупреждением в лог + одна строка-нотис
пользователю, остальные (читаемые) прогоны отрисовываются.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from apexcore.domain.errors import RepositoryError
from apexcore.domain.general_benchmark import GeneralBenchmarkReport
from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    MicroBenchSuiteResult,
    OverallScore,
    SystemInfo,
)
from apexcore.infrastructure.persistence import (
    SqliteGeneralBenchmarkRepository,
    SqliteMicroRunRepository,
    SqliteResultRepository,
)
from apexcore.interfaces.cli.commands import runs as runs_cmd


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0",
        cpu_model="Intel i7",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_general_report(score: float = 5500.0) -> GeneralBenchmarkReport:
    now = datetime.now(timezone.utc)
    return GeneralBenchmarkReport(
        system_info=_sys_info(),
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


def _make_micro_suite() -> MicroBenchSuiteResult:
    info = _sys_info()
    overall = OverallScore(
        overall_ratio=1.2,
        overall_score=1200.0,
        subscores={"R_MEM": 0.8, "R_CPU_compute": 0.9},
        ci_lower=1170.0,
        ci_upper=1230.0,
        ci_method="t_logscale",
        n_runs=1,
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
        preset="fast",
        n_runs=1,
    )


@pytest.fixture
def db_with_healthy_runs(tmp_path, monkeypatch):
    """Изолированная tmp-БД с одним micro- и одним general-прогоном.

    ``APEXCORE_DATA_DIR`` уводит ``load_settings().db_path`` в tmp, чтобы
    тест не цеплял реальную БД пользователя и yaml-override из data_dir.
    Возвращает путь к БД, который использует и тест, и
    :func:`collect_unified_listing`.
    """
    monkeypatch.setenv("APEXCORE_DATA_DIR", str(tmp_path))

    from apexcore.shared.config import load_settings

    db_path = load_settings().db_path

    micro_repo = SqliteMicroRunRepository(db_path)
    micro_repo.save(_make_micro_suite())
    micro_repo.close()

    gb_repo = SqliteGeneralBenchmarkRepository(db_path)
    gb_repo.save(_make_general_report())
    gb_repo.close()

    return db_path


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.DatabaseError("database disk image is malformed"),
        RepositoryError("не удалось прочитать таблицу runs"),
    ],
    ids=["sqlite_DatabaseError", "apexcore_RepositoryError"],
)
def test_listing_survives_corrupt_stress_table(
    db_with_healthy_runs, monkeypatch, caplog, exc
):
    """Битая таблица ``runs`` не должна прятать micro/general-прогоны."""

    def _raise(self, *args, **kwargs):
        raise exc

    monkeypatch.setattr(SqliteResultRepository, "list_runs", _raise)

    with caplog.at_level(logging.WARNING, logger="apexcore.interfaces.cli.commands.runs"):
        refs = runs_cmd.collect_unified_listing(limit=20)

    kinds = {r.kind for r in refs}
    # Здоровые репозитории всё равно отдали свои прогоны…
    assert "micro" in kinds
    assert "general" in kinds
    # …а сбойный стресс-репозиторий просто выпал из ленты, без падения.
    assert "stress" not in kinds

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("стресс" in r.getMessage() for r in warnings)


def test_listing_prints_partial_history_notice(
    db_with_healthy_runs, monkeypatch, capsys
):
    """При сбое одного репо пользователю показывается строка-нотис."""
    from apexcore.interfaces.cli import render

    # Расширяем консоль, чтобы rich не переносил длинный путь к БД по словам
    # (без терминала ширина по умолчанию 80 — путь рвётся посреди токена).
    monkeypatch.setattr(render.console, "_width", 4000)

    def _raise(self, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(SqliteResultRepository, "list_runs", _raise)

    runs_cmd.collect_unified_listing(limit=20)

    out = capsys.readouterr().out
    assert "недоступна" in out
    # Подсказка о пути к файлу БД для восстановления.
    assert str(db_with_healthy_runs) in out


def test_healthy_db_emits_no_notice(db_with_healthy_runs, capsys):
    """Без повреждений нотис не печатается, лента полна (нет ложных срабатываний)."""
    refs = runs_cmd.collect_unified_listing(limit=20)

    kinds = {r.kind for r in refs}
    assert "micro" in kinds
    assert "general" in kinds

    out = capsys.readouterr().out
    assert "недоступна" not in out
