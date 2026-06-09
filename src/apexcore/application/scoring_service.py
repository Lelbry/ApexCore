"""ScoringService — оркестратор общей оценки производительности (scoring v2).

Сводит вместе:
- ``infrastructure/microbench/`` — 12 микробенчмарков (источник данных).
- ``application/roofline.py`` + ``references.py`` — Roofline-эталоны.
- ``application/weights.py`` — профили весов.
- ``application/scoring.py`` + ``multi_run.py`` — расчёт балла и CI.
- ``infrastructure/persistence/SqliteMicroRunRepository`` — сохранение.

Использование (из CLI/меню):

    service = ScoringService(adapter, registry, repo)
    result = service.run_overall(preset="standard", cancel_token=token)
    # result.overall.overall_score == 1234.5

Сервис не зависит от UI и может быть вызван из CLI, меню, web-API.
Прогон progress-bar'а — на стороне UI (см. cli/menu/runners.py).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from apexcore.application import multi_run
from apexcore.application.references import build_reference
from apexcore.application.weights import WeightsProfile, load_weights
from apexcore.domain.models import MicroBenchSuiteResult, SystemInfo
from apexcore.domain.ports import MicroRunRepository, OSAdapter
from apexcore.infrastructure.microbench import build_default_microbench_registry

if TYPE_CHECKING:
    from apexcore.infrastructure.microbench.base import MicroBench

logger = logging.getLogger(__name__)


# Тип callback для progress-bar и т.п. UI: вызывается перед каждым прогоном
# (run_idx начиная с 1, total = n_runs из пресета).
ProgressCallback = Callable[[int, int], None]

# Тип callback для запуска одного прогона микробенчмарков. Подаётся снаружи,
# чтобы не тащить UI-зависимости (rich progress) внутрь сервиса.
SuiteRunner = Callable[
    [list["MicroBench"], float, int, SystemInfo, threading.Event | None],
    MicroBenchSuiteResult,
]


class ScoringService:
    """Сервис общей оценки производительности на микробенчмарках."""

    def __init__(
        self,
        adapter: OSAdapter,
        repo: MicroRunRepository | None = None,
        suite_runner: SuiteRunner | None = None,
        weights: WeightsProfile | None = None,
    ) -> None:
        self._adapter = adapter
        self._repo = repo
        self._suite_runner = suite_runner
        self._weights = weights or load_weights("default")

    def run_overall(
        self,
        preset: multi_run.Preset = "standard",
        duration_sec: float = 5.0,
        threads: int = 0,
        cancel_token: threading.Event | None = None,
        progress: ProgressCallback | None = None,
        save: bool = True,
        selected_workloads: list[str] | None = None,
    ) -> MicroBenchSuiteResult:
        """Прогнать общую оценку и вернуть агрегированный результат.

        Параметры:
        - ``preset``: fast (n=1) / standard (n=3) / accurate (n=5).
        - ``duration_sec``: длительность одного теста в секундах.
        - ``threads``: 0 = auto, иначе явное число потоков.
        - ``cancel_token``: для прерывания через Ctrl+C.
        - ``progress``: optional callback(run_idx, total).
        - ``save``: True = сохранить в repo (если он передан).
        - ``selected_workloads``: имена тестов или None для всех 12.

        Возвращает агрегированный ``MicroBenchSuiteResult`` с заполненным
        ``overall``, ``preset``, ``n_runs`` и (для accurate) CI.
        """
        if self._suite_runner is None:
            raise RuntimeError(
                "ScoringService.run_overall: suite_runner не задан в конструкторе"
            )

        sys_info = self._adapter.get_system_info()
        reference = build_reference(sys_info)
        n_runs = multi_run.PRESET_RUNS[preset]

        # Подбираем тесты.
        all_tests = build_default_microbench_registry()
        if selected_workloads:
            wanted = set(selected_workloads)
            tests = [t for t in all_tests if t.name in wanted]
            if not tests:
                raise ValueError(
                    f"Ни один из selected_workloads не найден в реестре: {selected_workloads}"
                )
        else:
            tests = list(all_tests)

        # Прогоняем N раз.
        per_run_suites: list[MicroBenchSuiteResult] = []
        for run_idx in range(1, n_runs + 1):
            if cancel_token is not None and cancel_token.is_set():
                logger.info("Прогон отменён до завершения (после %d/%d)", run_idx - 1, n_runs)
                break
            if progress is not None:
                progress(run_idx, n_runs)
            logger.info("Прогон %d/%d общей оценки (preset=%s)", run_idx, n_runs, preset)
            suite = self._suite_runner(tests, duration_sec, threads, sys_info, cancel_token)
            per_run_suites.append(suite)

        if not per_run_suites:
            # Полная отмена сразу — возвращаем «пустой» suite без overall.
            return self._empty_suite(sys_info, preset)

        # Агрегируем.
        aggregated = multi_run.aggregate_multi_run(
            per_run_suites=per_run_suites,
            reference=reference,
            weights=self._weights,
            preset=preset,
        )

        # Сохраняем.
        if save and self._repo is not None:
            try:
                self._repo.save(aggregated)
            except Exception as exc:
                logger.warning("Не удалось сохранить micro-прогон: %s", exc)

        return aggregated

    @staticmethod
    def _empty_suite(sys_info: SystemInfo, preset: multi_run.Preset) -> MicroBenchSuiteResult:
        """Заглушка для случая полной отмены до первого прогона."""
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc)
        return MicroBenchSuiteResult(
            system_info=sys_info,
            results=[],
            start_time=ts,
            end_time=ts,
            duration_sec_per_test=0.0,
            preset=preset,
            n_runs=0,
        )


__all__ = ["ProgressCallback", "ScoringService", "SuiteRunner"]
