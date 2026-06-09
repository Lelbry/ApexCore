"""Тесты `application/general_benchmark.GeneralBenchmarkOrchestrator`.

Главные инварианты:
1. Прогон собирает все фазы и считает score корректно.
2. Прогон с моками stress-движков и disk-бенчей выполняется быстро (≤ 1 c).
3. Если roofline вернул None — соответствующий ratio None, score None,
   но отчёт всё равно собран (с заполненными raw-измерениями).
4. ``on_progress`` вызывается для всех 5 фаз.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apexcore.application import general_benchmark as gb_mod
from apexcore.application.general_benchmark import (
    GeneralBenchmarkOrchestrator,
    GeneralBenchmarkParams,
)
from apexcore.domain.models import (
    CpuCores,
    MicroBenchResult,
    StressResult,
    SystemInfo,
)


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
        cpu_model="Intel(R) Core(TM) i7-12700K @ 5.0GHz",
        cpu_cores=CpuCores(physical=12, logical=20),
        ram_total_gb=32.0,
        gpu_list=["RTX 4070"],
        cpu_arch="x86_64",
        hostname="test-host",
        cpu_base_mhz=3600.0,
        timestamp=datetime.now(timezone.utc),
    )


class _MockDgemmEngine:
    name = "builtin_large_dgemm"
    category = "cpu_fp"
    is_external = False

    def __init__(self, gflops: float = 600.0) -> None:
        self._gflops = gflops

    def is_available(self) -> bool:
        return True

    def run(self, duration_sec, threads=None, cancel_token=None):
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=0.01,
            throughput=self._gflops,
            throughput_unit="GFLOPS",
        )


class _MockStreamEngine:
    name = "builtin_large_stream"
    category = "ram_bw"
    is_external = False

    def __init__(self, gb_s: float = 30.0) -> None:
        self._gb_s = gb_s

    def is_available(self) -> bool:
        return True

    def run(self, duration_sec, threads=None, cancel_token=None):
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=0.01,
            throughput=self._gb_s,
            throughput_unit="GB/s",
        )


class _MockDiskBench:
    """Универсальный мок для disk-бенчей. category одна, name настраиваемое."""

    def __init__(self, name: str, value_mb_s: float) -> None:
        self.name = name
        self.category = "disk"
        self.unit = "MB/s"
        self._value = value_mb_s
        # Disk-бенчи принимают target_dir в конструкторе — оставляем атрибут
        # для совместимости с конструкторами в orchestrator'е.
        self.target_dir = None

    def is_available(self) -> bool:
        return True

    def run(self, duration_sec, threads=None, cancel_token=None):
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=self._value,
            unit=self.unit,
            duration_actual_sec=0.01,
            iterations=1,
        )


@pytest.fixture
def patched_orchestrator(monkeypatch):
    """Подменяет все тяжёлые движки и disk-бенчи, чтобы тест быстрый."""

    monkeypatch.setattr(
        gb_mod, "BuiltinLargeDgemmEngine", lambda: _MockDgemmEngine(gflops=600.0)
    )
    monkeypatch.setattr(
        gb_mod, "BuiltinLargeStreamEngine", lambda: _MockStreamEngine(gb_s=30.0)
    )

    def fake_disk_seq_read(target_dir=None):
        return _MockDiskBench("disk_seq_read", 2500.0)

    def fake_disk_random(target_dir=None):
        return _MockDiskBench("disk_random_read", 500.0)

    def fake_disk_seq_write(target_dir=None):
        return _MockDiskBench("disk_seq_write", 1800.0)

    monkeypatch.setattr(gb_mod, "DiskSequentialReadBench", fake_disk_seq_read)
    monkeypatch.setattr(gb_mod, "DiskRandomReadBench", fake_disk_random)
    monkeypatch.setattr(gb_mod, "DiskSequentialWriteBench", fake_disk_seq_write)

    # Запас места на boot-диске — много.
    class _Free:
        free = 100 * 1024**3

    monkeypatch.setattr(gb_mod.shutil, "disk_usage", lambda _path: _Free())

    # Boot-диск — NVMe.
    from apexcore.infrastructure.disk_inventory import PhysicalDisk

    fake_physical = PhysicalDisk(
        index=0,
        model="Kingston KC3000",
        bus_type="NVMe",
        media_type="SSD",
        size_gb=2000.0,
        letters=["C:"],
    )
    monkeypatch.setattr(
        gb_mod, "get_boot_drive", lambda: ("C:\\", fake_physical)
    )


def test_orchestrator_runs_all_phases_and_computes_score(patched_orchestrator):
    """Полный happy-path: все измерения собраны, score не None."""
    adapter = _FakeAdapter(_make_sys_info())
    params = GeneralBenchmarkParams(
        cpu_phase_duration_sec=0.01,
        disk_read_duration_sec=0.01,
        cooldown_sec=0.0,
    )
    orch = GeneralBenchmarkOrchestrator(adapter)

    progress_calls: list[tuple[str, int, int]] = []

    def on_progress(phase, idx, total):
        progress_calls.append((phase, idx, total))

    report = orch.run(params=params, on_progress=on_progress)

    # Прогресс по всем 5 фазам.
    phase_names = [c[0] for c in progress_calls]
    assert phase_names == [
        "dgemm",
        "stream",
        "disk_seq_read",
        "disk_random_read",
        "disk_seq_write",
    ]

    # Измерения сохранены.
    assert report.dgemm_gflops == pytest.approx(600.0)
    assert report.stream_gb_s == pytest.approx(30.0)
    assert report.disk_seq_read_mb_s == pytest.approx(2500.0)
    assert report.disk_random_read_mb_s == pytest.approx(500.0)
    assert report.disk_seq_write_mb_s == pytest.approx(1800.0)

    # Disk-метаданные.
    assert report.disk_media_label == "NVMe"
    assert report.boot_drive_path == "C:\\"
    assert report.disk_model == "Kingston KC3000"

    # Roofline-пики (для i7-12700K @ 5 GHz должны посчитаться).
    assert report.dgemm_peak_gflops is not None
    assert report.stream_peak_gb_s is not None  # либо реальный, либо fallback

    # Score рассчитан.
    assert report.score is not None
    assert 0 < report.score <= 10_000

    # Ratio clamp'нуты к ≤1.0.
    assert report.r_dgemm is not None and report.r_dgemm <= 1.0
    assert report.r_stream is not None and report.r_stream <= 1.0
    assert report.r_disk is not None and report.r_disk <= 1.0

    # Cancelled = False, notes пусто или содержит только sanity warnings.
    assert report.cancelled is False


def test_orchestrator_score_none_when_disk_skipped(monkeypatch, patched_orchestrator):
    """Если на boot-диске нет места — disk-фаза пропускается, score = None."""

    class _LowFree:
        free = 100 * 1024**2  # 100 МБ — мало

    monkeypatch.setattr(gb_mod.shutil, "disk_usage", lambda _path: _LowFree())

    adapter = _FakeAdapter(_make_sys_info())
    params = GeneralBenchmarkParams(
        cpu_phase_duration_sec=0.01,
        disk_read_duration_sec=0.01,
        cooldown_sec=0.0,
    )
    orch = GeneralBenchmarkOrchestrator(adapter)
    report = orch.run(params=params)

    # CPU/RAM посчитаны.
    assert report.dgemm_gflops is not None
    assert report.stream_gb_s is not None
    # Disk пропущен.
    assert report.disk_seq_read_mb_s is None
    assert report.disk_random_read_mb_s is None
    assert report.disk_seq_write_mb_s is None
    # Score = None.
    assert report.r_disk is None
    assert report.score is None
    # В notes — причина пропуска.
    assert any("disk-фаза пропущена" in n for n in report.notes)


def test_orchestrator_handles_unknown_disk_with_fallback_profile(
    monkeypatch, patched_orchestrator
):
    """Если boot-диск не нашёлся среди PhysicalDisk — используется UNKNOWN_PROFILE."""
    monkeypatch.setattr(gb_mod, "get_boot_drive", lambda: ("C:\\", None))

    adapter = _FakeAdapter(_make_sys_info())
    params = GeneralBenchmarkParams(
        cpu_phase_duration_sec=0.01,
        disk_read_duration_sec=0.01,
        cooldown_sec=0.0,
    )
    orch = GeneralBenchmarkOrchestrator(adapter)
    report = orch.run(params=params)

    # disk_media_label = "HDD" (UNKNOWN_PROFILE = HDD_PROFILE).
    assert report.disk_media_label == "HDD"
    assert report.disk_model is None
    # Score рассчитан (с консервативными пиками).
    assert report.score is not None
