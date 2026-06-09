"""Unit-тесты на парсеры cache topology (sysfs + WMI helper)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apexcore.domain.cache import CacheTopology
from apexcore.infrastructure.adapters.cache import (
    DEFAULT_L1_BYTES,
    DEFAULT_L2_BYTES,
    DEFAULT_L3_BYTES,
    DRAM_BUFFER_BYTES,
    default_cache_topology,
    detect_topology_from_sysfs,
    parse_size_string,
    topology_from_wmi_kb,
)


def test_default_topology_has_all_levels():
    topo = default_cache_topology()
    assert isinstance(topo, CacheTopology)
    names = [lvl.name for lvl in topo.levels]
    assert names == ["L1", "L2", "L3", "DRAM"]
    for lvl in topo.levels:
        assert lvl.source == "fallback"


def test_default_topology_dram_buffer_size():
    topo = default_cache_topology()
    dram = topo.levels[3]
    assert dram.name == "DRAM"
    assert dram.size_bytes == DRAM_BUFFER_BYTES


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("32K", 32 * 1024),
        ("32k", 32 * 1024),
        ("1024K", 1024 * 1024),
        ("1024 KB", 1024 * 1024),
        ("8M", 8 * 1024 * 1024),
        ("4MB", 4 * 1024 * 1024),
        ("2G", 2 * 1024 * 1024 * 1024),
        ("512", 512),
    ],
)
def test_parse_size_string_valid(raw, expected):
    assert parse_size_string(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "abc", "K", "12X"])
def test_parse_size_string_invalid(raw):
    assert parse_size_string(raw) is None


def test_detect_topology_from_sysfs_missing_root(tmp_path):
    """Если корневая директория не существует — возвращается None."""
    fake_root = tmp_path / "no_such_dir"
    assert detect_topology_from_sysfs(fake_root) is None


def _make_fake_sysfs(root: Path, entries: list[dict]) -> None:
    """Хелпер: разложить структуру index*/{level,type,size}."""
    for i, entry in enumerate(entries):
        idx_dir = root / f"index{i}"
        idx_dir.mkdir(parents=True)
        (idx_dir / "level").write_text(str(entry["level"]))
        (idx_dir / "type").write_text(entry.get("type", "Unified"))
        (idx_dir / "size").write_text(entry["size"])


def test_detect_topology_from_sysfs_full(tmp_path):
    root = tmp_path / "cache"
    _make_fake_sysfs(
        root,
        [
            {"level": 1, "type": "Data", "size": "32K"},
            {"level": 1, "type": "Instruction", "size": "32K"},
            {"level": 2, "type": "Unified", "size": "256K"},
            {"level": 3, "type": "Unified", "size": "8M"},
        ],
    )
    topo = detect_topology_from_sysfs(root)
    assert topo is not None
    assert topo.levels[0].size_bytes == 32 * 1024
    assert topo.levels[0].source == "sysfs"
    assert topo.levels[1].size_bytes == 256 * 1024
    assert topo.levels[2].size_bytes == 8 * 1024 * 1024
    # DRAM всегда fallback.
    assert topo.levels[3].name == "DRAM"
    assert topo.levels[3].source == "fallback"


def test_detect_topology_from_sysfs_skips_l1_instruction(tmp_path):
    """L1 instruction-кеш не должен заменять L1 data-кеш."""
    root = tmp_path / "cache"
    _make_fake_sysfs(
        root,
        [
            {"level": 1, "type": "Instruction", "size": "32K"},
            {"level": 1, "type": "Data", "size": "48K"},
        ],
    )
    topo = detect_topology_from_sysfs(root)
    assert topo is not None
    assert topo.levels[0].size_bytes == 48 * 1024


def test_detect_topology_from_sysfs_partial(tmp_path):
    """Если найден только L2 — L1/L3 заполняются fallback."""
    root = tmp_path / "cache"
    _make_fake_sysfs(
        root,
        [{"level": 2, "type": "Unified", "size": "1024K"}],
    )
    topo = detect_topology_from_sysfs(root)
    assert topo is not None
    assert topo.levels[0].source == "fallback"
    assert topo.levels[0].size_bytes == DEFAULT_L1_BYTES
    assert topo.levels[1].source == "sysfs"
    assert topo.levels[1].size_bytes == 1024 * 1024
    assert topo.levels[2].source == "fallback"
    assert topo.levels[2].size_bytes == DEFAULT_L3_BYTES


def test_detect_topology_from_sysfs_empty(tmp_path):
    """Пустая директория без index* → None (вызывающий применит fallback)."""
    root = tmp_path / "cache"
    root.mkdir()
    assert detect_topology_from_sysfs(root) is None


def test_topology_from_wmi_kb_full():
    topo = topology_from_wmi_kb(l1_kb=None, l2_kb=512, l3_kb=12 * 1024)
    # L1 — None → fallback
    assert topo.levels[0].source == "fallback"
    assert topo.levels[0].size_bytes == DEFAULT_L1_BYTES
    # L2/L3 — wmi
    assert topo.levels[1].source == "wmi"
    assert topo.levels[1].size_bytes == 512 * 1024
    assert topo.levels[2].source == "wmi"
    assert topo.levels[2].size_bytes == 12 * 1024 * 1024
    # DRAM всегда fallback.
    assert topo.levels[3].source == "fallback"


def test_topology_from_wmi_kb_zero_to_fallback():
    """Значение 0 (так бывает на VM) трактуется как «не определено»."""
    topo = topology_from_wmi_kb(l1_kb=0, l2_kb=0, l3_kb=0)
    for lvl in topo.levels[:3]:
        assert lvl.source == "fallback"
    assert topo.levels[0].size_bytes == DEFAULT_L1_BYTES
    assert topo.levels[1].size_bytes == DEFAULT_L2_BYTES
    assert topo.levels[2].size_bytes == DEFAULT_L3_BYTES
