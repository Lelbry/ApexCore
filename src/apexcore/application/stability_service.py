"""StabilityService — оркестратор 10-минутного теста стабильности.

В отличие от ScoringService (общая оценка через micro-тесты), здесь:
- запускаются stress-движки на длительный период (default 10 минут);
- параллельно собирается полная история телеметрии (CPU/RAM/temp);
- по окончании вычисляется ThermalStabilityResult (Frame Rate Stability %,
  pass ≥ 97%, max temp, throttle observed) — см. docs/scoring_v2.md §7.

Сервис не сохраняет результат в специализированный repo (хранилище для
stability-runs не выделяется в v2.0; результат можно вернуть UI и/или
сохранить в общий ResultRepository как обычный BenchmarkResult).

UI (live телеметрия в живой таблице) — на стороне CLI/menu, см. видение
пользователя в плане. Этот сервис только запускает стресс и собирает
данные; визуализация — снаружи через подписку на InMemoryMetricsBus.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from apexcore.application.telemetry_service import (
    InMemoryMetricsBus,
    TelemetryService,
)
from apexcore.application.thermal import compute_thermal_stability
from apexcore.domain.errors import StressEngineUnavailableError
from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    StressResult,
    ThermalStabilityResult,
)
from apexcore.domain.ports import MetricsBus, OSAdapter, ResultRepository
from apexcore.infrastructure.stress.registry import StressRegistry, profile_engines

logger = logging.getLogger(__name__)


DEFAULT_STABILITY_DURATION_SEC = 600.0  # 10 минут (см. видение пользователя)
DEFAULT_PROFILE = "balanced"            # параллельный CPU+RAM stress


class StabilityService:
    """Сервис теста стабильности под нагрузкой."""

    def __init__(
        self,
        adapter: OSAdapter,
        registry: StressRegistry,
        repo: ResultRepository | None = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._repo = repo

    def run_stability(
        self,
        duration_sec: float = DEFAULT_STABILITY_DURATION_SEC,
        profile_name: str = DEFAULT_PROFILE,
        sampling_rate_sec: float = 0.5,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
        bus: MetricsBus | None = None,
        save: bool = False,
    ) -> tuple[BenchmarkResult, ThermalStabilityResult]:
        """Прогнать stability test и вернуть (result, thermal_metrics).

        Параметры:
        - ``duration_sec``: общая длительность стресса (default 10 минут).
          Каждый stress-движок профиля запускается последовательно
          в течение этого времени; для параллельного режима надо переписать
          BenchmarkService (см. issue #8).
        - ``profile_name``: профиль из stress/registry.PROFILES.
        - ``sampling_rate_sec``: интервал телеметрии.
        - ``bus``: если передан — клиент UI может подписаться на live updates
          (rich Live + Table добавляющая строки каждые sampling_rate_sec).
        - ``save``: сохранить ли BenchmarkResult в repo.
        """
        sys_info = self._adapter.get_system_info()
        bus = bus or InMemoryMetricsBus()
        telemetry = TelemetryService(
            adapter=self._adapter,
            bus=bus,
            sampling_rate_sec=sampling_rate_sec,
        )

        engines = profile_engines(profile_name, self._registry)
        if not engines:
            raise StressEngineUnavailableError(
                f"Профиль '{profile_name}' не имеет доступных движков для теста стабильности"
            )

        # Длительность каждой фазы — равномерно делим duration на число движков.
        # Это не идеально (хотелось бы параллель), но соответствует архитектуре
        # benchmark_service. В будущем (issue #8 в репозитории) нужно сделать
        # параллельный режим.
        per_engine_sec = duration_sec / max(len(engines), 1)

        config = BenchmarkConfig(
            profile_name=profile_name,
            duration_sec=per_engine_sec,
            sampling_rate_sec=sampling_rate_sec,
            threads=threads,
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
                    "[Stability] запуск %s на %.1f с (профиль %s)",
                    engine.name, per_engine_sec, profile_name,
                )
                res = engine.run(
                    duration_sec=per_engine_sec,
                    threads=threads,
                    cancel_token=cancel_token,
                )
                stress_results.append(res)
                if cancel_token is not None and cancel_token.is_set():
                    cancelled = True
                    break
        finally:
            metrics_history = telemetry.stop()
        end = datetime.now(timezone.utc)

        # Считаем thermal_stability на собранной истории.
        thermal_result = compute_thermal_stability(metrics_history)

        result = BenchmarkResult(
            system_info=sys_info,
            config=config,
            start_time=start,
            end_time=end,
            metrics_history=metrics_history,
            stress_results=stress_results,
            final_score=0.0,  # legacy
            status="cancelled" if cancelled else "completed",
            thermal=thermal_result,
        )

        if save and self._repo is not None:
            try:
                self._repo.save(result)
            except Exception as exc:
                logger.warning("Stability run save failed: %s", exc)

        return result, thermal_result


__all__ = [
    "DEFAULT_PROFILE",
    "DEFAULT_STABILITY_DURATION_SEC",
    "StabilityService",
]
