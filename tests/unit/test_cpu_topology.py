"""Тесты `infrastructure/cpu_topology.py` — детекция P/E ядер (Intel hybrid)."""

from __future__ import annotations

import platform
from datetime import datetime, timezone

import pytest
from rich.console import Console

from apexcore.domain.models import CpuCores, SystemInfo
from apexcore.infrastructure.cpu_topology import (
    HybridTopology,
    _classify,
    _detect_linux,
    _parse_cpu_list,
    _parse_windows_buffer,
    detect_hybrid_topology,
)
from apexcore.interfaces.cli import render as render_mod
from apexcore.interfaces.cli.render import _normalize_arch

# ─────────────────────────── Windows API parser ────────────────────────────


def _make_core_record(eff_class: int, cpu_indices: list[int], ptr_size: int = 8) -> bytes:
    """Собрать одну SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX-запись с RelationProcessorCore.

    ``cpu_indices`` — список логических CPU, которые входят в affinity-маску
    этого ядра (для P-core с SMT это пара '[2*i, 2*i+1]', для E-core — один CPU).

    Layout (см. docstring в cpu_topology._parse_windows_buffer):
      0..3   Relationship   DWORD = 0
      4..7   Size           DWORD
      8      Flags          BYTE
      9      EfficiencyClass BYTE
      10..29 Reserved[20]
      30..31 GroupCount     WORD = 1
      32..   GroupAffinity[0]: Mask (ptr_size) + Group(WORD) + Reserved[3]
    """
    mask = sum(1 << idx for idx in cpu_indices)
    # 32 байта до GroupAffinity + Mask(ptr) + 2 (Group) + 6 (Reserved[3])
    rec_size = 32 + ptr_size + 8
    out = bytearray(rec_size)
    out[4:8] = rec_size.to_bytes(4, "little")
    out[9] = eff_class
    out[30:32] = (1).to_bytes(2, "little")
    out[32 : 32 + ptr_size] = mask.to_bytes(ptr_size, "little")
    return bytes(out)


def test_parse_windows_buffer_hybrid_8p_8e(monkeypatch):
    """i9-12900K: 8 P-cores (CPU 0-15 парами) + 8 E-cores (CPU 16-23 по одному)."""
    monkeypatch.setattr(platform, "architecture", lambda: ("64bit", ""))
    p = b"".join(
        _make_core_record(eff_class=1, cpu_indices=[2 * i, 2 * i + 1])
        for i in range(8)
    )
    e = b"".join(
        _make_core_record(eff_class=0, cpu_indices=[16 + i]) for i in range(8)
    )
    buf = p + e
    assert _parse_windows_buffer(buf, len(buf)) == HybridTopology(
        p_cores=8,
        e_cores=8,
        p_threads=16,
        e_threads=8,
        p_cpus=tuple(range(0, 16)),
        e_cpus=tuple(range(16, 24)),
    )


def test_parse_windows_buffer_uniform_amd_returns_none(monkeypatch):
    """AMD Ryzen / классический Intel: все ядра одного EfficiencyClass."""
    monkeypatch.setattr(platform, "architecture", lambda: ("64bit", ""))
    buf = b"".join(
        _make_core_record(eff_class=0, cpu_indices=[2 * i, 2 * i + 1])
        for i in range(16)
    )
    assert _parse_windows_buffer(buf, len(buf)) is None


def test_parse_windows_buffer_empty_returns_none(monkeypatch):
    monkeypatch.setattr(platform, "architecture", lambda: ("64bit", ""))
    assert _parse_windows_buffer(b"", 0) is None


def test_parse_windows_buffer_truncated_record_stops(monkeypatch):
    """Если в буфере «обрезок» — парсер должен не упасть, а вернуть, что распарсилось."""
    monkeypatch.setattr(platform, "architecture", lambda: ("64bit", ""))
    full = _make_core_record(eff_class=1, cpu_indices=[0, 1])
    truncated = full[:20]  # обрезан второй кусок
    buf = full + truncated  # один валидный + мусор
    # Один EfficiencyClass — не hybrid → None (а не исключение).
    assert _parse_windows_buffer(buf, len(buf)) is None


# ─────────────────────────── classify ────────────────────────────


def test_classify_two_classes():
    """Два класса (как 12900K) — нижний → E, верхний → P."""
    # eff_class=0 → 3 E-cores (CPU 16, 17, 18); eff_class=1 → 2 P-cores (CPU 0-3, попарно)
    result = _classify({0: [(16,), (17,), (18,)], 1: [(0, 1), (2, 3)]})
    assert result == HybridTopology(
        p_cores=2,
        e_cores=3,
        p_threads=4,
        e_threads=3,
        p_cpus=(0, 1, 2, 3),
        e_cpus=(16, 17, 18),
    )


def test_classify_single_class_returns_none():
    assert _classify({0: [(0, 1), (2, 3), (4, 5), (6, 7)]}) is None


def test_classify_three_classes_merges_into_p():
    """Гипотетический CPU с 3 классами: всё что не min — в P (чтобы не терять ядра)."""
    result = _classify(
        {
            0: [(20,), (21,)],
            1: [(8, 9), (10, 11), (12, 13)],
            2: [(0, 1), (2, 3)],
        }
    )
    assert result == HybridTopology(
        p_cores=5,
        e_cores=2,
        p_threads=10,
        e_threads=2,
        p_cpus=(0, 1, 2, 3, 8, 9, 10, 11, 12, 13),
        e_cpus=(20, 21),
    )


def test_classify_empty_returns_none():
    assert _classify({}) is None


# ─────────────────────────── Linux sysfs ────────────────────────────


def test_parse_cpu_list_simple():
    assert _parse_cpu_list("0,2,4") == [0, 2, 4]


def test_parse_cpu_list_ranges():
    assert _parse_cpu_list("0-7,16-23") == list(range(0, 8)) + list(range(16, 24))


def test_parse_cpu_list_single_range():
    assert _parse_cpu_list("0-3") == [0, 1, 2, 3]


def test_parse_cpu_list_empty_string():
    assert _parse_cpu_list("") == []


def test_detect_linux_hybrid_alder_lake(tmp_path):
    """Linux 5.20+ на 12900K: intel_core_0/cpus=0-15 (8P×SMT), intel_atom_0/cpus=16-23 (8E)."""
    types_dir = tmp_path / "types"
    (types_dir / "intel_core_0").mkdir(parents=True)
    (types_dir / "intel_atom_0").mkdir(parents=True)
    (types_dir / "intel_core_0" / "cpus").write_text("0-15")
    (types_dir / "intel_atom_0" / "cpus").write_text("16-23")
    assert _detect_linux(types_dir) == HybridTopology(
        p_cores=8,
        e_cores=8,
        p_threads=16,
        e_threads=8,
        p_cpus=tuple(range(0, 16)),
        e_cpus=tuple(range(16, 24)),
    )


def test_detect_linux_no_types_dir_returns_none(tmp_path):
    """Старое ядро без CPU types — детекция недоступна."""
    assert _detect_linux(tmp_path / "nonexistent") is None


def test_detect_linux_only_core_no_atom_returns_none(tmp_path):
    """Не-гибридный Intel: только intel_core, нет intel_atom — не hybrid."""
    types_dir = tmp_path / "types"
    (types_dir / "intel_core_0").mkdir(parents=True)
    (types_dir / "intel_core_0" / "cpus").write_text("0-15")
    assert _detect_linux(types_dir) is None


# ─────────────────────────── top-level wrapper ────────────────────────────


def test_detect_hybrid_topology_swallows_exceptions(monkeypatch):
    """Любая ошибка изнутри детектора → None, наружу не пробрасывается."""
    def boom():
        raise OSError("simulated")

    monkeypatch.setattr(
        "apexcore.infrastructure.cpu_topology._detect_windows", boom
    )
    monkeypatch.setattr(
        "apexcore.infrastructure.cpu_topology._detect_linux", boom
    )
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert detect_hybrid_topology() is None


def test_detect_hybrid_topology_on_unknown_os_returns_none(monkeypatch):
    """На macOS / прочих ОС детектор корректно возвращает None."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert detect_hybrid_topology() is None


# ─────────────────────────── render integration ────────────────────────────


def _capture_system_info(info: SystemInfo, monkeypatch) -> str:
    """Отрендерить SystemInfo в plain-text для проверки строки «Ядра»."""
    fake_console = Console(
        width=160, record=True, force_terminal=False, color_system=None
    )
    monkeypatch.setattr(render_mod, "console", fake_console)
    render_mod.render_system_info(info)
    return fake_console.export_text()


def _sample(
    cores: CpuCores,
    *,
    cpu_base_mhz: float | None = None,
    cpu_base_p_mhz: float | None = None,
    cpu_base_e_mhz: float | None = None,
) -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0.26200",
        cpu_model="Intel Core i9-12900K",
        cpu_cores=cores,
        ram_total_gb=32.0,
        gpu_list=[],
        cpu_arch="AMD64",
        hostname="test",
        cpu_base_mhz=cpu_base_mhz,
        cpu_base_p_mhz=cpu_base_p_mhz,
        cpu_base_e_mhz=cpu_base_e_mhz,
        timestamp=datetime.now(timezone.utc),
    )


def test_render_hybrid_shows_p_and_e(monkeypatch):
    info = _sample(
        CpuCores(physical=16, logical=24, p_cores=8, e_cores=8, p_threads=16, e_threads=8)
    )
    out = _capture_system_info(info, monkeypatch)
    assert "P 8 / 16 потоков + E 8 / 8 потоков (всего 16 / 24)" in out
    assert "физ." not in out  # старый формат не должен показываться


def test_render_non_hybrid_shows_simple_format(monkeypatch):
    info = _sample(CpuCores(physical=16, logical=32))
    out = _capture_system_info(info, monkeypatch)
    assert "16 ядер / 32 потока" in out
    assert "физ." not in out


@pytest.mark.parametrize(
    "p_cores,e_cores,p_threads,e_threads",
    [
        (None, 8, 16, 8),   # частично заполнено — не hybrid
        (8, None, 16, 8),
        (8, 8, None, 8),
        (8, 8, 16, None),
    ],
)
def test_render_partial_hybrid_falls_back(
    monkeypatch, p_cores, e_cores, p_threads, e_threads
):
    """Если хотя бы одно из p_cores/e_cores отсутствует — fallback на обычный формат."""
    info = _sample(
        CpuCores(
            physical=16,
            logical=24,
            p_cores=p_cores,
            e_cores=e_cores,
            p_threads=p_threads,
            e_threads=e_threads,
        )
    )
    out = _capture_system_info(info, monkeypatch)
    assert "16 ядер / 24 потока" in out


# ─────────────────────────── normalize_arch ────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AMD64", "x64"),       # Windows на Intel/AMD
        ("amd64", "x64"),
        ("x86_64", "x64"),      # Linux Intel/AMD
        ("x64", "x64"),
        ("aarch64", "ARM64"),   # Linux ARM
        ("ARM64", "ARM64"),     # Windows ARM
        ("arm64", "ARM64"),
        ("i386", "x86"),        # 32-bit Intel — явно отличается от x64
        ("i486", "x86"),
        ("i586", "x86"),
        ("i686", "x86"),
        ("armv7l", "ARM"),      # 32-bit ARM
        ("armv6l", "ARM"),
        ("mips64", "mips64"),   # экзотика — без изменений
        ("riscv64", "riscv64"),
    ],
)
def test_normalize_arch_known_values(raw, expected):
    assert _normalize_arch(raw) == expected


def test_normalize_arch_none_returns_dash():
    assert _normalize_arch(None) == "—"


def test_normalize_arch_empty_string_returns_dash():
    assert _normalize_arch("") == "—"


def test_render_arch_normalized_on_windows(monkeypatch):
    """Реальный AMD64 (Windows) в выводе таблицы → 'x64'."""
    info = _sample(CpuCores(physical=16, logical=24))
    out = _capture_system_info(info, monkeypatch)
    assert "x64" in out
    assert "AMD64" not in out
    # Старые форматы тоже не должны просочиться:
    assert "x86-64" not in out
    assert "(64-бит)" not in out


# ─────────────────────────── частоты P/E vs одиночная ────────────────────


def test_render_hybrid_shows_two_frequency_rows(monkeypatch):
    """12900K-like: P 3.20 ГГц + E 2.40 ГГц → две отдельные строки."""
    info = _sample(
        CpuCores(physical=16, logical=24, p_cores=8, e_cores=8, p_threads=16, e_threads=8),
        cpu_base_mhz=2933.0,  # средняя по всем — не должна показываться при hybrid
        cpu_base_p_mhz=3192.0,
        cpu_base_e_mhz=2419.0,
    )
    out = _capture_system_info(info, monkeypatch)
    assert "Частота P-ядер" in out
    assert "Частота E-ядер" in out
    assert "3.19 ГГц" in out
    assert "2.42 ГГц" in out
    assert "Базовая частота CPU" not in out
    assert "Турбо" not in out  # в info турбо не показываем


def test_render_non_hybrid_shows_single_frequency_row(monkeypatch):
    """AMD Ryzen / classic Intel: одна строка «Базовая частота CPU»."""
    info = _sample(
        CpuCores(physical=16, logical=32),
        cpu_base_mhz=4500.0,
    )
    out = _capture_system_info(info, monkeypatch)
    assert "Базовая частота CPU" in out
    assert "4.50 ГГц" in out
    assert "Частота P-ядер" not in out
    assert "Частота E-ядер" not in out


def test_render_hybrid_with_tiny_pe_diff_falls_back_to_single(monkeypatch):
    """Если P и E отличаются меньше 50 МГц (шум sysfs) — одна строка."""
    info = _sample(
        CpuCores(physical=16, logical=24, p_cores=8, e_cores=8, p_threads=16, e_threads=8),
        cpu_base_mhz=3200.0,
        cpu_base_p_mhz=3210.0,
        cpu_base_e_mhz=3190.0,  # разница 20 МГц — не считается разными
    )
    out = _capture_system_info(info, monkeypatch)
    assert "Частота P-ядер" not in out
    assert "Базовая частота CPU" in out
    assert "3.20 ГГц" in out


def test_render_no_frequency_data_omits_row(monkeypatch):
    """Если ни одно из полей частот не задано — строки «Частота» не выводим."""
    info = _sample(CpuCores(physical=16, logical=32))
    out = _capture_system_info(info, monkeypatch)
    assert "Частота" not in out
    assert "Базовая частота CPU" not in out


def test_render_legacy_base_clock_ghz_fallback(monkeypatch):
    """Старые вызывающие, не использующие SystemInfo поля частоты,
    могут передать legacy `base_clock_ghz` — поддерживаем для обратной совместимости."""
    info = _sample(CpuCores(physical=16, logical=32))
    fake_console = Console(width=160, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(render_mod, "console", fake_console)
    render_mod.render_system_info(info, base_clock_ghz=3.5)
    out = fake_console.export_text()
    assert "Базовая частота CPU" in out
    assert "3.50 ГГц" in out
