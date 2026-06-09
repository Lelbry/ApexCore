"""Статистический модуль: критерии значимости отклонений и величина эффекта.

Алгоритм сравнения двух выборок (baseline vs current):
1. Проверка нормальности обеих выборок тестом Шапиро–Уилка (если n ≥ 3).
2. Если обе нормальны → Welch t-test (`ttest_ind`, equal_var=False).
3. Иначе → непараметрический Mann–Whitney U.
4. Считаем Cohen's d для оценки величины эффекта.
5. Доверительный интервал 95% для разности средних (через t-распределение).

Решение «деградация / норма / улучшение» принимается при ``p < alpha`` И
``|d| > effect_threshold`` (по умолчанию 0.5 — «умеренный» эффект по Коэну).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass
class ComparisonResult:
    """Результат стат-сравнения двух выборок одной метрики."""

    test_name: str
    n_a: int
    n_b: int
    mean_a: float
    mean_b: float
    std_a: float
    std_b: float
    delta_mean: float
    p_value: float
    effect_size: float  # Cohen's d.
    ci95_low: float
    ci95_high: float
    is_normal_a: bool
    is_normal_b: bool
    alpha: float
    is_significant: bool  # p < alpha и |d| > effect_threshold.
    direction: str  # 'better' | 'worse' | 'same'.
    higher_is_better: bool


def _shapiro_normal(sample: np.ndarray, alpha: float = 0.05) -> bool:
    """Тест Шапиро–Уилка. Слишком короткие выборки считаем «не определёнными» (вернём False)."""
    n = len(sample)
    if n < 3 or n > 5000:
        return False
    if float(np.var(sample)) == 0.0:
        return False
    try:
        _, p = stats.shapiro(sample)
        return bool(p > alpha)
    except (ValueError, FloatingPointError):
        return False


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0
    va = float(np.var(a, ddof=1))
    vb = float(np.var(b, ddof=1))
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)) if (na + nb - 2) > 0 else 0.0
    if pooled == 0:
        return 0.0
    return (float(np.mean(b)) - float(np.mean(a))) / pooled


def _ci95_diff(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """95% доверительный интервал для разности средних (b - a) по Welch."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return (float("nan"), float("nan"))
    ma, mb = float(np.mean(a)), float(np.mean(b))
    va = float(np.var(a, ddof=1))
    vb = float(np.var(b, ddof=1))
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return (mb - ma, mb - ma)
    df_num = (va / na + vb / nb) ** 2
    df_den = (va ** 2) / (na ** 2 * (na - 1)) + (vb ** 2) / (nb ** 2 * (nb - 1))
    df = df_num / df_den if df_den > 0 else (na + nb - 2)
    t_crit = float(stats.t.ppf(0.975, df))
    diff = mb - ma
    return (diff - t_crit * se, diff + t_crit * se)


def compare_metric_series(
    baseline: list[float],
    current: list[float],
    alpha: float = 0.05,
    effect_threshold: float = 0.5,
    higher_is_better: bool = False,
) -> ComparisonResult:
    """Сравнить две серии замеров одной метрики.

    ``higher_is_better=False`` означает, что бóльшие значения — это плохо
    (например, температуры или загрузка CPU при простое).
    """
    a = np.asarray(baseline, dtype=float)
    b = np.asarray(current, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        # Слишком мало данных — возвращаем вырожденный результат.
        ma = float(np.mean(a)) if len(a) else 0.0
        mb = float(np.mean(b)) if len(b) else 0.0
        return ComparisonResult(
            test_name="insufficient",
            n_a=len(a),
            n_b=len(b),
            mean_a=ma,
            mean_b=mb,
            std_a=float(np.std(a, ddof=1)) if len(a) > 1 else 0.0,
            std_b=float(np.std(b, ddof=1)) if len(b) > 1 else 0.0,
            delta_mean=mb - ma,
            p_value=1.0,
            effect_size=0.0,
            ci95_low=float("nan"),
            ci95_high=float("nan"),
            is_normal_a=False,
            is_normal_b=False,
            alpha=alpha,
            is_significant=False,
            direction="same",
            higher_is_better=higher_is_better,
        )

    norm_a = _shapiro_normal(a)
    norm_b = _shapiro_normal(b)
    if norm_a and norm_b:
        _t_stat, p_val = stats.ttest_ind(a, b, equal_var=False)
        test_name = "Welch t-test"
    else:
        try:
            _u_stat, p_val = stats.mannwhitneyu(a, b, alternative="two-sided")
            test_name = "Mann-Whitney U"
        except ValueError:
            # Все значения равны.
            return ComparisonResult(
                test_name="degenerate",
                n_a=len(a),
                n_b=len(b),
                mean_a=float(np.mean(a)),
                mean_b=float(np.mean(b)),
                std_a=float(np.std(a, ddof=1)),
                std_b=float(np.std(b, ddof=1)),
                delta_mean=float(np.mean(b) - np.mean(a)),
                p_value=1.0,
                effect_size=0.0,
                ci95_low=0.0,
                ci95_high=0.0,
                is_normal_a=norm_a,
                is_normal_b=norm_b,
                alpha=alpha,
                is_significant=False,
                direction="same",
                higher_is_better=higher_is_better,
            )

    d = _cohens_d(a, b)
    ci_lo, ci_hi = _ci95_diff(a, b)
    delta = float(np.mean(b) - np.mean(a))
    is_sig = (float(p_val) < alpha) and (abs(d) > effect_threshold)
    if not is_sig:
        direction = "same"
    elif (delta > 0 and higher_is_better) or (delta < 0 and not higher_is_better):
        direction = "better"
    else:
        direction = "worse"

    return ComparisonResult(
        test_name=test_name,
        n_a=len(a),
        n_b=len(b),
        mean_a=float(np.mean(a)),
        mean_b=float(np.mean(b)),
        std_a=float(np.std(a, ddof=1)),
        std_b=float(np.std(b, ddof=1)),
        delta_mean=delta,
        p_value=float(p_val),
        effect_size=d,
        ci95_low=ci_lo,
        ci95_high=ci_hi,
        is_normal_a=norm_a,
        is_normal_b=norm_b,
        alpha=alpha,
        is_significant=is_sig,
        direction=direction,
        higher_is_better=higher_is_better,
    )
