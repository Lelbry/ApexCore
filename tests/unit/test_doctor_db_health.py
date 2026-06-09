"""Проверка целостности БД в `apexcore doctor` (`_check_database_health`).

Дополняет устойчивое чтение ленты прогонов
(`test_runs_listing_resilience.py`): там повреждённая таблица не роняет
листинг, здесь — доктор проактивно сообщает о битом файле БД и даёт
подсказку по восстановлению, не падая сам.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apexcore.domain.general_benchmark import GeneralBenchmarkReport
from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.persistence import SqliteGeneralBenchmarkRepository
from apexcore.interfaces.cli.commands import doctor as doctor_cmd


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0",
        cpu_model="Intel i7",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        timestamp=datetime.now(timezone.utc),
    )


def _make_general_report() -> GeneralBenchmarkReport:
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
        score=5500.0,
        boot_drive_path="C:\\",
        disk_model="Kingston KC3000",
        disk_media_type="SSD",
        disk_bus_type="NVMe",
        disk_media_label="NVMe",
        notes=[],
        cancelled=False,
    )


@pytest.fixture
def healthy_db(tmp_path, monkeypatch):
    """Изолированная tmp-БД с одним валидным прогоном.

    ``APEXCORE_DATA_DIR`` уводит ``load_settings().db_path`` в tmp, чтобы
    тест не цеплял реальную БД и yaml-override из data_dir. Сохранение
    через репозиторий создаёт все 4 таблицы прогонов (apply_schema).
    """
    monkeypatch.setenv("APEXCORE_DATA_DIR", str(tmp_path))

    from apexcore.shared.config import load_settings

    db_path = load_settings().db_path
    repo = SqliteGeneralBenchmarkRepository(db_path)
    repo.save(_make_general_report())
    repo.close()
    return db_path


@pytest.fixture
def _wide_console(monkeypatch):
    """Расширить rich-консоль, чтобы длинный путь к БД не переносился по словам."""
    from apexcore.interfaces.cli import render

    monkeypatch.setattr(render.console, "_width", 4000)


def test_doctor_reports_healthy_db(healthy_db, _wide_console, capsys):
    """Целая БД → зелёный «✓ Цел», без сообщений о повреждении."""
    doctor_cmd._check_database_health()  # не должно бросать

    out = capsys.readouterr().out
    assert "Цел" in out
    assert "повреждена" not in out
    # Все 4 таблицы на месте → нет жалобы об отсутствующих.
    assert "Отсутствуют таблицы" not in out


def test_doctor_reports_corrupt_db(healthy_db, _wide_console, capsys):
    """Битый заголовок файла → доктор сообщает о повреждении, не падая."""
    # Затираем первые байты (магия «SQLite format 3\0» в заголовке) —
    # SQLite перестаёт распознавать файл как базу.
    with open(healthy_db, "r+b") as fh:
        fh.write(b"\x00" * 64)

    doctor_cmd._check_database_health()  # не должно бросать

    out = capsys.readouterr().out
    assert any(marker in out for marker in ("повреждена", "Не удалось открыть", "упала"))
    # Подсказка по восстановлению с путём к файлу БД.
    assert str(healthy_db) in out


def test_doctor_handles_missing_db(tmp_path, monkeypatch, _wide_console, capsys):
    """Файла БД ещё нет → доктор сообщает «ещё не создан», не падая."""
    monkeypatch.setenv("APEXCORE_DATA_DIR", str(tmp_path))

    doctor_cmd._check_database_health()

    out = capsys.readouterr().out
    assert "ещё не создан" in out
