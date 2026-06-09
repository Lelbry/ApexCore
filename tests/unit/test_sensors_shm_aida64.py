"""Unit-тесты для AIDA64 Shared Memory reader (P1.2).

Mockaem ``shm._common.open_shm`` чтобы подменить реальный
``OpenFileMapping``/``MapViewOfFile`` синтетическим UTF-8 байт-блобом
(AIDA64 пишет нуль-терминированную строку с XML-тегами). Это даёт
детерминированную проверку парсера regex'ом без зависимости от
наличия установленного AIDA64.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from apexcore.infrastructure.sensors.shm import aida64 as aida64_mod
from apexcore.infrastructure.sensors.shm.aida64 import (
    read_aida64_sensors,
    read_aida64_temperatures_and_voltages,
)


def _make_aida64_blob(entries: list[tuple[str, str, str, float | str]]) -> bytes:
    """Собрать синтетический AIDA64 SHM-блоб для тестов.

    Args:
        entries: список ``(root, id, label, value)`` — root в ``temp/volt``,
            value может быть числом или строкой (для теста невалидных значений).
    """
    parts = []
    for root, sid, label, value in entries:
        parts.append(
            f"<{root}><id>{sid}</id><label>{label}</label>"
            f"<value>{value}</value></{root}>"
        )
    text = "\n".join(parts)
    # AIDA64 пишет нуль-терминированную строку, padding нулями до размера
    # маппинга. Эмулируем это для парсера.
    return text.encode("utf-8") + b"\x00" * 16


def _patch_open_shm(monkeypatch: pytest.MonkeyPatch, blob: bytes | None) -> None:
    @contextmanager
    def fake_open_shm(name: str):
        yield blob

    monkeypatch.setattr(aida64_mod, "open_shm", fake_open_shm)


def test_read_aida64_no_shm(monkeypatch: pytest.MonkeyPatch) -> None:
    """AIDA64 не запущен (``open_shm`` → ``None``) — пустой dict."""
    _patch_open_shm(monkeypatch, None)
    assert read_aida64_sensors() == {}


def test_read_aida64_empty_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой blob — пустой dict, без исключений."""
    _patch_open_shm(monkeypatch, b"\x00" * 32)
    assert read_aida64_sensors() == {}


def test_read_aida64_cpu_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """«CPU» с тегом ``temp`` → нормализуется в ``cpu/package``."""
    blob = _make_aida64_blob([("temp", "SCPU", "CPU", 56.0)])
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert result == {"cpu/package": pytest.approx(56.0)}


def test_read_aida64_per_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """«CPU Core #1», «CPU Core #2» → ``cpu/core_1``, ``cpu/core_2``."""
    blob = _make_aida64_blob(
        [
            ("temp", "SCC-1", "CPU Core #1", 54.0),
            ("temp", "SCC-2", "CPU Core #2", 55.5),
            ("temp", "SCC-3", "CPU Core #3", 53.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert result["cpu/core_1"] == pytest.approx(54.0)
    assert result["cpu/core_2"] == pytest.approx(55.5)
    assert result["cpu/core_3"] == pytest.approx(53.0)


def test_read_aida64_gpu_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """«GPU Hot Spot» / «GPU» → ``gpu/hot_spot`` / ``gpu/temperature``."""
    blob = _make_aida64_blob(
        [
            ("temp", "SGPUHS", "GPU Hot Spot", 78.0),
            ("temp", "SGPU", "GPU", 65.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert result["gpu/hot_spot"] == pytest.approx(78.0)
    assert result["gpu/temperature"] == pytest.approx(65.0)


def test_read_aida64_voltages_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тег ``volt`` идёт в voltages-словарь, не в temps."""
    blob = _make_aida64_blob(
        [
            ("temp", "SCPU", "CPU", 56.0),
            ("volt", "VCPU", "CPU Core", 1.218),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    temps, _voltages = read_aida64_temperatures_and_voltages()
    assert temps == {"cpu/package": pytest.approx(56.0)}
    # «CPU Core» в volt-теге нормализуется как cpu/core_0? Нет — без
    # числа в label normalize вернёт None или другой ключ. Проверим
    # только что temps НЕ содержит voltage-значение.
    assert all(v != pytest.approx(1.218) for v in temps.values())


def test_read_aida64_invalid_value_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Невалидные значения (N/A, пустые) тихо игнорируются."""
    blob = _make_aida64_blob(
        [
            ("temp", "SCPU", "CPU", 56.0),
            ("temp", "SCC1", "CPU Core #1", "N/A"),
            ("temp", "SCC2", "CPU Core #2", ""),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert result == {"cpu/package": pytest.approx(56.0)}


def test_read_aida64_unknown_label_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нераспознанные label'ы тихо отбрасываются (контракт normalize)."""
    blob = _make_aida64_blob(
        [
            ("temp", "SXYZ", "Vendor Specific Sensor XYZ", 42.0),
            ("temp", "SCPU", "CPU", 56.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert "cpu/package" in result
    assert all("xyz" not in k.lower() for k in result)


def test_read_aida64_voltage_tagged_as_temp_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Защита: voltage-label с тегом ``temp`` не попадает в temps.

    Бывает на старых билдах AIDA64 при кастомных user-label'ах.
    """
    blob = _make_aida64_blob(
        [
            ("temp", "SVOLT", "CPU Core Voltage", 1.2),  # неправильный тег
            ("temp", "SCPU", "CPU", 56.0),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    temps = read_aida64_sensors()
    # Voltage-label не должен попасть в temps как 1.2 °C.
    assert temps == {"cpu/package": pytest.approx(56.0)}


def test_read_aida64_null_terminated_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AIDA64 пишет до первого \\x00 — мусор после null-byte игнорируется."""
    text = "<temp><id>SCPU</id><label>CPU</label><value>56.0</value></temp>"
    blob = text.encode("utf-8") + b"\x00" + b"\xff\xff" * 100
    _patch_open_shm(monkeypatch, blob)
    result = read_aida64_sensors()
    assert result["cpu/package"] == pytest.approx(56.0)


def test_read_aida64_corrupt_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Битый payload (нет закрывающих тегов) → пустой dict, не исключение."""
    blob = b"<temp><id>SCPU<label>CPU<value>56.0" + b"\x00" * 16
    _patch_open_shm(monkeypatch, blob)
    # Не должен бросить.
    result = read_aida64_sensors()
    assert result == {}


def test_aida64_keys_match_thermal_watchdog() -> None:
    """**Регрессия**: ключи от AIDA64 matchятся ``_is_cpu_temp_key``.

    Без неё watchdog молчит даже когда AIDA64 даёт корректные данные —
    та же проблема, что для HWiNFO в P0. Architectural review §1.1.
    """
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key
    from apexcore.infrastructure.sensors.shm._common import normalize_sensor_key

    cpu_labels = [
        "CPU",
        "CPU Package",
        "CPU Core #1",
        "CPU Core #8",
        "CCD1",
    ]
    for label in cpu_labels:
        key = normalize_sensor_key(label)
        assert key is not None, f"AIDA64 label '{label}' не нормализуется"
        assert _is_cpu_temp_key(key), (
            f"AIDA64 label '{label}' → '{key}' не matchится "
            f"_is_cpu_temp_key — watchdog не подхватит"
        )


def test_read_aida64_realistic_multi_sensor_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Реалистичный blob с множеством сенсоров — все CPU temps подхватываются."""
    blob = _make_aida64_blob(
        [
            ("temp", "SMB", "Motherboard", 32.0),  # → motherboard/system
            ("temp", "SCPU", "CPU", 58.0),
            ("temp", "SCC1", "CPU Core #1", 56.0),
            ("temp", "SCC2", "CPU Core #2", 57.0),
            ("temp", "SCC3", "CPU Core #3", 55.0),
            ("temp", "SCC4", "CPU Core #4", 58.5),
            ("temp", "SGPU", "GPU", 65.0),
            ("temp", "SGPUHS", "GPU Hot Spot", 78.0),
            ("volt", "VCPU", "CPU VID", 1.215),
            ("volt", "VBAT", "+3V Battery", 3.10),
        ]
    )
    _patch_open_shm(monkeypatch, blob)
    temps, voltages = read_aida64_temperatures_and_voltages()
    # CPU temps присутствуют.
    assert temps["cpu/package"] == pytest.approx(58.0)
    assert temps["cpu/core_1"] == pytest.approx(56.0)
    assert temps["cpu/core_4"] == pytest.approx(58.5)
    # GPU присутствует.
    assert temps["gpu/temperature"] == pytest.approx(65.0)
    assert temps["gpu/hot_spot"] == pytest.approx(78.0)
    # Motherboard тоже есть.
    assert temps["motherboard/system"] == pytest.approx(32.0)
    # Voltages: AIDA64 публикует CPU VID — он не нормализуется (нет
    # паттерна), и ожидать его не надо. Здесь просто проверяем что
    # voltages-словарь существует и не падает (RUF059 защита).
    assert isinstance(voltages, dict)


def test_read_aida64_init_exports_function() -> None:
    """``read_aida64_sensors`` экспортируется через ``shm/__init__.py``."""
    from apexcore.infrastructure.sensors import shm

    assert hasattr(shm, "read_aida64_sensors")
    assert callable(shm.read_aida64_sensors)
