"""Юнит-тесты для infrastructure/dram_info.py.

Покрывают чистый парсинг ``dmidecode -t 17`` и поведение кеша
``get_dram_info`` (платформенный reader вызывается один раз за lifetime
процесса, объём обновляется из свежего total_gb).
"""

from __future__ import annotations

import pytest

from apexcore.infrastructure import dram_info

# Реалистичный вывод `dmidecode -t 17`: два заполненных слота DDR5 4800 +
# один пустой (No Module Installed) который должен отфильтроваться.
_DMIDECODE_2x_DDR5 = """# dmidecode 3.3
Getting SMBIOS data from sysfs.
SMBIOS 3.5.0 present.

Handle 0x0040, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x003F
\tTotal Width: 64 bits
\tData Width: 64 bits
\tSize: 8 GB
\tForm Factor: SODIMM
\tLocator: DIMM 0
\tBank Locator: P0 CHANNEL A
\tType: DDR5
\tType Detail: Synchronous Unbuffered (Unregistered)
\tSpeed: 4800 MT/s
\tManufacturer: Samsung
\tConfigured Memory Speed: 4800 MT/s

Handle 0x0042, DMI type 17, 92 bytes
Memory Device
\tSize: 8 GB
\tLocator: DIMM 0
\tBank Locator: P0 CHANNEL B
\tType: DDR5
\tSpeed: 4800 MT/s
\tConfigured Memory Speed: 4800 MT/s

Handle 0x0044, DMI type 17, 92 bytes
Memory Device
\tSize: No Module Installed
\tLocator: DIMM 1
\tType: Unknown
"""

_DMIDECODE_EMPTY = """# dmidecode 3.3
Handle 0x0044, DMI type 17, 92 bytes
Memory Device
\tSize: No Module Installed
\tLocator: DIMM 1
\tType: Unknown
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    """Сбрасываем module-level кеш до и после каждого теста."""
    dram_info.reset_cache()
    yield
    dram_info.reset_cache()


# ─── _parse_dmidecode ────────────────────────────────────────────────────────


def test_parse_dmidecode_two_ddr5_modules():
    parsed = dram_info._parse_dmidecode(_DMIDECODE_2x_DDR5)
    assert parsed["type"] == "DDR5"
    assert parsed["speed_mts"] == pytest.approx(4800.0)
    assert parsed["modules"] == 2  # пустой слот отфильтрован
    assert parsed["source"] == "dmidecode"


def test_parse_dmidecode_all_empty_slots():
    parsed = dram_info._parse_dmidecode(_DMIDECODE_EMPTY)
    assert parsed["modules"] is None
    assert parsed["type"] is None
    assert parsed["speed_mts"] is None
    assert parsed["source"] == "dmidecode-empty"


def test_parse_dmidecode_prefers_configured_speed():
    """Если Configured Memory Speed отличается от Speed — берём configured."""
    text = (
        "Memory Device\n\tSize: 16 GB\n\tType: DDR4\n"
        "\tSpeed: 3200 MT/s\n\tConfigured Memory Speed: 2666 MT/s\n"
    )
    parsed = dram_info._parse_dmidecode(text)
    assert parsed["type"] == "DDR4"
    assert parsed["speed_mts"] == pytest.approx(2666.0)
    assert parsed["modules"] == 1


# ─── get_dram_info + кеш ───────────────────────────────────────────────────────


def test_get_dram_info_caches_platform_reader(monkeypatch):
    """Платформенный reader вызывается ровно один раз; далее — из кеша."""
    calls = {"n": 0}

    def fake_reader():
        calls["n"] += 1
        return {"type": "DDR5", "speed_mts": 6400.0, "modules": 2, "source": "test"}

    monkeypatch.setattr(dram_info, "_read_windows", fake_reader)
    monkeypatch.setattr(dram_info, "_read_linux", fake_reader)

    first = dram_info.get_dram_info(total_gb=31.7, cpu_model=None)
    assert first["available"] is True
    assert first["type"] == "DDR5"
    assert first["speed_mts"] == pytest.approx(6400.0)
    assert first["modules"] == 2
    assert calls["n"] == 1

    # Второй вызов — из кеша, reader не дёргается повторно.
    second = dram_info.get_dram_info(total_gb=31.7, cpu_model=None)
    assert calls["n"] == 1
    assert second["type"] == "DDR5"


def test_get_dram_info_updates_total_gb_from_cache(monkeypatch):
    """Объём берётся из свежего total_gb даже при попадании в кеш."""
    monkeypatch.setattr(
        dram_info, "_read_windows",
        lambda: {"type": "DDR5", "speed_mts": 6400.0, "modules": 2, "source": "test"},
    )
    monkeypatch.setattr(
        dram_info, "_read_linux",
        lambda: {"type": "DDR5", "speed_mts": 6400.0, "modules": 2, "source": "test"},
    )

    dram_info.get_dram_info(total_gb=31.7, cpu_model=None)
    updated = dram_info.get_dram_info(total_gb=15.4, cpu_model=None)
    assert updated["total_gb"] == pytest.approx(15.4)


def test_get_dram_info_unavailable_keeps_volume(monkeypatch):
    """Если reader вернул None (нет прав) — объём остаётся, детали None."""
    monkeypatch.setattr(dram_info, "_read_windows", lambda: None)
    monkeypatch.setattr(dram_info, "_read_linux", lambda: None)

    info = dram_info.get_dram_info(total_gb=15.4, cpu_model=None)
    assert info["available"] is False
    assert info["total_gb"] == pytest.approx(15.4)
    assert info["type"] is None
    assert info["speed_mts"] is None
    assert info["modules"] is None
    assert info["source"] == "psutil-only"


# ─── channels: эвристика по CPU + cap по числу модулей ────────────────────────


def _reader_with_modules(modules):
    def _r():
        return {"type": "DDR5", "speed_mts": 6400.0, "modules": modules, "source": "test"}
    return _r


def test_channels_capped_to_single_module(monkeypatch):
    """Одна планка на 2-канальной платформе → 1 канал, не платформенный максимум."""
    monkeypatch.setattr(dram_info, "_read_windows", _reader_with_modules(1))
    monkeypatch.setattr(dram_info, "_read_linux", _reader_with_modules(1))
    info = dram_info.get_dram_info(total_gb=16.0, cpu_model="AMD Ryzen 7 6800H with Radeon Graphics")
    assert info["modules"] == 1
    assert info["channels"] == 1


def test_channels_platform_max_for_four_modules(monkeypatch):
    """4 планки на 2-канальной платформе → 2 канала (min(2, 4))."""
    monkeypatch.setattr(dram_info, "_read_windows", _reader_with_modules(4))
    monkeypatch.setattr(dram_info, "_read_linux", _reader_with_modules(4))
    info = dram_info.get_dram_info(total_gb=64.0, cpu_model="Intel Core i9-12900K")
    assert info["modules"] == 4
    assert info["channels"] == 2


def test_channels_none_for_unknown_cpu(monkeypatch):
    """Нераспознанный CPU → channels None → UI покажет «н/д»."""
    monkeypatch.setattr(dram_info, "_read_windows", _reader_with_modules(2))
    monkeypatch.setattr(dram_info, "_read_linux", _reader_with_modules(2))
    info = dram_info.get_dram_info(total_gb=16.0, cpu_model="Some Exotic CPU UltraThing 9999")
    assert info["channels"] is None
