"""Тесты интеграции GPU-прогонов в CLI: рендер стресс-отчёта + история.

Покрывает три группы:
1. :func:`render_gpu_stress_report` — синтетический ``GpuStressReport``
   рисуется без исключений (PASS/FAIL/UNKNOWN + плейсхолдер-устройство +
   ``None``-поля).
2. :func:`collect_unified_listing` — оба новых типа (``gpu`` и
   ``gpu_stress``) появляются в объединённой ленте, когда в БД есть такие
   прогоны; балл GPU показывается как «X / 10000»-число, GPU-стресс — как
   вердикт.
3. ``_resolve_to_ref`` / ``show_run_by_ref`` / ``export_run_by_ref`` /
   ``delete_run`` — round-trip по UUID для обоих типов.

Изоляция БД — через ``APEXCORE_DATA_DIR`` (как в
``test_runs_listing_resilience``): ``load_settings().db_path`` уходит в
tmp, реальная БД пользователя не задевается.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from apexcore.domain.gpu import (
    GpuBenchmarkReport,
    GpuDeviceInfo,
    GpuDeviceType,
    GpuStressReport,
    GpuStressVerdict,
)
from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.persistence import (
    SqliteGpuBenchmarkRepository,
    SqliteGpuStressRepository,
)
from apexcore.interfaces.cli.commands import runs as runs_cmd
from apexcore.interfaces.cli.render import render_gpu_stress_report


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.26200",
        cpu_model="Test CPU",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        timestamp=datetime.now(timezone.utc),
    )


def _device(name: str = "NVIDIA GeForce RTX 4070 Ti", index: int = 0) -> GpuDeviceInfo:
    return GpuDeviceInfo(
        index=index,
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


def _stress_report(
    *,
    verdict: GpuStressVerdict = GpuStressVerdict.PASS,
    device: GpuDeviceInfo | None = None,
    max_temp_c: float | None = 72.0,
) -> GpuStressReport:
    started = datetime.now(timezone.utc)
    return GpuStressReport(
        id=uuid4(),
        system_info=_sys_info(),
        device=device or _device(),
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
        thermal_limit_c=83.0,
        throttle_detected=(verdict is GpuStressVerdict.FAIL),
        throttle_reasons=(
            ["частота ядра обвалилась ниже 60% пика"]
            if verdict is GpuStressVerdict.FAIL
            else []
        ),
        verdict=verdict,
        notes=["тестовая заметка"],
    )


def _bench_report(score: float | None = 8500.0) -> GpuBenchmarkReport:
    started = datetime.now(timezone.utc)
    return GpuBenchmarkReport(
        id=uuid4(),
        system_info=_sys_info(),
        device=_device(),
        started_at=started,
        ended_at=started,
        fp32_gflops=40000.0,
        mem_bandwidth_gb_s=500.0,
        r_fp32=0.85,
        r_mem=0.85,
        score=score,
        arch="nvidia_ada",
        peak_source="roofline",
    )


# ─── 1. Рендер стресс-отчёта ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "verdict",
    [
        GpuStressVerdict.PASS,
        GpuStressVerdict.WARN,
        GpuStressVerdict.FAIL,
        GpuStressVerdict.UNKNOWN,
    ],
)
def test_render_gpu_stress_report_all_verdicts(verdict, capsys):
    """Отчёт рисуется без исключений для всех вердиктов."""
    render_gpu_stress_report(_stress_report(verdict=verdict))
    out = capsys.readouterr().out
    assert "GPU" in out
    assert "Вердикт" in out


def test_render_gpu_stress_report_placeholder_device(capsys):
    """Плейсхолдер-устройство (index < 0) → «устройство не найдено», без падения."""
    device = _device(name="—", index=-1)
    report = _stress_report(verdict=GpuStressVerdict.UNKNOWN, device=device)
    render_gpu_stress_report(report)
    out = capsys.readouterr().out
    assert "не найдено" in out


def test_render_gpu_stress_report_none_fields(capsys):
    """Отсутствующая телеметрия (все None) → «—», без исключений."""
    started = datetime.now(timezone.utc)
    report = GpuStressReport(
        system_info=_sys_info(),
        device=_device(),
        started_at=started,
        ended_at=started,
        verdict=GpuStressVerdict.UNKNOWN,
    )
    render_gpu_stress_report(report)
    out = capsys.readouterr().out
    assert "—" in out


# ─── 2. История: оба GPU-типа в ленте ───────────────────────────────────────


@pytest.fixture
def db_with_gpu_runs(tmp_path, monkeypatch):
    """Изолированная tmp-БД с одним gpu-бенчмарком и одним gpu-стрессом."""
    monkeypatch.setenv("APEXCORE_DATA_DIR", str(tmp_path))

    from apexcore.shared.config import load_settings

    db_path = load_settings().db_path

    gpu_repo = SqliteGpuBenchmarkRepository(db_path)
    bench = _bench_report(score=8500.0)
    gpu_repo.save(bench)
    gpu_repo.close()

    gs_repo = SqliteGpuStressRepository(db_path)
    stress = _stress_report(verdict=GpuStressVerdict.PASS)
    gs_repo.save(stress)
    gs_repo.close()

    return db_path, bench, stress


def test_collect_includes_gpu_and_gpu_stress(db_with_gpu_runs):
    """collect_unified_listing показывает и gpu, и gpu_stress."""
    refs = runs_cmd.collect_unified_listing(limit=20)
    by_kind = {r.kind: r for r in refs}
    assert "gpu" in by_kind
    assert "gpu_stress" in by_kind


def test_collect_gpu_score_display(db_with_gpu_runs):
    """GPU-бенчмарк показывает балл как число «8 500» (X / 10000)."""
    refs = runs_cmd.collect_unified_listing(limit=20)
    gpu_ref = next(r for r in refs if r.kind == "gpu")
    # 8500 форматируется с пробелом-разделителем тысяч.
    assert "8" in gpu_ref.score_display and "500" in gpu_ref.score_display


def test_collect_gpu_score_none_shows_dash(tmp_path, monkeypatch):
    """GPU-бенчмарк без балла (score=None) → «—» в ленте."""
    monkeypatch.setenv("APEXCORE_DATA_DIR", str(tmp_path))
    from apexcore.shared.config import load_settings

    repo = SqliteGpuBenchmarkRepository(load_settings().db_path)
    repo.save(_bench_report(score=None))
    repo.close()

    refs = runs_cmd.collect_unified_listing(limit=20)
    gpu_ref = next(r for r in refs if r.kind == "gpu")
    assert gpu_ref.score_display == "—"


def test_collect_gpu_stress_verdict_display(db_with_gpu_runs):
    """GPU-стресс показывает вердикт (ПРОЙДЕНО для PASS), не число."""
    refs = runs_cmd.collect_unified_listing(limit=20)
    gs_ref = next(r for r in refs if r.kind == "gpu_stress")
    assert "ПРОЙДЕНО" in gs_ref.score_display


def test_kind_labels_have_gpu_entries():
    """Русские подписи новых типов заданы."""
    assert runs_cmd._KIND_LABELS["gpu"] == "Тест GPU"
    assert runs_cmd._KIND_LABELS["gpu_stress"] == "GPU-стресс"


# ─── 3. resolve / show / export / delete round-trip ─────────────────────────


def test_resolve_and_show_gpu_stress(db_with_gpu_runs, capsys):
    """UUID GPU-стресса резолвится в kind=gpu_stress и рендерится."""
    _db, _bench, stress = db_with_gpu_runs
    ref = runs_cmd._resolve_to_ref(str(stress.id))
    assert ref is not None
    assert ref.kind == "gpu_stress"
    runs_cmd.show_run_by_ref(ref)
    out = capsys.readouterr().out
    assert "Вердикт" in out


def test_resolve_and_show_gpu(db_with_gpu_runs, capsys):
    """UUID GPU-бенчмарка резолвится в kind=gpu и рендерится."""
    _db, bench, _stress = db_with_gpu_runs
    ref = runs_cmd._resolve_to_ref(str(bench.id))
    assert ref is not None
    assert ref.kind == "gpu"
    runs_cmd.show_run_by_ref(ref)
    out = capsys.readouterr().out
    assert "GPU" in out


def test_resolve_gpu_by_prefix(db_with_gpu_runs):
    """Префикс UUID GPU-стресса тоже резолвится."""
    _db, _bench, stress = db_with_gpu_runs
    prefix = str(stress.id)[:8]
    ref = runs_cmd._resolve_to_ref(prefix)
    assert ref is not None
    assert ref.kind == "gpu_stress"
    assert ref.uuid == str(stress.id)


def test_export_gpu_stress_json(db_with_gpu_runs, tmp_path, capsys):
    """JSON-экспорт GPU-стресса пишет файл."""
    _db, _bench, stress = db_with_gpu_runs
    ref = runs_cmd.RunRef(
        kind="gpu_stress",
        uuid=str(stress.id),
        start_time=stress.started_at,
        duration_sec=120.0,
        score_display="",
    )
    target = tmp_path / "out.json"
    result = runs_cmd.export_run_by_ref(ref, "json", target)
    assert result == target
    assert target.exists()
    assert "verdict" in target.read_text(encoding="utf-8")


def test_export_gpu_csv_unsupported(db_with_gpu_runs, tmp_path, capsys):
    """CSV для GPU-бенчмарка не поддерживается — файл не создаётся."""
    _db, bench, _stress = db_with_gpu_runs
    ref = runs_cmd.RunRef(
        kind="gpu",
        uuid=str(bench.id),
        start_time=bench.started_at,
        duration_sec=1.0,
        score_display="",
    )
    result = runs_cmd.export_run_by_ref(ref, "csv", tmp_path / "x.csv")
    assert result is None
    out = capsys.readouterr().out
    assert "CSV" in out


def test_delete_gpu_stress_run(db_with_gpu_runs, capsys):
    """delete_run удаляет GPU-стресс по UUID (автодетект таблицы)."""
    _db, _bench, stress = db_with_gpu_runs
    runs_cmd.delete_run(str(stress.id), yes=True)
    out = capsys.readouterr().out
    assert "Удалено" in out
    # После удаления в ленте больше нет этого gpu_stress.
    refs = runs_cmd.collect_unified_listing(limit=20)
    assert all(r.uuid != str(stress.id) for r in refs)


def test_delete_gpu_run(db_with_gpu_runs, capsys):
    """delete_run удаляет GPU-бенчмарк по UUID (автодетект таблицы)."""
    _db, bench, _stress = db_with_gpu_runs
    runs_cmd.delete_run(str(bench.id), yes=True)
    out = capsys.readouterr().out
    assert "Удалено" in out
