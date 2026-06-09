"""Тесты read-only shared-memory клиента (`apexcore.services.shm_adapter`).

Mmap реально не открывается — мы подменяем `_open_global_mapping` на
функцию, возвращающую in-memory ``bytearray`` (drop-in для mmap, через
дакт-тайпинг: slice indexing + close()). Это позволяет тестировать всю
логику graceful degrade без зависимости от Windows и без admin.
"""

from __future__ import annotations

import pytest

from apexcore.services import shm_adapter, shm_layout


class _FakeMapping:
    """Минимальный mmap-substitute: bytes-like + close()."""

    def __init__(self, payload: bytes) -> None:
        # Расширяем до BUFFER_SIZE — реальный mmap всегда фиксированного размера.
        self._buf = bytearray(shm_layout.BUFFER_SIZE)
        self._buf[: len(payload)] = payload
        self.closed = False

    def __getitem__(self, item: slice | int) -> bytes:  # type: ignore[override]
        return bytes(self._buf[item])

    def __len__(self) -> int:
        return len(self._buf)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_adapter_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Перед каждым тестом — сбрасываем кеш mapping'а в shm_adapter."""
    # close_shm_mapping сбрасывает кешированный handle и _open_failed.
    shm_adapter.close_shm_mapping()


def _install_fake_mapping(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> _FakeMapping:
    """Подменить _open_global_mapping на функцию, отдающую FakeMapping."""
    fake = _FakeMapping(payload)
    monkeypatch.setattr(shm_adapter, "_open_global_mapping", lambda: fake)
    return fake


# ────────── read_shm_snapshot ──────────


def test_read_returns_none_when_mapping_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mapping не открылся → None, без исключений."""

    def _raise() -> None:
        raise FileNotFoundError("Global\\apexcore_sensors not found")

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _raise)
    assert shm_adapter.read_shm_snapshot() is None


def test_read_returns_snapshot_when_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий валидный snapshot декодируется."""
    import time

    ts = time.time_ns()
    payload = shm_layout.pack_snapshot({"temp:cpu/cpu_package": 70.0}, ts)
    _install_fake_mapping(monkeypatch, payload)
    snap = shm_adapter.read_shm_snapshot()
    assert snap is not None
    assert snap.timestamp_ns == ts
    assert snap.values == {"temp:cpu/cpu_package": pytest.approx(70.0)}


def test_read_returns_none_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot старше FRESHNESS_LIMIT_NS → None (fallback на прямой LHM)."""
    import time

    stale_ts = time.time_ns() - shm_layout.FRESHNESS_LIMIT_NS - 1_000_000_000
    payload = shm_layout.pack_snapshot({"temp:cpu/cpu_package": 70.0}, stale_ts)
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_snapshot() is None


def test_read_returns_none_on_corrupt_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Битый буфер (нулевой) — None."""
    _install_fake_mapping(monkeypatch, b"")  # FakeMapping заполнит нулями
    assert shm_adapter.read_shm_snapshot() is None


# ────────── read_shm_by_prefix ──────────


def test_read_by_prefix_strips_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {
            "temp:cpu/cpu_package": 70.0,
            "temp:gpunvidia/gpu_core": 65.0,
            "volt:cpu/cpu_core": 1.25,
            "fan:fan/cpu_fan": 1100.0,
        },
        time.time_ns(),
    )
    _install_fake_mapping(monkeypatch, payload)
    temps = shm_adapter.read_shm_by_prefix("temp:")
    assert temps == {
        "cpu/cpu_package": pytest.approx(70.0),
        "gpunvidia/gpu_core": pytest.approx(65.0),
    }


def test_read_by_prefix_returns_empty_dict_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid snapshot без совпадающих ключей → пустой dict (НЕ None)."""
    import time

    payload = shm_layout.pack_snapshot(
        {"temp:cpu/cpu_package": 70.0},
        time.time_ns(),
    )
    _install_fake_mapping(monkeypatch, payload)
    fans = shm_adapter.read_shm_by_prefix("fan:")
    assert fans == {}


def test_read_by_prefix_returns_none_when_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mapping недоступен → None (не пустой dict — это сигнал fallback'а)."""

    def _raise() -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _raise)
    assert shm_adapter.read_shm_by_prefix("temp:") is None


# ────────── Удобные функции по типам ──────────


def test_read_shm_temperatures(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"temp:cpu/cpu_package": 70.0, "volt:cpu/vcore": 1.25}, time.time_ns()
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_temperatures() == {
        "cpu/cpu_package": pytest.approx(70.0)
    }


def test_read_shm_voltages(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"volt:cpu/vcore": 1.25, "temp:cpu/cpu_package": 70.0}, time.time_ns()
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_voltages() == {"cpu/vcore": pytest.approx(1.25)}


def test_read_shm_cpu_power(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"power:cpu_power/package": 95.5, "power:cpu_power/cores": 65.0},
        time.time_ns(),
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_cpu_power() == {
        "cpu_power/package": pytest.approx(95.5),
        "cpu_power/cores": pytest.approx(65.0),
    }


def test_read_shm_fans(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"fan:fan/cpu_fan": 1100.0, "fan:fan/chassis_fan_1": 1500.0}, time.time_ns()
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_fans() == {
        "fan/cpu_fan": pytest.approx(1100.0),
        "fan/chassis_fan_1": pytest.approx(1500.0),
    }


def test_read_shm_cpu_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"clock:cpu/cpu_core_1": 4900.0, "clock:cpu/cpu_core_2": 4800.0},
        time.time_ns(),
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_cpu_clocks() == {
        "cpu/cpu_core_1": pytest.approx(4900.0),
        "cpu/cpu_core_2": pytest.approx(4800.0),
    }


def test_read_shm_tjmax(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    payload = shm_layout.pack_snapshot(
        {"tjmax:cpu/cpu_core_1": 100.0}, time.time_ns()
    )
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.read_shm_tjmax() == {"cpu/cpu_core_1": pytest.approx(100.0)}


def test_read_shm_temperatures_and_voltages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Объединённый shm-вызов — разводит temp/volt в один проход."""
    import time

    payload = shm_layout.pack_snapshot(
        {
            "temp:cpu/cpu_package": 70.0,
            "volt:cpu/vcore": 1.25,
            "power:cpu_power/package": 95.5,  # игнорируем
        },
        time.time_ns(),
    )
    _install_fake_mapping(monkeypatch, payload)
    result = shm_adapter.read_shm_temperatures_and_voltages()
    assert result is not None
    temps, voltages = result
    assert temps == {"cpu/cpu_package": pytest.approx(70.0)}
    assert voltages == {"cpu/vcore": pytest.approx(1.25)}


def test_read_shm_temperatures_and_voltages_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _raise)
    assert shm_adapter.read_shm_temperatures_and_voltages() is None


# ────────── Кеш mapping'а ──────────


def test_mapping_handle_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mapping открывается один раз и переиспользуется между вызовами."""
    import time

    payload = shm_layout.pack_snapshot({"k": 1.0}, time.time_ns())
    call_counter = {"count": 0}

    def _open() -> _FakeMapping:
        call_counter["count"] += 1
        return _FakeMapping(payload)

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _open)

    assert shm_adapter.read_shm_snapshot() is not None
    assert shm_adapter.read_shm_snapshot() is not None
    assert shm_adapter.read_shm_snapshot() is not None
    assert call_counter["count"] == 1


def test_failed_open_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если первый open упал — не дёргаем его повторно (защита горячего пути)."""
    call_counter = {"count": 0}

    def _open() -> None:
        call_counter["count"] += 1
        raise FileNotFoundError("not found")

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _open)

    assert shm_adapter.read_shm_snapshot() is None
    assert shm_adapter.read_shm_snapshot() is None
    assert shm_adapter.read_shm_snapshot() is None
    assert call_counter["count"] == 1


def test_close_resets_open_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """После close_shm_mapping повторная попытка открыть — снова делается."""
    call_counter = {"count": 0}

    def _open() -> None:
        call_counter["count"] += 1
        raise FileNotFoundError("not found")

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _open)

    assert shm_adapter.read_shm_snapshot() is None
    shm_adapter.close_shm_mapping()
    assert shm_adapter.read_shm_snapshot() is None
    assert call_counter["count"] == 2


def test_is_shm_available_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_shm_available — простая обёртка над read_shm_snapshot."""
    import time

    payload = shm_layout.pack_snapshot({"k": 1.0}, time.time_ns())
    _install_fake_mapping(monkeypatch, payload)
    assert shm_adapter.is_shm_available() is True


def test_is_shm_available_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> None:
        raise FileNotFoundError()

    monkeypatch.setattr(shm_adapter, "_open_global_mapping", _raise)
    assert shm_adapter.is_shm_available() is False
