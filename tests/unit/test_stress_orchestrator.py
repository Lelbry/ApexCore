"""Юнит-тесты ``application/stress_orchestrator.compute_stress_verdict``."""

from __future__ import annotations

from apexcore.application.parallel_runner import ParallelStressResult
from apexcore.application.stress_orchestrator import compute_stress_verdict
from apexcore.domain.models import StressResult, ThermalStabilityResult


def _make_parallel(
    *,
    error_count: int = 0,
    cancelled: bool = False,
) -> ParallelStressResult:
    return ParallelStressResult(
        started_at=0.0,
        finished_at=10.0,
        duration_actual_sec=10.0,
        results=[
            StressResult(
                engine="fake",
                category="cpu_fp",
                duration_actual_sec=10.0,
                throughput=1.0,
                throughput_unit="ops/s",
                error_count=error_count,
            )
        ],
        cancelled=cancelled,
    )


def _make_thermal(
    *,
    stab_pct: float | None = 99.0,
    temp_avg: float | None = 70.0,
) -> ThermalStabilityResult:
    return ThermalStabilityResult(
        frame_rate_stability_pct=stab_pct,
        pass_threshold_97=stab_pct is not None and stab_pct >= 97.0,
        clock_min_mhz=4000.0 if stab_pct is not None else None,
        clock_max_mhz=4040.0 if stab_pct is not None else None,
        temp_max_c=temp_avg,
        temp_avg_c=temp_avg,
        samples=120,
    )


def test_pass_when_all_criteria_ok():
    verdict = compute_stress_verdict(
        parallel=_make_parallel(error_count=0),
        thermal=_make_thermal(stab_pct=99.0, temp_avg=72.0),
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is True
    assert verdict.sub_results["no_watchdog_trigger"] is True
    assert verdict.sub_results["no_verify_errors"] is True
    assert verdict.sub_results["freq_stability_97"] is True
    assert verdict.sub_results["avg_temp_safe"] is True


def test_fail_on_watchdog():
    verdict = compute_stress_verdict(
        parallel=_make_parallel(),
        thermal=_make_thermal(),
        watchdog_triggered=True,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is False
    assert "watchdog" in verdict.reason.lower()


def test_fail_on_verify_errors():
    verdict = compute_stress_verdict(
        parallel=_make_parallel(error_count=3),
        thermal=_make_thermal(),
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is False
    assert "verify" in verdict.reason.lower() or "ошибок" in verdict.reason.lower()


def test_low_freq_stability_does_not_fail_verdict():
    """Низкая частотная стабильность больше НЕ блокирует PASS.

    На гетерогенных Intel ``min(cpu_avg)/max(cpu_avg)`` систематически
    даёт ~50% (P-core boost vs E-core base + idle states) при отсутствии
    реального throttle. См. docstring `compute_stress_verdict`.
    Stability остаётся информационным sub-результатом, но не блокирует
    PASS — для оценки термальной стабильности есть r_thermal в
    стресс-балле и watchdog.
    """
    verdict = compute_stress_verdict(
        parallel=_make_parallel(),
        thermal=_make_thermal(stab_pct=53.0),
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is True
    # Sub-результат сохранён для информации.
    assert verdict.sub_results.get("freq_stability_97") is False


def test_fail_on_high_avg_temp():
    verdict = compute_stress_verdict(
        parallel=_make_parallel(),
        thermal=_make_thermal(stab_pct=99.0, temp_avg=95.0),
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is False


def test_fail_on_cancelled():
    verdict = compute_stress_verdict(
        parallel=_make_parallel(cancelled=True),
        thermal=_make_thermal(),
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is False
    assert "отменён" in verdict.reason.lower()


def test_pass_when_thermal_data_missing():
    """Если thermal-данных нет (None pct, None avg) — не штрафуем."""
    verdict = compute_stress_verdict(
        parallel=_make_parallel(),
        thermal=ThermalStabilityResult(),  # все поля None
        watchdog_triggered=False,
        watchdog_tjmax_c=100.0,
    )
    assert verdict.passed is True
