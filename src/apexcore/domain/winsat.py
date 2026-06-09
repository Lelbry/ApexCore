"""Pydantic-модели для аналога Windows Winsat.

Модуль реализует «пятый функциональный режим» apexcore — оценку компьютера
по шкале 1.0–9.9, точно имитирующей Get-CimInstance Win32_Winsat. Шкала
независима от scoring v2 (1000-балльной Roofline) — Winsat-модели живут в
этом отдельном файле, чтобы не пересекаться с публичным контрактом
``domain/models.py``.

Пять подкатегорий (как у Windows):
- ``cpu``       — гармоническое среднее AES-256 + SHA-1 (MB/s)
- ``memory``    — пропускная способность DRAM на чтение (MB/s)
- ``disk``      — min(sequential_read, random_read), MB/s
- ``graphics``  — Desktop Graphics (MVP: N/A)
- ``d3d``       — DirectX Gaming Graphics (MVP: N/A)

Итог — ``WinSPRLevel`` = минимум всех PASS-подскоров.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from apexcore.domain.models import SystemInfo

WinsatCategory = Literal["cpu", "memory", "disk", "graphics", "d3d"]
"""Пять подсистем Win32_Winsat (gaming-graphics → 'd3d', desktop-graphics → 'graphics')."""


class WinsatStatus(str, Enum):
    """Статус подоценки в Winsat-отчёте."""

    PASS = "pass"
    NA = "na"
    ERROR = "error"
    NOT_SUPPORTED_ON_OS = "not_supported_on_os"


class WinsatSubscore(BaseModel):
    """Одна подкатегория Winsat-отчёта (CPU / Memory / Disk / Graphics / D3D)."""

    model_config = ConfigDict(extra="forbid")

    category: WinsatCategory = Field(..., description="Подкатегория Win32_Winsat.")
    metric_name: str = Field(
        ...,
        description="Имя замеренной метрики, например 'hm(aes_256,sha1)' или 'memory_read'.",
    )
    metric_value: float = Field(
        ..., description="Численное значение метрики (MB/s); 0.0 для NA/ERROR."
    )
    metric_unit: str = Field(..., description="Единица измерения метрики.")
    score: float = Field(
        ..., ge=1.0, le=9.9, description="Винсатовская оценка 1.0–9.9 (capped)."
    )
    status: WinsatStatus = Field(..., description="Статус подоценки.")
    note: str | None = Field(
        default=None,
        description="Пояснение (например 'Будет в следующем релизе' для NA).",
    )


class WinsatReport(BaseModel):
    """Полный отчёт Winsat-аналога — структура аналогична Win32_Winsat WMI-классу."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="UUID прогона.")
    system_info: SystemInfo = Field(..., description="Снимок аппаратной конфигурации.")
    started_at: datetime = Field(..., description="Момент начала прогона (UTC).")
    ended_at: datetime = Field(..., description="Момент окончания прогона (UTC).")
    cpu_score: WinsatSubscore = Field(..., description="CPUScore.")
    memory_score: WinsatSubscore = Field(..., description="MemoryScore.")
    disk_score: WinsatSubscore = Field(..., description="DiskScore.")
    graphics_score: WinsatSubscore = Field(
        ..., description="GraphicsScore (Desktop Graphics)."
    )
    d3d_score: WinsatSubscore = Field(..., description="D3DScore (DirectX Gaming).")
    winspr_level: float = Field(
        ...,
        ge=1.0,
        le=9.9,
        description="WinSPRLevel = минимум всех PASS-подскоров.",
    )
    cancelled: bool = Field(
        default=False, description="True, если прогон был прерван пользователем."
    )


__all__ = [
    "WinsatCategory",
    "WinsatReport",
    "WinsatStatus",
    "WinsatSubscore",
]
