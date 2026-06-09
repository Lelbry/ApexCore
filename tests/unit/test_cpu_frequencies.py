"""Тесты `infrastructure/cpu_frequencies.py` — базовая частота per-CPU."""

from __future__ import annotations

import platform

import pytest

from apexcore.infrastructure.cpu_frequencies import (
    _read_linux,
    _read_sysfs_freq,
    average_mhz,
    read_base_frequencies_by_cpu,
)

# ─────────────────────────── average_mhz ────────────────────────────


def test_average_mhz_hybrid_p_cores():
    """12900K: P-cores 0-15 имеют 3192 МГц, E-cores 16-23 — 2419 МГц."""
    freqs = {i: 3192.0 for i in range(16)}
    freqs.update({i: 2419.0 for i in range(16, 24)})
    assert average_mhz(freqs, tuple(range(0, 16))) == pytest.approx(3192.0)
    assert average_mhz(freqs, tuple(range(16, 24))) == pytest.approx(2419.0)


def test_average_mhz_mixed_values():
    """Если значения немного разные — берём арифметическое среднее."""
    freqs = {0: 3000.0, 1: 3100.0, 2: 3200.0}
    assert average_mhz(freqs, (0, 1, 2)) == pytest.approx(3100.0)


def test_average_mhz_partial_overlap():
    """Если часть CPU в запросе не найдена — усредняем только найденные."""
    freqs = {0: 3000.0, 2: 3200.0}
    assert average_mhz(freqs, (0, 1, 2)) == pytest.approx(3100.0)


def test_average_mhz_no_overlap_returns_none():
    freqs = {0: 3000.0}
    assert average_mhz(freqs, (5, 6, 7)) is None


def test_average_mhz_empty_input_returns_none():
    assert average_mhz({}, (0, 1)) is None
    assert average_mhz({0: 3000.0}, ()) is None


# ─────────────────────────── Linux sysfs ────────────────────────────


def test_read_sysfs_freq_prefers_base_frequency(tmp_path):
    """base_frequency имеет приоритет над cpuinfo_max_freq."""
    cpufreq = tmp_path / "cpufreq"
    cpufreq.mkdir()
    (cpufreq / "base_frequency").write_text("3200000")  # 3.2 ГГц в кГц
    (cpufreq / "cpuinfo_max_freq").write_text("5200000")  # 5.2 ГГц в кГц
    assert _read_sysfs_freq(cpufreq) == pytest.approx(3200.0)


def test_read_sysfs_freq_falls_back_to_cpuinfo_max(tmp_path):
    """Если нет base_frequency — берём cpuinfo_max_freq (на AMD это max-boost)."""
    cpufreq = tmp_path / "cpufreq"
    cpufreq.mkdir()
    (cpufreq / "cpuinfo_max_freq").write_text("4500000")  # 4.5 ГГц
    assert _read_sysfs_freq(cpufreq) == pytest.approx(4500.0)


def test_read_sysfs_freq_returns_none_when_no_files(tmp_path):
    cpufreq = tmp_path / "cpufreq"
    cpufreq.mkdir()
    assert _read_sysfs_freq(cpufreq) is None


def test_read_sysfs_freq_ignores_zero(tmp_path):
    cpufreq = tmp_path / "cpufreq"
    cpufreq.mkdir()
    (cpufreq / "base_frequency").write_text("0")
    (cpufreq / "cpuinfo_max_freq").write_text("4500000")
    assert _read_sysfs_freq(cpufreq) == pytest.approx(4500.0)


def test_read_sysfs_freq_handles_invalid_content(tmp_path):
    cpufreq = tmp_path / "cpufreq"
    cpufreq.mkdir()
    (cpufreq / "base_frequency").write_text("not a number")
    (cpufreq / "cpuinfo_max_freq").write_text("4500000")
    assert _read_sysfs_freq(cpufreq) == pytest.approx(4500.0)


def test_read_linux_hybrid_alder_lake(tmp_path):
    """12900K на Linux: 16 P-cores с 3200 МГц + 8 E-cores с 2400 МГц."""
    cpu_root = tmp_path / "cpu"
    for i in range(16):
        d = cpu_root / f"cpu{i}" / "cpufreq"
        d.mkdir(parents=True)
        (d / "base_frequency").write_text("3200000")
    for i in range(16, 24):
        d = cpu_root / f"cpu{i}" / "cpufreq"
        d.mkdir(parents=True)
        (d / "base_frequency").write_text("2400000")
    result = _read_linux(cpu_root)
    assert len(result) == 24
    assert all(result[i] == pytest.approx(3200.0) for i in range(16))
    assert all(result[i] == pytest.approx(2400.0) for i in range(16, 24))


def test_read_linux_skips_non_cpu_dirs(tmp_path):
    """Каталоги вида `cpuidle`, `cpufreq`, `cpu_capacity` — не CPU, игнорируем."""
    cpu_root = tmp_path / "cpu"
    cpu_root.mkdir()
    (cpu_root / "cpuidle").mkdir()
    (cpu_root / "cpufreq").mkdir()
    (cpu_root / "cpu0" / "cpufreq").mkdir(parents=True)
    (cpu_root / "cpu0" / "cpufreq" / "base_frequency").write_text("3000000")
    result = _read_linux(cpu_root)
    assert result == {0: pytest.approx(3000.0)}


def test_read_linux_returns_empty_when_no_dir(tmp_path):
    """Если /sys/devices/system/cpu нет — пустой dict, не исключение."""
    assert _read_linux(tmp_path / "nonexistent") == {}


# ─────────────────────────── top-level wrapper ────────────────────────────


def test_read_base_frequencies_handles_unknown_os(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert read_base_frequencies_by_cpu() == {}


def test_read_base_frequencies_swallows_exceptions(monkeypatch):
    """Если внутренний детектор кидает — наружу пустой dict, не исключение."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    def boom():
        raise OSError("simulated")

    monkeypatch.setattr(
        "apexcore.infrastructure.cpu_frequencies._read_windows", boom
    )
    assert read_base_frequencies_by_cpu() == {}
