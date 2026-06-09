"""Юнит-тесты для Roofline-калькулятора (`apexcore.application.roofline`)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apexcore.application import roofline
from apexcore.domain.models import CpuCores, SystemInfo


def _make_sys_info(
    cpu_model: str = "Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz",
    cpu_arch: str | None = "AMD64",
    cores: int = 8,
) -> SystemInfo:
    return SystemInfo(
        os_name="Windows 11",
        os_version="10.0.22621",
        cpu_model=cpu_model,
        cpu_cores=CpuCores(physical=cores, logical=cores * 2),
        ram_total_gb=32.0,
        cpu_arch=cpu_arch,
        timestamp=datetime.now(timezone.utc),
    )


# ─── SIMD detection ─────────────────────────────────────────────────────────


def test_simd_alder_lake_detected_as_avx2():
    """Intel 12-го поколения (Alder Lake) — AVX2, не AVX-512 (E-cores не поддерживают)."""
    info = _make_sys_info(cpu_model="Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) == "avx2"


def test_simd_xeon_gold_detected_as_avx512():
    info = _make_sys_info(cpu_model="Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) == "avx512"


def test_simd_old_sandy_bridge_detected_as_avx():
    info = _make_sys_info(cpu_model="Intel(R) Core(TM) i7-2600K CPU @ 3.40GHz")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) == "avx"


def test_simd_ryzen_zen2_detected_as_avx2():
    info = _make_sys_info(cpu_model="AMD Ryzen 7 3700X 8-Core Processor")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) == "avx2"


def test_simd_arm_returns_none():
    info = _make_sys_info(cpu_model="Apple M2 Pro", cpu_arch="arm64")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) is None


def test_simd_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_SIMD", "sse4")
    info = _make_sys_info(cpu_model="Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz")
    assert roofline.detect_simd_level(info.cpu_model, info.cpu_arch) == "sse4"


# ─── AES-NI / SHA-NI ─────────────────────────────────────────────────────────


def test_aes_ni_modern_intel_yes():
    info = _make_sys_info(cpu_model="Intel Core i5-12400")
    assert roofline.detect_aes_ni(info.cpu_model, info.cpu_arch) is True


def test_aes_ni_old_core2_no():
    info = _make_sys_info(cpu_model="Intel Core 2 Duo E8400")
    assert roofline.detect_aes_ni(info.cpu_model, info.cpu_arch) is False


def test_aes_ni_arm_no():
    info = _make_sys_info(cpu_model="Apple M2", cpu_arch="arm64")
    assert roofline.detect_aes_ni(info.cpu_model, info.cpu_arch) is False


def test_sha_ni_alder_lake_yes():
    info = _make_sys_info(cpu_model="Intel Core i7-12700K")
    assert roofline.detect_sha_ni(info.cpu_model, info.cpu_arch) is True


def test_sha_ni_skylake_no():
    """Intel Skylake (6-е поколение) ещё не имеет SHA-NI в desktop."""
    info = _make_sys_info(cpu_model="Intel Core i7-6700K")
    assert roofline.detect_sha_ni(info.cpu_model, info.cpu_arch) is False


def test_sha_ni_ryzen_yes():
    info = _make_sys_info(cpu_model="AMD Ryzen 5 3600")
    assert roofline.detect_sha_ni(info.cpu_model, info.cpu_arch) is True


# ─── FLOPS peak ──────────────────────────────────────────────────────────────


def test_flops_peak_avx2_alder_lake(monkeypatch: pytest.MonkeyPatch):
    """8 cores × 32 ops/cycle SP × 3.6 GHz = 921.6 GFLOPS SP."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K", cores=8)
    peak_sp = roofline.compute_flops_peak(info, "sp")
    peak_dp = roofline.compute_flops_peak(info, "dp")
    assert peak_sp is not None and peak_sp == pytest.approx(8 * 32 * 3.6)
    assert peak_dp is not None and peak_dp == pytest.approx(8 * 16 * 3.6)


def test_flops_peak_arm_returns_none():
    info = _make_sys_info(cpu_model="Apple M2", cpu_arch="arm64")
    assert roofline.compute_flops_peak(info, "sp") is None


def test_flops_peak_no_clock_returns_none(monkeypatch: pytest.MonkeyPatch):
    """Если psutil не даёт частоту И нет '@ X GHz' в строке — None."""
    monkeypatch.delenv("APEXCORE_CPU_GHZ", raising=False)

    def _no_freq(*args, **kwargs):
        class _F:
            max = 0.0
            current = 0.0
            min = 0.0
        return _F()

    monkeypatch.setattr("psutil.cpu_freq", _no_freq)
    info = _make_sys_info(cpu_model="Some CPU without clock info")
    assert roofline.compute_flops_peak(info, "sp") is None


# ─── Integer peak ────────────────────────────────────────────────────────────


def test_integer_peak_basic(monkeypatch: pytest.MonkeyPatch):
    """8 cores × 4 ops/cycle × 3.6 GHz = 115.2 GIOPS."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    info = _make_sys_info(cores=8)
    peak = roofline.compute_integer_peak(info, 64)
    assert peak is not None and peak == pytest.approx(8 * 4.0 * 3.6)


def test_integer_peak_arm_returns_none():
    info = _make_sys_info(cpu_arch="arm64")
    assert roofline.compute_integer_peak(info, 32) is None


# ─── AES peak ────────────────────────────────────────────────────────────────


def test_aes_peak_with_aes_ni(monkeypatch: pytest.MonkeyPatch):
    """1300 MB/s/GHz × 3.6 GHz = 4680 MB/s."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K")
    peak = roofline.compute_aes_peak(info)
    assert peak is not None and peak == pytest.approx(1300.0 * 3.6)


def test_aes_peak_without_aes_ni():
    info = _make_sys_info(cpu_model="Intel Core 2 Duo E8400")
    assert roofline.compute_aes_peak(info) is None


# ─── SHA peak ────────────────────────────────────────────────────────────────


def test_sha1_peak_with_sha_ni(monkeypatch: pytest.MonkeyPatch):
    """clock × 1000 / 3 MB/s; для 3.6 GHz = 1200 MB/s."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K")
    peak = roofline.compute_sha1_peak(info)
    assert peak is not None and peak == pytest.approx(3.6 * 1000.0 / 3.0)


def test_sha1_peak_skylake_returns_none():
    info = _make_sys_info(cpu_model="Intel Core i7-6700K")
    assert roofline.compute_sha1_peak(info) is None


# ─── DRAM peak ───────────────────────────────────────────────────────────────


def test_dram_peak_env_override(monkeypatch: pytest.MonkeyPatch):
    """APEXCORE_DRAM_MTS=3200, APEXCORE_DRAM_MODULES=2 → 2 × 3200 × 8 = 51200 MB/s."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    info = _make_sys_info()
    peak = roofline.compute_dram_peak(info)
    assert peak is not None and peak == pytest.approx(2 * 3200 * 8.0)


def test_dram_peak_no_data_returns_none(monkeypatch: pytest.MonkeyPatch):
    """Если нет ни env-override, ни WMI/dmidecode (моки) — None."""
    monkeypatch.delenv("APEXCORE_DRAM_MTS", raising=False)
    monkeypatch.delenv("APEXCORE_DRAM_MODULES", raising=False)
    monkeypatch.setattr(roofline, "_read_dram_speed_mts_windows", lambda: None)
    monkeypatch.setattr(roofline, "_read_dram_speed_mts_linux", lambda: None)
    info = _make_sys_info()
    assert roofline.compute_dram_peak(info) is None


# ─── Aggregator ──────────────────────────────────────────────────────────────


def test_get_roofline_reference_full_machine(monkeypatch: pytest.MonkeyPatch):
    """На «нормальной» Alder Lake машине должно вернуть значения для всех тестов
    кроме fractal."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "3.6")
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K", cores=8)
    ref = roofline.get_roofline_reference(info)

    # Roofline-источник: ненулевые значения
    assert ref["memory_read"] is not None and ref["memory_read"] > 0
    assert ref["memory_write"] is not None and ref["memory_write"] > 0
    assert ref["memory_copy"] is not None
    # memory_copy = 2× memory_read (соглашение STREAM)
    assert ref["memory_copy"] == pytest.approx(ref["memory_read"] * 2.0)
    assert ref["flops_sp"] is not None and ref["flops_sp"] > 0
    assert ref["flops_dp"] is not None
    # SP = 2× DP по ops/cycle (32 vs 16)
    assert ref["flops_sp"] == pytest.approx(ref["flops_dp"] * 2.0)
    assert ref["int_iops_24"] is not None
    assert ref["int_iops_24"] == ref["int_iops_32"] == ref["int_iops_64"]
    assert ref["aes_256"] is not None
    assert ref["sha1"] is not None  # Alder Lake поддерживает SHA-NI

    # Fractal — всегда None (нет теоретического предела)
    assert ref["julia_sp"] is None
    assert ref["mandelbrot_dp"] is None


def test_get_roofline_reference_arm_returns_none():
    """На ARM почти всё None (Roofline-формулы для x86)."""
    info = _make_sys_info(cpu_model="Apple M2", cpu_arch="arm64", cores=8)
    ref = roofline.get_roofline_reference(info)
    assert ref["flops_sp"] is None
    assert ref["int_iops_32"] is None
    assert ref["aes_256"] is None


# ─── Hybrid P+E topology (Alder/Raptor Lake) ─────────────────────────────────


def test_flops_peak_hybrid_i9_12900k_uses_sum_p_e():
    """i9-12900K = 8P (5.2 GHz) + 8E (3.9 GHz). DP: 8·16·5.2 + 8·16·3.9 = 1164.8."""
    info = _make_sys_info(
        cpu_model="12th Gen Intel(R) Core(TM) i9-12900K", cores=16,
    )
    peak = roofline.compute_flops_peak(info, "dp")
    assert peak is not None
    assert peak == pytest.approx(8 * 16 * 5.2 + 8 * 16 * 3.9)


def test_flops_peak_hybrid_i7_12700k_uses_sum_p_e():
    """i7-12700K = 8P (5.0 GHz) + 4E (3.8 GHz). DP: 8·16·5.0 + 4·16·3.8 = 883.2."""
    info = _make_sys_info(
        cpu_model="12th Gen Intel(R) Core(TM) i7-12700K", cores=12,
    )
    peak = roofline.compute_flops_peak(info, "dp")
    assert peak is not None
    assert peak == pytest.approx(8 * 16 * 5.0 + 4 * 16 * 3.8)


def test_flops_peak_hybrid_table_mismatch_falls_back_to_homogeneous(
    monkeypatch: pytest.MonkeyPatch,
):
    """Если cores не совпадает с p_n+e_n в таблице — fallback на гомогенную формулу
    (защита от неверной классификации не-K вариантов)."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "5.0")
    info = _make_sys_info(cpu_model="Intel Core i9-12900", cores=10)  # 10 ≠ 16
    peak = roofline.compute_flops_peak(info, "dp")
    # Гомогенная: 10 × 16 × 5.0 = 800
    assert peak == pytest.approx(10 * 16 * 5.0)


def test_flops_peak_non_hybrid_homogeneous_formula(monkeypatch: pytest.MonkeyPatch):
    """Не-гибридный CPU (Ryzen) — гомогенная формула без изменений."""
    monkeypatch.setenv("APEXCORE_CPU_GHZ", "5.7")
    info = _make_sys_info(cpu_model="AMD Ryzen 9 7950X 16-Core Processor", cores=16)
    peak = roofline.compute_flops_peak(info, "dp")
    # AVX-512 на Zen 4 = 32 ops/cycle DP, но detect_simd_level определяет как
    # avx512 только по markers ("ryzen 9 7" в markers) — проверяем что значение
    # = cores × ops × clock без дополнительных модификаций.
    simd = roofline.detect_simd_level(info.cpu_model, info.cpu_arch)
    assert simd is not None
    expected_ops = roofline.SIMD_OPS_PER_CYCLE[simd]["dp"]
    assert peak == pytest.approx(16 * expected_ops * 5.7)


# ─── DRAM channels heuristic ─────────────────────────────────────────────────


def test_dram_peak_desktop_4_dimm_limited_to_2_channels(
    monkeypatch: pytest.MonkeyPatch,
):
    """4 DIMM в desktop dual-channel = 2 канала, не 4 (баг до фикса)."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "4")
    info = _make_sys_info(cpu_model="Intel Core i9-12900K")
    peak = roofline.compute_dram_peak(info)
    # До фикса было бы 4 × 3200 × 8 = 102400; после = min(4, 2) × 3200 × 8 = 51200.
    assert peak == pytest.approx(2 * 3200 * 8.0)


def test_dram_peak_desktop_2_dimm_unchanged(monkeypatch: pytest.MonkeyPatch):
    """2 DIMM в desktop = 2 канала (поведение не меняется)."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    info = _make_sys_info(cpu_model="Intel Core i7-12700K")
    peak = roofline.compute_dram_peak(info)
    assert peak == pytest.approx(2 * 3200 * 8.0)


def test_dram_peak_hedt_4_dimm_uses_4_channels(monkeypatch: pytest.MonkeyPatch):
    """HEDT (Xeon W) = 4 канала → 4 DIMM используются полностью."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "2933")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "4")
    info = _make_sys_info(cpu_model="Intel(R) Xeon(R) W-3275")
    peak = roofline.compute_dram_peak(info)
    assert peak == pytest.approx(4 * 2933 * 8.0)


def test_dram_peak_server_epyc_uses_8_channels(monkeypatch: pytest.MonkeyPatch):
    """EPYC Zen 3 (7xx3) = 8 каналов DDR4."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "3200")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "8")
    info = _make_sys_info(cpu_model="AMD EPYC 7763 64-Core Processor", cores=64)
    peak = roofline.compute_dram_peak(info)
    assert peak == pytest.approx(8 * 3200 * 8.0)


def test_max_dram_channels_unknown_cpu_returns_none():
    """Неизвестный CPU → None → fallback на старое поведение в compute_dram_peak."""
    assert roofline._max_dram_channels("SomeFancyExoticChip 9000") is None


# ─── TJmax detection ────────────────────────────────────────────────────────


def test_resolve_tjmax_intel_desktop():
    info = _make_sys_info(cpu_model="Intel(R) Core(TM) i9-12900K")
    assert roofline.resolve_tjmax(info) == 100


def test_resolve_tjmax_ryzen_7000():
    info = _make_sys_info(cpu_model="AMD Ryzen 9 7950X 16-Core Processor")
    assert roofline.resolve_tjmax(info) == 95


def test_resolve_tjmax_ryzen_5000():
    info = _make_sys_info(cpu_model="AMD Ryzen 7 5800X 8-Core Processor")
    assert roofline.resolve_tjmax(info) == 90


def test_resolve_tjmax_xeon_scalable():
    info = _make_sys_info(cpu_model="Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz")
    assert roofline.resolve_tjmax(info) == 100


def test_resolve_tjmax_unknown_cpu_returns_none():
    info = _make_sys_info(cpu_model="SomeFancyExoticChip 9000", cpu_arch="x86_64")
    assert roofline.resolve_tjmax(info) is None


def test_resolve_tjmax_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_TJMAX", "85")
    info = _make_sys_info(cpu_model="SomeFancyExoticChip 9000", cpu_arch="x86_64")
    assert roofline.resolve_tjmax(info) == 85


def test_resolve_tjmax_env_override_invalid_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_TJMAX", "not-a-number")
    info = _make_sys_info(cpu_model="Intel(R) Core(TM) i9-12900K")
    assert roofline.resolve_tjmax(info) == 100
