"""HTTP-сервер для веб-визуализации apexcore (FastAPI).

Только локальный (`127.0.0.1`) запуск. Содержит:
- REST API: система, прогоны, тренды, управление стрессом и бенчмарком;
- WebSocket: живой стрим `MetricSnapshot` от фонового семплера;
- статическая страница с Chart.js.

Зависимости: fastapi, uvicorn, websockets — в extras `webui`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform as _platform
import sqlite3
import sys as _sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, PlainTextResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Web UI требует extras 'webui'. Установите: pip install -e \".[webui]\""
    ) from exc


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles, заставляющий браузер ревалидировать ассеты по ETag.

    Без Cache-Control браузер эвристически кэширует ES-модули WebUI/мастера
    и после `apt upgrade` / переустановки может тихо отдавать устаревший JS
    (симптом: новый элемент UI «не появляется», хотя файл на диске свежий —
    .deb-сборка к тому же пинит mtime к дате changelog, так что Last-Modified
    не меняется между патчами одной версии). `no-cache` не запрещает кэш, а
    требует ревалидации: при неизменном файле — дешёвый 304, при изменённом —
    200 с новым контентом.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

from apexcore import __version__ as _apexcore_version
from apexcore.application.diagnostics_sensors import diagnose_sensors
from apexcore.application.general_benchmark import (
    GeneralBenchmarkOrchestrator,
    GeneralBenchmarkParams,
)
from apexcore.application.general_benchmark_score import compute_general_benchmark_score
from apexcore.application.sensor_service import (
    InMemorySensorBus,
    SensorService,
)
from apexcore.application.telemetry_service import (
    InMemoryMetricsBus,
    TelemetryService,
)
from apexcore.application.trends import build_run_trend
from apexcore.domain.errors import RepositoryError
from apexcore.domain.models import BenchmarkConfig, MetricSnapshot
from apexcore.domain.sensor_models import SensorSnapshot
from apexcore.infrastructure.adapters import AdapterFactory
from apexcore.infrastructure.persistence import (
    SqliteBaselineRepository,
    SqliteResultRepository,
)
from apexcore.infrastructure.stress import build_default_registry
from apexcore.infrastructure.stress.registry import PROFILES
from apexcore.interfaces.cli.menu.settings_store import (
    WEBUI_PORT_MAX,
    WEBUI_PORT_MIN,
    load_menu_settings,
    update_webui_host,
    update_webui_port,
)
from apexcore.shared.config import load_settings

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class StressStartRequest(BaseModel):
    engine: str
    duration_sec: float = 60.0
    threads: int = 0


class BenchRunRequest(BaseModel):
    profile: str = "cpu_heavy"
    duration_sec: float = 30.0
    rate_sec: float = 0.5
    threads: int = 0


class ConfigUpdateRequest(BaseModel):
    """Тело POST /api/config — изменение порта/хоста Web UI.

    Оба поля опциональны. Порт валидируется по диапазону [1024, 65535];
    хост — белый список (127.0.0.1 / localhost / 0.0.0.0 / ::1).
    """

    port: int | None = Field(default=None, ge=WEBUI_PORT_MIN, le=WEBUI_PORT_MAX)
    host: str | None = None


def _detect_platform() -> str:
    """Вернуть короткое имя платформы для UI: 'windows' / 'linux' / 'darwin'."""
    if _sys.platform.startswith("win"):
        return "windows"
    if _sys.platform == "darwin":
        return "darwin"
    return "linux"


class _StressController:
    """Фоновый контроллер для запуска/остановки стресс-движка из Web UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_engine: str | None = None
        self._started_at: datetime | None = None
        self._stop_event = threading.Event()
        self._last_result: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "engine": self._active_engine,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_result": self._last_result,
            }

    def start(self, engine_name: str, duration_sec: float, threads: int) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"Стресс уже запущен: {self._active_engine}")
            registry = build_default_registry()
            engine = registry.get(engine_name)
            if engine is None or not engine.is_available():
                raise ValueError(f"Движок '{engine_name}' недоступен")
            self._stop_event.clear()
            self._active_engine = engine_name
            self._started_at = datetime.now(timezone.utc)
            self._last_result = None

            def _run() -> None:
                try:
                    # Если есть поддержка stop_event в движке — передадим; иначе просто запустим.
                    result = engine.run(
                        duration_sec=duration_sec,
                        threads=threads if threads > 0 else None,
                    )
                    payload = {
                        "engine": result.engine,
                        "category": result.category,
                        "duration_sec": result.duration_actual_sec,
                        "threads": result.threads,
                        "throughput": result.throughput,
                        "throughput_unit": result.throughput_unit,
                    }
                    with self._lock:
                        self._last_result = payload
                except Exception as exc:
                    logger.exception("Стресс-движок упал")
                    with self._lock:
                        self._last_result = {"error": str(exc)}
                finally:
                    with self._lock:
                        self._active_engine = None
                        self._started_at = None

            t = threading.Thread(target=_run, name=f"webui-stress-{engine_name}", daemon=True)
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Пытается остановить стресс. Встроенные движки сейчас не поддерживают
        преждевременную остановку, поэтому отмечаем намерение — реальный стоп
        произойдёт по истечении duration. Это ограничение documented.
        """
        with self._lock:
            self._stop_event.set()
        return self.status()


class _BenchController:
    """Фоновый контроллер web-стресса (экран «Стресс-тест»).

    Паритет с CLI «Стресс-тест» (``interfaces/cli/menu/stress_menu.py``):
    CPU и RAM грузятся ОДНОВРЕМЕННО через ``ParallelStressRunner`` +
    ``ThermalWatchdog``, телеметрия пишется в ``metrics_history``. Итог
    сохраняется как ``BenchmarkResult`` в таблицу ``runs`` (stress_results
    + thermal + history); балл «Оценка под нагрузкой» считается лениво в
    ``GET /api/runs/{id}`` через ``compute_stress_score_context``.

    Cancel через ``threading.Event`` (см. ``cancel()``): оба движка делят
    один токен, watchdog/пользователь останавливают их разом; реальный
    stop за ≤ 1-2 сек на ближайшем cancel-tick.
    """

    def __init__(self, repo: SqliteResultRepository, baseline_repo: SqliteBaselineRepository) -> None:
        self._repo = repo
        self._baseline_repo = baseline_repo
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._result_id: str | None = None
        self._error: str | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "result_id": self._result_id,
                "error": self._error,
            }

    def start(self, req: BenchRunRequest) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Бенчмарк уже выполняется")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._result_id = None
            self._error = None
            # Свежий cancel-token на каждый прогон. Старый (если был)
            # уходит в GC вместе с предыдущим thread'ом.
            self._cancel_token = threading.Event()
            cancel_token = self._cancel_token

            duration_sec = float(req.duration_sec)
            sampling_rate_sec = (
                req.rate_sec if req.rate_sec and req.rate_sec > 0 else 0.5
            )
            threads = req.threads if req.threads and req.threads > 0 else None
            profile_name = req.profile or "timed_stress"
            repo = self._repo

            def _run() -> None:
                # Паритет с CLI «Стресс-тест» (interfaces/cli/menu/stress_menu.py):
                # CPU и RAM грузятся ОДНОВРЕМЕННО через ParallelStressRunner +
                # ThermalWatchdog, телеметрия пишется в history → честная тепловая
                # картина. Раньше web гонял профиль cpu_heavy ПОСЛЕДОВАТЕЛЬНО
                # (integer, потом FP) → в моменте грузилось одно, CPU грелся слабо.
                import os as _os

                from apexcore.application.parallel_runner import (
                    EngineSpec,
                    ParallelStressRunner,
                )
                from apexcore.application.thermal import compute_thermal_stability
                from apexcore.application.thermal_watchdog import ThermalWatchdog
                from apexcore.domain.models import BenchmarkResult
                from apexcore.infrastructure.stress.registry import (
                    pick_cpu_stressor,
                    pick_ram_stressor,
                )

                try:
                    from threadpoolctl import threadpool_limits as _threadpool_limits
                except Exception:  # pragma: no cover — зависимость объявлена
                    _threadpool_limits = None

                adapter = AdapterFactory.detect()
                registry = build_default_registry()
                bus = InMemoryMetricsBus()
                telemetry = TelemetryService(
                    adapter, bus, sampling_rate_sec=sampling_rate_sec
                )
                watchdog: ThermalWatchdog | None = None
                parallel = None
                history: list = []
                started_dt = datetime.now(timezone.utc)
                try:
                    _cpu_alias, cpu_engine = pick_cpu_stressor(registry)
                    _ram_alias, ram_engine = pick_ram_stressor(registry)
                    # Оставляем 2 ядра web-серверу (сэмплер сенсоров + asyncio
                    # event loop), иначе BLAS DGEMM забирает ВСЕ ядра и live-топбар
                    # «замирает» (R2). При авто (threads=None): BLAS лимитируем через
                    # threadpool_limits, STREAM — явным числом worker-потоков.
                    # Явный threads от пользователя уважаем как есть.
                    logical = _os.cpu_count() or 4
                    # DGEMM (builtin_large_dgemm) параллелится ВНУТРИ через BLAS
                    # (threadpool_limits ниже), поэтому python-поток ему нужен
                    # РОВНО один. Иначе N python-потоков × np.matmul плодят N
                    # буферов C (~128 МБ каждый) и оверсабскрайбят BLAS — на
                    # машине с малым ОЗУ (напр. 14.8 ГБ Astra) это даёт OOM и
                    # стресс-тест падает. STREAM (RAM) — memory-bound, ему нужно
                    # несколько python-потоков чтобы насытить контроллер памяти,
                    # но мало (~logical/4): иначе планировщик трэшит и сэмплер
                    # сенсоров голодает (топбар «замирает»).
                    cpu_threads = 1
                    if threads is None or threads <= 0:
                        blas_limit = max(2, logical - 2)
                        ram_threads = max(2, logical // 4)
                    else:
                        blas_limit = max(1, threads)
                        ram_threads = threads
                    plan = [
                        EngineSpec(engine=cpu_engine, threads=cpu_threads, label="CPU"),
                        EngineSpec(engine=ram_engine, threads=ram_threads, label="RAM"),
                    ]
                    telemetry.start(record_history=True)
                    watchdog = ThermalWatchdog(bus=bus, cancel_token=cancel_token)
                    watchdog.start()
                    limit_ctx = (
                        _threadpool_limits(limits=blas_limit)
                        if _threadpool_limits is not None
                        else contextlib.nullcontext()
                    )
                    with limit_ctx:
                        parallel = ParallelStressRunner().run(
                            plan=plan,
                            duration_sec=duration_sec,
                            cancel_token=cancel_token,
                        )
                except Exception as exc:
                    logger.exception("Стресс-тест (web) упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)
                finally:
                    if watchdog is not None:
                        with contextlib.suppress(Exception):
                            watchdog.stop()
                    with contextlib.suppress(Exception):
                        history = telemetry.stop()
                if parallel is None:
                    return  # упало на старте — статус уже "failed"
                try:
                    thermal = compute_thermal_stability(history)
                    result = BenchmarkResult(
                        system_info=adapter.get_system_info(),
                        config=BenchmarkConfig(
                            profile_name=profile_name,
                            duration_sec=duration_sec,
                            sampling_rate_sec=sampling_rate_sec,
                            threads=threads,
                        ),
                        start_time=started_dt,
                        end_time=datetime.now(timezone.utc),
                        metrics_history=history,
                        stress_results=list(parallel.results),
                        final_score=0.0,
                        status="cancelled" if parallel.cancelled else "completed",
                        thermal=thermal,
                    )
                    repo.save(result)
                    with self._lock:
                        self._status = (
                            "cancelled" if parallel.cancelled else "completed"
                        )
                        self._result_id = str(result.id)
                except Exception as exc:
                    logger.exception("Стресс-тест (web): сохранение упало")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-stress-{self._job_id}", daemon=True
            )
            self._thread = t
            t.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        """Поставить cancel-сигнал текущему bench-прогону.

        Безопасно при отсутствии активного прогона: просто no-op (статус
        остаётся как был). Реальный stop происходит на ближайшем тике в
        стресс-движках (≤ ~1-2 сек) — статус через `status()` станет
        `"cancelled"` когда `_run` thread завершится.
        """
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


class _GeneralController:
    """Фоновый контроллер для запуска «Общей оценки системы» из Web UI (§9.4).

    Запускает GeneralBenchmarkOrchestrator в отдельном потоке, сохраняет
    результат в general_benchmark_runs через репозиторий, возвращает id
    последнего прогона для polling из UI.
    """

    def __init__(self, adapter, db_path: Path) -> None:
        self._adapter = adapter
        self._db_path = db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._result_id: str | None = None
        self._error: str | None = None
        self._progress: dict[str, Any] = {}  # последний on_progress callback

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "result_id": self._result_id,
                "error": self._error,
                "progress": dict(self._progress),
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Общая оценка уже выполняется")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._result_id = None
            self._error = None
            self._progress = {}

            # Сигнатура ДОЛЖНА совпадать с ProgressCallback оркестратора
            # (general_benchmark.py): on_progress(phase, idx, total). Раньше
            # тут было (phase, payload) → каждая фаза падала с TypeError
            # «takes 2 positional arguments but 3 were given», все ratio
            # становились None и «Общая оценка» обрывалась за ~12 с без балла.
            def _on_progress(phase: str, idx: int, total: int) -> None:
                with self._lock:
                    self._progress = {"phase": phase, "idx": idx, "total": total}

            def _run() -> None:
                # Импортируем здесь чтобы избежать heavy imports на старте процесса.
                from apexcore.infrastructure.persistence.general_benchmark_repo import (
                    SqliteGeneralBenchmarkRepository,
                )
                try:
                    orchestrator = GeneralBenchmarkOrchestrator(self._adapter)
                    report = orchestrator.run(
                        params=GeneralBenchmarkParams(),
                        on_progress=_on_progress,
                    )
                    # Вычисляем итоговый балл (×10 000) — он не считается оркестратором
                    # автоматически, см. application/general_benchmark_score.py.
                    # Функция возвращает `float | None`: None если хотя бы одно из
                    # ratio (r_dgemm/r_stream/r_disk) недоступно (типично — disk
                    # фаза пропущена при < 1 ГБ свободного на boot-диске).
                    # Раньше тут стояло `score_ctx.score`, но функция возвращает
                    # сам float, а не объект с атрибутом `.score` → AttributeError
                    # ломал общую оценку в WebUI.
                    score = compute_general_benchmark_score(
                        r_dgemm=report.r_dgemm,
                        r_stream=report.r_stream,
                        r_disk=report.r_disk,
                    )
                    if score is not None:
                        report = report.model_copy(update={"score": score})
                    repo = SqliteGeneralBenchmarkRepository(self._db_path)
                    try:
                        repo.save(report)
                    finally:
                        repo.close()
                    with self._lock:
                        self._status = "completed"
                        self._result_id = str(report.id)
                except Exception as exc:
                    logger.exception("Общая оценка упала")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-general-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()


class _MicroController:
    """Фоновый контроллер для «Расширенного тестирования процессора» (§9.3).

    Поддерживает два режима:
    - **Single/Multi сравнение** (`start_single_multi`) — через
      ``application.single_multi_compare.run_single_multi_compare``.
    - **Полный прогон** (`start_full_run`) — все 12 микробенчмарков через
      ``application.scoring_service.ScoringService.run_overall`` с фиксированным
      пресетом ``standard`` (3 прогона). Сохраняется в таблицу ``micro_runs``.

    Поле ``mode`` в ``last_result`` различает два режима для frontend'а:
    ``"single_multi"`` или ``"full_run"``.
    """

    # «Параден» бенч для Single/Multi — int64 IOPS. Тот же дефолт, что и в
     # CLI-меню (interfaces/cli/menu/screens.py `Int64IopsBench()`): стабильный
     # целочисленный показатель, не зависит от BLAS/AVX thread-pool и даёт
     # сопоставимые числа на всех платформах. FP64 (flops_dp) даст числа
     # порядково другие — не повторяет CLI-поведение.
    DEFAULT_BENCH = 'int_iops_64'

    def __init__(self, adapter, db_path) -> None:
        self._adapter = adapter
        self._db_path = db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._error: str | None = None
        self._progress: str = ""
        self._last_result: dict[str, Any] | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "error": self._error,
                "progress": self._progress,
                "last_result": self._last_result,
            }

    def start_single_multi(self, *, bench_name: str | None = None,
                           duration_sec: float = 5.0,
                           threads: int = 0) -> dict[str, Any]:
        bench_name = bench_name or self.DEFAULT_BENCH
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Тест уже идёт")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = "подготовка"
            self._cancel_token = threading.Event()

            def _progress_cb(phase: str) -> None:
                with self._lock:
                    self._progress = phase

            def _run() -> None:
                try:
                    import psutil as _psutil

                    from apexcore.application.cpu_ranking import match_cpu_ranking
                    from apexcore.application.single_multi_compare import (
                        run_single_multi_compare,
                    )
                    from apexcore.infrastructure.microbench import (
                        build_default_microbench_registry,
                    )

                    bench = next(
                        (b for b in build_default_microbench_registry()
                         if b.name == bench_name),
                        None,
                    )
                    if bench is None:
                        raise ValueError(f"Микробенч не найден: {bench_name}")
                    total_threads = threads if threads > 0 else (
                        _psutil.cpu_count(logical=True) or 8
                    )
                    result = run_single_multi_compare(
                        bench=bench,
                        duration_sec=duration_sec,
                        total_threads=total_threads,
                        cancel_token=self._cancel_token,
                        progress_cb=_progress_cb,
                    )
                    # CPU ranking — позиция среди ~41 популярных CPU из
                    # `data/cpu_ranking.yaml`. Никогда не падает на корректных
                    # входных данных (kind="none" если CPU не нашёлся).
                    ranking: dict[str, Any] | None = None
                    try:
                        sys_info = self._adapter.get_system_info()
                        rmatch = match_cpu_ranking(sys_info.cpu_model, sys_info.cpu_cores)
                        ranking = {
                            "kind": rmatch.kind,
                            "reason": rmatch.reason,
                            "total": rmatch.total,
                            "matched_cpu_name": (
                                rmatch.entry.display_name if rmatch.entry else None
                            ),
                            "single_rank": rmatch.single_rank,
                            "single_percentile": rmatch.single_percentile,
                            "multi_rank": rmatch.multi_rank,
                            "multi_percentile": rmatch.multi_percentile,
                            "core_distance": rmatch.core_distance,
                        }
                    except Exception as exc:
                        logger.warning("CPU ranking failed: %s", exc)
                    with self._lock:
                        self._last_result = {
                            "mode": "single_multi",
                            "bench": bench.name,
                            "duration_sec_per_test": result.duration_sec_per_test,
                            "single": {
                                "value": result.single.value,
                                "unit": result.single.unit,
                                "duration_actual_sec": result.single.duration_actual_sec,
                            },
                            "multi": {
                                "value": result.multi.value,
                                "unit": result.multi.unit,
                                "duration_actual_sec": result.multi.duration_actual_sec,
                            },
                            "cores_used_multi": result.cores_used_multi,
                            "physical_cores": result.physical_cores,
                            "physical_p_cores": result.physical_p_cores,
                            "physical_e_cores": result.physical_e_cores,
                            "pinned_cpu": result.pinned_cpu,
                            "pinned_kind": result.pinned_kind,
                            "speedup": result.speedup,
                            "efficiency": result.efficiency,
                            "ranking": ranking,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        }
                        self._status = "completed"
                        self._progress = "готово"
                except Exception as exc:
                    logger.exception("Single/Multi прогон упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-micro-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def start_full_run(self, *, duration_sec: float = 5.0,
                       threads: int = 0,
                       tests: list[str] | None = None) -> dict[str, Any]:
        """Полный прогон scoring v2 — все 12 микробенчмарков, пресет standard.

        Параметр ``tests`` (опциональный) — список имён микробенчей для запуска.
        Если ``None`` или пустой — запускаются все 12. Используется для
        точечного запуска подмножества тестов из Web UI (карточка «Полный
        прогон» → переключатель «Выбрать тесты»).

        Пресет ``standard`` (3 прогона, median-of-3) выбран как разумный
        дефолт для web — даёт балл с медианной устойчивостью без 25-минутного
        ожидания (accurate). Веб не показывает выбор пресета — это внутренняя
        деталь алгоритма (для тонкой настройки пусть пользователь идёт в CLI).
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Тест уже идёт")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = "Прогон 1/3"
            self._cancel_token = threading.Event()

            adapter = self._adapter
            db_path = self._db_path

            def _run() -> None:
                try:
                    from apexcore.application.scoring_service import ScoringService
                    from apexcore.infrastructure.persistence import (
                        SqliteMicroRunRepository,
                    )
                    from apexcore.interfaces.cli.menu.runners import (
                        run_microbench_suite,
                    )

                    repo_local = SqliteMicroRunRepository(db_path)
                    service = ScoringService(
                        adapter=adapter,
                        repo=repo_local,
                        suite_runner=run_microbench_suite,
                    )

                    def _progress_cb(run_idx: int, total: int) -> None:
                        with self._lock:
                            self._progress = f"Прогон {run_idx}/{total}"

                    result = service.run_overall(
                        preset="standard",
                        duration_sec=duration_sec,
                        threads=threads,
                        cancel_token=self._cancel_token,
                        progress=_progress_cb,
                        save=True,
                        selected_workloads=tests or None,
                    )

                    # Соберём упрощённый payload без огромного suite'а.
                    overall = result.overall
                    payload: dict[str, Any] = {
                        "mode": "full_run",
                        "id": str(result.id),
                        "n_runs": result.n_runs,
                        "preset": result.preset,
                        "duration_sec_per_test": result.duration_sec_per_test,
                        "n_tests": len(result.results),
                        "tests": [
                            {
                                "name": r.name,
                                "category": r.category,
                                "value": r.value,
                                "unit": r.unit,
                                "error": r.error,
                            }
                            for r in result.results
                        ],
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if overall is not None:
                        # overall_score (единый балл) удалён в 0.9.x — micro
                        # это per-category анализ (subscores), не системный
                        # балл. overall_ratio оставлен для внутренних нужд.
                        payload["overall"] = {
                            "overall_ratio": overall.overall_ratio,
                            "subscores": dict(overall.subscores),
                            "ci_lower": overall.ci_lower,
                            "ci_upper": overall.ci_upper,
                            "ci_method": overall.ci_method,
                            "n_runs": overall.n_runs,
                            "provisional": overall.provisional,
                        }
                    with self._lock:
                        self._last_result = payload
                        self._status = "completed" if overall is not None else "cancelled"
                        self._progress = "готово" if overall is not None else "отменено"
                except Exception as exc:
                    logger.exception("Полный прогон scoring v2 упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-micro-full-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Best-effort отмена через cancel_token."""
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


# Человекочитаемые имена 5 стадий Winsat для UI (вместо raw cpu_aes/cpu_sha1/...).
_WINSAT_STAGE_LABELS: dict[str, str] = {
    "cpu_aes":     "AES-256 (CPU)",
    "cpu_sha1":    "SHA-1 (CPU)",
    "memory":      "Чтение памяти",
    "disk_seq":    "Диск · последовательное чтение",
    "disk_random": "Диск · случайное чтение",
    "dwm":         "Графика (DWM + DirectX)",
}


class _WinsatController:
    """Фоновый контроллер для «Наследие Winsat» (§9.7) — Windows-only.

    Оркестрирует WinsatService.run_formal() в отдельном потоке: 5 стадий
    (AES-256, SHA-1, Memory Read, Disk Seq, Disk Random) → 5 подскоров
    Win32_Winsat-формата + общий WinSPR-уровень. Результат сохраняется
    в таблицу winsat_runs через SqliteWinsatRepository.

    Linux: при попытке start() backend вернёт failed с понятной ошибкой
    — frontend сам отрисует platform-restriction.
    """

    def __init__(self, adapter, db_path) -> None:
        self._adapter = adapter
        self._db_path = db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._error: str | None = None
        # progress хранит человеческое имя стадии и индекс N/5.
        self._progress: dict[str, Any] = {"stage": "", "idx": 0, "total": 5}
        self._last_result: dict[str, Any] | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "error": self._error,
                "progress": dict(self._progress),
                "last_result": self._last_result,
            }

    def start(self, *, duration_sec: float = 5.0) -> dict[str, Any]:
        """Запустить Winsat-прогон. Длительность — на стадию (по умолчанию 5с)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Winsat уже идёт")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = {"stage": "подготовка", "idx": 0, "total": 5}
            self._cancel_token = threading.Event()

            adapter = self._adapter
            db_path = self._db_path

            def _run() -> None:
                try:
                    from apexcore.application.winsat_service import WinsatService
                    from apexcore.infrastructure.persistence.winsat_repo import (
                        SqliteWinsatRepository,
                    )

                    if not WinsatService.is_supported():
                        raise RuntimeError(
                            "Winsat доступен только на Windows. На текущей "
                            "ОС раздел недоступен."
                        )
                    repo_local = SqliteWinsatRepository(db_path)
                    service = WinsatService(adapter=adapter, repo=repo_local)

                    def _progress_cb(stage: str, idx: int, total: int) -> None:
                        with self._lock:
                            self._progress = {
                                "stage": _WINSAT_STAGE_LABELS.get(stage, stage),
                                "stage_raw": stage,
                                "idx": idx,
                                "total": total,
                            }

                    report = service.run_formal(
                        duration_sec_per_test=duration_sec,
                        cancel_token=self._cancel_token,
                        on_progress=_progress_cb,
                        save=True,
                    )
                    with self._lock:
                        self._last_result = report.model_dump(mode="json")
                        self._status = "cancelled" if report.cancelled else "completed"
                        self._progress = {
                            "stage": "готово", "idx": 5, "total": 5,
                        }
                except Exception as exc:
                    logger.exception("Winsat прогон упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-winsat-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Best-effort отмена через cancel_token."""
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


class _RamCacheController:
    """Фоновый контроллер «Ram & Cache» (§9.6).

    Прогон диагностический — результаты НЕ сохраняются в БД (как в CLI
    `apexcore ram-cache run`). Last_result хранится in-memory до перезапуска
    backend или нового запуска. JSON-экспорт доступен через
    payload последнего status().

    Прогон: 4 уровня (L1/L2/L3/DRAM) × 4 операции (read/write/copy/latency)
    = 16 измерений (можно подмножество через ``tests``). Один measurement
    занимает ~2 секунды, полный прогон ~30-40 секунд (на быстрой машине).
    """

    def __init__(self, adapter) -> None:
        self._adapter = adapter
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._error: str | None = None
        self._progress: dict[str, Any] = {
            "level": "", "operation": "", "idx": 0, "total": 16,
        }
        self._last_result: dict[str, Any] | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "error": self._error,
                "progress": dict(self._progress),
                "last_result": self._last_result,
            }

    def start(self, *, duration_sec_per_metric: float = 2.0,
              tests: list[str] | None = None) -> dict[str, Any]:
        """Запуск прогона. `tests` — опциональное подмножество
        канонических имён (см. ram_cache_service.all_test_names),
        например ``["l1_read", "dram_latency"]``. Пусто — все 16.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Ram&Cache уже идёт")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = {
                "level": "", "operation": "", "idx": 0,
                "total": len(tests) if tests else 16,
            }
            self._cancel_token = threading.Event()

            adapter = self._adapter

            def _run() -> None:
                try:
                    from apexcore.application.ram_cache_service import (
                        RamCacheService,
                        parse_test_name,
                    )

                    selected_pairs: set | None = None
                    if tests:
                        pairs = set()
                        for t in tests:
                            p = parse_test_name(t)
                            if p is not None:
                                pairs.add(p)
                        selected_pairs = pairs or None

                    service = RamCacheService(adapter=adapter)

                    def _progress_cb(level, op, idx, total):
                        with self._lock:
                            self._progress = {
                                "level": level,
                                "operation": op,
                                "idx": idx,
                                "total": total,
                            }

                    report = service.run(
                        duration_sec_per_metric=duration_sec_per_metric,
                        cancel_token=self._cancel_token,
                        on_progress=_progress_cb,
                        selected_pairs=selected_pairs,
                    )
                    with self._lock:
                        self._last_result = report.model_dump(mode="json")
                        self._status = "cancelled" if report.cancelled else "completed"
                        self._progress = {
                            "level": "", "operation": "",
                            "idx": len(report.metrics),
                            "total": len(report.metrics),
                        }
                except Exception as exc:
                    logger.exception("Ram&Cache прогон упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-ramcache-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


class _GpuController:
    """Фоновый контроллер GPU-бенчмарка (Roofline, шкала ×10 000).

    Мирроринг :class:`_GeneralController` + `_RamCacheController`: прогон в
    отдельном потоке через :class:`GpuBenchmarkOrchestrator`, сохранение
    результата в таблицу ``gpu_benchmark_runs`` через
    :class:`SqliteGpuBenchmarkRepository`. В отличие от общей оценки, балл
    (``score``) считает сам оркестратор — тут его пересчитывать не нужно.

    ``last_result`` держит ``report.model_dump(mode="json")`` последнего
    прогона in-memory (как Ram&Cache) — frontend читает его прямо из
    ``status()`` без отдельного запроса к репозиторию. Отчёт всё равно
    пишется в БД для истории. Отмена — через ``cancel_token`` (как в
    оркестраторе: последующие фазы не запускаются).

    Прогон graceful даже без GPU: оркестратор вернёт отчёт со ``score=None``
    и note «OpenCL/GPU недоступен» — поток завершится статусом ``completed``
    (это валидный результат, а не ошибка).
    """

    def __init__(self, adapter, db_path: Path) -> None:
        self._adapter = adapter
        self._db_path = db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._error: str | None = None
        self._progress: dict[str, Any] = {"phase": "", "idx": 0, "total": 5}
        self._last_result: dict[str, Any] | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "error": self._error,
                "progress": dict(self._progress),
                "last_result": self._last_result,
            }

    def start(
        self,
        *,
        device_index: int = 0,
        fp32_duration_sec: float = 5.0,
        fp64_duration_sec: float = 5.0,
        mem_duration_sec: float = 5.0,
        pcie_duration_sec: float = 2.0,
        cooldown_sec: float = 2.0,
    ) -> dict[str, Any]:
        if device_index < 0:
            raise ValueError("device_index должен быть ≥ 0")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("GPU-тест уже выполняется")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = {"phase": "", "idx": 0, "total": 5}
            self._cancel_token = threading.Event()

            # Сигнатура ДОЛЖНА совпадать с ProgressCallback оркестратора
            # (gpu_benchmark.py): on_progress(phase, idx, total). Фазы:
            # fp32 / fp64 / mem_bandwidth / pcie_h2d / pcie_d2h (total=5).
            def _on_progress(phase: str, idx: int, total: int) -> None:
                with self._lock:
                    self._progress = {"phase": phase, "idx": idx, "total": total}

            def _run() -> None:
                # Тяжёлые импорты (OpenCL-бэкенд, репозиторий) — внутри потока,
                # чтобы не грузить их на старте процесса.
                from apexcore.application.gpu_benchmark import (
                    GpuBenchmarkOrchestrator,
                    GpuBenchmarkParams,
                )
                from apexcore.infrastructure.gpu import build_default_gpu_backend
                from apexcore.infrastructure.persistence import (
                    SqliteGpuBenchmarkRepository,
                )
                try:
                    backend = build_default_gpu_backend()
                    orchestrator = GpuBenchmarkOrchestrator(self._adapter, backend)
                    params = GpuBenchmarkParams(
                        fp32_duration_sec=fp32_duration_sec,
                        fp64_duration_sec=fp64_duration_sec,
                        mem_duration_sec=mem_duration_sec,
                        pcie_duration_sec=pcie_duration_sec,
                        cooldown_sec=cooldown_sec,
                    )
                    report = orchestrator.run(
                        device_index=device_index,
                        params=params,
                        cancel_token=self._cancel_token,
                        on_progress=_on_progress,
                    )
                    # Сохраняем в БД (история). Отчёт без GPU (device.index == -1,
                    # score=None) тоже валиден — репозиторий его примет.
                    repo = SqliteGpuBenchmarkRepository(self._db_path)
                    try:
                        repo.save(report)
                    finally:
                        repo.close()
                    with self._lock:
                        self._last_result = report.model_dump(mode="json")
                        self._status = "cancelled" if report.cancelled else "completed"
                except Exception as exc:
                    logger.exception("GPU-бенчмарк упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-gpu-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


class _GpuStressController:
    """Фоновый контроллер GPU-стресс-теста (термостабильность, вердикт PASS/WARN/FAIL).

    Мирроринг :class:`_GpuController`, но headline это не балл, а вердикт
    (``verdict``). Прогон в отдельном потоке через
    :class:`GpuStressOrchestrator`; сохранение отчёта в таблицу
    ``gpu_stress_runs`` через :class:`SqliteGpuStressRepository`.

    ``last_result`` держит ``report.model_dump(mode="json")`` последнего прогона
    in-memory (как GPU-бенчмарк) — frontend читает его прямо из ``status()``.
    Отчёт всё равно пишется в БД для истории. Отмена — через ``cancel_token``
    (оркестратор досемплирует хвост и вернёт вердикт по частичным данным).

    Прогон graceful даже без GPU: оркестратор вернёт отчёт с
    ``verdict="unknown"`` + note «OpenCL/GPU недоступен» — поток завершится
    статусом ``completed`` (это валидный результат, а не ошибка).

    В отличие от GPU-бенчмарка прогресс приходит из оркестратора как
    ``on_progress(elapsed_sec, duration_sec)`` (два float'а, а не phase/idx/total)
    — храним оба под ``_lock`` для progress-бара «прошло / всего».
    """

    def __init__(self, adapter, db_path: Path) -> None:
        self._adapter = adapter
        self._db_path = db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._status: str = "idle"
        self._started_at: datetime | None = None
        self._error: str | None = None
        # progress: сколько секунд прошло из общей длительности прогона.
        self._progress: dict[str, Any] = {"elapsed_sec": 0.0, "duration_sec": 0.0}
        self._last_result: dict[str, Any] | None = None
        self._cancel_token: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "job_id": self._job_id,
                "status": self._status,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "error": self._error,
                "progress": dict(self._progress),
                "last_result": self._last_result,
            }

    def start(
        self,
        *,
        device_index: int = 0,
        duration_sec: float = 60.0,
    ) -> dict[str, Any]:
        if device_index < 0:
            raise ValueError("device_index должен быть ≥ 0")
        if duration_sec <= 0:
            raise ValueError("duration_sec должен быть > 0")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("GPU-стресс уже выполняется")
            self._job_id = str(uuid.uuid4())
            self._status = "running"
            self._started_at = datetime.now(timezone.utc)
            self._error = None
            self._progress = {"elapsed_sec": 0.0, "duration_sec": float(duration_sec)}
            self._cancel_token = threading.Event()

            # Сигнатура ДОЛЖНА совпадать с ProgressCallback оркестратора
            # (gpu_stress.py): on_progress(elapsed_sec, duration_sec) — оба float.
            def _on_progress(elapsed_sec: float, total_sec: float) -> None:
                with self._lock:
                    self._progress = {
                        "elapsed_sec": float(elapsed_sec),
                        "duration_sec": float(total_sec),
                    }

            def _run() -> None:
                # Тяжёлые импорты (OpenCL-бэкенд, репозиторий) — внутри потока,
                # чтобы не грузить их на старте процесса.
                from apexcore.application.gpu_stress import GpuStressOrchestrator
                from apexcore.infrastructure.gpu import build_default_gpu_backend
                from apexcore.infrastructure.persistence import (
                    SqliteGpuStressRepository,
                )
                try:
                    backend = build_default_gpu_backend()
                    orchestrator = GpuStressOrchestrator(self._adapter, backend)
                    report = orchestrator.run(
                        device_index=device_index,
                        duration_sec=duration_sec,
                        cancel_token=self._cancel_token,
                        on_progress=_on_progress,
                    )
                    # Сохраняем в БД (история). Отчёт без GPU (verdict=unknown)
                    # тоже валиден — репозиторий его примет.
                    repo = SqliteGpuStressRepository(self._db_path)
                    try:
                        repo.save(report)
                    finally:
                        repo.close()
                    with self._lock:
                        self._last_result = report.model_dump(mode="json")
                        self._status = "cancelled" if report.cancelled else "completed"
                except Exception as exc:
                    logger.exception("GPU-стресс упал")
                    with self._lock:
                        self._status = "failed"
                        self._error = str(exc)

            t = threading.Thread(
                target=_run, name=f"webui-gpu-stress-{self._job_id}", daemon=True,
            )
            self._thread = t
            t.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._cancel_token is not None:
                self._cancel_token.set()
        return self.status()


# ─── §9.9 helpers: lookup / export / delete для разных типов прогонов ────────

# Человекочитаемые имена типов для UI / имени файла экспорта.
_RUN_KIND_LABEL: dict[str, str] = {
    "stress":     "Стресс-тест",
    "micro":      "Расш. тест CPU",
    "winsat":     "Winsat",
    "general":    "Общая оценка",
    "gpu":        "Тест GPU",
    "gpu_stress": "GPU-стресс",
}


def _lookup_run(db_path, run_id_or_prefix: str):
    """Найти прогон по UUID/префиксу в одном из 4 репозиториев.

    Возвращает (kind, real_id, model) или (None, None, None) если не найден.
    Порядок проверки: stress → micro → winsat → general → gpu → gpu_stress
    (любая фиксированная последовательность; UUID коллизии между типами
    исключены).
    """
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )

    for kind, repo_cls in [
        ("stress",     SqliteResultRepository),
        ("micro",      SqliteMicroRunRepository),
        ("winsat",     SqliteWinsatRepository),
        ("general",    SqliteGeneralBenchmarkRepository),
        ("gpu",        SqliteGpuBenchmarkRepository),
        ("gpu_stress", SqliteGpuStressRepository),
    ]:
        repo = repo_cls(db_path)
        try:
            rid = repo.resolve_id(run_id_or_prefix)
            if rid is None:
                continue
            model = repo.get(rid)
            if model is None:
                continue
            return kind, rid, model
        except Exception:
            logger.exception("lookup failed for %s/%s", kind, run_id_or_prefix)
            continue
    return None, None, None


def _run_to_csv(kind: str, model) -> str:
    """Сериализовать прогон в CSV-формат. Для каждого типа своя структура.

    Все CSV-файлы валидны для Excel/LibreOffice — данные начинаются с
    шапки колонок, метаданные прогона уходят в строки-комментарии # key=val.
    """
    import csv
    import io

    buf = io.StringIO()
    if kind == "stress":
        # Используем готовый exporter через tempfile path. Получаем готовый CSV.
        import tempfile
        from pathlib import Path

        from apexcore.infrastructure.exporters.csv_exporter import export_run_csv
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as fh:
            tmp_path = Path(fh.name)
        try:
            export_run_csv(model, tmp_path)
            return tmp_path.read_text(encoding="utf-8")
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
    if kind == "micro":
        # Шапка метаданных + таблица: name, category, value, unit, error.
        buf.write(f"# apexcore_run={model.id}\n")
        buf.write("# kind=micro\n")
        if model.overall is not None:
            # overall_score удалён в 0.9.x — оставляем overall_ratio (доля пика).
            buf.write(f"# overall_ratio={model.overall.overall_ratio:.4f}\n")
            buf.write(f"# n_runs={model.n_runs}\n")
        buf.write(f"# start={model.start_time.isoformat()}\n")
        buf.write(f"# end={model.end_time.isoformat()}\n")
        writer = csv.writer(buf)
        writer.writerow(["name", "category", "value", "unit", "iterations", "error"])
        for r in model.results:
            writer.writerow([
                r.name, r.category, f"{r.value:.4f}",
                r.unit, r.iterations, r.error or "",
            ])
        return buf.getvalue()
    if kind == "winsat":
        buf.write(f"# apexcore_run={model.id}\n")
        buf.write("# kind=winsat\n")
        buf.write(f"# winspr_level={model.winspr_level:.1f}\n")
        buf.write(f"# start={model.started_at.isoformat()}\n")
        buf.write(f"# end={model.ended_at.isoformat()}\n")
        writer = csv.writer(buf)
        writer.writerow(["category", "metric_name", "metric_value", "metric_unit", "score", "status", "note"])
        for sub in [model.cpu_score, model.memory_score, model.disk_score,
                    model.graphics_score, model.d3d_score]:
            writer.writerow([
                sub.category, sub.metric_name, f"{sub.metric_value:.4f}",
                sub.metric_unit, f"{sub.score:.1f}", sub.status,
                sub.note or "",
            ])
        return buf.getvalue()
    if kind == "general":
        buf.write(f"# apexcore_run={model.id}\n")
        buf.write("# kind=general\n")
        if model.score is not None:
            buf.write(f"# score={model.score:.2f}\n")
        buf.write(f"# start={model.started_at.isoformat()}\n")
        buf.write(f"# end={model.ended_at.isoformat()}\n")
        writer = csv.writer(buf)
        writer.writerow(["component", "r_value", "raw_value", "unit"])
        # General benchmark поля: r_dgemm/r_stream/r_disk + три абсолютных.
        for comp, r_val, raw_val, unit in [
            ("CPU",  model.r_dgemm,  model.dgemm_gflops,       "GFLOPS"),
            ("RAM",  model.r_stream, model.stream_gb_s,        "GB/s"),
            ("Disk", model.r_disk,   model.disk_seq_read_mb_s, "MB/s"),
        ]:
            writer.writerow([
                comp,
                "" if r_val is None else f"{r_val:.4f}",
                "" if raw_val is None else f"{raw_val:.2f}",
                unit,
            ])
        return buf.getvalue()
    if kind == "gpu":
        buf.write(f"# apexcore_run={model.id}\n")
        buf.write("# kind=gpu\n")
        if model.score is not None:
            buf.write(f"# score={model.score:.2f}\n")
        buf.write(f"# device={model.device.name}\n")
        buf.write(f"# start={model.started_at.isoformat()}\n")
        buf.write(f"# end={model.ended_at.isoformat()}\n")
        writer = csv.writer(buf)
        writer.writerow(["metric", "value", "peak", "ratio", "unit"])
        # GPU-бенчмарк поля: fp32/mem (в балле) + fp64/pcie (информационно).
        for name, val, peak, ratio, unit in [
            ("fp32", model.fp32_gflops, model.fp32_peak_gflops, model.r_fp32, "GFLOPS"),
            ("mem_bandwidth", model.mem_bandwidth_gb_s,
             model.mem_bandwidth_peak_gb_s, model.r_mem, "GB/s"),
            ("fp64", model.fp64_gflops, None, model.r_fp64, "GFLOPS"),
            ("pcie_h2d", model.pcie_h2d_gb_s, None, None, "GB/s"),
            ("pcie_d2h", model.pcie_d2h_gb_s, None, None, "GB/s"),
        ]:
            writer.writerow([
                name,
                "" if val is None else f"{val:.2f}",
                "" if peak is None else f"{peak:.2f}",
                "" if ratio is None else f"{ratio:.4f}",
                unit,
            ])
        return buf.getvalue()
    if kind == "gpu_stress":
        buf.write(f"# apexcore_run={model.id}\n")
        buf.write("# kind=gpu_stress\n")
        buf.write(f"# verdict={model.verdict.value}\n")
        buf.write(f"# device={model.device.name}\n")
        buf.write(f"# duration_sec={model.duration_sec:.1f}\n")
        buf.write(f"# start={model.started_at.isoformat()}\n")
        buf.write(f"# end={model.ended_at.isoformat()}\n")
        writer = csv.writer(buf)
        # Посекундные отсчёты телеметрии — основные данные (для графика).
        writer.writerow(["t_sec", "temp_c", "power_w", "clock_mhz", "util_pct"])
        for s in model.samples:
            writer.writerow([
                f"{s.t_sec:.1f}",
                "" if s.temp_c is None else f"{s.temp_c:.1f}",
                "" if s.power_w is None else f"{s.power_w:.1f}",
                "" if s.clock_mhz is None else f"{s.clock_mhz:.0f}",
                "" if s.util_pct is None else f"{s.util_pct:.0f}",
            ])
        return buf.getvalue()
    raise ValueError(f"Неизвестный тип прогона: {kind}")


def _fmt_opt(value: float | None, unit: str = "", digits: int = 1) -> str:
    """Отформатировать опциональное число с единицей, None → «—»."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def _run_to_html(kind: str, model) -> str:
    """Сгенерировать «HTML для печати» одного прогона.

    Лёгкая HTML-страница со встроенным CSS — пользователь жмёт Ctrl+P и
    сохраняет PDF из браузера. Это надёжнее (без новых зависимостей)
    чем reportlab/weasyprint на Astra Linux.
    """
    import html as _html

    def esc(x) -> str:
        return _html.escape(str(x))

    def section(title: str, rows: list[tuple[str, str]]) -> str:
        rows_html = "".join(
            f'<tr><td class="k">{esc(k)}</td><td class="v">{esc(v)}</td></tr>'
            for k, v in rows
        )
        return f'<h2>{esc(title)}</h2><table>{rows_html}</table>'

    meta_rows: list[tuple[str, str]] = [
        ("Тип прогона", _RUN_KIND_LABEL.get(kind, kind)),
        ("ID", str(getattr(model, "id", "?"))),
    ]

    body = ""
    if kind == "stress":
        meta_rows += [
            ("Профиль", str(model.config.profile_name)),
            ("Начало", model.start_time.isoformat()),
            ("Конец",  model.end_time.isoformat()),
            ("Длительность, с", f"{(model.end_time - model.start_time).total_seconds():.1f}"),
            ("Итоговый балл (legacy)", f"{model.final_score:.4f}"),
            ("CPU", model.system_info.cpu_model),
            ("ОС",  f"{model.system_info.os_name} {model.system_info.os_version}"),
        ]
        engine_rows = [
            (sr.engine,
             f"{sr.throughput:.4g} {sr.throughput_unit} · потоков {sr.threads} · {sr.duration_actual_sec:.1f} с")
            for sr in model.stress_results
        ]
        body = (
            section("Метаданные", meta_rows)
            + (section("Стресс-движки", engine_rows) if engine_rows else "")
        )
    elif kind == "micro":
        # «Итоговый балл системы» удалён в 0.9.x — micro это детальный
        # per-category анализ (см. таблицу тестов ниже), не системный балл.
        meta_rows += [
            ("Начало", model.start_time.isoformat()),
            ("Конец",  model.end_time.isoformat()),
            ("Длительность, с", f"{(model.end_time - model.start_time).total_seconds():.1f}"),
            ("Пресет", str(model.preset or "—")),
            ("Прогонов", str(model.n_runs)),
            ("CPU", model.system_info.cpu_model),
        ]
        test_rows = [
            (r.name, f"{r.value:.3f} {r.unit}" + (f" · ошибка: {r.error}" if r.error else ""))
            for r in model.results
        ]
        body = (
            section("Метаданные", meta_rows)
            + (section("Микробенчмарки", test_rows) if test_rows else "")
        )
    elif kind == "winsat":
        meta_rows += [
            ("Начало", model.started_at.isoformat()),
            ("Конец",  model.ended_at.isoformat()),
            ("WinSPR Level", f"{model.winspr_level:.1f}"),
            ("CPU", model.system_info.cpu_model),
            ("ОС",  f"{model.system_info.os_name} {model.system_info.os_version}"),
        ]
        sub_rows = []
        for sub in [model.cpu_score, model.memory_score, model.disk_score,
                    model.graphics_score, model.d3d_score]:
            sub_rows.append((
                str(sub.category).upper(),
                f"{sub.score:.1f} · {sub.metric_name} = {sub.metric_value:.3f} {sub.metric_unit} · {sub.status}"
                + (f" · {sub.note}" if sub.note else ""),
            ))
        body = section("Метаданные", meta_rows) + section("Подскоры", sub_rows)
    elif kind == "general":
        meta_rows += [
            ("Начало", model.started_at.isoformat()),
            ("Конец",  model.ended_at.isoformat()),
            ("Итоговый балл", "—" if model.score is None else f"{model.score:.0f}"),
            ("CPU", model.system_info.cpu_model),
            ("Накопитель", model.boot_drive_path or model.disk_model or "—"),
        ]
        component_rows = [
            ("CPU (DGEMM, fp64)",
             f"{(model.dgemm_gflops or 0):.1f} GFLOPS · r = {(model.r_dgemm or 0):.4f}"),
            ("RAM (STREAM, copy)",
             f"{(model.stream_gb_s or 0):.2f} GB/s · r = {(model.r_stream or 0):.4f}"),
            ("Boot-disk (sequential read)",
             f"{(model.disk_seq_read_mb_s or 0):.0f} MB/s · r = {(model.r_disk or 0):.4f}"),
        ]
        body = section("Метаданные", meta_rows) + section("Компоненты", component_rows)
    elif kind == "gpu":
        meta_rows += [
            ("Устройство", model.device.name),
            ("Начало", model.started_at.isoformat()),
            ("Конец",  model.ended_at.isoformat()),
            ("Итоговый балл", "—" if model.score is None else f"{model.score:.0f} / 10000"),
            ("CPU", model.system_info.cpu_model),
        ]
        component_rows = [
            ("FP32 (в балле)",
             f"{(model.fp32_gflops or 0):.0f} GFLOPS · r = {(model.r_fp32 or 0):.4f}"),
            ("VRAM bandwidth (в балле)",
             f"{(model.mem_bandwidth_gb_s or 0):.1f} GB/s · r = {(model.r_mem or 0):.4f}"),
            ("FP64 (информационно)",
             f"{(model.fp64_gflops or 0):.0f} GFLOPS · r = {(model.r_fp64 or 0):.4f}"),
            ("PCIe host→device",
             "—" if model.pcie_h2d_gb_s is None else f"{model.pcie_h2d_gb_s:.1f} GB/s"),
            ("PCIe device→host",
             "—" if model.pcie_d2h_gb_s is None else f"{model.pcie_d2h_gb_s:.1f} GB/s"),
        ]
        body = section("Метаданные", meta_rows) + section("Компоненты", component_rows)
    elif kind == "gpu_stress":
        meta_rows += [
            ("Устройство", model.device.name),
            ("Начало", model.started_at.isoformat()),
            ("Конец",  model.ended_at.isoformat()),
            ("Длительность, с", f"{model.duration_sec:.1f}"),
            ("Вердикт", model.verdict.value.upper()),
            ("CPU", model.system_info.cpu_model),
        ]
        summary_rows = [
            ("Температура (пик / средн.)",
             f"{_fmt_opt(model.max_temp_c, '°C')} / {_fmt_opt(model.avg_temp_c, '°C')}"),
            ("Мощность (пик / средн.)",
             f"{_fmt_opt(model.max_power_w, ' Вт')} / {_fmt_opt(model.avg_power_w, ' Вт')}"),
            ("Частота ядра (средн. / мин.)",
             f"{_fmt_opt(model.avg_clock_mhz, ' МГц', 0)} / {_fmt_opt(model.min_clock_mhz, ' МГц', 0)}"),
            ("Средняя загрузка", _fmt_opt(model.avg_util_pct, '%', 0)),
            ("Тепловой лимит", _fmt_opt(model.thermal_limit_c, '°C', 0)),
            ("Троттлинг", "да" if model.throttle_detected else "нет"),
        ]
        reason_rows = [("причина", r) for r in (model.throttle_reasons or [])]
        note_rows = [("примечание", n) for n in (model.notes or [])]
        body = (
            section("Метаданные", meta_rows)
            + section("Сводка телеметрии", summary_rows)
            + (section("Троттлинг", reason_rows) if reason_rows else "")
            + (section("Примечания", note_rows) if note_rows else "")
        )
    else:
        body = section("Метаданные", meta_rows)

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>ApexCore · {esc(_RUN_KIND_LABEL.get(kind, kind))} · {esc(getattr(model, "id", ""))}</title>
<style>
  body {{
    font-family: -apple-system, Segoe UI, system-ui, sans-serif;
    color: #222; max-width: 900px; margin: 28px auto;
    padding: 0 20px; line-height: 1.45;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #777; font-size: 12px; margin-bottom: 18px; }}
  h2 {{
    font-size: 14px; margin: 22px 0 8px; color: #444;
    border-bottom: 1px solid #ddd; padding-bottom: 4px;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{
    padding: 4px 8px; border-bottom: 1px solid #eee;
    vertical-align: top; font-size: 13px;
  }}
  td.k {{ color: #666; width: 38%; }}
  td.v {{ color: #111; }}
  footer {{
    margin-top: 28px; color: #aaa; font-size: 11px; text-align: center;
  }}
  @media print {{
    body {{ margin: 14px; }} h1 {{ font-size: 18px; }} h2 {{ font-size: 12px; }}
  }}
</style></head>
<body>
  <h1>ApexCore · {esc(_RUN_KIND_LABEL.get(kind, kind))}</h1>
  <div class="sub">Отчёт о прогоне · сохраните страницу как PDF (Ctrl+P → «Сохранить как PDF»)</div>
  {body}
  <footer>сгенерировано ApexCore · apexcore</footer>
</body></html>
"""


def create_app() -> FastAPI:
    """Создать FastAPI-приложение со всеми ресурсами."""
    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)
    baseline_repo = SqliteBaselineRepository(settings.db_path)
    adapter = AdapterFactory.detect()
    bus = InMemoryMetricsBus()
    telemetry = TelemetryService(adapter=adapter, bus=bus, sampling_rate_sec=settings.sampling_rate_sec)
    # §9.2 — SensorService: фоновый семплер, конвертит MetricSnapshot →
    # SensorSnapshot (группировка по device, badge источника, threshold_warn/crit,
    # ThrottleState). Использует тот же adapter, что и telemetry.
    #
    # storage_lhm_names + storage_smartctl_info — собираются один раз; делают
    # `device` в storage-readings человеческим именем модели вместо безликого
    # «Накопитель» / «sda». Аналогично CLI `apexcore sensors` (см.
    # interfaces/cli/commands/sensors.py).
    sensor_bus = InMemorySensorBus()
    storage_lhm_names: dict[str, str] = {}
    storage_smartctl_info: dict[str, dict[str, str]] = {}
    gpu_devices: dict[str, str] = {}
    tjmax_by_key: dict[str, float] = {}
    physical_disks_list: list[dict] = []
    try:
        from apexcore.infrastructure.sensors import lhm as _lhm
        storage_lhm_names = _lhm.read_lhm_storage_names() or {}
        tjmax_by_key = _lhm.read_lhm_tjmax() or {}
    except Exception:
        pass
    try:
        from apexcore.infrastructure.sensors import smartctl as _smartctl
        storage_smartctl_info = _smartctl.read_smartctl_devices_info() or {}
    except Exception:
        pass
    try:
        from apexcore.infrastructure.sensors import nvidia_ml as _nvml
        nvml_names = _nvml.read_nvml_device_names() or {}
        if nvml_names:
            first = next(iter(nvml_names.values()))
            gpu_devices["nvml"] = first
            gpu_devices["gpunvidia"] = first
    except Exception:
        pass
    try:
        from apexcore.infrastructure.disk_inventory import list_physical_disks as _list_disks
        physical_disks_list = [
            {
                "index": d.index,
                "model": d.model,
                "bus_type": d.bus_type,
                "media_type": d.media_type,
                "size_gb": d.size_gb,
                "letters": list(d.letters),
                "display_type": d.display_type,
            }
            for d in _list_disks()
        ]
    except Exception:
        pass
    sensor_service = SensorService(
        adapter=adapter,
        bus=sensor_bus,
        sampling_rate_sec=settings.sampling_rate_sec,
        gpu_devices=gpu_devices,
        tjmax_by_key=tjmax_by_key,
        storage_lhm_names=storage_lhm_names,
        storage_smartctl_info=storage_smartctl_info,
    )
    stress_ctrl = _StressController()
    bench_ctrl = _BenchController(repo=repo, baseline_repo=baseline_repo)
    # §9.4 — общая оценка системы (CPU + RAM + boot-disk, без термоконтроля).
    general_ctrl = _GeneralController(adapter=adapter, db_path=settings.db_path)
    # §9.3 — расширенный тест CPU (Single/Multi + Полный прогон scoring v2).
    micro_ctrl = _MicroController(adapter=adapter, db_path=settings.db_path)
    # §9.7 — Наследие Winsat (Windows-only, persistence в winsat_runs).
    winsat_ctrl = _WinsatController(adapter=adapter, db_path=settings.db_path)
    # §9.6 — Ram & Cache (in-memory, без БД — диагностический тест).
    ramcache_ctrl = _RamCacheController(adapter=adapter)
    # §9.5 — GPU-бенчмарк (Roofline OpenCL, persistence в gpu_benchmark_runs).
    gpu_ctrl = _GpuController(adapter=adapter, db_path=settings.db_path)
    # §9.5 — GPU-стресс (термостабильность, persistence в gpu_stress_runs).
    gpu_stress_ctrl = _GpuStressController(adapter=adapter, db_path=settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        telemetry.start(record_history=False)
        sensor_service.start()
        try:
            yield
        finally:
            sensor_service.stop()
            telemetry.stop()
            repo.close()

    app = FastAPI(title="apexcore Web UI", version="0.2.0", lifespan=lifespan)

    if STATIC_DIR.exists():
        app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = STATIC_DIR / "index.html"
        if not path.exists():
            return HTMLResponse("<h1>apexcore</h1><p>Static UI not bundled.</p>")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/system")
    async def get_system() -> dict:
        return adapter.get_system_info().model_dump(mode="json")

    @app.get("/api/hardware")
    async def get_hardware() -> dict:
        """Реальная конфигурация железа для idle-превью (диск + DRAM).

        Заменяет hardcoded mock на экранах «Общая оценка» / «Ram & Cache».
        Boot-диск — через disk_inventory (без root, обе ОС). DRAM —
        объём из psutil + тип/частота/модули через WMI (Windows) /
        dmidecode (Linux, нужен root → graceful «н/д» без прав).
        Результат DRAM кешируется (один subprocess/UAC за lifetime).
        """
        from apexcore.infrastructure import dram_info
        from apexcore.infrastructure.disk_inventory import get_boot_drive

        sys_info = adapter.get_system_info()
        # Boot-диск (реальные данные, без root на обеих ОС).
        disk_payload: dict[str, Any] | None = None
        try:
            boot_path, disk = get_boot_drive()
            if disk is not None:
                disk_payload = {
                    "model": disk.model or None,
                    "display_type": disk.display_type,
                    "bus_type": disk.bus_type or None,
                    "media_type": disk.media_type or None,
                    "size_gb": disk.size_gb,
                    "mount": boot_path,
                }
            else:
                disk_payload = {"model": None, "display_type": None, "bus_type": None,
                                "media_type": None, "size_gb": None, "mount": boot_path}
        except Exception:  # pragma: no cover
            logger.exception("hardware: boot-disk detection failed")
        # DRAM (объём всегда; тип/частота — best-effort с кешем).
        dram_payload: dict[str, Any] | None = None
        try:
            dram_payload = dram_info.get_dram_info(
                total_gb=sys_info.ram_total_gb, cpu_model=sys_info.cpu_model,
            )
        except Exception:  # pragma: no cover
            logger.exception("hardware: dram detection failed")
        return {"boot_disk": disk_payload, "dram": dram_payload}

    @app.get("/api/runs")
    async def list_runs(limit: int = 50, profile: str | None = None) -> list[dict]:
        # Битая/недоступная БД не должна ронять список 500-ой (см. /api/trend):
        # отдаём пустой список, фронт покажет «нет прогонов».
        try:
            runs = repo.list_runs(limit=limit, profile_name=profile)
        except (sqlite3.DatabaseError, RepositoryError) as exc:
            logger.warning("runs list: чтение прогонов не удалось (%s) — пустой список", exc)
            runs = []
        return [
            {
                "id": str(r.id),
                "profile_name": r.config.profile_name,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat(),
                "final_score": r.final_score,
                "status": r.status,
                "samples": len(r.metrics_history),
            }
            for r in runs
        ]

    @app.get("/api/history")
    async def history_unified(limit: int = 50) -> list[dict]:
        """Объединённая история прогонов из всех 4 таблиц.

        Возвращает список с унифицированной схемой для каждой записи:
        ``{id, type, type_label, profile_name, start_time, end_time,
            duration_sec, score, score_label, score_scale, status, samples}``.

        Тип определяется источником: stress / general / cpu_advanced /
        winsat. Для стресс-прогонов score вычисляется на лету через
        ``compute_stress_score_context`` (lazy migration: legacy runs где
        ``final_score=0`` в БД получают реальный балл если в payload есть
        ``system_info + stress_results + thermal``). Для остальных —
        напрямую из соответствующего поля.

        Списки из всех 4 таблиц мерджятся, сортируются по start_time desc,
        возвращаются первые ``limit``.
        """
        from apexcore.application.stress_score import (
            compute_stress_score_context,
        )
        from apexcore.application.parallel_runner import ParallelStressResult
        from apexcore.infrastructure.persistence import (
            SqliteGeneralBenchmarkRepository,
            SqliteGpuBenchmarkRepository,
            SqliteGpuStressRepository,
            SqliteMicroRunRepository,
            SqliteWinsatRepository,
        )

        items: list[dict] = []

        # 1. Стресс-прогоны (таблица runs).
        try:
            stress_runs = repo.list_runs(limit=limit)
        except Exception as exc:  # pragma: no cover
            logger.exception("history: stress list failed")
            stress_runs = []
        for r in stress_runs:
            score: float | None = None
            score_label = "—"
            # Lazy compute stress_score из payload (если данных хватает).
            try:
                # ParallelStressResult — простой dataclass, конструируем из results.
                parallel = ParallelStressResult(
                    started_at=0.0,
                    finished_at=0.0,
                    duration_actual_sec=(r.end_time - r.start_time).total_seconds(),
                    results=list(r.stress_results or []),
                )
                if r.thermal is not None:
                    ctx = compute_stress_score_context(
                        system_info=r.system_info,
                        parallel=parallel,
                        thermal=r.thermal,
                        duration_sec=(r.end_time - r.start_time).total_seconds(),
                    )
                    if ctx.stress_score is not None:
                        # ctx.stress_score уже в шкале ×10 000 (см. compute_stress_score
                        # в application/stress_score.py: возвращает STRESS_SCORE_SCALE × GM).
                        score = float(ctx.stress_score)
                        score_label = f"{int(round(score))}"
            except Exception as exc:  # pragma: no cover
                # Аналог F-15: silent fallback с logger.debug прятал
                # AttributeError-style регрессии в production (debug-уровень
                # обычно отключён). logger.exception пишет traceback в лог
                # — баг сразу виден.
                logger.exception("history: stress_score compute failed for %s: %s", r.id, exc)
            items.append(
                {
                    "id": str(r.id),
                    "type": "stress",
                    "type_label": "Стресс-тест",
                    "profile_name": r.config.profile_name,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat(),
                    "duration_sec": (r.end_time - r.start_time).total_seconds(),
                    "score": score,
                    "score_label": score_label,
                    "score_scale": "×10 000",
                    "status": r.status,
                    "samples": len(r.metrics_history),
                }
            )

        # 2. Общая оценка системы (таблица general_benchmark_runs).
        try:
            grepo = SqliteGeneralBenchmarkRepository(settings.db_path)
            for g in grepo.list_runs(limit=limit):
                score = float(g.score) if g.score is not None else None
                items.append(
                    {
                        "id": str(g.id),
                        "type": "general",
                        "type_label": "Общая оценка системы",
                        "profile_name": "general_benchmark",
                        "start_time": g.started_at.isoformat(),
                        "end_time": g.ended_at.isoformat(),
                        "duration_sec": (g.ended_at - g.started_at).total_seconds(),
                        "score": score,
                        "score_label": (
                            "—" if score is None else f"{int(round(score))}"
                        ),
                        "score_scale": "×10 000",
                        "status": "cancelled" if g.cancelled else "completed",
                        "samples": 0,
                    }
                )
        except Exception as exc:
            logger.exception("history: general list failed: %s", exc)

        # 3. Расш. тест CPU (таблица micro_runs).
        try:
            mrepo = SqliteMicroRunRepository(settings.db_path)
            for m in mrepo.list_runs(limit=limit):
                # MicroBenchSuiteResult имеет вложенное поле `overall`,
                # из которого берутся score / scoring_version. Доступ
                # `m.overall_score` напрямую — это AttributeError, потому
                # что в `_DB_columns` (на save) `overall_score` это столбец
                # таблицы, но не атрибут модели. Старый код тихо валился
                # в `except Exception → logger.debug` и micro_runs не
                # показывались в /api/history вообще. Исправляем доступом
                # через overall (Pydantic-сабмодель `overall`).
                # Балл в Истории показываем только для Winsat / Стресс-тест /
                # Общая оценка. Для «Расш. тест CPU» (micro) — намеренно «—»:
                # это детальный CPU-анализ, не системный балл.
                items.append(
                    {
                        "id": str(m.id),
                        "type": "cpu_advanced",
                        "type_label": "Расш. тест CPU",
                        "profile_name": m.preset or "micro",
                        "start_time": m.start_time.isoformat(),
                        "end_time": m.end_time.isoformat(),
                        "duration_sec": (m.end_time - m.start_time).total_seconds(),
                        "score": None,
                        "score_label": "—",
                        "score_scale": "×1 000",
                        "status": "completed",
                        "samples": m.n_runs,
                    }
                )
        except Exception as exc:
            # Раньше тихо logger.debug — exception не видим в production.
            # exception() пишет full traceback в server log → можно сразу
            # диагностировать (например пользователь сообщит «история
            # micro_runs пустая» — мы посмотрим лог и увидим причину).
            logger.exception("history: micro list failed: %s", exc)

        # 4. Winsat (таблица winsat_runs).
        try:
            wrepo = SqliteWinsatRepository(settings.db_path)
            for w in wrepo.list_runs(limit=limit):
                # winspr_level — итог по самому слабому звену (минимум подскоров).
                # Шкала 1.0..9.9, не ×1000. Показываем как float.
                lvl = float(w.winspr_level) if w.winspr_level is not None else None
                # 5 подскоров для tooltip'а в History: CPU / Memory / Disk /
                # Graphics / D3D. status важен — если sub-test упал, показать
                # это пользователю (FAILED / NOT_SUPPORTED_ON_OS).
                breakdown = []
                for label, sub in [
                    ("CPU", w.cpu_score),
                    ("Memory", w.memory_score),
                    ("Disk", w.disk_score),
                    ("Graphics", w.graphics_score),
                    ("Gaming D3D", w.d3d_score),
                ]:
                    if sub is None:
                        continue
                    breakdown.append({
                        "label": label,
                        "score": float(sub.score),
                        "status": str(sub.status.value if hasattr(sub.status, "value") else sub.status),
                        "metric_name": sub.metric_name,
                        "metric_value": float(sub.metric_value),
                        "metric_unit": sub.metric_unit,
                        # note содержит человекочитаемое пояснение для NA/FAILED статусов
                        # (например 'winsat dwm недоступен' если нет admin для GFX/D3D).
                        "note": sub.note,
                    })
                items.append(
                    {
                        "id": str(w.id),
                        "type": "winsat",
                        "type_label": "Наследие Winsat",
                        "profile_name": "winsat",
                        "start_time": w.started_at.isoformat(),
                        "end_time": w.ended_at.isoformat() if w.ended_at else w.started_at.isoformat(),
                        "duration_sec": (
                            (w.ended_at - w.started_at).total_seconds() if w.ended_at else 0.0
                        ),
                        "score": lvl,
                        "score_label": (
                            "—" if lvl is None else f"WinSPR {lvl:.1f}"
                        ),
                        "score_scale": "1.0–9.9",
                        "score_breakdown": breakdown,
                        "status": "completed",
                        "samples": 0,
                    }
                )
        except Exception as exc:  # pragma: no cover
            # Аналог F-15: см. комментарий выше у history: stress_score.
            logger.exception("history: winsat list failed: %s", exc)

        # 5. GPU-бенчмарк (таблица gpu_benchmark_runs). Headline — балл ×10 000
        #    (как general), считает сам оркестратор → берём напрямую.
        try:
            gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
            for g in gpu_repo.list_runs(limit=limit):
                gpu_score = float(g.score) if g.score is not None else None
                items.append(
                    {
                        "id": str(g.id),
                        "type": "gpu",
                        "type_label": "Тест GPU",
                        "profile_name": "gpu_benchmark",
                        "start_time": g.started_at.isoformat(),
                        "end_time": g.ended_at.isoformat(),
                        "duration_sec": (g.ended_at - g.started_at).total_seconds(),
                        "score": gpu_score,
                        "score_label": (
                            "—" if gpu_score is None else f"{round(gpu_score)}"
                        ),
                        "score_scale": "×10 000",
                        "status": "cancelled" if g.cancelled else "completed",
                        "samples": 0,
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("history: gpu list failed: %s", exc)

        # 6. GPU-стресс (таблица gpu_stress_runs). Headline — не балл, а вердикт
        #    PASS/WARN/FAIL/UNKNOWN. Кладём его в score_label (score=None, шкала
        #    не числовая). frontend показывает как текстовый вердикт.
        try:
            gpu_stress_repo = SqliteGpuStressRepository(settings.db_path)
            for gs in gpu_stress_repo.list_runs(limit=limit):
                verdict = gs.verdict.value if hasattr(gs.verdict, "value") else str(gs.verdict)
                items.append(
                    {
                        "id": str(gs.id),
                        "type": "gpu_stress",
                        "type_label": "GPU-стресс",
                        "profile_name": "gpu_stress",
                        "start_time": gs.started_at.isoformat(),
                        "end_time": gs.ended_at.isoformat(),
                        "duration_sec": gs.duration_sec,
                        "score": None,
                        "score_label": verdict.upper(),
                        "score_scale": "вердикт",
                        "verdict": verdict,
                        "status": "cancelled" if gs.cancelled else "completed",
                        "samples": gs.samples_taken,
                    }
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("history: gpu_stress list failed: %s", exc)

        # Сортируем мердж по start_time desc, возвращаем первые limit.
        items.sort(key=lambda x: x["start_time"], reverse=True)
        return items[:limit]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        full_id = repo.resolve_id(run_id)
        result = repo.get(full_id) if full_id is not None else None
        if result is None:
            # Не стресс-прогон (gpu / gpu_stress / general / winsat / micro) —
            # диспатчим через _lookup_run и отдаём model_dump напрямую (как
            # /api/general/runs/{id} и /api/winsat/runs/{id}). Для gpu/gpu_stress
            # это единственный unified-путь к полному отчёту из Истории.
            _kind, _rid, model = _lookup_run(settings.db_path, run_id)
            if model is None:
                raise HTTPException(status_code=404, detail="not_found")
            return model.model_dump(mode="json")
        # Lazy compute stress_score: BenchmarkService записывает legacy
        # `final_score=0.0` (см. benchmark_service.py:113), реальный балл
        # считается отдельно через `compute_stress_score_context`. CLI делает
        # это в render-time (render.py:1014-1070), а Web UI до сих пор не
        # перевычислял на /api/runs/{id} — frontend stress.js видел 0.0 и
        # показывал «—». Делаем lazy compute прямо здесь.
        #
        # Дополнительно: legacy-прогоны (старая БД, profile_name='cpu_heavy')
        # могут иметь thermal=None но богатый metrics_history. В этом случае
        # вычисляем ThermalStabilityResult на лету через
        # compute_thermal_stability(), кладём обратно в payload — фронт получает
        # frame_rate_stability_pct, temp_max_c, pass_threshold_97 даже для
        # legacy-данных.
        payload = result.model_dump(mode="json")
        try:
            from apexcore.application.thermal import compute_thermal_stability

            thermal = result.thermal
            if thermal is None and result.metrics_history:
                thermal = compute_thermal_stability(list(result.metrics_history))
                payload["thermal"] = thermal.model_dump(mode="json")

            if thermal is not None and result.stress_results:
                from apexcore.application.stress_score import (
                    compute_stress_score_context,
                )
                from apexcore.application.parallel_runner import (
                    ParallelStressResult,
                )
                duration_sec = (result.end_time - result.start_time).total_seconds()
                parallel = ParallelStressResult(
                    started_at=0.0,
                    finished_at=0.0,
                    duration_actual_sec=duration_sec,
                    results=list(result.stress_results),
                )
                ctx = compute_stress_score_context(
                    system_info=result.system_info,
                    parallel=parallel,
                    thermal=thermal,
                    duration_sec=duration_sec,
                )
                if ctx.stress_score is not None:
                    # ctx.stress_score уже в шкале × 10 000.
                    payload["final_score"] = float(ctx.stress_score)
                # duration_sec в payload пригодится фронту для «фактической
                # длительности». В Pydantic-модели этого поля нет — кладём ad-hoc.
                payload["duration_sec"] = duration_sec
        except Exception as exc:  # pragma: no cover
            # Аналог F-15: silent fallback скрывал бы AttributeError на
            # legacy-payload'ах. logger.exception оставляет traceback в логе.
            logger.exception("get_run: stress_score compute failed for %s: %s", run_id, exc)
        return payload

    # ─── §9.9 — экспорт + удаление одного прогона (любой тип) ────────────
    @app.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str, format: str = "json"):
        """Экспорт прогона в JSON / CSV / HTML-для-печати.

        Автоматически находит прогон по UUID/префиксу во всех 4 таблицах
        (stress / micro / winsat / general). Формат HTML — это автономная
        страница, пользователь сам делает Ctrl+P → «Сохранить как PDF».
        """
        fmt = (format or "json").lower()
        if fmt not in ("json", "csv", "html"):
            raise HTTPException(status_code=400, detail="format must be json|csv|html")
        kind, rid, model = _lookup_run(settings.db_path, run_id)
        if model is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        prefix = (rid or run_id)[:8]
        kind_short = kind or "run"
        filename = f"apexcore_{kind_short}_{prefix}.{fmt}"
        disposition = f'attachment; filename="{filename}"'

        if fmt == "json":
            return PlainTextResponse(
                model.model_dump_json(indent=2),
                media_type="application/json; charset=utf-8",
                headers={"Content-Disposition": disposition},
            )
        if fmt == "csv":
            try:
                csv_text = _run_to_csv(kind, model)
            except Exception as exc:
                logger.exception("CSV export failed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return PlainTextResponse(
                csv_text,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": disposition},
            )
        # html — открываем inline, чтобы пользователь сразу видел и мог Ctrl+P.
        html_text = _run_to_html(kind, model)
        return HTMLResponse(
            html_text,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str) -> dict:
        """Удалить прогон любого типа по UUID/префиксу."""
        from uuid import UUID as _UUID

        from apexcore.infrastructure.persistence import (
            SqliteGeneralBenchmarkRepository,
            SqliteGpuBenchmarkRepository,
            SqliteGpuStressRepository,
            SqliteMicroRunRepository,
            SqliteResultRepository,
            SqliteWinsatRepository,
        )

        kind, rid, model = _lookup_run(settings.db_path, run_id)
        if model is None or rid is None or kind is None:
            raise HTTPException(status_code=404, detail="run_not_found")
        repos = {
            "stress":     SqliteResultRepository,
            "micro":      SqliteMicroRunRepository,
            "winsat":     SqliteWinsatRepository,
            "general":    SqliteGeneralBenchmarkRepository,
            "gpu":        SqliteGpuBenchmarkRepository,
            "gpu_stress": SqliteGpuStressRepository,
        }
        target_repo = repos[kind](settings.db_path)
        try:
            ok = target_repo.delete(_UUID(rid))
        except Exception as exc:
            logger.exception("delete failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="not_deleted")
        return {"deleted": True, "kind": kind, "id": rid}

    @app.get("/api/export/all")
    async def export_all(format: str = "json"):
        """Экспорт всей истории (все 4 типа прогонов) в zip-архив.

        JSON: один файл с объектом ``{stress: [...], micro: [...], ...}``.
        CSV:  zip-архив, по файлу на каждый прогон (имена включают тип и id).
        """
        import io
        import zipfile

        from apexcore.infrastructure.persistence import (
            SqliteGeneralBenchmarkRepository,
            SqliteMicroRunRepository,
            SqliteResultRepository,
            SqliteWinsatRepository,
        )

        fmt = (format or "json").lower()
        if fmt not in ("json", "csv"):
            raise HTTPException(status_code=400, detail="format must be json|csv")

        repos_pairs = [
            ("stress",  SqliteResultRepository(settings.db_path)),
            ("micro",   SqliteMicroRunRepository(settings.db_path)),
            ("winsat",  SqliteWinsatRepository(settings.db_path)),
            ("general", SqliteGeneralBenchmarkRepository(settings.db_path)),
        ]

        if fmt == "json":
            # Один большой JSON-объект с разделами по типам.
            payload: dict[str, list[dict]] = {}
            for kind, r in repos_pairs:
                try:
                    items = r.list_runs(limit=1000)
                except Exception:
                    items = []
                payload[kind] = [m.model_dump(mode="json") for m in items]
            import json as _json
            blob = _json.dumps(payload, ensure_ascii=False, indent=2)
            return PlainTextResponse(
                blob,
                media_type="application/json; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="apexcore_all_runs.json"'},
            )

        # CSV: упакуем все прогоны в один zip — отдельный CSV на прогон.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for kind, r in repos_pairs:
                try:
                    items = r.list_runs(limit=1000)
                except Exception:
                    items = []
                for m in items:
                    try:
                        csv_text = _run_to_csv(kind, m)
                    except Exception:
                        continue
                    prefix = str(getattr(m, "id", "unknown"))[:8]
                    zf.writestr(f"{kind}/{prefix}.csv", csv_text)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="apexcore_all_runs.zip"'},
        )

    @app.get("/api/trend")
    async def get_trend(
        metric: str = "final_score",
        profile: str | None = None,
        last: int = 20,
        window: int = 5,
    ) -> dict:
        # Битая/недоступная БД (напр. SQLITE_CORRUPT с локально-битой btree-
        # страницей) не должна ронять Dashboard 500-ой — деградируем в пустой
        # тренд («нет данных»). Аналог устойчивости runs list.
        try:
            runs = repo.list_runs(limit=last, profile_name=profile)
        except (sqlite3.DatabaseError, RepositoryError) as exc:
            logger.warning("trend: чтение прогонов не удалось (%s) — пустой тренд", exc)
            runs = []
        series = build_run_trend(runs, metric=metric, window=window)
        return {
            "metric": series.metric,
            "values": series.values,
            "timestamps": series.timestamps,
            "rolling_mean": series.rolling_mean,
            "rolling_p95": series.rolling_p95,
            "window": series.window,
        }

    @app.get("/api/stress/list")
    async def stress_list() -> list[dict]:
        registry = build_default_registry()
        return [
            {
                "name": e.name,
                "category": e.category,
                "available": e.is_available(),
                "is_external": e.is_external,
            }
            for e in registry.all()
        ]

    @app.get("/api/stress/status")
    async def stress_status() -> dict:
        return stress_ctrl.status()

    @app.post("/api/stress/start")
    async def stress_start(req: StressStartRequest) -> dict:
        try:
            return stress_ctrl.start(req.engine, req.duration_sec, req.threads)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/stress/stop")
    async def stress_stop() -> dict:
        return stress_ctrl.stop()

    @app.get("/api/bench/profiles")
    async def bench_profiles() -> list[str]:
        return list(PROFILES.keys())

    @app.get("/api/bench/status")
    async def bench_status() -> dict:
        return bench_ctrl.status()

    @app.post("/api/bench/start")
    async def bench_start(req: BenchRunRequest) -> dict:
        try:
            return bench_ctrl.start(req)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/bench/cancel")
    async def bench_cancel() -> dict:
        """Запросить отмену текущего bench-прогона.

        Идемпотентно: при отсутствии активного прогона возвращает
        текущий status без побочных эффектов. Реальный stop происходит
        в worker-потоке на ближайшем cancel-token tick (≤ ~1-2 сек),
        итоговый `result.status` станет `"cancelled"`.
        """
        return bench_ctrl.cancel()

    # ─── /api/config ──────────────────────────────────────────────────────
    # Web UI читает текущий порт/хост/платформу/версию, чтобы:
    # 1) показать корректный URL в Settings,
    # 2) спрятать Windows-only разделы (Winsat, sensord) на Linux,
    # 3) проверить, что фронт и backend одинаковой версии.
    @app.get("/api/config")
    async def get_config() -> dict:
        s = load_menu_settings()
        return {
            "host": s.webui_host,
            "port": s.webui_port,
            "version": _apexcore_version,
            "platform": _detect_platform(),
            "platform_release": _platform.release(),
            # Текущий процесс может слушать другой порт, если был передан --port.
            # Это сравнение позволяет UI понять «мы на сохранённом или временном».
            # Точные значения runtime-листенера фронту не нужны (он же на этом порту).
        }

    # ─── /api/doctor — sensor health sweep (DESIGN_BRIEF §9.8) ────────────
    # Аналог `apexcore doctor` CLI: для каждого backend'а чтения сенсоров
    # (LHM DLL / runtime, HWiNFO/CoreTemp/AIDA64 SHM, NVML, smartctl,
    # psutil, WMI, hwmon на Linux) — статус + sensor_count + sample +
    # DegradedReason.short() с человеческой инструкцией.
    @app.get("/api/doctor")
    async def get_doctor() -> dict:
        # diagnose_sensors() — синхронная (читает sensor-источники).
        # В отдельный thread не убираем: операция короткая (~100ms),
        # вызывается из Web UI Diagnose разово, без частого polling.
        diag = diagnose_sensors()
        return {
            "platform": diag.platform,
            "has_cpu_temperature": diag.has_cpu_temperature,
            "has_gpu_temperature": diag.has_gpu_temperature,
            "cpu_temp_source": diag.cpu_temp_source,
            "gpu_temp_source": diag.gpu_temp_source,
            "driver_active": diag.driver_active,
            "backends": [
                {
                    "name": b.name,
                    "ok": b.ok,
                    "sensor_count": b.sensor_count,
                    "sample": b.sample,
                    "detail": b.detail,
                    "reason": b.reason.value if b.reason else None,
                    "reason_short": b.reason.short() if b.reason else None,
                }
                for b in diag.backends
            ],
            "advice": diag.advice,
            "degraded_reasons": [
                {"value": r.value, "short": r.short()}
                for r in diag.degraded_reasons
            ],
        }

    # ─── /api/repair-drivers — спавнит UAC-окно с apexcore repair-drivers ──
    # Только Windows; на Linux endpoint возвращает 400. Spawn не блокируется —
    # возвращаем сразу. Реальный прогресс пользователь видит в окне.
    @app.post("/api/repair-drivers")
    async def post_repair_drivers() -> dict:
        import platform as _plat
        import subprocess
        import sys as _sys

        if _plat.system().lower() != "windows":
            raise HTTPException(
                status_code=400,
                detail="repair-drivers доступен только на Windows (PawnIO/sensord — Windows-only).",
            )
        # CREATE_NEW_CONSOLE = 0x00000010, DETACHED_PROCESS = 0x00000008
        creation_flags = 0x00000010
        try:
            if getattr(_sys, "frozen", False):
                # production — frozen apexcore.exe
                cmd = [_sys.executable, "repair-drivers"]
            else:
                cmd = [_sys.executable, "-m", "apexcore", "repair-drivers"]
            subprocess.Popen(  # noqa: S603 — кастомная команда из known argv
                cmd,
                creationflags=creation_flags,
                close_fds=True,
            )
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Не удалось запустить repair-drivers: {exc}",
            ) from exc
        return {"started": True}

    @app.post("/api/config")
    async def update_config(req: ConfigUpdateRequest) -> dict:
        try:
            if req.host is not None:
                update_webui_host(req.host)
            if req.port is not None:
                update_webui_port(req.port)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        s = load_menu_settings()
        return {
            "saved": True,
            "restart_required": True,
            "host": s.webui_host,
            "port": s.webui_port,
            "note": "Перезапустите apexcore webui для применения нового порта.",
        }

    # ─── §9.4 — Общая оценка системы (general benchmark) ─────────────────
    # POST /api/general/start  — запустить (~1.5 мин); возвращает status.
    # GET  /api/general/status — текущее состояние + progress (phase).
    # GET  /api/general/runs/{id} — детальный GeneralBenchmarkReport.
    @app.post("/api/general/start")
    async def general_start() -> dict:
        try:
            return general_ctrl.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/general/status")
    async def general_status() -> dict:
        return general_ctrl.status()

    @app.get("/api/general/runs/{run_id}")
    async def general_run(run_id: str) -> dict:
        from apexcore.infrastructure.persistence.general_benchmark_repo import (
            SqliteGeneralBenchmarkRepository,
        )
        gbrepo = SqliteGeneralBenchmarkRepository(settings.db_path)
        try:
            full_id = gbrepo.resolve_id(run_id)
            if full_id is None:
                raise HTTPException(status_code=404, detail="not_found")
            report = gbrepo.get(full_id)
            if report is None:
                raise HTTPException(status_code=404, detail="not_found")
            return report.model_dump(mode="json")
        finally:
            gbrepo.close()

    # ─── §9.3 — Расш. тест CPU: Single/Multi сравнение ──────────────────
    @app.post("/api/micro/start-single-multi")
    async def micro_start_single_multi(body: dict | None = None) -> dict:
        body = body or {}
        try:
            return micro_ctrl.start_single_multi(
                bench_name=body.get("bench"),
                duration_sec=float(body.get("duration_sec", 5.0)),
                threads=int(body.get("threads", 0)),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/micro/start-full-run")
    async def micro_start_full_run(body: dict | None = None) -> dict:
        """Полный прогон микробенчмарков → итоговый балл системы.

        Body: ``{ "duration_sec"?: float, "threads"?: int, "tests"?: list[str] }``.
        Поле ``tests`` опционально — если задано, прогоняются только указанные
        движки (например, ``["aes_256", "sha1"]``). Если не задано — все 12.

        Пресет фиксирован = ``standard`` (3 прогона) — web не показывает выбор
        точности, для accurate (5 прогонов, 95% CI) пользователь идёт в CLI:
        ``apexcore micro run --preset accurate``.
        """
        body = body or {}
        tests_raw = body.get("tests")
        tests = (
            [str(t) for t in tests_raw if t]
            if isinstance(tests_raw, list) and tests_raw
            else None
        )
        try:
            return micro_ctrl.start_full_run(
                duration_sec=float(body.get("duration_sec", 5.0)),
                threads=int(body.get("threads", 0)),
                tests=tests,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/micro/status")
    async def micro_status() -> dict:
        return micro_ctrl.status()

    @app.post("/api/micro/stop")
    async def micro_stop() -> dict:
        return micro_ctrl.stop()

    # ─── §9.7 — Наследие Winsat (Windows-only) ────────────────────────────
    @app.post("/api/winsat/start")
    async def winsat_start(body: dict | None = None) -> dict:
        """Запуск Winsat-аналога. Body: ``{ "duration_sec"?: float }``.

        На Linux вернёт failed-статус (frontend сам отрисует ограничение).
        Дефолт duration_sec_per_test = 5.0 с — стандарт Winsat.
        """
        body = body or {}
        try:
            return winsat_ctrl.start(
                duration_sec=float(body.get("duration_sec", 5.0)),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/winsat/status")
    async def winsat_status() -> dict:
        return winsat_ctrl.status()

    @app.post("/api/winsat/stop")
    async def winsat_stop() -> dict:
        return winsat_ctrl.stop()

    @app.get("/api/winsat/runs")
    async def winsat_runs_list(limit: int = 20) -> list[dict]:
        """Список последних Winsat-прогонов из таблицы winsat_runs."""
        try:
            from apexcore.infrastructure.persistence.winsat_repo import (
                SqliteWinsatRepository,
            )
            wrepo = SqliteWinsatRepository(settings.db_path)
            reports = wrepo.list_runs(limit=limit)
            return [r.model_dump(mode="json") for r in reports]
        except Exception as exc:
            logger.exception("winsat_runs_list failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ─── §9.6 — Ram & Cache ───────────────────────────────────────────────
    @app.post("/api/ram-cache/start")
    async def ramcache_start(body: dict | None = None) -> dict:
        """Запуск Ram&Cache. Body:
        ``{ "duration_sec_per_metric"?: float, "tests"?: list[str] }``.

        Каноническое имя теста: ``"<level>_<operation>"``, например
        ``"l1_read"``, ``"dram_latency"``. Если ``tests`` пусто — все 16.
        """
        body = body or {}
        tests_raw = body.get("tests")
        tests = (
            [str(t) for t in tests_raw if t]
            if isinstance(tests_raw, list) and tests_raw
            else None
        )
        try:
            return ramcache_ctrl.start(
                duration_sec_per_metric=float(body.get("duration_sec_per_metric", 2.0)),
                tests=tests,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/ram-cache/status")
    async def ramcache_status() -> dict:
        return ramcache_ctrl.status()

    @app.post("/api/ram-cache/stop")
    async def ramcache_stop() -> dict:
        return ramcache_ctrl.stop()

    # ─── §9.5 — GPU-бенчмарк (Roofline OpenCL) ────────────────────────────
    @app.get("/api/gpu/devices")
    async def gpu_devices() -> dict:
        """Список GPU-устройств через дефолтный OpenCL-бэкенд + флаг доступности.

        Graceful degrade: если ICD-loader не загрузился / устройств нет —
        ``available: false`` и пустой ``devices`` (без исключения). Frontend
        сам покажет дружелюбное «OpenCL/GPU не обнаружен» и заблокирует запуск.
        """
        from apexcore.infrastructure.gpu import build_default_gpu_backend
        try:
            backend = build_default_gpu_backend()
            available = backend.is_available()
            devices = backend.list_devices() if available else []
        except Exception:
            logger.exception("gpu_devices: перечисление устройств упало")
            available = False
            devices = []
        return {
            "available": bool(available and devices),
            "devices": [d.model_dump(mode="json") for d in devices],
        }

    @app.post("/api/gpu/start")
    async def gpu_start(body: dict | None = None) -> dict:
        """Запуск GPU-бенчмарка. Body:
        ``{ "device_index"?: int, "fp32_duration_sec"?: float,
        "fp64_duration_sec"?: float, "mem_duration_sec"?: float,
        "pcie_duration_sec"?: float, "cooldown_sec"?: float }``.

        Дефолты соответствуют :class:`GpuBenchmarkParams`. При отсутствии GPU
        прогон всё равно стартует и завершится graceful-отчётом (score=None).
        """
        body = body or {}
        try:
            return gpu_ctrl.start(
                device_index=int(body.get("device_index", 0)),
                fp32_duration_sec=float(body.get("fp32_duration_sec", 5.0)),
                fp64_duration_sec=float(body.get("fp64_duration_sec", 5.0)),
                mem_duration_sec=float(body.get("mem_duration_sec", 5.0)),
                pcie_duration_sec=float(body.get("pcie_duration_sec", 2.0)),
                cooldown_sec=float(body.get("cooldown_sec", 2.0)),
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/gpu/status")
    async def gpu_status() -> dict:
        return gpu_ctrl.status()

    @app.post("/api/gpu/stop")
    async def gpu_stop() -> dict:
        return gpu_ctrl.stop()

    # ─── §9.5 — GPU-стресс-тест (термостабильность, вердикт PASS/WARN/FAIL) ──
    @app.post("/api/gpu/stress/start")
    async def gpu_stress_start(body: dict | None = None) -> dict:
        """Запуск GPU-стресс-теста. Body:
        ``{ "device_index"?: int, "duration_sec"?: float }``.

        Дефолт длительности — 60 с (см. GpuStressOrchestrator.run). При
        отсутствии GPU прогон всё равно стартует и завершится graceful-отчётом
        (verdict=unknown), а не ошибкой.
        """
        body = body or {}
        try:
            return gpu_stress_ctrl.start(
                device_index=int(body.get("device_index", 0)),
                duration_sec=float(body.get("duration_sec", 60.0)),
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/gpu/stress/status")
    async def gpu_stress_status() -> dict:
        return gpu_stress_ctrl.status()

    @app.post("/api/gpu/stress/stop")
    async def gpu_stress_stop() -> dict:
        return gpu_stress_ctrl.stop()

    @app.get("/api/winsat/runs/{run_id}")
    async def winsat_run_one(run_id: str) -> dict:
        """Полный Winsat-прогон по UUID."""
        try:
            from apexcore.infrastructure.persistence.winsat_repo import (
                SqliteWinsatRepository,
            )
            wrepo = SqliteWinsatRepository(settings.db_path)
            real_id = wrepo.resolve_id(run_id) or run_id
            report = wrepo.get(real_id)
            if report is None:
                raise HTTPException(status_code=404, detail="winsat_run_not_found")
            return report.model_dump(mode="json")
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("winsat_run_one failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ─── §9.2 — Sensors live: snapshot + WebSocket stream ────────────────
    # SensorSnapshot — структурированный snapshot всех датчиков с группировкой
    # по device (CPU/GPU/Memory/MB/Fans/Storage), badge источника
    # (HWiNFO/LHM/CoreTemp/AIDA64/NVML/smartctl/psutil) и ThrottleState.
    # См. PROJECT_CONTEXT.md §10 для контракта данных.
    @app.get("/api/sensors/snapshot")
    async def get_sensors_snapshot() -> dict:
        snap = sensor_service.latest()
        if snap is None:
            # Семплер ещё не успел собрать первый snapshot — делаем синхронный.
            snap = sensor_service.make_snapshot()
        if snap is None:
            raise HTTPException(status_code=503, detail="sensor_snapshot_unavailable")
        payload = snap.model_dump(mode="json")
        # Обогащаем ответ списком физических дисков (с буквами C:/D:/...) —
        # это статичная инвентаризация, frontend matchит её с storage-readings
        # по model substring. Снято один раз при старте процесса.
        payload["storage_devices"] = physical_disks_list
        return payload

    @app.websocket("/ws/sensors")
    async def ws_sensors(ws: WebSocket) -> None:
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SensorSnapshot] = asyncio.Queue(maxsize=128)

        def _on_sensor(snap: SensorSnapshot) -> None:
            with contextlib.suppress(asyncio.QueueFull, RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, snap)

        unsubscribe = sensor_bus.subscribe(_on_sensor)
        try:
            # Сразу пушим текущий latest, чтобы UI не ждал rate_sec.
            current = sensor_service.latest()
            if current is not None:
                await ws.send_json(current.model_dump(mode="json"))
            while True:
                snap = await queue.get()
                await ws.send_json(snap.model_dump(mode="json"))
        except WebSocketDisconnect:
            return
        finally:
            unsubscribe()

    @app.websocket("/ws/metrics")
    async def ws_metrics(ws: WebSocket) -> None:
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[MetricSnapshot] = asyncio.Queue(maxsize=128)

        def _on_metric(snap: MetricSnapshot) -> None:
            with contextlib.suppress(asyncio.QueueFull, RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, snap)

        unsubscribe = bus.subscribe(_on_metric)
        try:
            # Сразу пушим последний снимок, чтобы топбар/дашборд не ждали
            # первый тик семплера (rate_sec) — симметрично /ws/sensors.
            current = telemetry.latest()
            if current is not None:
                await ws.send_json(current.model_dump(mode="json"))
            while True:
                snap = await queue.get()
                await ws.send_json(snap.model_dump(mode="json"))
        except WebSocketDisconnect:
            return
        finally:
            unsubscribe()

    # ─── First-run Setup wizard ─────────────────────────────────────────────
    # Подключаем /setup, /setup/*, /ws/setup, /api/setup/* — общий с Windows
    # WebView2 bootstrapper HTML/CSS/JS в static/setup/. См. setup_router.py.
    try:
        from apexcore.interfaces.webui.setup_router import make_setup_router
        app.include_router(make_setup_router())
    except Exception:  # pragma: no cover
        logger.exception("Не удалось подключить /setup router — wizard будет недоступен")

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Поднять uvicorn-сервер. Точка вызова из CLI ``apexcore webui``."""
    import uvicorn  # type: ignore

    uvicorn.run(
        "apexcore.interfaces.webui.server:create_app",
        host=host,
        port=port,
        factory=True,
        reload=reload,
        log_level="info",
    )
