"""Экспорт прогона в JSON."""

from __future__ import annotations

from pathlib import Path

from apexcore.domain.models import BenchmarkResult


def export_run_json(result: BenchmarkResult, out_path: Path) -> Path:
    """Сохранить весь BenchmarkResult в pretty-JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return out_path
