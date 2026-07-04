"""Тесты WebUI-контроллера GPU-бенчмарка (`_GpuController`) + регистрация роутов.

Мирроринг подхода `test_gpu_benchmark_orchestrator.py`: реальный OpenCL/GPU не
нужен — дефолтный бэкенд подменяется in-memory фейком через monkeypatch
(`apexcore.infrastructure.gpu.build_default_gpu_backend`), поскольку контроллер
строит бэкенд внутри рабочего потока.

Проверяем инварианты (спека `docs/gpu_benchmark.md` §10.3 + требования UI):
1. Роуты `/api/gpu/{devices,start,status,stop}` зарегистрированы на app.
2. Idle-статус имеет ожидаемую форму (running/job_id/status/progress/last_result).
3. Нет GPU (бэкенд недоступен) → прогон завершается `completed` (это валидный
   результат, а не ошибка), `last_result.score is None`, есть note.
4. Некорректный `device_index` (< 0) → ValueError (эндпойнт превратит в 400).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

# extras 'webui' — fastapi нужен для server.py. Нет — skip весь модуль.
pytest.importorskip("fastapi")

from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.interfaces.webui.server import (
    _GpuController,
    _GpuStressController,
    create_app,
)


class _FakeAdapter:
    name = "fake"

    def get_system_info(self) -> SystemInfo:
        return SystemInfo(
            os_name="Windows",
            os_version="10.0",
            cpu_model="Intel(R) Core(TM) i9-12900K",
            cpu_cores=CpuCores(physical=16, logical=24),
            ram_total_gb=32.0,
            gpu_list=[],
            cpu_arch="x86_64",
            hostname="test-host",
            cpu_base_mhz=3200.0,
            timestamp=datetime.now(timezone.utc),
        )


class _UnavailableBackend:
    """Бэкенд без GPU: is_available() == False, list_devices() == []."""

    name = "fake_unavailable"

    def is_available(self) -> bool:
        return False

    def list_devices(self) -> list:
        return []

    def supports(self, device_index, kind) -> bool:  # pragma: no cover - не вызывается
        return False

    def measure(self, *a, **k):  # pragma: no cover - не вызывается
        raise AssertionError("measure не должен вызываться без устройств")


def _wait_until_done(ctrl: _GpuController, timeout_sec: float = 5.0) -> dict:
    """Дождаться завершения фонового потока контроллера."""
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        st = ctrl.status()
        if not st["running"]:
            return st
        time.sleep(0.02)
    raise AssertionError("GPU-контроллер не завершился за отведённое время")


def test_gpu_routes_registered() -> None:
    """Все 4 GPU-эндпойнта зарегистрированы на FastAPI-приложении."""
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/api/gpu/devices" in paths
    assert "/api/gpu/start" in paths
    assert "/api/gpu/status" in paths
    assert "/api/gpu/stop" in paths


def test_idle_status_shape(tmp_path) -> None:
    """Стартовый статус контроллера — ожидаемая форма для frontend'а."""
    ctrl = _GpuController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    st = ctrl.status()
    assert st["running"] is False
    assert st["job_id"] is None
    assert st["status"] == "idle"
    assert st["error"] is None
    assert st["last_result"] is None
    assert st["progress"] == {"phase": "", "idx": 0, "total": 5}


def test_negative_device_index_raises(tmp_path) -> None:
    """device_index < 0 → ValueError (эндпойнт /api/gpu/start вернёт 400)."""
    ctrl = _GpuController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    with pytest.raises(ValueError):
        ctrl.start(device_index=-1)


def test_no_gpu_completes_gracefully(tmp_path, monkeypatch) -> None:
    """Нет GPU → прогон завершается `completed`, last_result.score None, есть note.

    Отчёт без устройства (`device.index == -1`, `score=None`) — валидный
    результат оркестратора, а не ошибка. Контроллер сохраняет его в БД и
    кладёт в `last_result`. Прогон НЕ помечается `failed`.
    """
    # Контроллер строит бэкенд через build_default_gpu_backend внутри потока —
    # подменяем его на фейк без устройств.
    monkeypatch.setattr(
        "apexcore.infrastructure.gpu.build_default_gpu_backend",
        lambda: _UnavailableBackend(),
    )
    ctrl = _GpuController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    ctrl.start(
        device_index=0,
        fp32_duration_sec=0.01,
        fp64_duration_sec=0.01,
        mem_duration_sec=0.01,
        pcie_duration_sec=0.01,
        cooldown_sec=0.0,
    )
    st = _wait_until_done(ctrl)

    assert st["status"] == "completed"
    assert st["error"] is None
    result = st["last_result"]
    assert result is not None
    assert result["score"] is None
    assert result["cancelled"] is False
    assert result["device"]["index"] == -1  # placeholder-устройство
    assert any("недоступен" in n.lower() for n in result["notes"])


def test_stop_before_start_is_noop(tmp_path) -> None:
    """stop() без активного прогона не падает и возвращает idle-статус."""
    ctrl = _GpuController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    st = ctrl.stop()
    assert st["running"] is False
    assert st["status"] == "idle"


# ─────────────────── GPU-стресс: контроллер + роуты + история ────────────────
#
# Тот же подход, что и у GPU-бенчмарка: реальный OpenCL/GPU не нужен, дефолтный
# бэкенд подменяется фейком без устройств (monkeypatch build_default_gpu_backend),
# т.к. контроллер строит бэкенд внутри рабочего потока. Оркестратор без устройств
# возвращает отчёт verdict=UNKNOWN + note — валидный результат, не ошибка.


def test_gpu_stress_routes_registered() -> None:
    """3 GPU-стресс-эндпойнта зарегистрированы на FastAPI-приложении."""
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/api/gpu/stress/start" in paths
    assert "/api/gpu/stress/status" in paths
    assert "/api/gpu/stress/stop" in paths


def test_gpu_stress_idle_status_shape(tmp_path) -> None:
    """Стартовый статус GPU-стресс-контроллера — ожидаемая форма для frontend'а."""
    ctrl = _GpuStressController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    st = ctrl.status()
    assert st["running"] is False
    assert st["job_id"] is None
    assert st["status"] == "idle"
    assert st["error"] is None
    assert st["last_result"] is None
    assert st["progress"] == {"elapsed_sec": 0.0, "duration_sec": 0.0}


def test_gpu_stress_negative_device_index_raises(tmp_path) -> None:
    """device_index < 0 → ValueError (эндпойнт /api/gpu/stress/start вернёт 400)."""
    ctrl = _GpuStressController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    with pytest.raises(ValueError):
        ctrl.start(device_index=-1)


def test_gpu_stress_stop_before_start_is_noop(tmp_path) -> None:
    """stop() без активного прогона не падает и возвращает idle-статус."""
    ctrl = _GpuStressController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    st = ctrl.stop()
    assert st["running"] is False
    assert st["status"] == "idle"


def test_gpu_stress_no_gpu_completes_gracefully(tmp_path, monkeypatch) -> None:
    """Нет GPU → прогон завершается `completed`, verdict=unknown, есть note.

    Отчёт без устройства (`device.index == -1`, `verdict=UNKNOWN`) — валидный
    результат оркестратора, а не ошибка. Контроллер сохраняет его в БД и кладёт
    в `last_result`. Прогон НЕ помечается `failed`.
    """
    monkeypatch.setattr(
        "apexcore.infrastructure.gpu.build_default_gpu_backend",
        lambda: _UnavailableBackend(),
    )
    ctrl = _GpuStressController(adapter=_FakeAdapter(), db_path=tmp_path / "runs.db")
    ctrl.start(device_index=0, duration_sec=0.05)
    st = _wait_until_done(ctrl)

    assert st["status"] == "completed"
    assert st["error"] is None
    result = st["last_result"]
    assert result is not None
    assert result["verdict"] == "unknown"
    assert result["cancelled"] is False
    assert result["device"]["index"] == -1  # placeholder-устройство
    assert any("недоступен" in n.lower() for n in result["notes"])


def _seed_gpu_runs(db_path) -> tuple[str, str]:
    """Заполнить temp-БД одним gpu-бенчмарк и одним gpu-стресс прогоном.

    Возвращает (gpu_id, gpu_stress_id) для проверки их появления в истории.
    """
    from apexcore.domain.gpu import (
        GpuBenchmarkReport,
        GpuDeviceInfo,
        GpuDeviceType,
        GpuStressReport,
        GpuStressVerdict,
    )
    from apexcore.infrastructure.persistence import (
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
    )

    sys_info = _FakeAdapter().get_system_info()
    device = GpuDeviceInfo(
        index=0, name="Fake GPU", vendor="NVIDIA",
        device_type=GpuDeviceType.DISCRETE,
    )
    now = datetime.now(timezone.utc)

    bench = GpuBenchmarkReport(
        system_info=sys_info, device=device, started_at=now, ended_at=now,
        score=6543.0, fp32_gflops=12000.0, r_fp32=0.65,
    )
    stress = GpuStressReport(
        system_info=sys_info, device=device, started_at=now, ended_at=now,
        duration_sec=60.0, verdict=GpuStressVerdict.PASS,
        max_temp_c=72.0, avg_temp_c=70.0, samples_taken=60,
    )

    brepo = SqliteGpuBenchmarkRepository(db_path)
    srepo = SqliteGpuStressRepository(db_path)
    try:
        brepo.save(bench)
        srepo.save(stress)
    finally:
        brepo.close()
        srepo.close()
    return str(bench.id), str(stress.id)


def test_history_unified_includes_gpu_and_gpu_stress(tmp_path, monkeypatch) -> None:
    """`/api/history` мерджит gpu (kind=gpu, балл) + gpu_stress (kind=gpu_stress, вердикт).

    Направляем `create_app()` на temp-БД через APEXCORE_DB_PATH, засеваем оба
    GPU-репозитория, извлекаем closure-обработчик `/api/history` из app.routes
    и зовём его напрямую (TestClient недоступен — httpx не входит в зависимости).
    """
    import asyncio

    db_path = tmp_path / "history.sqlite3"
    monkeypatch.setenv("APEXCORE_DB_PATH", str(db_path))
    gpu_id, gpu_stress_id = _seed_gpu_runs(db_path)

    app = create_app()
    endpoint = None
    for route in app.routes:
        if getattr(route, "path", None) == "/api/history":
            endpoint = route.endpoint
            break
    assert endpoint is not None, "маршрут /api/history не найден"

    items = asyncio.run(endpoint(limit=50))
    by_id = {it["id"]: it for it in items}

    # GPU-бенчмарк: kind=gpu, показывает балл.
    assert gpu_id in by_id, "gpu-бенчмарк не попал в историю"
    gpu_item = by_id[gpu_id]
    assert gpu_item["type"] == "gpu"
    assert gpu_item["type_label"] == "Тест GPU"
    assert gpu_item["score"] == 6543.0
    assert gpu_item["score_label"] == "6543"

    # GPU-стресс: kind=gpu_stress, показывает вердикт (не балл).
    assert gpu_stress_id in by_id, "gpu-стресс не попал в историю"
    gs_item = by_id[gpu_stress_id]
    assert gs_item["type"] == "gpu_stress"
    assert gs_item["type_label"] == "GPU-стресс"
    assert gs_item["score"] is None
    assert gs_item["score_label"] == "PASS"
    assert gs_item["verdict"] == "pass"
