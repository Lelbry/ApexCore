"""Юнит-тесты для reference module (`apexcore.application.references`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apexcore.application import references, roofline
from apexcore.domain.models import CpuCores, SystemInfo


def _make_sys_info(
    cpu_model: str = "Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz",
    cores: int = 8,
) -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.22621",
        cpu_model=cpu_model,
        cpu_cores=CpuCores(physical=cores, logical=cores * 2),
        ram_total_gb=32.0,
        cpu_arch="AMD64",
        timestamp=datetime.now(timezone.utc),
    )


def test_load_empirical_returns_values():
    """YAML с empirical proxy должен загружаться и иметь fractal-ключи."""
    data = references.load_empirical()
    values = data.get("values", {})
    assert "julia_sp" in values
    assert "mandelbrot_dp" in values
    assert values["julia_sp"]["value"] > 0


def test_reference_value_extra_forbidden():
    """Контракт фиксирован — extra fields отвергаются."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        references.ReferenceValue(
            workload_id="memory_read",
            value=100.0,
            unit="MB/s",
            source="roofline",
            unknown="oops",  # type: ignore[call-arg]
        )


def test_build_reference_full_machine(monkeypatch: pytest.MonkeyPatch):
    """На «нормальной» Alder Lake машине: 10 значений из Roofline + 2 fallback."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K", cores=8)

    ref = references.build_reference(info)

    # Все 12 micro-тестов должны быть представлены.
    expected_workloads = {
        "memory_read", "memory_write", "memory_copy",
        "flops_sp", "flops_dp",
        "int_iops_24", "int_iops_32", "int_iops_64",
        "aes_256", "sha1",
        "julia_sp", "mandelbrot_dp",
    }
    assert set(ref.values.keys()) == expected_workloads

    # Roofline для memory/flops/integer/crypto.
    for wid in ("memory_read", "flops_sp", "int_iops_32", "aes_256", "sha1"):
        assert ref.values[wid].source == "roofline"
        assert ref.values[wid].provisional is False
        assert ref.values[wid].value > 0

    # Empirical fallback для fractal.
    for wid in ("julia_sp", "mandelbrot_dp"):
        assert ref.values[wid].source == "empirical_proxy"
        assert ref.values[wid].provisional is True

    # aggregate_notes должны помечать партиал.
    assert "roofline_partial" in ref.aggregate_notes
    assert "fallback_used:julia_sp" in ref.aggregate_notes
    assert "fallback_used:mandelbrot_dp" in ref.aggregate_notes


def test_build_reference_arm_skips_workloads(monkeypatch: pytest.MonkeyPatch):
    """На ARM Roofline недоступен; empirical fallback есть только для 4 тестов
    (julia, mandelbrot, aes, sha) — остальные пропускаются."""
    monkeypatch.delenv("APEXCORE_CPU_GHZ", raising=False)
    monkeypatch.delenv("APEXCORE_DRAM_MTS", raising=False)
    monkeypatch.delenv("APEXCORE_DRAM_MODULES", raising=False)
    monkeypatch.setattr(roofline, "_read_dram_speed_mts_windows", lambda: None)
    monkeypatch.setattr(roofline, "_read_dram_speed_mts_linux", lambda: None)

    info = SystemInfo(
        os_name="Linux",
        os_version="6.0",
        cpu_model="Apple M2 Pro",
        cpu_cores=CpuCores(physical=8, logical=8),
        ram_total_gb=16.0,
        cpu_arch="arm64",
        timestamp=datetime.now(timezone.utc),
    )
    ref = references.build_reference(info)

    # ARM: 0 Roofline-значений; empirical fallback для julia/mandelbrot/aes/sha.
    expected_present = {"julia_sp", "mandelbrot_dp", "aes_256", "sha1"}
    assert set(ref.values.keys()) == expected_present

    for wid in expected_present:
        assert ref.values[wid].source == "empirical_proxy"

    # Остальные 8 — пропущены.
    expected_skipped = {
        "memory_read", "memory_write", "memory_copy",
        "flops_sp", "flops_dp",
        "int_iops_24", "int_iops_32", "int_iops_64",
    }
    skipped_in_notes = {
        n.split(":", 1)[1] for n in ref.aggregate_notes if n.startswith("workload_skipped:")
    }
    assert skipped_in_notes == expected_skipped


def test_reference_set_id_format(monkeypatch: pytest.MonkeyPatch):
    """ID набора формируется из имени CPU."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    info = _make_sys_info(cpu_model="AMD Ryzen 7 5800X 8-Core Processor")
    ref = references.build_reference(info)
    assert ref.id.startswith("roofline-")
    # Содержит элементы CPU model в нижнем регистре.
    assert "amd" in ref.id.lower() or "ryzen" in ref.id.lower()
