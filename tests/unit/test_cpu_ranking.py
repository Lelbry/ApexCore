"""Юнит-тесты для CPU ranking module (`apexcore.application.cpu_ranking`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apexcore.application import cpu_ranking
from apexcore.application.cpu_ranking import (
    CpuRankingEntry,
    CpuRankingSet,
    load_cpu_ranking,
    match_cpu_ranking,
)
from apexcore.domain.models import CpuCores


@pytest.fixture(autouse=True)
def _reset_ranking_cache():
    """Перед/после каждого теста сбрасываем кеш загрузки."""
    cpu_ranking.reset_cache()
    yield
    cpu_ranking.reset_cache()


# ─── Загрузка реального YAML ────────────────────────────────────────────────


def test_yaml_loads_and_validates():
    rs = load_cpu_ranking()
    assert rs.schema_id == "cpu-ranking-v1"
    assert len(rs.cpus) >= 20
    ids = [e.id for e in rs.cpus]
    assert "intel-i9-12900k" in ids


def test_load_is_cached():
    a = load_cpu_ranking()
    b = load_cpu_ranking()
    assert a is b


# ─── Pydantic-валидация ──────────────────────────────────────────────────────


def test_yaml_rejects_wrong_schema_id():
    with pytest.raises(ValidationError):
        CpuRankingSet.model_validate({
            "schema_id": "cpu-ranking-v2",
            "cpus": [],
        })


def test_yaml_rejects_duplicate_ids():
    payload = {
        "schema_id": "cpu-ranking-v1",
        "cpus": [
            {
                "id": "x", "display_name": "X", "cpu_pattern": "x",
                "physical_cores": 8, "logical_threads": 16,
                "single_score": 1000, "multi_score": 10000,
            },
            {
                "id": "x", "display_name": "X2", "cpu_pattern": "y",
                "physical_cores": 8, "logical_threads": 16,
                "single_score": 1100, "multi_score": 11000,
            },
        ],
    }
    with pytest.raises(ValidationError):
        CpuRankingSet.model_validate(payload)


def test_entry_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CpuRankingEntry(
            id="x", display_name="X", cpu_pattern="x",
            physical_cores=8, logical_threads=16,
            single_score=1000, multi_score=10000,
            extra_field="oops",  # type: ignore[call-arg]
        )


# ─── Матчинг: exact ─────────────────────────────────────────────────────────


def test_match_exact_substring():
    m = match_cpu_ranking(
        "Intel(R) Core(TM) i9-12900K CPU @ 3.20GHz",
        CpuCores(physical=16, logical=24, p_cores=8, e_cores=8),
    )
    assert m.kind == "exact"
    assert m.entry is not None
    assert m.entry.id == "intel-i9-12900k"
    assert m.multi_rank is not None and m.multi_rank >= 1
    assert m.multi_percentile is not None
    assert 0 < m.multi_percentile <= 100


def test_match_exact_normalization_variants():
    """Разные написания одной модели должны давать один и тот же entry."""
    expected = "intel-i9-12900k"
    for raw in [
        "Intel(R) Core(TM) i9-12900K CPU @ 3.20GHz",
        "Intel  Core    i9-12900K",
        "intel core i9-12900k",
    ]:
        m = match_cpu_ranking(raw, None)
        assert m.kind == "exact", f"failed for {raw!r}"
        assert m.entry is not None
        assert m.entry.id == expected


def test_match_exact_picks_longer_pattern(monkeypatch: pytest.MonkeyPatch):
    """При нескольких совпадениях выбираем самый длинный pattern (специфичнее)."""
    fake_set = CpuRankingSet(
        schema_id="cpu-ranking-v1",
        cpus=[
            CpuRankingEntry(
                id="generic",
                display_name="Generic 12900",
                cpu_pattern="12900",
                physical_cores=16,
                logical_threads=24,
                single_score=1000,
                multi_score=20000,
            ),
            CpuRankingEntry(
                id="specific",
                display_name="Intel Core i9-12900K",
                cpu_pattern="i9-12900k",
                physical_cores=16,
                p_cores=8,
                e_cores=8,
                logical_threads=24,
                single_score=2000,
                multi_score=27000,
            ),
        ],
    )
    monkeypatch.setattr(cpu_ranking, "_RANKING_CACHE", fake_set)
    m = match_cpu_ranking("intel core i9-12900k cpu @ 3.20ghz", None)
    assert m.kind == "exact"
    assert m.entry is not None
    assert m.entry.id == "specific"


# ─── Матчинг: approx_cores ──────────────────────────────────────────────────


def test_match_approx_by_cores_hybrid_to_hybrid():
    m = match_cpu_ranking(
        "Some Unknown Hybrid CPU 16C 8P 8E",
        CpuCores(physical=16, logical=24, p_cores=8, e_cores=8),
    )
    assert m.kind == "approx_cores"
    assert m.entry is not None
    # Ближайший hybrid с 16/8/8 в базе — i9-12900K или i7-13700K (дистанция 0).
    assert m.entry.id in {"intel-i9-12900k", "intel-i7-13700k"}
    assert m.core_distance is not None and m.core_distance <= 4


def test_match_approx_excludes_topology_mismatch(monkeypatch: pytest.MonkeyPatch):
    """Hybrid и non-hybrid не должны сопоставляться даже при близких числах ядер."""
    fake_set = CpuRankingSet(
        schema_id="cpu-ranking-v1",
        cpus=[
            CpuRankingEntry(
                id="hybrid",
                display_name="Hybrid 8/8",
                cpu_pattern="hybrid-only",
                physical_cores=16,
                p_cores=8,
                e_cores=8,
                logical_threads=24,
                single_score=1000,
                multi_score=20000,
            ),
        ],
    )
    monkeypatch.setattr(cpu_ranking, "_RANKING_CACHE", fake_set)
    # User non-hybrid (нет p_cores/e_cores): не должен матчиться с hybrid-only.
    m = match_cpu_ranking(
        "AMD Ryzen 7 5800X",
        CpuCores(physical=8, logical=16),
    )
    assert m.kind == "none"


def test_match_approx_respects_max_distance(monkeypatch: pytest.MonkeyPatch):
    """Если ближайший CPU слишком далёкий по топологии — kind=none."""
    fake_set = CpuRankingSet(
        schema_id="cpu-ranking-v1",
        cpus=[
            CpuRankingEntry(
                id="huge",
                display_name="64-core monster",
                cpu_pattern="monster",
                physical_cores=64,
                logical_threads=128,
                single_score=2000,
                multi_score=120000,
            ),
        ],
    )
    monkeypatch.setattr(cpu_ranking, "_RANKING_CACHE", fake_set)
    m = match_cpu_ranking(
        "Some 6-core CPU",
        CpuCores(physical=6, logical=12),
    )
    assert m.kind == "none"


# ─── Матчинг: none ──────────────────────────────────────────────────────────


def test_match_none_empty_input():
    m = match_cpu_ranking("", None)
    assert m.kind == "none"
    assert m.entry is None
    assert m.total > 0


def test_match_none_empty_database(monkeypatch: pytest.MonkeyPatch):
    fake_set = CpuRankingSet(schema_id="cpu-ranking-v1", cpus=[])
    monkeypatch.setattr(cpu_ranking, "_RANKING_CACHE", fake_set)
    m = match_cpu_ranking("Anything", CpuCores(physical=8, logical=16))
    assert m.kind == "none"
    assert m.total == 0


# ─── Percentile ─────────────────────────────────────────────────────────────


def test_percentile_calculation():
    """5-й из 30 → ceil(5/30*100) = ceil(16.67) = 17."""
    assert cpu_ranking._percentile_for_rank(5, 30) == 17


def test_percentile_first_place_floor():
    """1-й из 30 даёт ceil(1/30*100) = 4, и floor 1 гарантирует ≥ 1."""
    assert cpu_ranking._percentile_for_rank(1, 30) == 4


def test_percentile_last_place():
    assert cpu_ranking._percentile_for_rank(30, 30) == 100


def test_match_ranks_independent_per_metric(monkeypatch: pytest.MonkeyPatch):
    """multi_rank и single_rank считаются независимо."""
    fake_set = CpuRankingSet(
        schema_id="cpu-ranking-v1",
        cpus=[
            CpuRankingEntry(
                id="multi-king",
                display_name="Multi King",
                cpu_pattern="multi-king",
                physical_cores=32,
                logical_threads=64,
                single_score=1500,
                multi_score=50000,
            ),
            CpuRankingEntry(
                id="single-king",
                display_name="Single King",
                cpu_pattern="single-king",
                physical_cores=4,
                logical_threads=8,
                single_score=2500,
                multi_score=10000,
            ),
        ],
    )
    monkeypatch.setattr(cpu_ranking, "_RANKING_CACHE", fake_set)
    m = match_cpu_ranking("single-king", None)
    assert m.kind == "exact"
    assert m.single_rank == 1
    assert m.multi_rank == 2
