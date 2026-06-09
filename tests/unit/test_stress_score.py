"""Тесты `application/stress_score` (4-компонентный балл с r_thermal).

Главные инварианты:
1. ``compute_stress_score`` — pure (одинаковые входы → одинаковый выход).
2. Если хоть один из четырёх ratio = None → балл = None (не вводим в заблуждение).
3. ``compute_r_thermal`` — таблица граничных случаев из research §6.2.
4. ``compute_stress_score_context`` под фиксированными env-vars даёт
   воспроизводимый результат — это «детерминированность для идентичных
   серверов», заявленная в `docs/stress_score.md`.
5. Гейт MIN_DURATION_FOR_SCORE_SEC (90 сек) — короче прогон → r_thermal=None
   → балл недоступен (research §8.3).

Используется CPU-модель **i7-7700K** (Kaby Lake, не-гибрид) чтобы изолировать
тесты от P+E эвристики `_detect_hybrid_topology` — арифметика остаётся
простой `cores × ops × clock`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from apexcore.application.parallel_runner import ParallelStressResult
from apexcore.application.stress_score import (
    RELIABLE_DURATION_SEC,
    STRESS_SCORE_SCALE,
    compute_r_thermal,
    compute_stress_score,
    compute_stress_score_context,
)
from apexcore.domain.models import (
    CpuCores,
    StressResult,
    SystemInfo,
    ThermalStabilityResult,
)

# Заведомо ≥ RELIABLE_DURATION_SEC, чтобы тесты, проверяющие свойства балла,
# не путались с warning «приближённая оценка». На сам расчёт гейта больше нет.
LONG_ENOUGH = RELIABLE_DURATION_SEC + 30.0


# ─── Pure: compute_stress_score ────────────────────────────────────────────


def test_score_full_inputs():
    # GM(0.5, 0.4, 0.95, 1.0) ≈ 0.6817 → 6817 при шкале ×10000.
    score = compute_stress_score(0.5, 0.4, 0.95, 1.0)
    assert score is not None
    assert math.isclose(score, STRESS_SCORE_SCALE * math.exp(
        (math.log(0.5) + math.log(0.4) + math.log(0.95) + math.log(1.0)) / 4.0
    ))


def test_score_none_if_any_missing():
    assert compute_stress_score(None, 0.4, 0.95, 1.0) is None
    assert compute_stress_score(0.5, None, 0.95, 1.0) is None
    assert compute_stress_score(0.5, 0.4, None, 1.0) is None
    assert compute_stress_score(0.5, 0.4, 0.95, None) is None


def test_score_none_if_any_zero():
    # GM не определён при нуле — возвращаем None.
    assert compute_stress_score(0.0, 0.4, 0.95, 1.0) is None
    assert compute_stress_score(0.5, 0.0, 0.95, 1.0) is None
    assert compute_stress_score(0.5, 0.4, 0.0, 1.0) is None
    assert compute_stress_score(0.5, 0.4, 0.95, 0.0) is None


def test_score_deterministic_for_same_inputs():
    a = compute_stress_score(0.31, 0.42, 0.99, 1.05)
    b = compute_stress_score(0.31, 0.42, 0.99, 1.05)
    assert a == b


# ─── Pure: compute_r_thermal (research §6.2 boundary cases) ─────────────────


@pytest.mark.parametrize(
    "t_max,tjmax,expected",
    [
        (50.0, 100.0, 1.15),   # headroom 1.67 → cap
        (70.0, 100.0, 1.00),   # headroom 1.00 → 1.0
        (85.0, 100.0, 0.75),   # headroom 0.50 → 0.75 (warning)
        (95.0, 100.0, 0.5833),  # headroom 0.167 → 0.583 (critical)
        (100.0, 100.0, 0.50),  # headroom 0.0 → floor (throttling)
    ],
)
def test_r_thermal_boundary_cases(t_max, tjmax, expected):
    """Таблица 6.2 из docs/research/stress_test_mark_method.md."""
    r = compute_r_thermal(t_max, tjmax)
    assert r == pytest.approx(expected, abs=0.01)


def test_r_thermal_floor_clamped_below_zero_headroom():
    """T_max > TJmax (например, разогнанная система) — clamp на floor=0.50."""
    assert compute_r_thermal(110.0, 100.0) == pytest.approx(0.50)


def test_r_thermal_cap_clamped_above_high_headroom():
    """Очень холодная система (T_max = 10) — clamp на cap=1.15."""
    assert compute_r_thermal(10.0, 100.0) == pytest.approx(1.15)


# ─── compute_stress_score_context ──────────────────────────────────────────


def _make_system_info(cores: int = 8) -> SystemInfo:
    """i7-7700K = Kaby Lake (не-гибрид). Тесты независимы от P+E эвристики."""
    return SystemInfo(
        os_name="Windows",
        os_version="11",
        cpu_model="Intel(R) Core(TM) i7-7700K CPU @ 4.20GHz",
        cpu_cores=CpuCores(physical=cores, logical=cores * 2),
        ram_total_gb=32.0,
        cpu_arch="x86_64",
        timestamp=datetime.now(timezone.utc),
    )


def _make_parallel(
    dgemm: float | None = 200.0,
    stream: float | None = 25.0,
) -> ParallelStressResult:
    results = []
    if dgemm is not None:
        results.append(StressResult(
            engine="builtin-dgemm-large", category="cpu_fp",
            duration_actual_sec=LONG_ENOUGH, throughput=dgemm,
            throughput_unit="GFLOPS",
        ))
    if stream is not None:
        results.append(StressResult(
            engine="builtin-large-stream", category="ram_bw",
            duration_actual_sec=LONG_ENOUGH, throughput=stream,
            throughput_unit="GB/s",
        ))
    return ParallelStressResult(
        started_at=0.0, finished_at=LONG_ENOUGH,
        duration_actual_sec=LONG_ENOUGH, results=results,
    )


def _make_thermal(
    stability_pct: float | None = 99.0,
    t_max_c: float | None = 70.0,
) -> ThermalStabilityResult:
    return ThermalStabilityResult(
        frame_rate_stability_pct=stability_pct,
        temp_max_c=t_max_c,
    )


@pytest.fixture
def fixed_env(monkeypatch):
    """Зафиксировать всё, что Roofline-функции могут читать из окружения.

    Без этого compute_flops_peak зависит от реального CPU тестера, а
    compute_dram_peak — от WMI/dmidecode. Override снимает обе зависимости
    → тесты воспроизводимы на любой машине.
    """
    monkeypatch.setenv("APEXCORE_SIMD", "avx2")
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "5.0")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")


def test_context_full_path(fixed_env):
    # cores=8, AVX2 dp=16 ops/cycle, 5 GHz → 8*16*5 = 640 GFLOPS peak.
    # DRAM: 2 × 3200 × 8 = 51200 MB/s → /1000 = 51.2 GB/s peak.
    # TJmax=100 (intel_desktop), T_max=70 → r_thermal=1.0.
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal(stability_pct=99.0, t_max_c=70.0)
    ctx = compute_stress_score_context(
        system_info=_make_system_info(cores=8),
        parallel=parallel,
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert ctx.dgemm_peak_gflops == pytest.approx(640.0)
    assert ctx.stream_peak_gb_s == pytest.approx(51.2)
    assert ctx.tjmax_c == 100
    assert ctx.t_max_c == pytest.approx(70.0)
    assert ctx.r_dgemm == pytest.approx(200.0 / 640.0)
    assert ctx.r_stream == pytest.approx(25.0 / 51.2)
    assert ctx.r_stability == pytest.approx(0.99)
    assert ctx.r_thermal == pytest.approx(1.0)
    assert ctx.stress_score is not None
    assert 0 < ctx.stress_score < STRESS_SCORE_SCALE * 1.15  # cap r_thermal


def test_context_deterministic_for_same_system(fixed_env):
    """Два прогона одной и той же конфигурации → один и тот же балл."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal()
    sys_info = _make_system_info()
    a = compute_stress_score_context(
        system_info=sys_info, parallel=parallel, thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    b = compute_stress_score_context(
        system_info=sys_info, parallel=parallel, thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert a.stress_score == b.stress_score
    assert a == b


def test_context_score_none_without_stability(fixed_env):
    """Без частотных данных балл не строится — рендер покажет «недоступна»."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal(stability_pct=None)
    ctx = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert ctx.r_stability is None
    assert ctx.stress_score is None
    # Но r_dgemm и r_stream всё ещё считаются — для Roofline-блока контекста.
    assert ctx.r_dgemm is not None
    assert ctx.r_stream is not None


def test_context_score_none_without_dgemm(fixed_env):
    parallel = _make_parallel(dgemm=None, stream=25.0)
    thermal = _make_thermal()
    ctx = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert ctx.r_dgemm is None
    assert ctx.stress_score is None


def test_context_score_none_without_temp(fixed_env):
    """Без CPU temp r_thermal=None → балл недоступен (строгий режим, research §1)."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal(t_max_c=None)
    ctx = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert ctx.t_max_c is None
    assert ctx.r_thermal is None
    assert ctx.stress_score is None
    # Остальные ratio посчитаны — рендер показывает «недоступна» с точной причиной.
    assert ctx.r_dgemm is not None
    assert ctx.r_stream is not None
    assert ctx.r_stability is not None


def test_context_score_computed_even_for_short_run(fixed_env):
    """Балл вычисляется даже при коротком прогоне < 90 сек.

    Гейт убран по запросу пользователя (2026-05-17): «даже если тест шёл
    меньше 90 секунд всё равно выведи баллы». Warning «приближённая оценка»
    показывает рендер, не сама функция расчёта.
    """
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal()
    ctx = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=thermal,
        duration_sec=30.0,  # < RELIABLE_DURATION_SEC, но не отсекаем
    )
    assert ctx.r_thermal is not None
    assert ctx.stress_score is not None
    assert ctx.duration_sec == pytest.approx(30.0)
    assert ctx.duration_sec < RELIABLE_DURATION_SEC  # признак «приближённая»


def test_context_score_none_when_tjmax_unknown(fixed_env, monkeypatch):
    """Если cpu_model не распознан в TJmax таблице — r_thermal=None."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    thermal = _make_thermal()
    unknown_info = SystemInfo(
        os_name="Windows",
        os_version="11",
        cpu_model="SomeFancyExoticChip 9000",
        cpu_cores=CpuCores(physical=8, logical=16),
        ram_total_gb=32.0,
        cpu_arch="x86_64",
        timestamp=datetime.now(timezone.utc),
    )
    ctx = compute_stress_score_context(
        system_info=unknown_info,
        parallel=parallel,
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert ctx.tjmax_c is None
    assert ctx.r_thermal is None
    assert ctx.stress_score is None


def test_context_score_changes_with_throughput(fixed_env):
    """Если throughput выше — балл выше. Базовая проверка монотонности."""
    thermal = _make_thermal()
    low = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=_make_parallel(dgemm=100.0, stream=20.0),
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    high = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=_make_parallel(dgemm=300.0, stream=40.0),
        thermal=thermal,
        duration_sec=LONG_ENOUGH,
    )
    assert low.stress_score is not None
    assert high.stress_score is not None
    assert high.stress_score > low.stress_score


def test_context_score_drops_with_throttling(fixed_env):
    """Тротлинг → падает stability → падает балл (одно из ключевых свойств)."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    healthy = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=_make_thermal(stability_pct=99.0),
        duration_sec=LONG_ENOUGH,
    )
    throttled = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=_make_thermal(stability_pct=80.0),
        duration_sec=LONG_ENOUGH,
    )
    assert healthy.stress_score is not None
    assert throttled.stress_score is not None
    assert healthy.stress_score > throttled.stress_score


def test_context_score_drops_with_high_temp(fixed_env):
    """Высокая T_max → падает r_thermal → падает балл (sustainable signal)."""
    parallel = _make_parallel(dgemm=200.0, stream=25.0)
    cool = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=_make_thermal(t_max_c=65.0),  # excellent → r_thermal=1.06
        duration_sec=LONG_ENOUGH,
    )
    hot = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=parallel,
        thermal=_make_thermal(t_max_c=95.0),  # critical → r_thermal=0.58
        duration_sec=LONG_ENOUGH,
    )
    assert cool.stress_score is not None
    assert hot.stress_score is not None
    assert cool.stress_score > hot.stress_score


def test_context_weak_cool_beats_strong_hot(fixed_env):
    """Свойство монотонности (research §3.3, §6.1): «слабая+холодная ≥ сильная+горячая».

    Поскольку r_thermal входит в GM как любой другой компонент, можно
    подобрать комбинацию, где её просадка перевешивает преимущество в
    производительности. Это базовая проверка что свойство достижимо.
    """
    # Слабая система (dgemm=160, stream=20) с очень холодным охлаждением (T_max=55).
    weak_cool = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=_make_parallel(dgemm=160.0, stream=20.0),
        thermal=_make_thermal(stability_pct=98.0, t_max_c=55.0),
        duration_sec=LONG_ENOUGH,
    )
    # Сильная система (dgemm=200, stream=25) почти на пределе охлаждения (T_max=95).
    strong_hot = compute_stress_score_context(
        system_info=_make_system_info(),
        parallel=_make_parallel(dgemm=200.0, stream=25.0),
        thermal=_make_thermal(stability_pct=92.0, t_max_c=95.0),
        duration_sec=LONG_ENOUGH,
    )
    assert weak_cool.stress_score is not None
    assert strong_hot.stress_score is not None
    assert weak_cool.stress_score >= strong_hot.stress_score
