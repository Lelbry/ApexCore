"""Pydantic-модель отчёта «Оценок общей производительности».

Шкала ×10 000, формула GM(r_dgemm, r_stream, r_disk) — без термальной
стабильности (в отличие от стресс-балла). Спецификация:
``docs/general_benchmark.md``.

Шкала, ratio и компоненты отличны от Winsat (1.0–9.9) и от scoring v2
(×1000), поэтому модель живёт в отдельном файле и не пересекается с
публичным контрактом ``domain/models.py``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from apexcore.domain.models import SystemInfo


class GeneralBenchmarkReport(BaseModel):
    """Полный отчёт комплексного бенчмарка (CPU + RAM + Boot-диск).

    Сохраняется как JSON в таблице ``general_benchmark_runs``. Все поля
    кроме идентификаторов опциональны: если этап не выполнился
    (нет roofline, нет места на C:, движок упал) — соответствующее поле
    остаётся ``None``, ``score`` тогда тоже ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="UUID прогона.")
    system_info: SystemInfo = Field(..., description="Снимок системы.")
    started_at: datetime = Field(..., description="Момент начала прогона (UTC).")
    ended_at: datetime = Field(..., description="Момент окончания прогона (UTC).")

    # Длительности фаз (фактические).
    dgemm_duration_sec: float = Field(default=0.0)
    stream_duration_sec: float = Field(default=0.0)
    disk_seq_read_duration_sec: float = Field(default=0.0)
    disk_random_read_duration_sec: float = Field(default=0.0)
    disk_seq_write_duration_sec: float = Field(default=0.0)

    # Измерения.
    dgemm_gflops: float | None = Field(default=None)
    stream_gb_s: float | None = Field(default=None)
    disk_seq_read_mb_s: float | None = Field(default=None)
    disk_random_read_mb_s: float | None = Field(default=None)
    disk_seq_write_mb_s: float | None = Field(default=None)

    # Roofline / disk пики.
    dgemm_peak_gflops: float | None = Field(default=None)
    stream_peak_gb_s: float | None = Field(default=None)
    disk_seq_read_peak_mb_s: float | None = Field(default=None)
    disk_random_read_peak_mb_s: float | None = Field(default=None)
    disk_seq_write_peak_mb_s: float | None = Field(default=None)

    # Ratio (после clamp ≤1.0).
    r_dgemm: float | None = Field(default=None)
    r_stream: float | None = Field(default=None)
    r_disk: float | None = Field(default=None)

    # Финальный балл в шкале ×10 000.
    score: float | None = Field(default=None)

    # Контекст загрузочного диска.
    boot_drive_path: str | None = Field(default=None)
    disk_model: str | None = Field(default=None)
    disk_media_type: str | None = Field(default=None)
    disk_bus_type: str | None = Field(default=None)
    disk_media_label: str | None = Field(default=None)

    # Notes / warnings — короткие человекочитаемые сообщения.
    notes: list[str] = Field(default_factory=list)
    cancelled: bool = Field(default=False)


__all__ = ["GeneralBenchmarkReport"]
