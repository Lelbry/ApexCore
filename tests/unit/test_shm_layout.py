"""Тесты бинарного лэйаута shared-memory snapshot'а."""

from __future__ import annotations

import struct

import pytest

from apexcore.services import shm_layout
from apexcore.services.shm_layout import (
    BUFFER_SIZE,
    FRESHNESS_LIMIT_NS,
    HEADER_FMT,
    HEADER_SIZE,
    MAGIC,
    MAX_KEY_LEN,
    SHM_VERSION,
    Snapshot,
    pack_snapshot,
    unpack_snapshot,
)

# ────────── Round-trip pack/unpack ──────────


def test_pack_unpack_empty() -> None:
    """Пустой snapshot — заголовок без записей."""
    payload = pack_snapshot({}, 1234567890)
    assert len(payload) == HEADER_SIZE
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert snap.timestamp_ns == 1234567890
    assert snap.values == {}


def test_pack_unpack_single_value() -> None:
    payload = pack_snapshot({"temp:cpu/cpu_package": 65.5}, 100)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert snap.timestamp_ns == 100
    # float32 → небольшая потеря точности возможна, сравниваем с allclose
    assert snap.values["temp:cpu/cpu_package"] == pytest.approx(65.5, rel=1e-5)


def test_pack_unpack_many_keys_preserves_order() -> None:
    """Порядок ключей сохраняется (Python 3.7+ insertion-order)."""
    keys = [f"temp:cpu/cpu_core_{i}" for i in range(8)]
    values = {k: float(i * 10) for i, k in enumerate(keys)}
    payload = pack_snapshot(values, 42)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert list(snap.values.keys()) == keys


def test_pack_unpack_mixed_types() -> None:
    """Реалистичный snapshot со всеми type-префиксами."""
    src = {
        "temp:cpu/cpu_package": 70.0,
        "volt:cpu/cpu_core": 1.275,
        "power:cpu_power/package": 95.5,
        "fan:fan/cpu_fan": 1450.0,
        "clock:cpu/cpu_core_1": 4900.0,
        "tjmax:cpu/cpu_core_1": 100.0,
    }
    payload = pack_snapshot(src, 999)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert set(snap.values.keys()) == set(src.keys())
    for key, expected in src.items():
        assert snap.values[key] == pytest.approx(expected, rel=1e-4)


# ────────── Header layout ──────────


def test_header_format_constants() -> None:
    """HEADER_SIZE = 24, format совпадает с описанием в docstring."""
    assert struct.calcsize(HEADER_FMT) == 24
    assert HEADER_SIZE == 24


def test_header_magic_and_version() -> None:
    payload = pack_snapshot({}, 0)
    magic, version, flags, ts, count, rsvd = struct.unpack_from(
        HEADER_FMT, payload, 0
    )
    assert magic == MAGIC
    assert version == SHM_VERSION
    assert flags == 0
    assert ts == 0
    assert count == 0
    assert rsvd == 0


def test_buffer_size_is_64kb() -> None:
    assert BUFFER_SIZE == 65536


# ────────── Защита от мусора ──────────


def test_unpack_too_short_returns_none() -> None:
    """Буфер короче header'а — None, без исключений."""
    assert unpack_snapshot(b"") is None
    assert unpack_snapshot(b"\x00" * 10) is None
    assert unpack_snapshot(b"\x00" * (HEADER_SIZE - 1)) is None


def test_unpack_wrong_magic_returns_none() -> None:
    """Чужой magic — None (например, zero-init буфер от mmap)."""
    bad = b"\x00" * HEADER_SIZE
    assert unpack_snapshot(bad) is None


def test_unpack_wrong_version_returns_none() -> None:
    """Несовпадающая версия — None."""
    payload = bytearray(pack_snapshot({}, 0))
    # version хранится после magic (offset 4), 2 байта LE
    struct.pack_into("<H", payload, 4, SHM_VERSION + 1)
    assert unpack_snapshot(bytes(payload)) is None


def test_unpack_truncated_record_returns_none() -> None:
    """Запись обрезается на полпути — snapshot невалиден."""
    payload = pack_snapshot({"key1": 1.0, "key2": 2.0}, 0)
    truncated = payload[:-3]  # выкинули половину value
    assert unpack_snapshot(truncated) is None


def test_unpack_zero_key_len_returns_none() -> None:
    """key_len = 0 — мусор, snapshot отбрасываем."""
    # Берём валидный snapshot с одной записью и зануляем key_len
    payload = bytearray(pack_snapshot({"k": 1.0}, 0))
    # key_len в первой записи: offset HEADER_SIZE
    struct.pack_into("<H", payload, HEADER_SIZE, 0)
    assert unpack_snapshot(bytes(payload)) is None


# ────────── NaN / inf фильтр ──────────


def test_pack_drops_nan() -> None:
    payload = pack_snapshot({"key1": 1.0, "key2": float("nan")}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert "key1" in snap.values
    assert "key2" not in snap.values


def test_pack_drops_inf() -> None:
    payload = pack_snapshot({"key1": 1.0, "key2": float("inf")}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert "key1" in snap.values
    assert "key2" not in snap.values


def test_pack_drops_neg_inf() -> None:
    payload = pack_snapshot({"key": float("-inf")}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert snap.values == {}


def test_pack_drops_non_numeric() -> None:
    """Не-число должен молча отбрасываться (защита от поломанных входов)."""
    payload = pack_snapshot({"key1": 1.0, "key2": "not a number"}, 0)  # type: ignore[dict-item]
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert snap.values == {"key1": pytest.approx(1.0)}


# ────────── Длина ключа ──────────


def test_pack_drops_overlong_key() -> None:
    """Ключ > MAX_KEY_LEN отбрасывается без падения."""
    long_key = "x" * (MAX_KEY_LEN + 1)
    payload = pack_snapshot({long_key: 1.0, "ok": 2.0}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert long_key not in snap.values
    assert snap.values == {"ok": pytest.approx(2.0)}


def test_pack_drops_empty_key() -> None:
    """Пустой ключ → key_len = 0 → отбрасываем."""
    payload = pack_snapshot({"": 1.0, "ok": 2.0}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert "" not in snap.values
    assert snap.values == {"ok": pytest.approx(2.0)}


def test_pack_unicode_key_roundtrip() -> None:
    """UTF-8 ключи (на всякий — мы используем ASCII, но проверим)."""
    payload = pack_snapshot({"кулер/cpu": 1100.0}, 0)
    snap = unpack_snapshot(payload)
    assert snap is not None
    assert snap.values["кулер/cpu"] == pytest.approx(1100.0)


# ────────── BUFFER_SIZE предел ──────────


def test_pack_truncates_on_overflow() -> None:
    """Если суммарный размер выходит за BUFFER_SIZE — обрезаем по порядку."""
    # Минимальный размер записи = 2 (key_len) + 1 (key 1 байт) + 4 (value) = 7 B
    # При BUFFER_SIZE=64 КБ и HEADER_SIZE=24 умещается ~9360 записей.
    # Делаем заведомо много с длинными ключами.
    big = {f"key_with_long_name_{i:05d}": float(i) for i in range(5000)}
    payload = pack_snapshot(big, 0)
    assert len(payload) <= BUFFER_SIZE
    snap = unpack_snapshot(payload)
    assert snap is not None
    # Что-то да поместилось, но не всё
    assert 0 < len(snap.values) < 5000


# ────────── Snapshot.is_fresh ──────────


def test_snapshot_is_fresh_when_recent() -> None:
    now = 10_000_000_000
    snap = Snapshot(timestamp_ns=now - 1_000_000_000, values={})  # 1s ago
    assert snap.is_fresh(now_ns=now)


def test_snapshot_is_stale_when_old() -> None:
    now = 10_000_000_000
    snap = Snapshot(
        timestamp_ns=now - FRESHNESS_LIMIT_NS - 1,
        values={},
    )
    assert not snap.is_fresh(now_ns=now)


def test_snapshot_is_fresh_at_boundary() -> None:
    """Ровно на границе FRESHNESS_LIMIT_NS считаем свежим (≤)."""
    now = 10_000_000_000
    snap = Snapshot(timestamp_ns=now - FRESHNESS_LIMIT_NS, values={})
    assert snap.is_fresh(now_ns=now)


def test_snapshot_is_fresh_uses_time_when_now_not_given() -> None:
    """Default now_ns=None — использует time.time_ns()."""
    import time

    snap = Snapshot(timestamp_ns=time.time_ns(), values={})
    assert snap.is_fresh()  # только что создан → свежий


# ────────── _is_finite ──────────


def test_is_finite_accepts_normal_floats() -> None:
    assert shm_layout._is_finite(0.0)
    assert shm_layout._is_finite(-273.15)
    assert shm_layout._is_finite(1e30)


def test_is_finite_rejects_nan_inf() -> None:
    assert not shm_layout._is_finite(float("nan"))
    assert not shm_layout._is_finite(float("inf"))
    assert not shm_layout._is_finite(float("-inf"))
