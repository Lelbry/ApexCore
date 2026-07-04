"""Reference module: гибрид Roofline + empirical proxy для всех 12 micro-тестов.

Спецификация: ``new-app/docs/scoring_v2.md`` §4.

Для каждого подтеста reference value определяется по приоритету:

    1. Roofline (теоретический пик архитектуры, ``roofline.py``).
    2. Empirical proxy из ``data/empirical_reference.yaml`` (для тестов без
       аналитического Roofline: fractal; и fallback для crypto без AES-NI/SHA-NI).
    3. Frozen machine reference (опционально, см. §12 в плане; в v2.0 не реализовано).

Если ни Roofline, ни empirical fallback не дали значения — workload помечается
как «skipped» в ``ReferenceSet.notes``, а вызывающий ``geomean_score`` пропускает
этот workload в агрегации.
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from apexcore.application import roofline
from apexcore.domain.models import SystemInfo

ReferenceSource = Literal["roofline", "empirical_proxy", "frozen_machine"]


class ReferenceValue(BaseModel):
    """Эталонное значение для одного workload."""

    model_config = ConfigDict(extra="forbid")

    workload_id: str = Field(..., description="Имя micro-теста (memory_read, flops_sp, ...).")
    value: float = Field(..., description="Эталонное значение в исходных единицах.")
    unit: str = Field(..., description="Единица: MB/s, GFLOPS, GIOPS, FPS.")
    source: ReferenceSource = Field(..., description="Откуда взято значение.")
    provisional: bool = Field(
        default=False,
        description="True если значение временное (empirical < 10 runs или placeholder).",
    )
    notes: str | None = Field(default=None, description="Свободный комментарий.")


class ReferenceSet(BaseModel):
    """Набор эталонов для всех 12 micro-тестов конкретной reference-машины."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Идентификатор набора (например 'roofline-i9-12900k').")
    version: str = Field(default="1.0.0", description="Версия набора.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    machine_description: str = Field(default="", description="Описание целевой машины.")
    values: dict[str, ReferenceValue] = Field(
        default_factory=dict,
        description="Ключ — workload_id (имя micro-теста).",
    )
    aggregate_notes: list[str] = Field(
        default_factory=list,
        description="Машинные пометки уровня набора: 'roofline_partial', 'fallback_used:<id>'.",
    )


# ─── Загрузка empirical YAML ─────────────────────────────────────────────────


_EMPIRICAL_CACHE: dict | None = None


def _empirical_yaml_path() -> Path:
    """Путь к empirical_reference.yaml в установленном пакете."""
    # Используем importlib.resources для совместимости с installed wheel и source layout.
    files = resources.files("apexcore").joinpath("data", "empirical_reference.yaml")
    # files.as_file() даёт реальный путь (если ресурс — на диске).
    return Path(str(files))


def load_empirical() -> dict:
    """Загрузить empirical_reference.yaml. Кеширует результат."""
    global _EMPIRICAL_CACHE
    if _EMPIRICAL_CACHE is not None:
        return _EMPIRICAL_CACHE
    path = _empirical_yaml_path()
    if not path.exists():
        _EMPIRICAL_CACHE = {"values": {}}
        return _EMPIRICAL_CACHE
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    _EMPIRICAL_CACHE = data
    return data


def _empirical_value(workload_id: str) -> ReferenceValue | None:
    """Достать ReferenceValue из empirical YAML для одного workload."""
    raw = load_empirical()
    values = raw.get("values", {}) or {}
    entry = values.get(workload_id)
    if not entry:
        return None
    try:
        return ReferenceValue(
            workload_id=entry.get("workload_id", workload_id),
            value=float(entry["value"]),
            unit=str(entry.get("unit", "")),
            source="empirical_proxy",
            provisional=bool(entry.get("provisional", True)),
            notes=entry.get("notes"),
        )
    except (KeyError, ValueError, TypeError):
        return None


# ─── Сборка ReferenceSet ─────────────────────────────────────────────────────


# Маппинг workload_id → unit (для consistency с MicroBenchResult.unit).
_WORKLOAD_UNITS: dict[str, str] = {
    "memory_read": "MB/s",
    "memory_write": "MB/s",
    "memory_copy": "MB/s",
    "flops_sp": "GFLOPS",
    "flops_dp": "GFLOPS",
    "int_iops_24": "GIOPS",
    "int_iops_32": "GIOPS",
    "int_iops_64": "GIOPS",
    "aes_256": "MB/s",
    "sha1": "MB/s",
    "julia_sp": "FPS",
    "mandelbrot_dp": "FPS",
}


def build_reference(system_info: SystemInfo) -> ReferenceSet:
    """Построить полный ReferenceSet для конкретной системы.

    Алгоритм для каждого workload:
    1. Запросить Roofline-эталон через ``roofline.get_roofline_reference``.
    2. Если Roofline дал None — попытаться empirical_proxy из YAML.
    3. Если ни того, ни другого — workload пропускается, в notes пишется
       ``workload_skipped:<id>``.

    ReferenceSet.id формируется как ``roofline-{cpu_model_short}``.
    """
    rl_values = roofline.get_roofline_reference(system_info)
    values: dict[str, ReferenceValue] = {}
    notes: list[str] = []
    fallback_count = 0

    for workload_id, unit in _WORKLOAD_UNITS.items():
        rl_value = rl_values.get(workload_id)
        if rl_value is not None and rl_value > 0:
            values[workload_id] = ReferenceValue(
                workload_id=workload_id,
                value=float(rl_value),
                unit=unit,
                source="roofline",
                provisional=False,
                notes=None,
            )
            continue

        # Попытка empirical fallback.
        emp = _empirical_value(workload_id)
        if emp is not None:
            values[workload_id] = emp
            fallback_count += 1
            notes.append(f"fallback_used:{workload_id}")
            continue

        # Совсем нет данных — пропускаем.
        notes.append(f"workload_skipped:{workload_id}")

    if fallback_count > 0:
        notes.insert(0, "roofline_partial")

    cpu_short = (
        system_info.cpu_model.split()[0:3]  # первые 3 слова
        if system_info.cpu_model
        else ["unknown"]
    )
    ref_id = "roofline-" + "-".join(cpu_short).lower().replace("(", "").replace(")", "")

    return ReferenceSet(
        id=ref_id,
        version="1.0.0",
        machine_description=f"Roofline-derived for {system_info.cpu_model}",
        values=values,
        aggregate_notes=notes,
    )


__all__ = [
    "ReferenceSet",
    "ReferenceSource",
    "ReferenceValue",
    "build_reference",
    "load_empirical",
]
