"""Тесты для блока «Положение среди популярных CPU» в карточке Single/Multi."""

from __future__ import annotations

import pytest
from rich.console import Console

from apexcore.application.cpu_ranking import CpuRankingEntry, RankingMatch
from apexcore.domain.models import MicroBenchResult, SingleMultiResult
from apexcore.interfaces.cli import render as render_mod


def _make_result() -> SingleMultiResult:
    single = MicroBenchResult(
        name="int_iops_64", category="integer", value=17.0,
        unit="GIOPS", duration_actual_sec=5.0, iterations=10, threads=1,
    )
    multi = MicroBenchResult(
        name="int_iops_64", category="integer", value=311.0,
        unit="GIOPS", duration_actual_sec=5.0, iterations=240, threads=24,
    )
    return SingleMultiResult(
        bench_name="int_iops_64",
        duration_sec_per_test=5.0,
        single=single,
        multi=multi,
        cores_used_multi=24,
        physical_cores=16,
        physical_p_cores=8,
        physical_e_cores=8,
        pinned_cpu=0,
        pinned_kind="P-core",
    )


def _i9_12900k_entry() -> CpuRankingEntry:
    return CpuRankingEntry(
        id="intel-i9-12900k",
        display_name="Intel Core i9-12900K",
        cpu_pattern="i9-12900k",
        family="alder_lake",
        physical_cores=16,
        p_cores=8,
        e_cores=8,
        logical_threads=24,
        single_score=1985,
        multi_score=27100,
    )


def _capture(result, monkeypatch, ranking=None, width: int = 160) -> str:
    fake_console = Console(width=width, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(render_mod, "console", fake_console)
    render_mod.render_single_multi_result(result, ranking=ranking)
    return fake_console.export_text()


# ─── Обратная совместимость ─────────────────────────────────────────────────


def test_render_without_ranking_does_not_show_section(monkeypatch):
    """Без аргумента ``ranking`` секция не появляется (старое поведение)."""
    out = _capture(_make_result(), monkeypatch)
    assert "Положение среди популярных CPU" not in out


def test_render_default_arg_compatibility(monkeypatch):
    """Сигнатура совместима со старым вызовом из одного позиционного аргумента."""
    fake_console = Console(width=160, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(render_mod, "console", fake_console)
    render_mod.render_single_multi_result(_make_result())  # без ranking=
    out = fake_console.export_text()
    assert "Тест Single-Core / Multi-Core" in out


# ─── kind="exact" ──────────────────────────────────────────────────────────


def test_render_exact_match_shows_percentile(monkeypatch):
    entry = _i9_12900k_entry()
    match = RankingMatch(
        entry=entry, kind="exact", reason="exact match",
        total=30, multi_rank=5, multi_percentile=17,
        single_rank=7, single_percentile=23,
    )
    out = _capture(_make_result(), monkeypatch, ranking=match)
    assert "Положение среди популярных CPU" in out
    assert "Intel Core i9-12900K" in out
    assert "точное совпадение" in out
    assert "Топ 17%" in out
    assert "Топ 23%" in out
    assert "5 место из 30" in out
    assert "7 место из 30" in out


# ─── kind="approx_cores" ───────────────────────────────────────────────────


def test_render_approx_match_shows_distance(monkeypatch):
    entry = _i9_12900k_entry()
    match = RankingMatch(
        entry=entry, kind="approx_cores", reason="closest by cores",
        total=30, multi_rank=10, multi_percentile=34,
        single_rank=12, single_percentile=40,
        core_distance=2,
    )
    out = _capture(_make_result(), monkeypatch, ranking=match)
    assert "ближайший по ядрам" in out
    assert "расхождение 2" in out
    assert "Топ 34%" in out


# ─── kind="none" ───────────────────────────────────────────────────────────


def test_render_none_match_shows_disclaimer(monkeypatch):
    match = RankingMatch(
        entry=None, kind="none", reason="not in DB",
        total=30, multi_rank=None, multi_percentile=None,
        single_rank=None, single_percentile=None,
    )
    out = _capture(_make_result(), monkeypatch, ranking=match)
    assert "Положение среди популярных CPU" in out
    assert "не найден в публичной базе" in out
    # Сноска про ограниченность базы должна быть.
    assert "База:" in out
    assert "новые" in out.lower() or "новейшие" in out.lower() or "новые" in out


# ─── Цветовая шкала percentile ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "percentile, expected",
    [
        (1, "green"),
        (25, "green"),
        (26, "yellow"),
        (50, "yellow"),
        (75, "yellow"),
        (76, "red"),
        (100, "red"),
    ],
)
def test_percentile_color_thresholds(percentile, expected):
    assert render_mod._percentile_color(percentile) == expected
