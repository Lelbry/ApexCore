"""Thermal stability — отдельная метрика стабильности под нагрузкой.

Спецификация: ``docs/scoring_v2.md`` §7.

В scoring v2 thermal stability **вынесена из общего балла** (была в v1 с
весом 0.05) и считается как самостоятельная pass/fail-метрика по образцу
UL 3DMark Stress Test:

    Frame Rate Stability % = 100 · min(cpu_avg) / max(cpu_avg)
    pass = (Frame Rate Stability ≥ 97%)

Опционально: TSC (Thermal Sensitivity Coefficient) = (S_cold - S_steady) / S_cold —
для случаев когда есть raw scores в начале и в конце прогона. Если их нет
(один прогон без time-window) — TSC = None.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable

from apexcore.domain.models import MetricSnapshot, ThermalStabilityResult

PASS_THRESHOLD_PERCENT = 97.0


def _extract_clocks(snapshots: Iterable[MetricSnapshot]) -> list[float]:
    """Получить cpu_avg частоты из snapshots, отбрасывая 0/None."""
    out: list[float] = []
    for snap in snapshots:
        avg = snap.frequencies.get("cpu_avg")
        if avg is not None and avg > 0:
            out.append(float(avg))
    return out


def _extract_temps_max(snapshots: Iterable[MetricSnapshot]) -> list[float]:
    """Получить max(temperatures) для каждого snapshot, у которого есть температуры."""
    out: list[float] = []
    for snap in snapshots:
        if snap.temperatures:
            out.append(max(snap.temperatures.values()))
    return out


def compute_thermal_stability(
    metrics_history: list[MetricSnapshot],
) -> ThermalStabilityResult:
    """Вычислить ThermalStabilityResult по истории телеметрии.

    Алгоритм:
    1. Из всех snapshots извлечь cpu_avg → min, max → frame_rate_stability_pct.
    2. Из всех snapshots с температурами → max и avg температуры.
    3. throttle_observed = есть ли хоть один snapshot с cpu_throttled=True.

    Если данных нет (пустая история, нет cpu_avg, нет температур) — соответствующие
    поля = None. Сама структура всегда возвращается (не None).
    """
    if not metrics_history:
        return ThermalStabilityResult()

    clocks = _extract_clocks(metrics_history)
    if clocks:
        clock_min = min(clocks)
        clock_max = max(clocks)
        stability_pct = (
            100.0 * clock_min / clock_max if clock_max > 0 else None
        )
    else:
        clock_min = None
        clock_max = None
        stability_pct = None

    temps = _extract_temps_max(metrics_history)
    temp_max = max(temps) if temps else None
    temp_avg = statistics.fmean(temps) if temps else None

    throttle = any(s.cpu_throttled for s in metrics_history)

    return ThermalStabilityResult(
        frame_rate_stability_pct=stability_pct,
        pass_threshold_97=(
            stability_pct >= PASS_THRESHOLD_PERCENT if stability_pct is not None else None
        ),
        tsc=None,  # требует throughput-by-time, см. spec §7
        clock_min_mhz=clock_min,
        clock_max_mhz=clock_max,
        temp_max_c=temp_max,
        temp_avg_c=temp_avg,
        throttle_observed=throttle,
        samples=len(metrics_history),
    )


def compute_tsc(score_cold: float | None, score_steady: float | None) -> float | None:
    """Thermal Sensitivity Coefficient = (S_cold − S_steady) / S_cold.

    Применяется когда у нас есть два балла: «холодный» (первый прогон) и
    «устойчивый» (после ≥10 минут). Возвращает None если:
    - score_cold None или ≤ 0,
    - score_steady None.
    """
    if score_cold is None or score_cold <= 0:
        return None
    if score_steady is None:
        return None
    return (score_cold - score_steady) / score_cold


__all__ = [
    "PASS_THRESHOLD_PERCENT",
    "compute_thermal_stability",
    "compute_tsc",
]
