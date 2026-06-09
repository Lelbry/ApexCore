"""Модели для теста «Расширенный тест ОЗУ и кеша (Ram&Cache)».

Метрики Read / Write / Copy / Latency для четырёх уровней иерархии памяти —
DRAM, L1, L2, L3. Тест диагностический, не входит в общий балл (scoring v2).
См. docs/ram_cache.md.

Зависимости только pydantic; никакой инфраструктуры.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apexcore.domain.models import SystemInfo

LevelName = Literal["L1", "L2", "L3", "DRAM"]
"""Уровни иерархии памяти, используемые в тесте."""

OperationName = Literal["read", "write", "copy", "latency"]
"""Четыре измеряемые операции теста Ram&Cache."""

UnitName = Literal["MB/s", "ns"]
"""Единицы: пропускная способность (MB/s) или задержка (ns)."""

BackendName = Literal["numba", "numpy"]
"""Бэкенд исполнения внутреннего цикла."""

CacheSizeSource = Literal["wmi", "sysfs", "fallback"]
"""Источник размеров кеша: WMI (Windows), sysfs (Linux) или дефолтный fallback."""


class CacheLevel(BaseModel):
    """Описание одного уровня иерархии памяти."""

    model_config = ConfigDict(extra="forbid")

    name: LevelName = Field(..., description="Имя уровня: L1, L2, L3 или DRAM.")
    size_bytes: int = Field(
        ...,
        description="Размер в байтах. Для DRAM — размер тестового буфера, не реального RAM.",
    )
    source: CacheSizeSource = Field(
        ...,
        description="Откуда взято значение: wmi (Win32_Processor), sysfs (/sys/.../cache), fallback.",
    )


class CacheTopology(BaseModel):
    """Полный набор уровней памяти, обнаруженный на машине.

    Всегда содержит четыре уровня (L1, L2, L3, DRAM). Если уровень не
    обнаружен — заполняется значением из дефолтного fallback с пометкой
    ``source="fallback"``.
    """

    model_config = ConfigDict(extra="forbid")

    levels: list[CacheLevel] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="Уровни в порядке L1, L2, L3, DRAM.",
    )


class RamCacheMetric(BaseModel):
    """Одно значение в матрице 4×4 (один уровень × одна операция)."""

    model_config = ConfigDict(extra="forbid")

    level: LevelName = Field(..., description="Уровень иерархии памяти.")
    operation: OperationName = Field(..., description="Тип операции.")
    value: float = Field(
        ...,
        description="Значение метрики: MB/s для read/write/copy, ns для latency.",
    )
    unit: UnitName = Field(..., description="Единица измерения.")
    backend: BackendName = Field(
        ...,
        description="Какой бэкенд использовался во внутреннем цикле.",
    )
    duration_actual_sec: float = Field(
        ..., description="Фактическая длительность измерения, с."
    )
    iterations: int = Field(default=1, description="Сколько итераций успело пройти.")
    error: str | None = Field(
        default=None,
        description="Если измерение не удалось — текст ошибки. Иначе None.",
    )


class RamCacheReport(BaseModel):
    """Полный результат прогона расширенного теста ОЗУ и кеша."""

    model_config = ConfigDict(extra="forbid")

    system_info: SystemInfo = Field(
        ..., description="Снимок системы на момент прогона."
    )
    topology: CacheTopology = Field(
        ..., description="Уровни памяти и источники размеров."
    )
    metrics: list[RamCacheMetric] = Field(
        default_factory=list,
        description="16 значений: 4 уровня × 4 операции.",
    )
    started_at: datetime = Field(..., description="Время начала прогона.")
    ended_at: datetime = Field(..., description="Время окончания прогона.")
    duration_sec_per_metric: float = Field(
        ..., description="Запрошенная длительность на одно измерение, с."
    )
    backend_default: BackendName = Field(
        ..., description="Бэкенд по умолчанию (numba если был доступен, иначе numpy)."
    )
    cancelled: bool = Field(
        default=False,
        description="True, если пользователь прервал прогон Ctrl+C.",
    )
