"""Weights profile: загрузка профилей весов для scoring v2.

Спецификация: ``docs/scoring_v2.md`` §5 и §6.

Профили лежат в ``src/apexcore/data/weights/<name>.yaml`` (включаются в пакет
через ``[tool.setuptools.package-data]`` в pyproject.toml). Дефолт — equal
weights во всей иерархии.

При смене весов профиля поднимается major-версия apexcore (см. §8 спецификации:
смена весов = breaking change).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class WeightsProfile(BaseModel):
    """Профиль весов для scoring v2."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Имя профиля (соответствует имени YAML без расширения).")
    version: str = Field(default="1.0.0", description="Семантическая версия профиля.")
    description: str = Field(default="", description="Свободное описание для отчёта.")
    created_at: str = Field(default="", description="Дата создания (ISO 8601 или произвольная).")
    source: str = Field(
        default="default-equal",
        description="Метод определения весов: default-equal, AHP-saaty, user-custom, etc.",
    )
    subsystem_weights: dict[str, float] = Field(
        ...,
        description=(
            "Веса между подсистемами. Ключи: 'R_MEM', 'R_CPU_compute'. "
            "Значения нормируются к сумме=1 при использовании."
        ),
    )
    compute_category_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Веса между категориями внутри R_CPU_compute. Ключи: r_flops, r_integer, "
            "r_crypto, r_fractal."
        ),
    )


# ─── Загрузка ────────────────────────────────────────────────────────────────


_BUNDLED_DIR_NAME = "weights"


def _bundled_path(name: str) -> Path:
    """Путь к профилю в installed package."""
    files = resources.files("apexcore").joinpath("data", _BUNDLED_DIR_NAME, f"{name}.yaml")
    return Path(str(files))


def load_weights(name: str = "default", search_paths: list[Path] | None = None) -> WeightsProfile:
    """Загрузить профиль весов по имени.

    Алгоритм поиска:
    1. Если ``search_paths`` задан — искать ``<path>/<name>.yaml`` в каждом.
    2. Иначе — bundled путь ``apexcore/data/weights/<name>.yaml``.

    Бросает ``FileNotFoundError`` если файл не найден, ``ValueError`` если YAML
    некорректен.
    """
    paths_to_try: list[Path] = []
    if search_paths:
        paths_to_try.extend(p / f"{name}.yaml" for p in search_paths)
    paths_to_try.append(_bundled_path(name))

    for path in paths_to_try:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"weights profile {path} must be a YAML mapping")
        try:
            return WeightsProfile(**raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid weights profile {path}: {exc}") from exc

    raise FileNotFoundError(
        f"Weights profile '{name}' not found. Tried: {[str(p) for p in paths_to_try]}"
    )


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Нормализовать веса так, чтобы сумма была равна 1.0.

    Если сумма <=0 — все ключи получают равные веса. Используется внутри
    geomean_score для корректного применения w_i / Σw_i.
    """
    total = sum(weights.values())
    if total <= 0:
        n = len(weights) or 1
        return dict.fromkeys(weights, 1.0 / n)
    return {k: v / total for k, v in weights.items()}


__all__ = [
    "WeightsProfile",
    "load_weights",
    "normalize_weights",
]
