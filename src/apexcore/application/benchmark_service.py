"""Оркестрация стресс-прогона (для теста стабильности).

В scoring v2 этот сервис **не отвечает** за общую оценку производительности.
Общая оценка вычисляется в ``ScoringService`` на основании микробенчмарков
(см. ``application/scoring_service.py``). Здесь — только запуск стресс-
движков для длительных прогонов (например, 10-минутный тест стабильности
с телеметрией).

Поток выполнения:
1. Запросить у адаптера `SystemInfo`.
2. Запустить TelemetryService (фоновый сэмплер метрик).
3. Для каждого выбранного профилем стресс-движка:
   - запустить движок на ``config.duration_sec`` секунд;
   - собрать `StressResult`.
4. Остановить телеметрию, забрать историю снимков.
5. Сохранить `BenchmarkResult` в репозитории.

Поле ``BenchmarkResult.final_score`` всегда равно 0.0 — это устаревшее
поле scoring v1, оставлено для обратной совместимости JSON.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from apexcore.application.telemetry_service import (
    InMemoryMetricsBus,
    TelemetryService,
)
from apexcore.domain.errors import StressEngineUnavailableError
from apexcore.domain.models import BenchmarkConfig, BenchmarkResult, StressResult
from apexcore.domain.ports import (
    BaselineRepository,
    OSAdapter,
    ResultRepository,
)
from apexcore.infrastructure.stress.registry import StressRegistry, profile_engines

logger = logging.getLogger(__name__)


class BenchmarkService:
    """Прикладной сервис: запускает полный прогон бенчмарка."""

    def __init__(
        self,
        adapter: OSAdapter,
        registry: StressRegistry,
        repo: ResultRepository,
        baseline_repo: BaselineRepository | None = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._repo = repo
        self._baseline_repo = baseline_repo

    def run(
        self,
        config: BenchmarkConfig,
        cancel_token: threading.Event | None = None,
    ) -> BenchmarkResult:
        sys_info = self._adapter.get_system_info()
        bus = InMemoryMetricsBus()
        telemetry = TelemetryService(
            adapter=self._adapter,
            bus=bus,
            sampling_rate_sec=config.sampling_rate_sec,
        )

        engines = self._select_engines(config)
        if not engines:
            raise StressEngineUnavailableError(
                f"Для профиля '{config.profile_name}' не найдено доступных стресс-движков"
            )

        start = datetime.now(timezone.utc)
        telemetry.start()
        stress_results: list[StressResult] = []
        cancelled = False
        try:
            for engine in engines:
                if cancel_token is not None and cancel_token.is_set():
                    cancelled = True
                    break
                logger.info(
                    "Запуск стресс-движка %s (%s) на %.1f с",
                    engine.name,
                    engine.category,
                    config.duration_sec,
                )
                res = engine.run(
                    duration_sec=config.duration_sec,
                    threads=config.threads,
                    cancel_token=cancel_token,
                )
                stress_results.append(res)
                if cancel_token is not None and cancel_token.is_set():
                    cancelled = True
                    break
        finally:
            metrics_history = telemetry.stop()
        end = datetime.now(timezone.utc)

        result = BenchmarkResult(
            system_info=sys_info,
            config=config,
            start_time=start,
            end_time=end,
            metrics_history=metrics_history,
            stress_results=stress_results,
            final_score=0.0,  # scoring v1 deprecated; реальный балл в MicroBenchSuiteResult.overall
            status="cancelled" if cancelled else "completed",
        )

        # ВНИМАНИЕ: в scoring v2 этот сервис не вычисляет общий балл — он
        # отвечает только за выполнение стресс-фаз и сбор телеметрии.
        # Общая оценка системы = ScoringService на микробенчмарках.
        # Тест стабильности 10 минут = StabilityService на этом же сервисе
        # + thermal.compute_thermal_stability().

        self._repo.save(result)
        return result

    def _select_engines(self, config: BenchmarkConfig) -> list:
        if config.engines:
            chosen = []
            for name in config.engines:
                e = self._registry.get(name)
                if e is None or not e.is_available():
                    continue
                chosen.append(e)
            return chosen
        return profile_engines(config.profile_name, self._registry)
