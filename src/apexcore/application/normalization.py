"""Регрессионный детектор: «насколько прогон отклонился от собственного baseline».

ВНИМАНИЕ: Это **НЕ** основная система оценки производительности. После
scoring v2 (см. ``scoring.py`` и ``docs/scoring_v2.md``) основной балл
вычисляется через Roofline-ratios и взвешенное геометрическое среднее.
Этот модуль остался только как диагностический инструмент:

- сравнение двух прогонов (CLI ``apexcore compare``);
- детект деградации системы во времени (regression detection).

Основные удалённые компоненты (по сравнению с v1):
- ``composite_score`` → удалён (нестандартный log10 без reference).
- ``DEFAULT_WEIGHTS`` → удалён (теперь веса в weights.py).
- ``_thermal_stability`` → перенесён в thermal.py (отдельная метрика).
- thermal_stability как категория → удалена из субскоров.

Оставшийся API:
- ``normalize_run(run, baseline, method)`` — нормализованный балл [0,1]
  относительно собственного baseline. Используется для compare/diagnose.
- ``baseline_from_run`` / ``baseline_from_runs`` — построение baseline
  из BenchmarkResult'ов (для compare-команды).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal

import numpy as np

from apexcore.domain.models import (
    BaselineProfile,
    BenchmarkResult,
    NormalizedScore,
    StressResult,
)

Method = Literal["min_max", "z_score"]


# ─────────────────────────── Утилиты ────────────────────────────────────────


def _system_fingerprint(result: BenchmarkResult) -> str:
    si = result.system_info
    return f"{si.os_name}|{si.cpu_model}|{si.cpu_cores.physical}c{si.cpu_cores.logical}t|{si.ram_total_gb:.0f}GB"


def _higher_is_better(category: str) -> bool:
    """Для каких stress-категорий «больше = лучше»."""
    return category != "ram_lat"


def _stress_throughputs_by_category(stress_results: list[StressResult]) -> dict[str, list[float]]:
    """Сгруппировать throughput по категориям (одна категория = несколько движков)."""
    out: dict[str, list[float]] = {}
    for s in stress_results:
        out.setdefault(s.category, []).append(s.throughput)
    return out


# ─────────────────────────── Базовые профили из прогонов ────────────────────


def baseline_from_run(run: BenchmarkResult, name: str) -> BaselineProfile:
    """Построить baseline из одного прогона: mean = throughput, std = разумный эпсилон."""
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    raw: dict[str, list[float]] = {}
    for cat, vals in _stress_throughputs_by_category(run.stress_results).items():
        means[cat] = float(np.mean(vals))
        stds[cat] = max(float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0, abs(means[cat]) * 0.05)
        raw[cat] = list(vals)
    return BaselineProfile(
        name=name,
        profile_name=run.config.profile_name,
        system_fingerprint=_system_fingerprint(run),
        means=means,
        stds=stds,
        sample_size=1,
        raw_samples=raw,
        created_at=datetime.now(timezone.utc),
    )


def baseline_from_runs(runs: list[BenchmarkResult], name: str) -> BaselineProfile:
    """Построить baseline из набора прогонов: посчитать mean/std по выборке."""
    if not runs:
        raise ValueError("Нужен хотя бы один прогон для построения baseline")
    raw: dict[str, list[float]] = {}
    for run in runs:
        for cat, vals in _stress_throughputs_by_category(run.stress_results).items():
            raw.setdefault(cat, []).extend(vals)
    means = {k: float(np.mean(v)) for k, v in raw.items() if v}
    stds = {
        k: max(float(np.std(v, ddof=1)) if len(v) > 1 else 0.0, abs(means.get(k, 1.0)) * 0.02)
        for k, v in raw.items()
    }
    return BaselineProfile(
        name=name,
        profile_name=runs[0].config.profile_name,
        system_fingerprint=_system_fingerprint(runs[0]),
        means=means,
        stds=stds,
        sample_size=len(runs),
        raw_samples=raw,
        created_at=datetime.now(timezone.utc),
    )


# ─────────────────────────── Регрессионный балл ─────────────────────────────


def _sigmoid(x: float) -> float:
    if x > 50:
        return 1.0
    if x < -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _normalize_value(
    value: float,
    mean: float,
    std: float,
    method: Method,
    higher_is_better: bool,
) -> float:
    """Нормализовать одно значение метрики в [0, 1] (1 = лучше baseline)."""
    if std <= 0:
        std = max(abs(mean) * 0.05, 1e-6)
    if method == "z_score":
        z = (value - mean) / std
        if not higher_is_better:
            z = -z
        # Сигмоида с пологим участком: 1 σ улучшения ≈ 0.62, 2 σ ≈ 0.76.
        return _sigmoid(z)
    # min_max: окно ±3σ вокруг среднего, центр = 0.5.
    lo = mean - 3 * std
    hi = mean + 3 * std
    if hi == lo:
        return 0.5
    raw = (value - lo) / (hi - lo)
    raw = float(np.clip(raw, 0.0, 1.0))
    return raw if higher_is_better else 1.0 - raw


def normalize_run(
    run: BenchmarkResult,
    baseline: BaselineProfile,
    method: Method = "z_score",
    weights: dict[str, float] | None = None,
) -> NormalizedScore:
    """Нормализовать прогон относительно baseline (regression detection).

    Возвращает балл [0,1] = «насколько отклонился от собственного baseline».
    НЕ для публичных сравнений между ПК — для этого используйте scoring.py
    (общая оценка через Roofline).

    Если ``weights`` не указаны — все категории baseline получают равный вес.
    """
    by_cat = _stress_throughputs_by_category(run.stress_results)
    subscores: dict[str, float] = {}
    used_weights: dict[str, float] = {}

    for cat, values in by_cat.items():
        if cat not in baseline.means:
            continue
        avg = float(np.mean(values))
        sub = _normalize_value(
            avg,
            baseline.means[cat],
            baseline.stds.get(cat, 0.0),
            method=method,
            higher_is_better=_higher_is_better(cat),
        )
        subscores[cat] = sub
        if weights:
            used_weights[cat] = weights.get(cat, 0.0)
        else:
            used_weights[cat] = 1.0  # equal weights по умолчанию

    total_weight = sum(used_weights.values())
    composite = (
        sum(subscores[k] * used_weights[k] for k in subscores) / total_weight
        if total_weight > 0
        else float(np.mean(list(subscores.values())) if subscores else 0.0)
    )

    return NormalizedScore(
        composite=float(composite),
        subscores=subscores,
        method=method,
        weights=used_weights,
    )


__all__ = [
    "Method",
    "baseline_from_run",
    "baseline_from_runs",
    "normalize_run",
]
