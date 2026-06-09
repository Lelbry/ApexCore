"""Автоматизированный валидационный прогон.

Сценарий:
1. Прогнать «здоровый» бенчмарк (baseline).
2. Поочерёдно применить сценарии деградации (вызов внешних shell/ps1).
3. Прогнать бенчмарк снова и провести диагностику против baseline.
4. Проверить, что ожидаемый код диагностики попал в выдачу.
5. Сохранить отчёт в JSON и распечатать accuracy.

Запускать локально на ВМ — со sudo на Linux или из админ-PowerShell на Windows.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from apexcore.application.benchmark_service import BenchmarkService
from apexcore.application.diagnostics import diagnose_run
from apexcore.domain.models import BenchmarkConfig
from apexcore.infrastructure.adapters import AdapterFactory
from apexcore.infrastructure.persistence import (
    SqliteBaselineRepository,
    SqliteResultRepository,
)
from apexcore.infrastructure.stress import build_default_registry
from apexcore.shared.config import load_settings


@dataclass
class Scenario:
    name: str
    expected_codes: list[str]
    apply_cmd: list[str] | None = None
    reset_cmd: list[str] | None = None
    sleep_after_apply: float = 5.0


def linux_scenarios() -> list[Scenario]:
    base = Path(__file__).parent / "degrade_cpu_linux.sh"
    return [
        Scenario(
            name="cpu_freq_limit",
            expected_codes=["cpu_int_degradation", "cpu_fp_degradation", "cpu_freq_unstable"],
            apply_cmd=["bash", str(base), "--max-freq", "1500MHz"],
            reset_cmd=["bash", str(base), "--reset"],
        ),
        Scenario(
            name="background_noise",
            expected_codes=["cpu_percent_degradation", "cpu_int_degradation"],
            apply_cmd=["bash", str(base), "--noise"],
            reset_cmd=["bash", str(base), "--reset"],
        ),
    ]


def windows_scenarios() -> list[Scenario]:
    base = Path(__file__).parent / "degrade_cpu_windows.ps1"
    return [
        Scenario(
            name="cpu_max_50",
            expected_codes=["cpu_int_degradation", "cpu_fp_degradation"],
            apply_cmd=["powershell", "-NoProfile", "-File", str(base), "-MaxCpu", "50"],
            reset_cmd=["powershell", "-NoProfile", "-File", str(base), "-Reset"],
        ),
        Scenario(
            name="background_noise",
            expected_codes=["cpu_percent_degradation"],
            apply_cmd=["powershell", "-NoProfile", "-File", str(base), "-Noise"],
            reset_cmd=["powershell", "-NoProfile", "-File", str(base), "-Reset"],
        ),
    ]


@dataclass
class ScenarioResult:
    name: str
    expected_codes: list[str]
    detected_codes: list[str] = field(default_factory=list)
    passed: bool = False
    run_id: str | None = None


def run_bench(profile: str, duration: float) -> str:
    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)
    baseline_repo = SqliteBaselineRepository(settings.db_path)
    adapter = AdapterFactory.detect()
    registry = build_default_registry()
    service = BenchmarkService(adapter, registry, repo, baseline_repo)
    cfg = BenchmarkConfig(profile_name=profile, duration_sec=duration)
    result = service.run(cfg)
    return str(result.id)


def main() -> int:
    profile = "balanced"
    duration = 30.0

    sys_name = platform.system().lower()
    scenarios = linux_scenarios() if sys_name == "linux" else windows_scenarios()

    print(f"[1/{len(scenarios) + 1}] Прогон baseline ({profile}, {duration:.0f}s)…")
    baseline_id = run_bench(profile, duration)
    print(f"    baseline UUID: {baseline_id}")

    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)
    baseline_run = repo.get(baseline_id)
    if baseline_run is None:
        print("Не удалось прочитать baseline; прерываю", file=sys.stderr)
        return 1

    results: list[ScenarioResult] = []
    for i, sc in enumerate(scenarios, start=2):
        print(f"[{i}/{len(scenarios) + 1}] Сценарий '{sc.name}'")
        if sc.apply_cmd:
            try:
                subprocess.run(sc.apply_cmd, check=False)
            except FileNotFoundError as e:
                print(f"    apply_cmd недоступен: {e}; пропускаю")
                results.append(
                    ScenarioResult(name=sc.name, expected_codes=sc.expected_codes, passed=False)
                )
                continue
        time.sleep(sc.sleep_after_apply)
        run_id = run_bench(profile, duration)
        cur = repo.get(run_id)
        if cur is None:
            print("    не удалось прочитать прогон; пропускаю")
            results.append(
                ScenarioResult(name=sc.name, expected_codes=sc.expected_codes, passed=False)
            )
            continue
        diags = diagnose_run(cur, baseline_run=baseline_run)
        codes = [d.code for d in diags]
        passed = any(code in codes for code in sc.expected_codes)
        results.append(
            ScenarioResult(
                name=sc.name,
                expected_codes=sc.expected_codes,
                detected_codes=codes,
                passed=passed,
                run_id=run_id,
            )
        )
        print(f"    detected: {codes}  →  {'OK' if passed else 'MISS'}")
        if sc.reset_cmd:
            try:
                subprocess.run(sc.reset_cmd, check=False)
            except FileNotFoundError:
                pass

    accuracy = sum(1 for r in results if r.passed) / len(results) if results else 0.0
    report = {
        "baseline_run_id": baseline_id,
        "profile": profile,
        "duration_sec": duration,
        "scenarios": [r.__dict__ for r in results],
        "accuracy": accuracy,
    }
    out = Path("validation_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОбщая точность детекции: {accuracy*100:.1f}%")
    print(f"Отчёт: {out.resolve()}")
    return 0 if accuracy >= 0.5 else 2


if __name__ == "__main__":
    sys.exit(main())
