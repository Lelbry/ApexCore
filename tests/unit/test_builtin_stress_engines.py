"""Юнит-тесты новых native-движков: large DGEMM, FFT-stress, large STREAM.

Все тесты используют короткий ``duration_sec=0.5`` чтобы не висеть в CI,
но дают достаточный сигнал, что движок работает: throughput > 0,
размеры рабочих массивов соответствуют ожидаемой формуле.
"""

from __future__ import annotations

from apexcore.infrastructure.stress.builtin_fft_stress import (
    BuiltinFftStressEngine,
    _next_pow2,
    _resolve_fft_n,
)
from apexcore.infrastructure.stress.builtin_large_dgemm import (
    BuiltinLargeDgemmEngine,
    _suggest_dim,
)
from apexcore.infrastructure.stress.builtin_large_stream import (
    BuiltinLargeStreamEngine,
)

# ─── BuiltinLargeDgemmEngine ────────────────────────────────────────────────


def test_suggest_dim_for_typical_l3():
    # L3 = 16 МБ → working_set 4·L3 = 64 МБ → dim ≥ √(64МБ/3/8) ≈ 1672
    n = _suggest_dim(16 * 1024 * 1024)
    assert n >= 1024
    assert n % 64 == 0
    # 3·n²·8 ≥ 4·L3
    assert 3 * n * n * 8 >= 4 * 16 * 1024 * 1024


def test_suggest_dim_floor_and_ceiling():
    # Очень маленький L3 — всё равно не меньше 1024.
    assert _suggest_dim(1 * 1024 * 1024) >= 1024
    # Очень большой L3 — не больше 4096 (потолок).
    assert _suggest_dim(2 * 1024 ** 3) <= 4096


def test_large_dgemm_quick_run():
    engine = BuiltinLargeDgemmEngine(l3_bytes=2 * 1024 * 1024)  # маленький L3 → dim=1024
    res = engine.run(duration_sec=0.5, threads=1)
    assert res.engine == "builtin_large_dgemm"
    assert res.category == "cpu_fp"
    assert res.throughput >= 0  # GFLOPS неотрицательны
    assert res.duration_actual_sec > 0
    assert "matrix_dim" in res.extra
    assert res.extra["matrix_dim"] >= 1024
    # Verify должен пройти на спокойной машине без ошибок.
    assert res.error_count == 0


def test_large_dgemm_working_set_in_extra():
    engine = BuiltinLargeDgemmEngine(l3_bytes=8 * 1024 * 1024)
    res = engine.run(duration_sec=0.5, threads=1)
    ws = res.extra["working_set_mb"]
    # Working set должен быть > L3 (8 МБ).
    assert ws > 8.0


# ─── BuiltinFftStressEngine ──────────────────────────────────────────────────


def test_next_pow2():
    assert _next_pow2(1) == 2
    assert _next_pow2(7) == 8
    assert _next_pow2(8) == 8
    assert _next_pow2(9) == 16
    assert _next_pow2(1023) == 1024


def test_resolve_fft_n_returns_pow2():
    n_small, n_large = _resolve_fft_n("large", l2_bytes=512 * 1024, l3_bytes=16 * 1024 * 1024)
    # Все размеры должны быть степенью двойки.
    assert n_small > 1 and (n_small & (n_small - 1)) == 0
    assert n_large > 1 and (n_large & (n_large - 1)) == 0
    # Large должен быть значительно больше small.
    assert n_large >= n_small


def test_fft_engine_small_quick_run():
    engine = BuiltinFftStressEngine(size="small", l2_bytes=256 * 1024, l3_bytes=4 * 1024 * 1024)
    res = engine.run(duration_sec=0.5, threads=1)
    assert res.engine == "builtin_fft_stress"
    assert res.category == "cpu_fp"
    assert res.throughput >= 0
    assert res.extra["size"] == "small"
    assert res.error_count == 0


def test_fft_engine_blend_alternates():
    engine = BuiltinFftStressEngine(size="blend", l2_bytes=256 * 1024, l3_bytes=4 * 1024 * 1024)
    res = engine.run(duration_sec=0.5, threads=1)
    assert res.extra["size"] == "blend"
    # blend режим использует и n_small, и n_large.
    assert res.extra["n_small"] != res.extra["n_large"]


# ─── BuiltinLargeStreamEngine ────────────────────────────────────────────────


def test_large_stream_quick_run():
    # Принудительно фиксируем размер 256 МБ (минимум) — иначе тест может съесть много RAM.
    engine = BuiltinLargeStreamEngine(size_mb=256)
    res = engine.run(duration_sec=0.5, threads=1)
    assert res.engine == "builtin_large_stream"
    assert res.category == "ram_bw"
    assert res.throughput > 0
    assert res.throughput_unit == "GB/s"
    assert res.extra["size_mb_per_array"] == 256
    assert res.extra["verify_every"] >= 1
    assert res.error_count == 0


def test_large_stream_size_within_bounds():
    """resolve_size_mb всегда уважает MIN/MAX даже при большой свободной памяти."""
    engine = BuiltinLargeStreamEngine()
    n = engine._resolve_size_mb()
    assert engine.MIN_SIZE_MB <= n <= engine.MAX_SIZE_MB
