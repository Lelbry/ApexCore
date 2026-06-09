"""CPU ranking: рейтинговое сравнение CPU пользователя с публичной выборкой.

Используется блоком «Положение среди популярных CPU» в карточке результата
теста Single-Core / Multi-Core (``render.render_single_multi_result``).

Источник данных — ``data/cpu_ranking.yaml`` (~23 десктопных CPU 2020-2024,
числа single_score/multi_score нормализованы по Cinebench R23 1T/nT из
публичных обзоров). Сами абсолютные числа в UI не показываются — только
рейтинговая позиция (топ N%, M-е место из N).

Алгоритм матчинга:
    1. exact: ``cpu_pattern`` (нормализованный) — подстрока нормализованного
       ``cpu_model``. При нескольких — выбирается самый длинный pattern.
    2. approx_cores: ближайший по топологии (физ. ядра + P/E). Hybrid и
       non-hybrid не сопоставляются (определяется по наличию p_cores/e_cores).
       Порог манхэттенской дистанции ≤ 4.
    3. none: пользователю показывается короткая подсказка + сноска.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apexcore.application._cpu_text import normalize_cpu_model
from apexcore.domain.models import CpuCores

_EXPECTED_SCHEMA_ID = "cpu-ranking-v1"


# ─── Pydantic-модели ─────────────────────────────────────────────────────────


class CpuRankingEntry(BaseModel):
    """Одна запись в публичной базе CPU."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Машинный slug (kebab-case, ASCII).")
    display_name: str = Field(..., description="Имя для показа в UI.")
    cpu_pattern: str = Field(
        ...,
        description="Нормализованная подстрока для матчинга (lowercase).",
    )
    family: str = Field(default="", description="Семейство архитектуры (для справки).")
    physical_cores: int = Field(..., ge=1, description="Физические ядра.")
    p_cores: int | None = Field(default=None, description="P-cores для hybrid Intel.")
    e_cores: int | None = Field(default=None, description="E-cores для hybrid Intel.")
    logical_threads: int = Field(..., ge=1, description="Логические потоки.")
    single_score: float = Field(..., gt=0, description="Single-thread очки.")
    multi_score: float = Field(..., gt=0, description="Multi-thread очки.")
    notes: str = Field(default="", description="Свободный комментарий.")

    @property
    def is_hybrid(self) -> bool:
        """True если запись описывает hybrid CPU (P + E ядра)."""
        return self.p_cores is not None and self.e_cores is not None


class CpuRankingSet(BaseModel):
    """Корневая структура YAML-файла с публичной базой CPU."""

    model_config = ConfigDict(extra="forbid")

    schema_id: str = Field(..., description="Версия схемы (cpu-ranking-v1).")
    version: str = Field(default="0.1.0", description="Версия датасета.")
    created_at: str = Field(default="", description="Дата сборки (ISO).")
    notes: str = Field(default="", description="Описание набора.")
    cpus: list[CpuRankingEntry] = Field(
        default_factory=list, description="Список CPU."
    )

    @model_validator(mode="after")
    def _validate_schema_and_uniqueness(self) -> CpuRankingSet:
        if self.schema_id != _EXPECTED_SCHEMA_ID:
            raise ValueError(
                f"unsupported schema_id={self.schema_id!r} "
                f"(expected {_EXPECTED_SCHEMA_ID!r})"
            )
        seen: set[str] = set()
        for entry in self.cpus:
            if entry.id in seen:
                raise ValueError(f"duplicate cpu id: {entry.id!r}")
            seen.add(entry.id)
        return self


# ─── Результат матчинга ──────────────────────────────────────────────────────


MatchKind = Literal["exact", "approx_cores", "none"]


@dataclass(frozen=True)
class RankingMatch:
    """Результат поиска CPU пользователя в публичной базе."""

    entry: CpuRankingEntry | None
    kind: MatchKind
    reason: str
    total: int  # размер базы
    multi_rank: int | None  # 1-based, None при kind="none"
    multi_percentile: int | None  # 0..100, чем меньше — тем лучше
    single_rank: int | None
    single_percentile: int | None
    core_distance: int | None = None  # для approx_cores


# ─── Загрузка YAML ───────────────────────────────────────────────────────────


_RANKING_CACHE: CpuRankingSet | None = None


def _ranking_yaml_path() -> Path:
    files = resources.files("apexcore").joinpath("data", "cpu_ranking.yaml")
    return Path(str(files))


def load_cpu_ranking() -> CpuRankingSet:
    """Загрузить и провалидировать публичную базу CPU. Кеширует результат."""
    global _RANKING_CACHE
    if _RANKING_CACHE is not None:
        return _RANKING_CACHE
    path = _ranking_yaml_path()
    if not path.exists():
        _RANKING_CACHE = CpuRankingSet(
            schema_id=_EXPECTED_SCHEMA_ID, cpus=[]
        )
        return _RANKING_CACHE
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    _RANKING_CACHE = CpuRankingSet.model_validate(raw)
    return _RANKING_CACHE


def reset_cache() -> None:
    """Сбросить кеш загрузки (для тестов)."""
    global _RANKING_CACHE
    _RANKING_CACHE = None


# ─── Алгоритм матчинга ───────────────────────────────────────────────────────


def _percentile_for_rank(rank: int, total: int) -> int:
    """Перевести 1-based rank в percentile (топ-N%, чем меньше — тем лучше).

    Rank=1 (лучший) даёт «топ 0%», но в UI показывать «топ 0%» странно;
    округляем по верхней границе, чтобы 1-й из 30 был «топ 4%».
    """
    if total <= 0 or rank < 1:
        return 0
    return max(1, math.ceil(rank / total * 100))


def _compute_ranks(
    entries: list[CpuRankingEntry], target_id: str
) -> tuple[int, int]:
    """Найти rank-и (multi, single) для CPU с заданным id."""
    by_multi = sorted(entries, key=lambda e: -e.multi_score)
    by_single = sorted(entries, key=lambda e: -e.single_score)
    multi_rank = next(
        (i + 1 for i, e in enumerate(by_multi) if e.id == target_id), 0
    )
    single_rank = next(
        (i + 1 for i, e in enumerate(by_single) if e.id == target_id), 0
    )
    return multi_rank, single_rank


def _find_exact(entries: list[CpuRankingEntry], normalized: str) -> CpuRankingEntry | None:
    """Найти exact-match: cpu_pattern — подстрока normalized. Берём самый длинный pattern."""
    candidates = [
        e for e in entries if e.cpu_pattern.lower() in normalized
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: -len(e.cpu_pattern))
    return candidates[0]


def _find_approx_cores(
    entries: list[CpuRankingEntry], cpu_cores: CpuCores, max_distance: int = 4
) -> tuple[CpuRankingEntry | None, int]:
    """Найти ближайший по топологии CPU. Hybrid vs non-hybrid не сравнимы."""
    user_hybrid = (
        cpu_cores.p_cores is not None and cpu_cores.e_cores is not None
    )
    best: CpuRankingEntry | None = None
    best_dist = max_distance + 1
    for e in entries:
        if e.is_hybrid != user_hybrid:
            continue
        dist = abs(e.physical_cores - cpu_cores.physical)
        if user_hybrid:
            # cpu_cores.p_cores / e_cores не None, проверено выше.
            dist += abs((e.p_cores or 0) - (cpu_cores.p_cores or 0))
            dist += abs((e.e_cores or 0) - (cpu_cores.e_cores or 0))
        if dist < best_dist:
            best_dist = dist
            best = e
    if best is None:
        return None, -1
    return best, best_dist


def match_cpu_ranking(
    cpu_model: str, cpu_cores: CpuCores | None = None
) -> RankingMatch:
    """Найти CPU пользователя в публичной базе и вычислить рейтинговую позицию.

    Возвращает ``RankingMatch`` всегда (с kind="none", если ничего не нашлось).
    Никогда не падает на корректных входных данных.
    """
    ranking_set = load_cpu_ranking()
    total = len(ranking_set.cpus)
    if total == 0:
        return RankingMatch(
            entry=None,
            kind="none",
            reason="база CPU пуста",
            total=0,
            multi_rank=None,
            multi_percentile=None,
            single_rank=None,
            single_percentile=None,
        )

    normalized = normalize_cpu_model(cpu_model) if cpu_model else ""

    # Exact-match по cpu_pattern.
    if normalized:
        exact = _find_exact(ranking_set.cpus, normalized)
        if exact is not None:
            multi_rank, single_rank = _compute_ranks(ranking_set.cpus, exact.id)
            return RankingMatch(
                entry=exact,
                kind="exact",
                reason=f"точное совпадение по шаблону {exact.cpu_pattern!r}",
                total=total,
                multi_rank=multi_rank,
                multi_percentile=_percentile_for_rank(multi_rank, total),
                single_rank=single_rank,
                single_percentile=_percentile_for_rank(single_rank, total),
            )

    # Approx по ядрам (только если есть cpu_cores).
    if cpu_cores is not None:
        approx, dist = _find_approx_cores(ranking_set.cpus, cpu_cores)
        if approx is not None:
            multi_rank, single_rank = _compute_ranks(
                ranking_set.cpus, approx.id
            )
            return RankingMatch(
                entry=approx,
                kind="approx_cores",
                reason=(
                    f"ближайший по топологии {approx.display_name!r} "
                    f"(расхождение {dist})"
                ),
                total=total,
                multi_rank=multi_rank,
                multi_percentile=_percentile_for_rank(multi_rank, total),
                single_rank=single_rank,
                single_percentile=_percentile_for_rank(single_rank, total),
                core_distance=dist,
            )

    return RankingMatch(
        entry=None,
        kind="none",
        reason="CPU не найден ни по шаблону, ни по топологии",
        total=total,
        multi_rank=None,
        multi_percentile=None,
        single_rank=None,
        single_percentile=None,
    )


__all__ = [
    "CpuRankingEntry",
    "CpuRankingSet",
    "RankingMatch",
    "load_cpu_ranking",
    "match_cpu_ranking",
    "reset_cache",
]
