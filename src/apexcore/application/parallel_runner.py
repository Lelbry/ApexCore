"""Параллельный запуск нескольких стресс-движков на разных пулах потоков.

Существующая ``StabilityService`` запускает движки последовательно
(CPU → RAM → ...), что противоречит §4.1 отчёта: индустриальная практика
(AIDA64 System Stability Test, Prime95 Blend, ``stress-ng --class cpu
--class memory --parallel``) загружает компоненты одновременно.

``ParallelStressRunner`` решает задачу: каждому движку даётся свой
``threading.Thread``, общий ``cancel_token`` (один на всех — чтобы
ThermalWatchdog мог разом остановить всё) и одинаковая ``duration``.
По завершении каждого движка результат собирается; финальный список
``StressResult`` сохраняет порядок плана.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.shared.timing import now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineSpec:
    """Описание одной нагрузки в параллельном плане."""

    engine: StressEngine
    threads: int | None = None
    label: str | None = None  # для UI (например, «CPU-FP», «RAM-stress»)

    @property
    def display_name(self) -> str:
        return self.label or self.engine.name


@dataclass
class EngineProgress:
    """Промежуточный статус движка для UI."""

    spec_index: int
    engine_name: str
    state: str  # "starting" | "running" | "done" | "cancelled" | "error"
    started_at: float | None = None
    finished_at: float | None = None
    result: StressResult | None = None
    error: str | None = None


@dataclass
class ParallelStressResult:
    """Итог параллельного прогона."""

    started_at: float
    finished_at: float
    duration_actual_sec: float
    results: list[StressResult] = field(default_factory=list)
    cancelled: bool = False
    errors: dict[int, str] = field(default_factory=dict)  # spec_index → ошибка


ProgressCallback = Callable[[EngineProgress], None]


class ParallelStressRunner:
    """Запустить N движков одновременно на duration_sec секунд.

    Использование:
        runner = ParallelStressRunner()
        result = runner.run(
            plan=[EngineSpec(eng_a), EngineSpec(eng_b)],
            duration_sec=600,
            cancel_token=token,
            on_progress=callback,
        )

    Контракт:
        - все движки получают один и тот же ``cancel_token`` —
          сработавший watchdog останавливает разом все потоки;
        - ошибка одного движка не валит остальные (фиксируется в
          ``result.errors[idx]``);
        - финальный ``results`` всегда той же длины, что и ``plan`` —
          для упавших движков ставится ``StressResult`` со звёздочкой
          (throughput=0.0, error_count=1).
    """

    def run(
        self,
        plan: list[EngineSpec],
        duration_sec: float,
        *,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ParallelStressResult:
        if not plan:
            return ParallelStressResult(
                started_at=now(),
                finished_at=now(),
                duration_actual_sec=0.0,
            )
        token = cancel_token if cancel_token is not None else threading.Event()
        results_slot: list[StressResult | None] = [None] * len(plan)
        errors: dict[int, str] = {}

        def worker(idx: int, spec: EngineSpec) -> None:
            self._emit(on_progress, EngineProgress(
                spec_index=idx,
                engine_name=spec.engine.name,
                state="starting",
            ))
            t0 = now()
            try:
                if not spec.engine.is_available():
                    raise RuntimeError(f"движок {spec.engine.name} недоступен в этой среде")
                self._emit(on_progress, EngineProgress(
                    spec_index=idx,
                    engine_name=spec.engine.name,
                    state="running",
                    started_at=t0,
                ))
                res = spec.engine.run(
                    duration_sec=duration_sec,
                    threads=spec.threads,
                    cancel_token=token,
                )
                results_slot[idx] = res
                t1 = now()
                state = "cancelled" if token.is_set() else "done"
                self._emit(on_progress, EngineProgress(
                    spec_index=idx,
                    engine_name=spec.engine.name,
                    state=state,
                    started_at=t0,
                    finished_at=t1,
                    result=res,
                ))
            except Exception as exc:
                t1 = now()
                err_text = f"{type(exc).__name__}: {exc}"
                errors[idx] = err_text
                results_slot[idx] = StressResult(
                    engine=spec.engine.name,
                    category=spec.engine.category,
                    duration_actual_sec=t1 - t0,
                    throughput=0.0,
                    throughput_unit="",
                    threads=spec.threads or 0,
                    error_count=1,
                    raw_output=err_text,
                    extra={"failed": True},
                )
                self._emit(on_progress, EngineProgress(
                    spec_index=idx,
                    engine_name=spec.engine.name,
                    state="error",
                    started_at=t0,
                    finished_at=t1,
                    error=err_text,
                ))
                logger.exception(
                    "Engine %s упал в parallel runner", spec.engine.name
                )

        started = now()
        threads = [
            threading.Thread(
                target=worker,
                args=(idx, spec),
                name=f"stress-{spec.engine.name}",
                daemon=True,
            )
            for idx, spec in enumerate(plan)
        ]
        for t in threads:
            t.start()
        # Ждём с запасом: каждому движку даётся duration + 30 с финализации.
        # join без timeout мог бы зависнуть на крашнувшемся subprocess,
        # поэтому крутим цикл с проверкой и cancel при превышении лимита.
        join_deadline = started + duration_sec + 30.0
        for t in threads:
            remaining = max(0.5, join_deadline - now())
            t.join(timeout=remaining)
            if t.is_alive():
                # Hard-stop: что-то застряло, выставляем cancel и ждём ещё чуть.
                token.set()
                t.join(timeout=5.0)
        finished = now()
        return ParallelStressResult(
            started_at=started,
            finished_at=finished,
            duration_actual_sec=finished - started,
            results=[r if r is not None else StressResult(
                engine=plan[i].engine.name,
                category=plan[i].engine.category,
                duration_actual_sec=0.0,
                throughput=0.0,
                throughput_unit="",
                threads=0,
                error_count=1,
                raw_output="join timeout без результата",
                extra={"failed": True},
            ) for i, r in enumerate(results_slot)],
            cancelled=token.is_set(),
            errors=errors,
        )

    @staticmethod
    def _emit(cb: ProgressCallback | None, ev: EngineProgress) -> None:
        if cb is None:
            return
        try:
            cb(ev)
        except Exception:
            logger.debug("Progress callback упал, продолжаем", exc_info=True)


__all__ = [
    "EngineProgress",
    "EngineSpec",
    "ParallelStressResult",
    "ParallelStressRunner",
    "ProgressCallback",
]
