"""Тесты реестра стресс-движков и встроенного CPU-движка (быстрый прогон)."""

from __future__ import annotations

from apexcore.infrastructure.stress import build_default_registry
from apexcore.infrastructure.stress.builtin_cpu import BuiltinCpuIntEngine
from apexcore.infrastructure.stress.registry import profile_engines


def test_registry_contains_builtin_engines():
    reg = build_default_registry()
    names = {e.name for e in reg.all()}
    for required in (
        "builtin_cpu_int",
        "builtin_cpu_fp",
        "builtin_ram_bw",
        "builtin_ram_lat",
    ):
        assert required in names


def test_builtin_engines_are_available():
    reg = build_default_registry()
    avail = {e.name for e in reg.available()}
    assert "builtin_cpu_int" in avail
    assert "builtin_cpu_fp" in avail


def test_profile_engines_returns_only_available():
    reg = build_default_registry()
    engines = profile_engines("cpu_heavy", reg)
    assert len(engines) >= 1
    assert all(e.is_available() for e in engines)


def test_builtin_cpu_int_quick_run():
    engine = BuiltinCpuIntEngine()
    # Очень короткий прогон, чтобы тесты не висели; numba-прогрев допустим.
    res = engine.run(duration_sec=0.5, threads=1)
    assert res.engine == "builtin_cpu_int"
    assert res.category == "cpu_int"
    assert res.throughput > 0
    assert res.duration_actual_sec > 0
