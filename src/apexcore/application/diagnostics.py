"""Стат-движок диагностики: правила формирования рекомендаций.

Правила работают как в режиме «один прогон» (по абсолютным порогам), так и в
режиме «прогон vs baseline» (по статистическим критериям из statistics.py).
"""

from __future__ import annotations

import numpy as np

from apexcore.application.statistics import compare_metric_series
from apexcore.domain.models import (
    BenchmarkResult,
    Diagnostic,
    DiagnosticSeverity,
)

# Пороги для абсолютных правил. Подбирались эмпирически и могут быть
# переопределены через профили (фаза 2).
TEMP_WARN_C = 80.0
TEMP_CRIT_C = 92.0
THROTTLE_RATIO_WARN = 0.05  # Если >5% отсчётов с тротлингом — warn.
THROTTLE_RATIO_CRIT = 0.20
FREQ_VARIATION_WARN = 0.20  # Коэффициент вариации частоты CPU при нагрузке.

ALPHA_DEFAULT = 0.05
EFFECT_THRESHOLD = 0.5


def diagnose_run(
    run: BenchmarkResult,
    baseline_run: BenchmarkResult | None = None,
    alpha: float = ALPHA_DEFAULT,
) -> list[Diagnostic]:
    """Полный набор правил диагностики для одного прогона.

    ``baseline_run`` — опциональный прогон-эталон для стат-сравнения.
    """
    diags: list[Diagnostic] = []
    diags.extend(_rules_temperature(run))
    diags.extend(_rules_throttling(run))
    diags.extend(_rules_frequency_variation(run))
    if baseline_run is not None:
        diags.extend(_rules_vs_baseline(run, baseline_run, alpha=alpha))
    diags.extend(_rules_simd_vs_int(run, baseline_run))
    return diags


# ─────────────────────────── Абсолютные правила ─────────────────────────────


def _rules_temperature(run: BenchmarkResult) -> list[Diagnostic]:
    """Перегрев CPU/GPU по температурам в истории метрик."""
    diags: list[Diagnostic] = []
    if not run.metrics_history:
        return diags
    max_temp = 0.0
    for snap in run.metrics_history:
        if snap.temperatures:
            for label, val in snap.temperatures.items():
                if val > max_temp:
                    max_temp = val
                if val >= TEMP_CRIT_C:
                    msg = (
                        f"Сенсор '{label}' зафиксировал {val:.1f}°C — "
                        f"выше критического порога {TEMP_CRIT_C:.0f}°C"
                    )
                    diags.append(
                        Diagnostic(
                            code="temperature_critical",
                            severity=DiagnosticSeverity.CRITICAL,
                            message=msg,
                            metric=f"temperature.{label}",
                            evidence={"temp_c": val, "threshold_c": TEMP_CRIT_C},
                            recommendation="Проверить охлаждение, термопасту, отвод тепла из корпуса",
                        )
                    )
                    return diags  # Хватит одной критической, остальные будут шумом.
    if max_temp >= TEMP_WARN_C:
        diags.append(
            Diagnostic(
                code="temperature_warning",
                severity=DiagnosticSeverity.WARN,
                message=f"Достигнута температура {max_temp:.1f}°C — выше комфортного порога {TEMP_WARN_C:.0f}°C",
                metric="temperature.max",
                evidence={"temp_c": max_temp, "threshold_c": TEMP_WARN_C},
                recommendation="Контролировать температуру под продолжительной нагрузкой",
            )
        )
    return diags


def _rules_throttling(run: BenchmarkResult) -> list[Diagnostic]:
    """Тротлинг CPU: считаем долю отсчётов с признаком throttle."""
    if not run.metrics_history:
        return []
    n = len(run.metrics_history)
    throttled = sum(1 for s in run.metrics_history if s.cpu_throttled)
    ratio = throttled / n
    if ratio >= THROTTLE_RATIO_CRIT:
        return [
            Diagnostic(
                code="cpu_thermal_throttle_critical",
                severity=DiagnosticSeverity.CRITICAL,
                message=f"CPU дросселит частоту в {ratio*100:.1f}% отсчётов — серьёзный термотротлинг",
                metric="cpu_throttled",
                evidence={"ratio": ratio, "samples": n, "throttled": throttled},
                recommendation="Улучшить охлаждение или снизить TDP-лимит CPU",
            )
        ]
    if ratio >= THROTTLE_RATIO_WARN:
        return [
            Diagnostic(
                code="cpu_thermal_throttle_warning",
                severity=DiagnosticSeverity.WARN,
                message=f"Зафиксирован тротлинг CPU в {ratio*100:.1f}% отсчётов",
                metric="cpu_throttled",
                evidence={"ratio": ratio, "samples": n, "throttled": throttled},
                recommendation="Профилактика охлаждения и термопасты",
            )
        ]
    return []


def _rules_frequency_variation(run: BenchmarkResult) -> list[Diagnostic]:
    """Высокая дисперсия частоты при стабильной (>50%) нагрузке = подозрение на нестабильность питания."""
    freqs = [s.frequencies.get("cpu_avg") for s in run.metrics_history]
    cpus = [s.cpu_percent for s in run.metrics_history]
    pairs = [(f, c) for f, c in zip(freqs, cpus, strict=False) if f is not None and c is not None]
    pairs = [(f, c) for f, c in pairs if c > 50]  # Учитываем только под нагрузкой.
    if len(pairs) < 5:
        return []
    f_vals = np.array([p[0] for p in pairs], dtype=float)
    if f_vals.mean() <= 0:
        return []
    cv = float(f_vals.std(ddof=1) / f_vals.mean())
    if cv >= FREQ_VARIATION_WARN:
        return [
            Diagnostic(
                code="cpu_freq_unstable",
                severity=DiagnosticSeverity.WARN,
                message=f"Коэффициент вариации частоты CPU под нагрузкой {cv*100:.1f}% — нестабильно",
                metric="cpu_freq_cv",
                evidence={"coefficient_of_variation": cv, "samples": len(pairs)},
                recommendation="Проверить блок питания, троттлинг по току (PL2/EDC), Power Plan",
            )
        ]
    return []


# ─────────────────────────── Сравнение с baseline ───────────────────────────


def _rules_vs_baseline(
    run: BenchmarkResult,
    baseline: BenchmarkResult,
    alpha: float = ALPHA_DEFAULT,
) -> list[Diagnostic]:
    """Сравнить текущий прогон с baseline по основным метрикам."""
    diags: list[Diagnostic] = []
    metrics = [
        ("cpu_percent", False),
        ("ram_percent", False),
    ]
    for name, higher_is_better in metrics:
        a = [getattr(s, name) for s in baseline.metrics_history]
        b = [getattr(s, name) for s in run.metrics_history]
        if not a or not b:
            continue
        cmp = compare_metric_series(a, b, alpha=alpha, higher_is_better=higher_is_better)
        if not cmp.is_significant or cmp.direction != "worse":
            continue
        diags.append(
            Diagnostic(
                code=f"{name}_degradation",
                severity=DiagnosticSeverity.WARN,
                message=(
                    f"Метрика {name} деградировала: {cmp.mean_a:.2f} → {cmp.mean_b:.2f} "
                    f"(p={cmp.p_value:.4f}, d={cmp.effect_size:+.2f})"
                ),
                metric=name,
                p_value=cmp.p_value,
                effect_size=cmp.effect_size,
                evidence={
                    "mean_baseline": cmp.mean_a,
                    "mean_current": cmp.mean_b,
                    "test": cmp.test_name,
                    "ci95": [cmp.ci95_low, cmp.ci95_high],
                },
                recommendation="Проверить фоновые процессы, состояние охлаждения и зарядку (для лэптопа)",
            )
        )

    diags.extend(_rules_throughput_vs_baseline(run, baseline, alpha))
    return diags


def _rules_throughput_vs_baseline(
    run: BenchmarkResult,
    baseline: BenchmarkResult,
    alpha: float = ALPHA_DEFAULT,
) -> list[Diagnostic]:
    """Сравнить throughput по категориям стресс-движков."""
    by_cat_a: dict[str, list[float]] = {}
    by_cat_b: dict[str, list[float]] = {}
    for s in baseline.stress_results:
        by_cat_a.setdefault(s.category, []).append(s.throughput)
    for s in run.stress_results:
        by_cat_b.setdefault(s.category, []).append(s.throughput)

    diags: list[Diagnostic] = []
    cat_messages = {
        "cpu_int": "целочисленная производительность CPU",
        "cpu_fp": "плавающая производительность CPU",
        "ram_bw": "пропускная способность RAM",
        "ram_lat": "латентность RAM",
    }
    for cat, a in by_cat_a.items():
        b = by_cat_b.get(cat)
        if not b or not a:
            continue
        higher_is_better = cat != "ram_lat"
        cmp = compare_metric_series(a, b, alpha=alpha, higher_is_better=higher_is_better)
        if not cmp.is_significant or cmp.direction != "worse":
            continue
        descr = cat_messages.get(cat, cat)
        diags.append(
            Diagnostic(
                code=f"{cat}_degradation",
                severity=DiagnosticSeverity.CRITICAL if abs(cmp.effect_size) > 1.0 else DiagnosticSeverity.WARN,
                message=(
                    f"Деградация подсистемы: {descr} "
                    f"({cmp.mean_a:.3g} → {cmp.mean_b:.3g}, p={cmp.p_value:.4f}, d={cmp.effect_size:+.2f})"
                ),
                metric=f"throughput.{cat}",
                p_value=cmp.p_value,
                effect_size=cmp.effect_size,
                evidence={
                    "throughput_baseline": cmp.mean_a,
                    "throughput_current": cmp.mean_b,
                    "test": cmp.test_name,
                },
                recommendation=(
                    "Проверить охлаждение и фоновые задачи"
                    if cat.startswith("cpu")
                    else "Проверить режим работы памяти (XMP/EXPO, частоту, тайминги)"
                ),
            )
        )
    return diags


def _rules_simd_vs_int(
    run: BenchmarkResult,
    baseline_run: BenchmarkResult | None,
) -> list[Diagnostic]:
    """Если cpu_fp падает значимо сильнее cpu_int — подозрение на проблемы SIMD/охлаждения.

    Без baseline просто сравниваем относительные доли (грубая эвристика).
    """
    by_cat: dict[str, list[float]] = {}
    for s in run.stress_results:
        by_cat.setdefault(s.category, []).append(s.throughput)
    if "cpu_fp" not in by_cat or "cpu_int" not in by_cat:
        return []

    if baseline_run is not None:
        b_by: dict[str, list[float]] = {}
        for s in baseline_run.stress_results:
            b_by.setdefault(s.category, []).append(s.throughput)
        if "cpu_fp" in b_by and "cpu_int" in b_by:
            ratio_now = float(np.mean(by_cat["cpu_fp"])) / max(float(np.mean(by_cat["cpu_int"])), 1e-9)
            ratio_base = float(np.mean(b_by["cpu_fp"])) / max(float(np.mean(b_by["cpu_int"])), 1e-9)
            if ratio_base > 0 and ratio_now / ratio_base < 0.85:
                return [
                    Diagnostic(
                        code="simd_relative_drop",
                        severity=DiagnosticSeverity.WARN,
                        message=(
                            f"FP/INT-отношение упало: {ratio_base:.2f} → {ratio_now:.2f}. "
                            "FP-нагрузка пострадала сильнее целочисленной."
                        ),
                        metric="ratio.cpu_fp_to_cpu_int",
                        evidence={"ratio_now": ratio_now, "ratio_baseline": ratio_base},
                        recommendation="Проверить тротлинг при AVX, ограничения по току (TVB, AVX-offset)",
                    )
                ]
    return []
