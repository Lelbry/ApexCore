"""Множественные прогоны и доверительные интервалы для scoring v2.

Спецификация: ``new-app/docs/scoring_v2.md`` §6.

Три пресета точности:

- **fast** (n=1): один прогон, point estimate, без CI.
- **standard** (n=3): median-of-3 на per-workload throughput (как SPEC CPU 2017
  Run Rules), без CI.
- **accurate** (n≥5): mean (или trimmed mean при n≥10) с 95% CI на лог-шкале
  по t-распределению (Lilja 2000); bootstrap (Kalibera-Jones 2012) при
  выраженной асимметрии.

Все функции чистые — принимают список ``MicroBenchSuiteResult`` и возвращают
агрегированный результат + CI.
"""

from __future__ import annotations

import math
import statistics
from typing import Literal

from apexcore.application import scoring
from apexcore.application.references import ReferenceSet
from apexcore.application.weights import WeightsProfile
from apexcore.domain.models import (
    MicroBenchResult,
    MicroBenchSuiteResult,
)

Preset = Literal["fast", "standard", "accurate"]

PRESET_RUNS: dict[Preset, int] = {
    "fast": 1,
    "standard": 3,
    "accurate": 5,
}


# ─── Константы для t-распределения ──────────────────────────────────────────

# Двусторонние критические значения t для α=0.05 (95% CI), степени свободы df.
# Источник: стандартные таблицы Student t. Для n≥30 ≈ 1.96 (нормальное).
_T_TABLE_95: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def _t_critical_95(df: int) -> float:
    """Критическое значение t для 95% CI и df степеней свободы."""
    if df <= 0:
        return float("inf")
    if df >= 30:
        return 1.96  # нормальное приближение
    return _T_TABLE_95.get(df, 1.96)


# ─── Trimmed mean ────────────────────────────────────────────────────────────


def trimmed_mean(values: list[float], trim_frac: float = 0.1) -> float:
    """Усечённое среднее: отбрасывает ⌊n·trim⌋ наибольших и наименьших.

    Применяется для time-метрик при n≥10 для устойчивости к outliers.
    Для меньших выборок trim=0 (просто среднее).
    """
    if not values:
        raise ValueError("trimmed_mean: empty input")
    n = len(values)
    if n < 10 or trim_frac <= 0:
        return statistics.fmean(values)
    k = int(n * trim_frac)
    if k == 0:
        return statistics.fmean(values)
    sorted_v = sorted(values)
    trimmed = sorted_v[k : n - k]
    return statistics.fmean(trimmed)


# ─── Median-of-N (для standard пресета) ──────────────────────────────────────


def median_per_workload(suites: list[MicroBenchSuiteResult]) -> dict[str, float]:
    """Для каждого workload_id берёт медиану его value по всем прогонам.

    Используется в standard-пресете (median-of-3 как SPEC CPU 2017 Run Rules).
    Workload, у которого хотя бы в одном прогоне был error — игнорируется
    в этом прогоне (но если в других прогонах он валиден — медиана берётся
    по валидным).
    """
    by_name: dict[str, list[float]] = {}
    for suite in suites:
        for res in suite.results:
            if res.error or res.value <= 0:
                continue
            by_name.setdefault(res.name, []).append(res.value)
    return {name: statistics.median(values) for name, values in by_name.items() if values}


def mean_per_workload(
    suites: list[MicroBenchSuiteResult],
    use_trimmed: bool = False,
) -> dict[str, float]:
    """Среднее (или trimmed) по workload-у."""
    by_name: dict[str, list[float]] = {}
    for suite in suites:
        for res in suite.results:
            if res.error or res.value <= 0:
                continue
            by_name.setdefault(res.name, []).append(res.value)
    if use_trimmed:
        return {name: trimmed_mean(values) for name, values in by_name.items() if values}
    return {name: statistics.fmean(values) for name, values in by_name.items() if values}


def _aggregate_suite(
    suites: list[MicroBenchSuiteResult],
    aggregator: dict[str, float],
) -> MicroBenchSuiteResult:
    """Создать «синтетический» MicroBenchSuiteResult с агрегированными значениями.

    Берёт первый прогон как шаблон, заменяет в нём value на агрегированные
    значения по imя workload-а. Метаданные (system_info, start/end) — из первого
    прогона; для end_time можно было бы взять последний, но мы оставляем
    оригинальную семантику суперсета.
    """
    if not suites:
        raise ValueError("_aggregate_suite: empty input")
    template = suites[0]
    new_results: list[MicroBenchResult] = []
    for res in template.results:
        agg_value = aggregator.get(res.name)
        if agg_value is None:
            # workload отсутствует в агрегаторе (везде упал) — оставляем как был
            # с пометкой, что не агрегирован.
            new_results.append(res.model_copy(update={"value": 0.0, "error": "all_runs_failed"}))
        else:
            new_results.append(res.model_copy(update={"value": agg_value, "error": None}))
    last_end = max(s.end_time for s in suites)
    return MicroBenchSuiteResult(
        system_info=template.system_info,
        results=new_results,
        start_time=template.start_time,
        end_time=last_end,
        duration_sec_per_test=template.duration_sec_per_test,
        threads=template.threads,
    )


# ─── CI на лог-шкале ────────────────────────────────────────────────────────


def compute_ci_logscale(
    per_run_overall_ratios: list[float],
    alpha: float = 0.05,
) -> tuple[float | None, float | None, str | None]:
    """95% CI для overall_ratio на лог-шкале по t-распределению.

    Возвращает (low, high, method). При n<2 — (None, None, None).
    Method = 't_logscale' для основного случая.
    """
    if len(per_run_overall_ratios) < 2:
        return None, None, None
    if any(r <= 0 for r in per_run_overall_ratios):
        return None, None, None
    log_values = [math.log(r) for r in per_run_overall_ratios]
    n = len(log_values)
    mean_log = statistics.fmean(log_values)
    sd_log = statistics.stdev(log_values) if n >= 2 else 0.0
    se_log = sd_log / math.sqrt(n)
    t_crit = _t_critical_95(n - 1)
    half = t_crit * se_log
    low = math.exp(mean_log - half)
    high = math.exp(mean_log + half)
    return low, high, "t_logscale"


# ─── Главная функция ────────────────────────────────────────────────────────


def aggregate_multi_run(
    per_run_suites: list[MicroBenchSuiteResult],
    reference: ReferenceSet,
    weights: WeightsProfile,
    preset: Preset,
) -> MicroBenchSuiteResult:
    """Агрегировать N прогонов в один MicroBenchSuiteResult с overall и CI.

    Алгоритм по пресету:
    - **fast** (n=1): просто посчитать geomean_score на единственном прогоне,
      без CI.
    - **standard** (n=3): median-of-N на per-workload value, потом geomean_score
      на агрегированном suite. Без CI (n=3 даёт грубую оценку).
    - **accurate** (n≥5): mean (или trimmed mean при n≥10) на per-workload,
      geomean_score на агрегате; CI на лог-шкале по t-распределению.

    Возвращает один MicroBenchSuiteResult с:
    - results = агрегированные значения,
    - overall = OverallScore с заполненными CI (для accurate),
    - preset = переданный пресет,
    - n_runs = количество исходных прогонов.
    """
    if not per_run_suites:
        raise ValueError("aggregate_multi_run: empty per_run_suites")

    n_runs = len(per_run_suites)

    # Шаг 1: per-workload aggregation в зависимости от пресета.
    if preset == "fast":
        # Только один прогон ожидается; если их несколько — берём первый.
        aggregator = {res.name: res.value for res in per_run_suites[0].results if not res.error}
    elif preset == "standard":
        aggregator = median_per_workload(per_run_suites)
    elif preset == "accurate":
        use_trimmed = n_runs >= 10
        aggregator = mean_per_workload(per_run_suites, use_trimmed=use_trimmed)
    else:
        raise ValueError(f"unknown preset: {preset!r}")

    aggregated_suite = _aggregate_suite(per_run_suites, aggregator)

    # Шаг 2: CI (только для accurate и n≥2).
    ci_low, ci_high, ci_method = (None, None, None)
    if preset == "accurate" and n_runs >= 2:
        # Считаем geomean_score по каждому отдельному прогону, потом CI на ratios.
        per_run_ratios: list[float] = []
        for suite in per_run_suites:
            single_score = scoring.geomean_score(suite, reference, weights, n_runs=1)
            if single_score.overall_ratio > 0:
                per_run_ratios.append(single_score.overall_ratio)
        ci_low, ci_high, ci_method = compute_ci_logscale(per_run_ratios)
    elif preset == "standard":
        ci_method = "median_of_n"
    else:
        ci_method = "no_ci_n1" if n_runs == 1 else None

    # Шаг 3: главный score на агрегированном suite.
    overall = scoring.geomean_score(aggregated_suite, reference, weights, n_runs=n_runs)

    # Заполняем CI в ratio-шкале (раньше было ×1000 для overall_score,
    # который удалён в 0.9.x — теперь CI соотносится с overall_ratio).
    overall = overall.model_copy(
        update={
            "ci_lower": ci_low if ci_low is not None else None,
            "ci_upper": ci_high if ci_high is not None else None,
            "ci_method": ci_method,
        }
    )

    aggregated_suite.overall = overall
    aggregated_suite.preset = preset
    aggregated_suite.n_runs = n_runs
    return aggregated_suite


__all__ = [
    "PRESET_RUNS",
    "Preset",
    "aggregate_multi_run",
    "compute_ci_logscale",
    "mean_per_workload",
    "median_per_workload",
    "trimmed_mean",
]
