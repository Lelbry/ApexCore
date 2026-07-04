"""Тесты `application/gpu_benchmark.GpuBenchmarkOrchestrator`.

Главные инварианты (``docs/gpu_benchmark.md`` §8, §10.3):
1. Happy-path: FP32 + VRAM + пики → score построен, ratio clamp'нуты ≤1.0.
2. FP64-unsupported устройство: фаза пропущена, но score **всё равно** есть
   (FP64 вне балла).
3. Неизвестный пик VRAM (модель не в таблице, нет env-override): r_mem = None
   → score = None, но отчёт собран с raw-измерениями.
4. Бэкенд недоступен: graceful-отчёт (score=None, cancelled=False, note).
5. Отмена в середине прогона: cancelled=True, оставшиеся фазы не гоняются.

Пики устройства задаются через env-override'ы, которые понимает
``compute_gpu_peak`` (``APEXCORE_GPU_FP32_PEAK_GFLOPS`` /
``APEXCORE_GPU_MEM_PEAK_GB_S`` / ``APEXCORE_GPU_FP64_PEAK_GFLOPS``) —
детерминированно, без зависимости от таблицы арх. Бэкенд — in-memory fake,
реальный OpenCL/GPU не нужен.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from apexcore.application.gpu_benchmark import (
    GpuBenchmarkOrchestrator,
    GpuBenchmarkParams,
)
from apexcore.domain.gpu import (
    GpuDeviceInfo,
    GpuDeviceType,
    GpuMeasurement,
    GpuWorkloadKind,
)
from apexcore.domain.models import CpuCores, SystemInfo

# ─── Fakes ───────────────────────────────────────────────────────────────────


class _FakeAdapter:
    name = "fake"

    def __init__(self, sys_info: SystemInfo) -> None:
        self._sys_info = sys_info

    def get_system_info(self) -> SystemInfo:
        return self._sys_info


def _make_sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0",
        cpu_model="Intel(R) Core(TM) i9-12900K",
        cpu_cores=CpuCores(physical=16, logical=24),
        ram_total_gb=32.0,
        gpu_list=["NVIDIA GeForce RTX 4070 Ti"],
        cpu_arch="x86_64",
        hostname="test-host",
        cpu_base_mhz=3200.0,
        timestamp=datetime.now(timezone.utc),
    )


def _unit_for(kind: GpuWorkloadKind) -> str:
    return "GFLOPS" if kind in (GpuWorkloadKind.FP32, GpuWorkloadKind.FP64) else "GB/s"


class _FakeBackend:
    """In-memory GPU-бэкенд: возвращает заскриптованные измерения.

    ``throughputs`` — map ``GpuWorkloadKind → throughput``. Отсутствие ключа
    означает «фаза выдала 0» (числитель None). ``cancel_on`` — kind, на замере
    которого бэкенд эмулирует внешнюю отмену (устанавливает переданный event),
    имитируя нажатие «Стоп» во время фазы.
    """

    name = "fake_backend"

    def __init__(
        self,
        devices: list[GpuDeviceInfo],
        throughputs: dict[GpuWorkloadKind, float] | None = None,
        *,
        available: bool = True,
        fp64_supported: bool = True,
        cancel_on: GpuWorkloadKind | None = None,
    ) -> None:
        self._devices = devices
        self._throughputs = throughputs or {}
        self._available = available
        self._fp64_supported = fp64_supported
        self._cancel_on = cancel_on
        self.measured_kinds: list[GpuWorkloadKind] = []

    def is_available(self) -> bool:
        return self._available

    def list_devices(self) -> list[GpuDeviceInfo]:
        return list(self._devices)

    def supports(self, device_index: int, kind: GpuWorkloadKind) -> bool:
        if kind == GpuWorkloadKind.FP64:
            return self._fp64_supported
        return True

    def measure(
        self,
        device_index: int,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None = None,
    ) -> GpuMeasurement:
        self.measured_kinds.append(kind)
        if self._cancel_on is not None and kind == self._cancel_on and cancel_token is not None:
            cancel_token.set()
        return GpuMeasurement(
            kind=kind,
            throughput=self._throughputs.get(kind, 0.0),
            unit=_unit_for(kind),
            duration_sec=0.01,
            iterations=1,
        )


def _discrete_device(
    name: str = "NVIDIA GeForce RTX 4070 Ti", fp64_supported: bool = True
) -> GpuDeviceInfo:
    return GpuDeviceInfo(
        index=0,
        name=name,
        vendor="NVIDIA",
        platform_name="NVIDIA CUDA",
        device_type=GpuDeviceType.DISCRETE,
        compute_units=60,
        max_clock_mhz=2655,
        global_mem_mb=12288,
        fp64_supported=fp64_supported,
    )


def _integrated_device(name: str = "Intel(R) UHD Graphics 770") -> GpuDeviceInfo:
    """Встроенный GPU (iGPU): нет собственной VRAM, per-model mem-пик = None."""
    return GpuDeviceInfo(
        index=0,
        name=name,
        vendor="Intel",
        platform_name="Intel(R) OpenCL",
        device_type=GpuDeviceType.INTEGRATED,
        compute_units=32,
        max_clock_mhz=1550,
        global_mem_mb=0,
        fp64_supported=False,
    )


def _fast_params() -> GpuBenchmarkParams:
    return GpuBenchmarkParams(
        fp32_duration_sec=0.01,
        fp64_duration_sec=0.01,
        mem_duration_sec=0.01,
        pcie_duration_sec=0.01,
        cooldown_sec=0.0,
    )


@pytest.fixture
def env_peaks(monkeypatch):
    """Задать архитектурные пики через env-override (детерминированно)."""
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "40000")
    monkeypatch.setenv("APEXCORE_GPU_MEM_PEAK_GB_S", "500")
    monkeypatch.setenv("APEXCORE_GPU_FP64_PEAK_GFLOPS", "625")


# ─── Тесты ───────────────────────────────────────────────────────────────────


def test_happy_path_computes_score(env_peaks):
    """Все фазы отработали, FP32 + VRAM + пики есть → score построен."""
    device = _discrete_device()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 24000.0,      # 24000/40000 = 0.60
            GpuWorkloadKind.FP64: 400.0,        # 400/625 = 0.64 (вне балла)
            GpuWorkloadKind.MEM_BANDWIDTH: 375.0,  # 375/500 = 0.75
            GpuWorkloadKind.PCIE_H2D: 12.0,
            GpuWorkloadKind.PCIE_D2H: 13.0,
        },
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)

    progress: list[tuple[str, int, int]] = []
    report = orch.run(
        params=_fast_params(),
        on_progress=lambda ph, i, t: progress.append((ph, i, t)),
    )

    # Прогресс по всем 5 фазам.
    assert [c[0] for c in progress] == [
        "fp32",
        "fp64",
        "mem_bandwidth",
        "pcie_h2d",
        "pcie_d2h",
    ]

    # Raw-измерения.
    assert report.fp32_gflops == pytest.approx(24000.0)
    assert report.fp64_gflops == pytest.approx(400.0)
    assert report.mem_bandwidth_gb_s == pytest.approx(375.0)
    assert report.pcie_h2d_gb_s == pytest.approx(12.0)
    assert report.pcie_d2h_gb_s == pytest.approx(13.0)

    # Пики из env-override.
    assert report.fp32_peak_gflops == pytest.approx(40000.0)
    assert report.mem_bandwidth_peak_gb_s == pytest.approx(500.0)

    # Ratio clamp'нуты ≤1.0.
    assert report.r_fp32 == pytest.approx(0.60)
    assert report.r_mem == pytest.approx(0.75)
    assert report.r_fp64 == pytest.approx(0.64)

    # score = GM(0.60, 0.75) × 10000 ≈ 6708.
    assert report.score is not None
    expected = 10_000.0 * (0.60 * 0.75) ** 0.5
    assert report.score == pytest.approx(expected)

    assert report.cancelled is False
    assert report.device.name == "NVIDIA GeForce RTX 4070 Ti"
    assert report.pcie_duration_sec == pytest.approx(0.02)  # h2d + d2h


def test_fp64_unsupported_still_scored(env_peaks):
    """Устройство без FP64: фаза пропущена, но score построен (FP64 вне балла)."""
    device = _discrete_device(fp64_supported=False)
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 20000.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 250.0,
            GpuWorkloadKind.PCIE_H2D: 10.0,
            GpuWorkloadKind.PCIE_D2H: 11.0,
        },
        fp64_supported=False,
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)

    progress: list[str] = []
    report = orch.run(
        params=_fast_params(),
        on_progress=lambda ph, i, t: progress.append(ph),
    )

    # FP64-фаза не измерялась и не в прогрессе.
    assert GpuWorkloadKind.FP64 not in backend.measured_kinds
    assert "fp64" not in progress
    assert report.fp64_gflops is None
    assert report.r_fp64 is None

    # Балл всё равно построен.
    assert report.score is not None
    assert report.r_fp32 == pytest.approx(0.50)
    assert report.r_mem == pytest.approx(0.50)
    assert any("FP64 не поддерживается" in n for n in report.notes)


def test_unknown_mem_peak_yields_none_score():
    """Модель не в таблице арх + нет env-override VRAM-пика → r_mem None → score None."""
    # Имя, которое не резолвится в известную арх; env-override'ов НЕ ставим.
    device = GpuDeviceInfo(
        index=0,
        name="Totally Unknown Accelerator XZ-9000",
        vendor="ACME",
        device_type=GpuDeviceType.UNKNOWN,
        compute_units=0,
        max_clock_mhz=0,
        fp64_supported=False,
    )
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 12345.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 200.0,
            GpuWorkloadKind.PCIE_H2D: 8.0,
            GpuWorkloadKind.PCIE_D2H: 9.0,
        },
        fp64_supported=False,
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    # Raw-измерения собраны.
    assert report.fp32_gflops == pytest.approx(12345.0)
    assert report.mem_bandwidth_gb_s == pytest.approx(200.0)
    # Пики неизвестны → ratio None → score None.
    assert report.fp32_peak_gflops is None
    assert report.mem_bandwidth_peak_gb_s is None
    assert report.r_fp32 is None
    assert report.r_mem is None
    assert report.score is None
    assert report.cancelled is False


def test_backend_unavailable_graceful_report():
    """Бэкенд недоступен → отчёт с note, score=None, cancelled=False, без исключений."""
    backend = _FakeBackend([], available=False)
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    assert report.score is None
    assert report.cancelled is False
    assert report.device.index == -1  # placeholder
    assert any("недоступен" in n.lower() for n in report.notes)
    # Ни одной фазы не гонялось.
    assert backend.measured_kinds == []


def test_no_devices_graceful_report():
    """Бэкенд доступен, но устройств нет → тот же graceful-отчёт."""
    backend = _FakeBackend([], available=True)
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    assert report.score is None
    assert report.cancelled is False
    assert backend.measured_kinds == []


def test_cancel_mid_run_stops_remaining_phases(env_peaks):
    """Отмена на FP32: последующие фазы (FP64/MEM/PCIe) не запускаются."""
    device = _discrete_device()
    token = threading.Event()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 24000.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 375.0,
        },
        cancel_on=GpuWorkloadKind.FP32,  # бэкенд ставит token во время FP32
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params(), cancel_token=token)

    # FP32 успел выполниться, дальше — стоп.
    assert backend.measured_kinds == [GpuWorkloadKind.FP32]
    assert report.fp32_gflops == pytest.approx(24000.0)
    assert report.mem_bandwidth_gb_s is None
    assert report.cancelled is True
    # r_mem нет → score None (VRAM не измерена).
    assert report.r_mem is None
    assert report.score is None
    assert any("прерван" in n.lower() for n in report.notes)


def test_pre_cancelled_token_runs_no_phases(env_peaks):
    """Если токен уже set до старта — ни одна фаза не гоняется, score None."""
    device = _discrete_device()
    token = threading.Event()
    token.set()
    backend = _FakeBackend(
        [device],
        {GpuWorkloadKind.FP32: 24000.0, GpuWorkloadKind.MEM_BANDWIDTH: 375.0},
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params(), cancel_token=token)

    assert backend.measured_kinds == []
    assert report.cancelled is True
    assert report.score is None


def test_device_index_out_of_range_graceful(env_peaks):
    """Запрошен несуществующий device_index → graceful-отчёт, без исключения."""
    device = _discrete_device()
    backend = _FakeBackend([device], {GpuWorkloadKind.FP32: 24000.0})
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(device_index=5, params=_fast_params())

    assert report.score is None
    assert report.cancelled is False
    assert backend.measured_kinds == []
    assert any("device_index" in n for n in report.notes)


# ─── iGPU: пик памяти = пропускная способность системной DRAM ────────────────


def test_integrated_gpu_scored_via_system_dram(monkeypatch):
    """iGPU без табличного mem-пика → берём пик из системной DRAM → score есть.

    Встроенная графика делит DRAM с CPU, поэтому per-model VRAM-пик отсутствует
    (``compute_gpu_peak`` вернул бы ``mem_bandwidth_peak_gb_s=None`` → r_mem None →
    score None). Оркестратор должен подставить пропускную способность системной
    DRAM как потолок памяти iGPU.
    """
    # Детерминированный DRAM-пик: DDR5-4800 dual-channel = 2·4800·8 = 76 800 МБ/с
    # → 76.8 ГБ/с (та же формула compute_dram_peak, что у «Общей оценки»).
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "4800")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    # FP32-пик фиксируем через env, чтобы балл не зависел от таблицы арх.
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "5000")

    device = _integrated_device()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 2500.0,          # 2500/5000 = 0.50
            GpuWorkloadKind.MEM_BANDWIDTH: 38.4,   # 38.4/76.8 = 0.50
            GpuWorkloadKind.PCIE_H2D: 8.0,
            GpuWorkloadKind.PCIE_D2H: 9.0,
        },
        fp64_supported=False,
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    # Пик памяти теперь — реальное число (системная DRAM), а не None.
    assert report.mem_bandwidth_peak_gb_s == pytest.approx(76.8)
    assert report.r_mem == pytest.approx(0.50)
    assert report.r_fp32 == pytest.approx(0.50)
    # Балл построен (раньше был бы None).
    assert report.score is not None
    assert report.score == pytest.approx(10_000.0 * (0.50 * 0.50) ** 0.5)
    # Источник помечен, note прозрачно объясняет происхождение потолка.
    assert "igpu_dram" in report.peak_source
    assert any("iGPU" in n and "DRAM" in n for n in report.notes)


def test_env_mem_peak_overrides_igpu_dram_path(monkeypatch):
    """Даже для iGPU env-override APEXCORE_GPU_MEM_PEAK_GB_S приоритетнее DRAM-пути."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "4800")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "5000")
    monkeypatch.setenv("APEXCORE_GPU_MEM_PEAK_GB_S", "200")  # приоритетнее DRAM

    device = _integrated_device()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 2500.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 100.0,  # 100/200 = 0.50 (по env-пику)
            GpuWorkloadKind.PCIE_H2D: 8.0,
            GpuWorkloadKind.PCIE_D2H: 9.0,
        },
        fp64_supported=False,
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    # Победил env-override, а не 76.8 ГБ/с из DRAM.
    assert report.mem_bandwidth_peak_gb_s == pytest.approx(200.0)
    assert report.r_mem == pytest.approx(0.50)
    assert "igpu_dram" not in report.peak_source
    assert "env_override" in report.peak_source


def test_integrated_gpu_none_when_dram_undeterminable(monkeypatch):
    """iGPU, но DRAM-пик определить нельзя → mem-пик остаётся None → score None."""
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "5000")
    # Гарантированно недоступный DRAM-пик: патчим функцию в модуле gpu_roofline.
    monkeypatch.setattr(
        "apexcore.application.gpu_roofline.compute_dram_peak",
        lambda system_info: None,
    )

    device = _integrated_device()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 2500.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 38.4,
            GpuWorkloadKind.PCIE_H2D: 8.0,
            GpuWorkloadKind.PCIE_D2H: 9.0,
        },
        fp64_supported=False,
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    # Поведение как раньше: нет пика памяти → нет r_mem → нет балла.
    assert report.mem_bandwidth_peak_gb_s is None
    assert report.r_mem is None
    assert report.score is None
    assert "igpu_dram" not in report.peak_source
    # Но FP32-замер собран (raw-данные не теряются).
    assert report.fp32_gflops == pytest.approx(2500.0)


def test_discrete_gpu_unaffected_by_igpu_dram_path(monkeypatch):
    """Дискретная карта: DRAM-путь не трогает её mem-пик (он из env/таблицы)."""
    monkeypatch.setenv("APEXCORE_DRAM_MTS", "4800")
    monkeypatch.setenv("APEXCORE_DRAM_MODULES", "2")
    monkeypatch.setenv("APEXCORE_GPU_FP32_PEAK_GFLOPS", "40000")
    monkeypatch.setenv("APEXCORE_GPU_MEM_PEAK_GB_S", "504")

    device = _discrete_device()
    backend = _FakeBackend(
        [device],
        {
            GpuWorkloadKind.FP32: 20000.0,
            GpuWorkloadKind.MEM_BANDWIDTH: 378.0,
            GpuWorkloadKind.PCIE_H2D: 10.0,
            GpuWorkloadKind.PCIE_D2H: 11.0,
        },
    )
    orch = GpuBenchmarkOrchestrator(_FakeAdapter(_make_sys_info()), backend)
    report = orch.run(params=_fast_params())

    # Пик памяти — из env (дискретная логика), НЕ 76.8 ГБ/с из DRAM.
    assert report.mem_bandwidth_peak_gb_s == pytest.approx(504.0)
    assert "igpu_dram" not in report.peak_source
