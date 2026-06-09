"""Логика расчёта Winsat-оценок 1.0–9.9.

Маппинг metric → score реализован через таблицу пороговых точек
(``data/winsat_thresholds.yaml``) с лог-линейной интерполяцией по log2(value).
WinSPRLevel = минимум по всем PASS-подскорам, как делает Windows.

Для CPU используется гармоническое среднее AES-256 + SHA-1 (как в Winsat:
``CPUScore = HM(шифрование, хеширование)``). Для DiskScore — минимум двух
подкатегорий (sequential + random), что повторяет логику ``winsat formal``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from math import log2
from pathlib import Path

import yaml

from apexcore.domain.winsat import WinsatStatus, WinsatSubscore


@dataclass(frozen=True)
class _Point:
    value: float
    score: float


@dataclass(frozen=True)
class _CategoryThresholds:
    metric: str
    unit: str
    points: tuple[_Point, ...]


SCORE_MIN = 1.0
SCORE_MAX = 9.9


def _load_thresholds() -> dict[str, _CategoryThresholds]:
    """Загрузить YAML с порогами из пакетных данных apexcore.data."""
    try:
        text = resources.files("apexcore.data").joinpath("winsat_thresholds.yaml").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        path = Path(__file__).resolve().parent.parent / "data" / "winsat_thresholds.yaml"
        text = path.read_text(encoding="utf-8")

    raw = yaml.safe_load(text)
    out: dict[str, _CategoryThresholds] = {}
    for category, body in raw.items():
        if category == "version":
            continue
        if not isinstance(body, dict):
            continue
        pts = tuple(
            _Point(value=float(p["value"]), score=float(p["score"]))
            for p in body["points"]
        )
        out[category] = _CategoryThresholds(
            metric=str(body["metric"]),
            unit=str(body["unit"]),
            points=pts,
        )
    return out


_THRESHOLDS: dict[str, _CategoryThresholds] | None = None


def _thresholds() -> dict[str, _CategoryThresholds]:
    """Lazy-cache порогов (читаем YAML один раз за процесс)."""
    global _THRESHOLDS
    if _THRESHOLDS is None:
        _THRESHOLDS = _load_thresholds()
    return _THRESHOLDS


def score_from_metric(value: float, points: Iterable[_Point]) -> float:
    """Преобразовать величину метрики (MB/s) в оценку 1.0–9.9.

    Между соседними точками — линейная интерполяция по log2(value).
    Снаружи диапазона — clamp на ``SCORE_MIN`` / ``SCORE_MAX``.

    >>> from apexcore.application.winsat_scoring import score_from_metric, _Point
    >>> pts = (_Point(100, 1.0), _Point(200, 2.0), _Point(800, 5.0))
    >>> score_from_metric(50, pts)
    1.0
    >>> score_from_metric(1000, pts)
    9.9
    """
    pts = tuple(points)
    if not pts:
        raise ValueError("пустой список порогов")
    if value <= pts[0].value:
        return SCORE_MIN
    if value >= pts[-1].value:
        return SCORE_MAX

    for i in range(len(pts) - 1):
        lo, hi = pts[i], pts[i + 1]
        if lo.value <= value < hi.value:
            log_lo = log2(lo.value)
            log_hi = log2(hi.value)
            t = (log2(value) - log_lo) / (log_hi - log_lo)
            interp = lo.score + t * (hi.score - lo.score)
            return max(SCORE_MIN, min(SCORE_MAX, interp))
    return SCORE_MAX


def harmonic_mean_pair(a: float, b: float) -> float:
    """Гармоническое среднее двух положительных чисел.

    Используется для CPUScore: HM(AES, SHA1). HM штрафует за дисбаланс,
    что соответствует логике Winsat «процессор настолько хорош, насколько
    хороша его слабейшая криптоподсистема».
    """
    if a <= 0 or b <= 0:
        return 0.0
    return 2.0 / (1.0 / a + 1.0 / b)


def compute_cpu_score(aes_mbps: float, sha1_mbps: float) -> WinsatSubscore:
    """Рассчитать CPUScore по AES-256 и SHA-1 throughput (MB/s)."""
    cat = _thresholds()["cpu"]
    hm = harmonic_mean_pair(aes_mbps, sha1_mbps)
    score = score_from_metric(hm, cat.points)
    return WinsatSubscore(
        category="cpu",
        metric_name=cat.metric,
        metric_value=hm,
        metric_unit=cat.unit,
        score=score,
        status=WinsatStatus.PASS,
    )


def compute_memory_score(memory_read_mbps: float) -> WinsatSubscore:
    """Рассчитать MemoryScore по пропускной способности DRAM на чтение."""
    cat = _thresholds()["memory"]
    score = score_from_metric(memory_read_mbps, cat.points)
    return WinsatSubscore(
        category="memory",
        metric_name=cat.metric,
        metric_value=memory_read_mbps,
        metric_unit=cat.unit,
        score=score,
        status=WinsatStatus.PASS,
    )


def compute_disk_score(seq_read_mbps: float, random_read_mbps: float) -> WinsatSubscore:
    """Рассчитать DiskScore = min(sequential, random) — как в Winsat."""
    seq_cat = _thresholds()["disk_sequential_read"]
    rnd_cat = _thresholds()["disk_random_read"]
    s_seq = score_from_metric(seq_read_mbps, seq_cat.points)
    s_rnd = score_from_metric(random_read_mbps, rnd_cat.points)
    final = min(s_seq, s_rnd)
    return WinsatSubscore(
        category="disk",
        metric_name=f"min(seq={seq_read_mbps:.0f},rnd={random_read_mbps:.0f})",
        metric_value=min(seq_read_mbps, random_read_mbps),
        metric_unit="MB/s",
        score=final,
        status=WinsatStatus.PASS,
        note=f"seq_score={s_seq:.2f}, random_score={s_rnd:.2f}",
    )


def compute_graphics_score(score: float, dwm_fps: float | None) -> WinsatSubscore:
    """GraphicsScore (Desktop Graphics, DWM) — берётся напрямую из winsat dwm.

    Score не пересчитывается через локальные thresholds — это «соответствие
    с native Windows», поэтому показываем ровно те цифры, что выдаёт
    `winsat dwm -xml`. Метрика — DWMFps (frames per second).
    """
    # Clamp в допустимый диапазон Pydantic-модели (1.0..9.9).
    score_clamped = max(1.0, min(9.9, float(score)))
    return WinsatSubscore(
        category="graphics",
        metric_name="dwm_assessment",
        metric_value=float(dwm_fps) if dwm_fps is not None else 0.0,
        metric_unit="FPS",
        score=score_clamped,
        status=WinsatStatus.PASS,
    )


def compute_d3d_score(score: float, vmem_bw_mb_s: float | None) -> WinsatSubscore:
    """GamingScore (DirectX 3D-производительность) — из того же winsat dwm.

    Score берётся прямо из <WinSPR>/<GamingScore>, метрика — пропускная
    способность видеопамяти (VideoMemBandwidth) в MB/s.
    """
    score_clamped = max(1.0, min(9.9, float(score)))
    return WinsatSubscore(
        category="d3d",
        metric_name="d3d_assessment",
        metric_value=float(vmem_bw_mb_s) if vmem_bw_mb_s is not None else 0.0,
        metric_unit="MB/s",
        score=score_clamped,
        status=WinsatStatus.PASS,
    )


def na_subscore(category: str, note: str = "Будет в следующем релизе") -> WinsatSubscore:
    """Заглушка для подкатегорий, которых нет в MVP (graphics, d3d)."""
    return WinsatSubscore(
        category=category,  # type: ignore[arg-type]
        metric_name="-",
        metric_value=0.0,
        metric_unit="-",
        score=SCORE_MIN,
        status=WinsatStatus.NA,
        note=note,
    )


def error_subscore(category: str, note: str) -> WinsatSubscore:
    """Заглушка для подкатегорий, которые упали с ошибкой."""
    return WinsatSubscore(
        category=category,  # type: ignore[arg-type]
        metric_name="-",
        metric_value=0.0,
        metric_unit="-",
        score=SCORE_MIN,
        status=WinsatStatus.ERROR,
        note=note,
    )


def compute_winspr_level(subs: Iterable[WinsatSubscore]) -> float:
    """WinSPRLevel = минимум всех PASS-подскоров.

    Если ни одного PASS — возвращает ``SCORE_MIN`` (1.0). NA/ERROR
    подкатегории игнорируются (как делает Windows для отсутствующих
    подсистем — например, на ВМ без D3D).
    """
    pass_scores = [s.score for s in subs if s.status == WinsatStatus.PASS]
    if not pass_scores:
        return SCORE_MIN
    return min(pass_scores)


__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "compute_cpu_score",
    "compute_disk_score",
    "compute_memory_score",
    "compute_winspr_level",
    "error_subscore",
    "harmonic_mean_pair",
    "na_subscore",
    "score_from_metric",
]
