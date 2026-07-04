"""Чистые функции скоринга v2 (HM, GM, geomean_score, CI).

Спецификация: ``new-app/docs/scoring_v2.md``.

Это ядро системы оценки производительности. Функции принимают данные на вход,
возвращают данные на выход, не имеют побочных эффектов и легко тестируются
без БД, телеметрии и других зависимостей.

Иерархия скоринга
-----------------
::

    r_ij = measured_ij / reference_ij                            # per-workload ratio
    r_category = HM(r_ij для всех j в категории)                 # weakest-link sensitive
    R_MEM = r_memory
    R_CPU_compute = GM_weighted(r_flops, r_integer, r_crypto, r_fractal)
    R_overall = GM_weighted(R_MEM, R_CPU_compute)   # overall_ratio (доля пика)

Единый «итоговый балл» (overall_score = 1000 · R_overall) удалён в 0.9.x как
устаревший: micro-прогон даёт детальный per-category анализ (subscores), а не
системный балл. ``overall_ratio`` оставлен — он нужен для CI и multi-run.

Источники: Smith 1988 (HM для rate), Fleming-Wallace 1986 (GM для нормализованных
ratio), Williams 2009 (Roofline-интерпретация).
"""

from __future__ import annotations

import math

from apexcore.application.references import ReferenceSet
from apexcore.application.weights import WeightsProfile, normalize_weights
from apexcore.domain.models import (
    MicroBenchResult,
    MicroBenchSuiteResult,
    OverallScore,
)

SCORING_VERSION = "2.0.0"

# Категория micro-теста → имя category-ratio в OverallScore.subscores.
_CATEGORY_TO_RATIO_KEY = {
    "memory": "r_memory",
    "flops": "r_flops",
    "integer": "r_integer",
    "crypto": "r_crypto",
    "fractal": "r_fractal",
}


# ─── Базовые средние ────────────────────────────────────────────────────────


def harmonic_mean(values: list[float]) -> float:
    """Harmonic mean: ``n / Σ(1/v)``.

    Используется внутри категории для weakest-link sensitivity (Smith 1988).
    Бросает ``ValueError`` при пустом списке или ≤0 значениях.
    """
    if not values:
        raise ValueError("harmonic_mean: empty input")
    if any(v <= 0 for v in values):
        raise ValueError("harmonic_mean: all values must be positive")
    return len(values) / sum(1.0 / v for v in values)


def geometric_mean(values: list[float]) -> float:
    """Geometric mean через лог-шкалу: ``exp(mean(ln(v)))``.

    Используется между категориями (Fleming-Wallace 1986). Численно устойчиво
    для больших ranges (через логарифмы).
    """
    if not values:
        raise ValueError("geometric_mean: empty input")
    if any(v <= 0 for v in values):
        raise ValueError("geometric_mean: all values must be positive")
    return math.exp(sum(math.log(v) for v in values) / len(values))


def weighted_geometric_mean(values: dict[str, float], weights: dict[str, float]) -> float:
    """Взвешенный GM: ``exp(Σ w_i · ln(v_i) / Σ w_i)``.

    Веса нормируются на сумму=1 автоматически. Если ключи весов не покрывают
    все ключи values — недостающие веса считаются 0 (workload игнорируется).
    Если ни одного валидного веса нет — возвращает невзвешенный GM.
    """
    if not values:
        raise ValueError("weighted_geometric_mean: empty values")
    if any(v <= 0 for v in values.values()):
        raise ValueError("weighted_geometric_mean: all values must be positive")

    relevant_weights = {k: weights.get(k, 0.0) for k in values}
    total = sum(relevant_weights.values())
    if total <= 0:
        # Все веса нули — вернуть равновзвешенный GM.
        return geometric_mean(list(values.values()))

    norm = normalize_weights(relevant_weights)
    log_sum = sum(norm[k] * math.log(values[k]) for k in values)
    return math.exp(log_sum)


# ─── Per-workload ratios ─────────────────────────────────────────────────────


def compute_workload_ratios(
    suite: MicroBenchSuiteResult,
    reference: ReferenceSet,
) -> tuple[dict[str, float], list[str]]:
    """Перевести `MicroBenchResult` в безразмерные ratios относительно reference.

    Все micro-тесты на текущий момент — throughput (higher-is-better), поэтому
    ratio = measured / reference. Для будущей `memory_lat` (lower-is-better,
    см. этап 13) понадобится инверсия — она добавится позже.

    Возвращает (ratios, notes), где:
    - ratios: dict[workload_id → r],
    - notes: список диагностических пометок (skipped workloads, errors).
    """
    ratios: dict[str, float] = {}
    notes: list[str] = []
    for res in suite.results:
        if res.error:
            notes.append(f"workload_error:{res.name}")
            continue
        if res.value <= 0:
            notes.append(f"workload_zero_value:{res.name}")
            continue
        ref_value = reference.values.get(res.name)
        if ref_value is None:
            notes.append(f"workload_no_reference:{res.name}")
            continue
        if ref_value.value <= 0:
            notes.append(f"workload_invalid_reference:{res.name}")
            continue
        ratios[res.name] = res.value / ref_value.value
    return ratios, notes


# ─── Per-category aggregation ────────────────────────────────────────────────


def aggregate_category(
    ratios: dict[str, float],
    category: str,
    workload_results: list[MicroBenchResult],
) -> float | None:
    """HM ratios всех подтестов одной категории.

    ``workload_results`` нужен только для определения категории каждого имени.
    Возвращает None, если в категории нет ни одного валидного ratio.
    """
    in_cat = [
        ratios[r.name]
        for r in workload_results
        if r.category == category and r.name in ratios
    ]
    if not in_cat:
        return None
    return harmonic_mean(in_cat)


# ─── Главная функция ────────────────────────────────────────────────────────


def geomean_score(
    suite: MicroBenchSuiteResult,
    reference: ReferenceSet,
    weights: WeightsProfile,
    n_runs: int = 1,
) -> OverallScore:
    """Полный pipeline: micro-результаты → итоговый OverallScore.

    Шаги (см. docs/scoring_v2.md §5):
    1. Per-workload ratios против reference.
    2. HM ratios внутри каждой категории (memory, flops, integer, crypto, fractal).
    3. R_MEM = r_memory; R_CPU_compute = weighted GM(r_flops, r_integer, r_crypto, r_fractal).
    4. R_overall = weighted GM(R_MEM, R_CPU_compute) → overall_ratio.

    Единый балл overall_score (1000·R) больше не вычисляется (удалён в 0.9.x);
    результат несёт overall_ratio + subscores.

    `n_runs` пишется в результат для информации (множественные прогоны
    обрабатываются в multi_run.py — этап 6).
    """
    notes: list[str] = []
    provisional = False

    ratios, ratio_notes = compute_workload_ratios(suite, reference)
    notes.extend(ratio_notes)

    if any(rn.startswith("workload_no_reference:") for rn in ratio_notes):
        # Для частичного reference выводим общий маркер.
        notes.append("partial_reference")

    # provisional флаг = любой использованный reference value помечен provisional.
    used_workload_ids = set(ratios.keys())
    if any(
        wid in reference.values and reference.values[wid].provisional
        for wid in used_workload_ids
    ):
        provisional = True
        notes.append("provisional_reference_used")

    # Подскоры по категориям (HM).
    category_ratios: dict[str, float] = {}
    for cat, ratio_key in _CATEGORY_TO_RATIO_KEY.items():
        cat_value = aggregate_category(ratios, cat, suite.results)
        if cat_value is not None and cat_value > 0:
            category_ratios[ratio_key] = cat_value

    if not category_ratios:
        # Совсем нет валидных данных — возвращаем «пустой» score.
        return OverallScore(
            overall_ratio=0.0,
            subscores={},
            n_runs=n_runs,
            reference_id=reference.id,
            weights_profile=weights.name,
            scoring_version=SCORING_VERSION,
            provisional=True,
            notes=[*notes, "no_valid_ratios"],
        )

    # R_MEM (одна категория = подсистема).
    r_mem = category_ratios.get("r_memory")
    # R_CPU_compute (4 категории внутри).
    cpu_categories = {
        k: v
        for k, v in category_ratios.items()
        if k in ("r_flops", "r_integer", "r_crypto", "r_fractal")
    }
    r_cpu_compute: float | None = None
    if cpu_categories:
        r_cpu_compute = weighted_geometric_mean(
            cpu_categories, weights.compute_category_weights
        )

    subscores: dict[str, float] = dict(category_ratios)  # включаем все r_*
    if r_mem is not None:
        subscores["R_MEM"] = r_mem
    if r_cpu_compute is not None:
        subscores["R_CPU_compute"] = r_cpu_compute

    # R_overall: weighted GM(R_MEM, R_CPU_compute).
    subsystem_values: dict[str, float] = {}
    if r_mem is not None:
        subsystem_values["R_MEM"] = r_mem
    if r_cpu_compute is not None:
        subsystem_values["R_CPU_compute"] = r_cpu_compute

    if not subsystem_values:
        return OverallScore(
            overall_ratio=0.0,
            subscores=subscores,
            n_runs=n_runs,
            reference_id=reference.id,
            weights_profile=weights.name,
            scoring_version=SCORING_VERSION,
            provisional=True,
            notes=[*notes, "no_subsystems"],
        )

    if len(subsystem_values) == 1:
        notes.append("single_subsystem_only")

    r_overall = weighted_geometric_mean(subsystem_values, weights.subsystem_weights)
    # overall_score (единый балл 1000·R) удалён в 0.9.x как устаревший —
    # micro-прогон даёт детальный per-category анализ (subscores), не
    # системный балл. overall_ratio оставлен (нужен для CI/multi-run).

    return OverallScore(
        overall_ratio=r_overall,
        subscores=subscores,
        n_runs=n_runs,
        reference_id=reference.id,
        weights_profile=weights.name,
        scoring_version=SCORING_VERSION,
        provisional=provisional,
        notes=notes,
    )


__all__ = [
    "SCORING_VERSION",
    "aggregate_category",
    "compute_workload_ratios",
    "geomean_score",
    "geometric_mean",
    "harmonic_mean",
    "weighted_geometric_mean",
]
