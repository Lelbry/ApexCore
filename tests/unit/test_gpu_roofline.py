"""Юнит-тесты GPU-Roofline-калькулятора (`apexcore.application.gpu_roofline`).

Покрывают: якорные (known) GPU против опубликованных FP32 TFLOPS, unknown
fallback, устройства без FP64, env-overrides, резолв архитектуры (per-model
пин vs family-правило vs вендор-фильтр).
"""

from __future__ import annotations

import pytest

from apexcore.application import gpu_roofline
from apexcore.domain.gpu import GpuDeviceInfo


def _dev(
    name: str,
    vendor: str = "",
    compute_units: int = 0,
    max_clock_mhz: int = 0,
    fp64_supported: bool = False,
) -> GpuDeviceInfo:
    return GpuDeviceInfo(
        index=0,
        name=name,
        vendor=vendor,
        compute_units=compute_units,
        max_clock_mhz=max_clock_mhz,
        fp64_supported=fp64_supported,
    )


def _rel_close(actual: float, expected: float, tol: float = 0.05) -> bool:
    return abs(actual - expected) / expected <= tol


# ─── Якорные GPU: FP32 против опубликованных TFLOPS (±5%) ────────────────────


def test_anchor_rtx_4070_ti():
    """RTX 4070 Ti: 60 SM @ 2655 MHz → ~40.7 TFLOPS FP32 (офиц. 40.09)."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4070 Ti", "NVIDIA", 60, 2655, fp64_supported=True)
    )
    assert peak.arch == "nvidia_ada"
    assert peak.fp32_peak_gflops is not None
    assert _rel_close(peak.fp32_peak_gflops, 40700)
    # FP64 = FP32 / 64 ≈ 636.
    assert peak.fp64_peak_gflops is not None
    assert _rel_close(peak.fp64_peak_gflops, 636)
    # Пропускная способность — из per-model таблицы.
    assert peak.mem_bandwidth_peak_gb_s == pytest.approx(504.2)
    assert "roofline" in peak.source
    assert "model_table" in peak.source


def test_anchor_intel_uhd_770():
    """Intel UHD 770: 32 EU @ 1550 MHz → ~0.79 TFLOPS FP32, без FP64, iGPU-память."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("Intel(R) UHD Graphics 770", "Intel(R) Corporation", 32, 1550)
    )
    assert peak.arch == "intel_gen12"
    assert peak.fp32_peak_gflops is not None
    assert _rel_close(peak.fp32_peak_gflops, 793)
    # Gen12 без аппаратного FP64 → None (даже если бы fp64_supported=True).
    assert peak.fp64_peak_gflops is None
    # iGPU: память общая с DRAM → None + note.
    assert peak.mem_bandwidth_peak_gb_s is None
    assert any("iGPU" in n for n in peak.notes)


def test_anchor_amd_radeon_680m():
    """AMD Radeon 680M: 12 CU @ 2400 MHz → ~3.69 TFLOPS FP32 (RDNA2 iGPU)."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("AMD Radeon 680M", "Advanced Micro Devices, Inc.", 12, 2400, fp64_supported=True)
    )
    assert peak.arch == "amd_rdna2"
    assert _rel_close(peak.fp32_peak_gflops, 3686)
    # FP64 = FP32 / 16.
    assert _rel_close(peak.fp64_peak_gflops, 3686 / 16)
    # iGPU → память None.
    assert peak.mem_bandwidth_peak_gb_s is None


@pytest.mark.parametrize(
    ("name", "vendor", "cu", "mhz", "arch", "expected_tflops"),
    [
        ("NVIDIA GeForce RTX 3080", "NVIDIA", 68, 1710, "nvidia_ampere", 29.77),
        ("NVIDIA GeForce RTX 2070", "NVIDIA", 36, 1620, "nvidia_turing", 7.465),
        ("AMD Radeon RX 7900 XTX", "AMD", 96, 2500, "amd_rdna3", 61.44),
        ("AMD Radeon RX 6800 XT", "AMD", 72, 2250, "amd_rdna2", 20.74),
        ("Intel(R) Arc(TM) A770 Graphics", "Intel", 512, 2400, "intel_arc", 19.66),
    ],
)
def test_arch_fp32_matches_published_specs(name, vendor, cu, mhz, arch, expected_tflops):
    """Каждая архитектура воспроизводит опубликованный FP32-пик реального GPU (±5%)."""
    peak = gpu_roofline.compute_gpu_peak(_dev(name, vendor, cu, mhz, fp64_supported=True))
    assert peak.arch == arch
    assert peak.fp32_peak_gflops is not None
    assert _rel_close(peak.fp32_peak_gflops, expected_tflops * 1000)


# ─── FP64 semantics ──────────────────────────────────────────────────────────


def test_fp64_none_when_device_reports_unsupported():
    """fp64_supported=False → FP64-пик None даже на арх с fp64_ratio>0."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4070 Ti", "NVIDIA", 60, 2655, fp64_supported=False)
    )
    assert peak.fp32_peak_gflops is not None
    assert peak.fp64_peak_gflops is None
    assert any("FP64" in n for n in peak.notes)


def test_fp64_none_on_intel_even_if_supported_flag_true():
    """Intel Gen12/Arc: ratio=0 → FP64 None, независимо от fp64_supported."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("Intel(R) Arc(TM) A770 Graphics", "Intel", 512, 2400, fp64_supported=True)
    )
    assert peak.fp32_peak_gflops is not None
    assert peak.fp64_peak_gflops is None


def test_fp64_ratio_ampere_is_one_sixtyfourth():
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 3080", "NVIDIA", 68, 1710, fp64_supported=True)
    )
    assert peak.fp64_peak_gflops == pytest.approx(peak.fp32_peak_gflops / 64.0)


def test_fp64_ratio_turing_is_one_thirtysecond():
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 2070", "NVIDIA", 36, 1620, fp64_supported=True)
    )
    assert peak.fp64_peak_gflops == pytest.approx(peak.fp32_peak_gflops / 32.0)


# ─── Unknown / degenerate fallbacks ──────────────────────────────────────────


def test_unknown_gpu_returns_all_none():
    """Неизвестный GPU → все пики None, source=fallback, note про арх."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("Totally Made Up GPU 9000", "SomeVendor", 40, 2000, fp64_supported=True)
    )
    assert peak.arch is None
    assert peak.fp32_peak_gflops is None
    assert peak.fp64_peak_gflops is None
    assert peak.mem_bandwidth_peak_gb_s is None
    assert peak.source == "fallback"
    assert any("не распознан" in n for n in peak.notes)


def test_known_arch_but_zero_opencl_fields():
    """Арх распознана, но compute_units/clock=0 → FP32 None + note."""
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4070 Ti", "NVIDIA", 0, 0, fp64_supported=True)
    )
    assert peak.arch == "nvidia_ada"
    assert peak.fp32_peak_gflops is None
    assert peak.fp64_peak_gflops is None
    # Память всё равно берётся из per-model таблицы.
    assert peak.mem_bandwidth_peak_gb_s == pytest.approx(504.2)
    assert any("OpenCL" in n for n in peak.notes)


def test_discrete_gpu_not_in_model_table_has_none_mem():
    """Discrete GPU с известной арх, но без записи в models → mem None + note."""
    # RTX 4055 не существует, но подпадает под family nvidia_ada (rtx 40xx).
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4055", "NVIDIA", 20, 2000, fp64_supported=True)
    )
    assert peak.arch == "nvidia_ada"
    assert peak.fp32_peak_gflops is not None  # арх известна → пик считается
    assert peak.mem_bandwidth_peak_gb_s is None
    assert any("пропускной способности" in n for n in peak.notes)


# ─── Env-overrides ───────────────────────────────────────────────────────────


def test_env_override_fp32(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "12345")
    peak = gpu_roofline.compute_gpu_peak(_dev("Unknown GPU", "X", 10, 1000))
    assert peak.fp32_peak_gflops == pytest.approx(12345.0)
    assert "env_override" in peak.source


def test_env_override_mem(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_GPU_MEM_PEAK_GB_S", "777.5")
    peak = gpu_roofline.compute_gpu_peak(_dev("Unknown GPU", "X"))
    assert peak.mem_bandwidth_peak_gb_s == pytest.approx(777.5)
    assert "env_override" in peak.source


def test_env_override_fp64(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APEXCORE_GPU_FP64_PEAK_GFLOPS", "500")
    # Даже на устройстве без FP64 override берёт верх.
    peak = gpu_roofline.compute_gpu_peak(_dev("Intel(R) UHD Graphics 770", "Intel", 32, 1550))
    assert peak.fp64_peak_gflops == pytest.approx(500.0)


def test_env_override_arch(monkeypatch: pytest.MonkeyPatch):
    """APEXCORE_GPU_ARCH пинит арх для нераспознанного имени."""
    monkeypatch.setenv("APEXCORE_GPU_ARCH", "nvidia_turing")
    peak = gpu_roofline.compute_gpu_peak(_dev("Mystery Card", "NVIDIA", 30, 1500, fp64_supported=True))
    assert peak.arch == "nvidia_turing"
    assert peak.fp32_peak_gflops is not None


def test_env_override_invalid_ignored(monkeypatch: pytest.MonkeyPatch):
    """Нечисловой/неположительный override игнорируется (fallback на расчёт)."""
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "not-a-number")
    monkeypatch.setenv("APEXCORE_GPU_MEM_PEAK_GB_S", "-5")
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4070 Ti", "NVIDIA", 60, 2655, fp64_supported=True)
    )
    # FP32 посчитан по формуле, а не из битого override.
    assert _rel_close(peak.fp32_peak_gflops, 40700)
    assert "env_override" not in peak.source
    # mem из таблицы, а не из отрицательного override.
    assert peak.mem_bandwidth_peak_gb_s == pytest.approx(504.2)


def test_env_override_arch_unknown_key_ignored(monkeypatch: pytest.MonkeyPatch):
    """Неизвестный ключ в APEXCORE_GPU_ARCH игнорируется → обычный резолв."""
    monkeypatch.setenv("APEXCORE_GPU_ARCH", "nonexistent_arch")
    peak = gpu_roofline.compute_gpu_peak(
        _dev("NVIDIA GeForce RTX 4070 Ti", "NVIDIA", 60, 2655, fp64_supported=True)
    )
    assert peak.arch == "nvidia_ada"


# ─── resolve_gpu_arch: приоритеты и вендор-фильтр ────────────────────────────


def test_resolve_arch_case_insensitive_and_tm_marker():
    assert gpu_roofline.resolve_gpu_arch("nvidia geforce RTX 4070 ti", "nvidia") == "nvidia_ada"
    assert gpu_roofline.resolve_gpu_arch("Intel(R) UHD Graphics 770", "Intel(R) Corp") == "intel_gen12"


def test_resolve_arch_family_rules_for_unlisted_model():
    """Модели нет в `models`, но family-правило ловит семейство."""
    assert gpu_roofline.resolve_gpu_arch("NVIDIA GeForce RTX 3050 Laptop GPU", "NVIDIA") == "nvidia_ampere"
    assert gpu_roofline.resolve_gpu_arch("AMD Radeon RX 6500 XT", "AMD") == "amd_rdna2"


def test_resolve_arch_generic_amd_igpu_alias():
    """Родовое 'AMD Radeon(TM) Graphics' → amd_rdna2 (после нормализации)."""
    assert gpu_roofline.resolve_gpu_arch("AMD Radeon(TM) Graphics", "AMD") == "amd_rdna2"


def test_resolve_arch_generic_alias_does_not_swallow_uhd():
    """Родовой AMD-alias не должен матчить Intel UHD Graphics."""
    assert gpu_roofline.resolve_gpu_arch("Intel(R) UHD Graphics 770", "Intel") == "intel_gen12"


def test_resolve_arch_vendor_filter_blocks_cross_vendor_family():
    """Вендор AMD + имя, случайно похожее на NVIDIA-паттерн → не отдаём nvidia-арх."""
    # Имя содержит 'rtx 4090', но вендор — AMD: family-правило nvidia_ada
    # отбрасывается вендор-фильтром, результат None.
    assert gpu_roofline.resolve_gpu_arch("Weird RTX 4090 clone", "Advanced Micro Devices") is None


def test_resolve_arch_unknown_returns_none():
    assert gpu_roofline.resolve_gpu_arch("Some Random Adapter", "Unknown") is None


def test_resolve_arch_intel_vendor_not_confused_with_ati_substring():
    """'Intel(R) Corporation' содержит 'ati' — не должно классифицироваться как AMD."""
    # Регресс на баг подстрочного поиска вендора.
    assert gpu_roofline.resolve_gpu_arch("Intel(R) UHD Graphics 770", "Intel(R) Corporation") == "intel_gen12"
