"""Тесты Pydantic-моделей Winsat-аналога."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.domain.winsat import (
    WinsatCategory,
    WinsatReport,
    WinsatStatus,
    WinsatSubscore,
)


def _sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.26200",
        cpu_model="AMD Ryzen 7 5800X",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        gpu_list=["NVIDIA GeForce RTX 3070"],
        timestamp=datetime.now(timezone.utc),
    )


def _pass_subscore(category: WinsatCategory, score: float) -> WinsatSubscore:
    return WinsatSubscore(
        category=category,
        metric_name="test_metric",
        metric_value=42.0,
        metric_unit="MB/s",
        score=score,
        status=WinsatStatus.PASS,
    )


def test_subscore_score_in_range_is_valid() -> None:
    sub = _pass_subscore("cpu", 9.5)
    assert sub.score == 9.5
    assert sub.status == WinsatStatus.PASS


def test_subscore_score_above_9_9_rejected() -> None:
    with pytest.raises(ValidationError):
        WinsatSubscore(
            category="cpu",
            metric_name="m",
            metric_value=1.0,
            metric_unit="MB/s",
            score=10.0,
            status=WinsatStatus.PASS,
        )


def test_subscore_score_below_1_0_rejected() -> None:
    with pytest.raises(ValidationError):
        WinsatSubscore(
            category="cpu",
            metric_name="m",
            metric_value=1.0,
            metric_unit="MB/s",
            score=0.5,
            status=WinsatStatus.PASS,
        )


def test_subscore_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        WinsatSubscore(
            category="cpu",
            metric_name="m",
            metric_value=1.0,
            metric_unit="MB/s",
            score=5.0,
            status=WinsatStatus.PASS,
            unexpected_field=123,  # type: ignore[call-arg]
        )


def test_report_round_trip_json() -> None:
    started = datetime.now(timezone.utc)
    report = WinsatReport(
        system_info=_sys_info(),
        started_at=started,
        ended_at=started,
        cpu_score=_pass_subscore("cpu", 9.5),
        memory_score=_pass_subscore("memory", 9.5),
        disk_score=_pass_subscore("disk", 8.7),
        graphics_score=WinsatSubscore(
            category="graphics",
            metric_name="-",
            metric_value=0.0,
            metric_unit="-",
            score=1.0,
            status=WinsatStatus.NA,
            note="Будет в следующем релизе",
        ),
        d3d_score=WinsatSubscore(
            category="d3d",
            metric_name="-",
            metric_value=0.0,
            metric_unit="-",
            score=1.0,
            status=WinsatStatus.NA,
        ),
        winspr_level=8.7,
    )
    payload = report.model_dump_json()
    restored = WinsatReport.model_validate_json(payload)
    assert restored.id == report.id
    assert restored.winspr_level == 8.7
    assert restored.disk_score.score == 8.7
    assert restored.graphics_score.status == WinsatStatus.NA
