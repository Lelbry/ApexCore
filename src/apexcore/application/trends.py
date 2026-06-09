"""Временные тренды по истории прогонов и снимков метрик.

Поддерживается:
- ``build_run_trend`` — ряд значений по последовательности прогонов (одна точка на прогон)
  с расчётом скользящего среднего и оконного p95;
- ``rolling_mean`` / ``rolling_p95`` — оконные агрегаты по ``MetricSnapshot``-ам.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from apexcore.domain.models import BenchmarkResult, MetricSnapshot


@dataclass
class TrendSeries:
    """Результат построения тренда: исходный ряд + оконные агрегаты."""

    metric: str
    values: list[float] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)
    rolling_mean: list[float] = field(default_factory=list)
    rolling_p95: list[float] = field(default_factory=list)
    window: int = 1


def build_run_trend(
    runs: list[BenchmarkResult],
    metric: str = "final_score",
    window: int = 5,
) -> TrendSeries:
    """Построить тренд по последовательности прогонов.

    Поддерживаемые ``metric``:
    - ``final_score`` (поле прогона);
    - ``cpu_percent``/``ram_percent`` (среднее по metrics_history прогона);
    - ``throughput.<category>`` (среднее по соответствующим стресс-результатам).
    """
    # Сортируем от старых к новым, чтобы тренд читался слева направо.
    runs_sorted = sorted(runs, key=lambda r: r.start_time)
    values: list[float] = []
    ts: list[str] = []

    for r in runs_sorted:
        v = _extract_run_metric(r, metric)
        if v is None:
            continue
        values.append(v)
        ts.append(r.start_time.isoformat())

    series = TrendSeries(metric=metric, values=values, timestamps=ts, window=max(1, window))
    if values:
        series.rolling_mean = rolling_mean(values, series.window)
        series.rolling_p95 = rolling_p95(values, series.window)
    return series


def build_metric_trend(
    snapshots: list[MetricSnapshot],
    metric: str = "cpu_percent",
    window: int = 5,
) -> TrendSeries:
    """Тренд по снимкам внутри одного прогона."""
    values = [getattr(s, metric, None) for s in snapshots]
    values = [float(v) for v in values if v is not None]
    series = TrendSeries(metric=metric, values=values, window=max(1, window))
    if values:
        series.rolling_mean = rolling_mean(values, window)
        series.rolling_p95 = rolling_p95(values, window)
    return series


def rolling_mean(values: list[float], window: int) -> list[float]:
    """Скользящее среднее с окном ``window`` (минимум 1)."""
    if not values:
        return []
    w = max(1, min(window, len(values)))
    arr = np.asarray(values, dtype=float)
    out: list[float] = []
    for i in range(len(arr)):
        lo = max(0, i - w + 1)
        out.append(float(arr[lo : i + 1].mean()))
    return out


def rolling_p95(values: list[float], window: int) -> list[float]:
    """Скользящий 95-й перцентиль с тем же окном."""
    if not values:
        return []
    w = max(1, min(window, len(values)))
    arr = np.asarray(values, dtype=float)
    out: list[float] = []
    for i in range(len(arr)):
        lo = max(0, i - w + 1)
        out.append(float(np.percentile(arr[lo : i + 1], 95)))
    return out


# ─────────────────────────── Извлечение значений метрик ─────────────────────


def _extract_run_metric(run: BenchmarkResult, metric: str) -> float | None:
    if metric == "final_score":
        return float(run.final_score)
    if metric in {"cpu_percent", "ram_percent", "ram_used_gb"}:
        vals = [getattr(s, metric) for s in run.metrics_history]
        return float(np.mean(vals)) if vals else None
    if metric.startswith("throughput."):
        cat = metric.split(".", 1)[1]
        ths = [s.throughput for s in run.stress_results if s.category == cat]
        return float(np.mean(ths)) if ths else None
    if metric == "temperature_max":
        ts = [max(s.temperatures.values()) for s in run.metrics_history if s.temperatures]
        return float(np.mean(ts)) if ts else None
    return None
