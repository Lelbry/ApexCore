"""Unit-тесты для ``_WmiWorker`` — dedicated COM-apartment thread (P1.3).

Мокаем ``pythoncom`` и ``wmi`` через sys.modules, чтобы не требовать
реальные win32 API в тестах. Покрытие: init success/failure, query
success/failure, timeout, fallback в legacy-путь.

Регрессионный инвариант (см. CLAUDE.md): ``_WMI_PACKAGE_BROKEN`` + широкий
``except`` в ``read_msacpi_thermal_zone`` сохраняются как safety-net даже
после введения worker'а — это проверяется в test_sensors_wmi.py.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from apexcore.infrastructure.sensors import wmi_temps


@pytest.fixture(autouse=True)
def _reset_wmi_state() -> None:
    """Сбросить module-level state между тестами (см. test_sensors_wmi.py)."""
    wmi_temps._WMI_PACKAGE_BROKEN = False
    wmi_temps._reset_wmi_worker_for_tests()


# ─── Fake pythoncom / wmi для тестов ───────────────────────────────────────


class _FakePythoncom:
    """Минимальная заглушка ``pythoncom`` — CoInitializeEx/CoUninitialize."""

    COINIT_APARTMENTTHREADED = 0x2

    def __init__(self) -> None:
        self.init_calls: list[int] = []
        self.uninit_calls = 0
        self.raise_on_init: BaseException | None = None

    def CoInitializeEx(self, flags: int) -> None:
        self.init_calls.append(flags)
        if self.raise_on_init is not None:
            raise self.raise_on_init

    def CoUninitialize(self) -> None:
        self.uninit_calls += 1


class _FakeWmiZone:
    """Имитация ``z`` из ``MSAcpi_ThermalZoneTemperature()``."""

    def __init__(self, current_temp: int | None) -> None:
        self.CurrentTemperature = current_temp


class _FakeWmiInstance:
    def __init__(self, zones: list[_FakeWmiZone]) -> None:
        self._zones = zones

    def MSAcpi_ThermalZoneTemperature(self) -> list[_FakeWmiZone]:
        return list(self._zones)


class _FakeWmiModule:
    """Имитация пакета ``wmi``: ``wmi.WMI(namespace=...)`` → fake instance."""

    def __init__(
        self,
        zones: list[_FakeWmiZone] | None = None,
        raise_on_query: BaseException | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._zones = zones or []
        self._raise_on_query = raise_on_query
        self._delay = delay_seconds

    def WMI(self, namespace: str) -> _FakeWmiInstance:
        assert namespace == "root\\wmi"
        if self._delay > 0:
            time.sleep(self._delay)
        if self._raise_on_query is not None:
            raise self._raise_on_query
        return _FakeWmiInstance(self._zones)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    pythoncom: _FakePythoncom | None,
    wmi_mod: _FakeWmiModule | None,
) -> None:
    """Заинсталлировать фейки через sys.modules — внутрь worker thread их подхватит."""
    if pythoncom is not None:
        monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    else:
        # Force ImportError при `import pythoncom`.
        monkeypatch.setitem(sys.modules, "pythoncom", None)
    if wmi_mod is not None:
        monkeypatch.setitem(sys.modules, "wmi", wmi_mod)
    else:
        monkeypatch.setitem(sys.modules, "wmi", None)


# ─── Worker init success / failure ─────────────────────────────────────────


def test_worker_starts_when_pythoncom_and_wmi_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COM init + ``import wmi`` ОК → worker готов."""
    pythoncom = _FakePythoncom()
    wmi_mod = _FakeWmiModule(zones=[_FakeWmiZone(3131)])
    _install_fakes(monkeypatch, pythoncom, wmi_mod)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is True
    assert worker.failed is False
    # CoInitializeEx был вызван с APARTMENTTHREADED.
    assert pythoncom.init_calls == [_FakePythoncom.COINIT_APARTMENTTHREADED]


def test_worker_fails_when_pythoncom_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет ``pythoncom`` → worker fails fast."""
    _install_fakes(monkeypatch, pythoncom=None, wmi_mod=_FakeWmiModule())

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is False
    assert worker.failed is True


def test_worker_fails_when_wmi_import_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``import wmi`` бросает com_error → worker fails."""
    _install_fakes(monkeypatch, pythoncom=_FakePythoncom(), wmi_mod=None)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is False
    assert worker.failed is True


def test_worker_fails_when_coinit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CoInitializeEx`` бросает → worker fails."""
    pythoncom = _FakePythoncom()
    pythoncom.raise_on_init = OSError("simulated COM init failure")
    _install_fakes(monkeypatch, pythoncom, _FakeWmiModule())

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is False
    assert worker.failed is True


# ─── Worker query success / failure ────────────────────────────────────────


def test_worker_query_msacpi_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """WMI вернул thermal zone → worker отдал нормализованный dict."""
    # 313.15 K = 40°C → CurrentTemperature 3131 (десятые доли K).
    wmi_mod = _FakeWmiModule(zones=[_FakeWmiZone(3131), _FakeWmiZone(3231)])
    _install_fakes(monkeypatch, _FakePythoncom(), wmi_mod)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is True
    data = worker.query_msacpi(timeout=2.0)
    assert data is not None
    assert data["thermal_zone_0"] == pytest.approx(40.0, abs=0.1)
    assert data["thermal_zone_1"] == pytest.approx(50.0, abs=0.1)


def test_worker_query_returns_none_on_wmi_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """WMI бросает на запросе → worker возвращает None (но не failed)."""
    wmi_mod = _FakeWmiModule(raise_on_query=OSError("simulated WMI query failure"))
    _install_fakes(monkeypatch, _FakePythoncom(), wmi_mod)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is True
    data = worker.query_msacpi(timeout=2.0)
    assert data is None
    # WMI query упал — но worker сам не помечается failed (может быть transient).
    assert worker.failed is False


def test_worker_query_skips_zone_without_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zone с ``CurrentTemperature=None`` отсеивается."""
    wmi_mod = _FakeWmiModule(
        zones=[_FakeWmiZone(3131), _FakeWmiZone(None), _FakeWmiZone(3231)]
    )
    _install_fakes(monkeypatch, _FakePythoncom(), wmi_mod)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is True
    data = worker.query_msacpi(timeout=2.0)
    assert data is not None
    # «None» зона при index=1 отсеяна. Зоны 0 и 2 остались с teir исходными
    # индексами (enumerate не пропускает).
    assert "thermal_zone_0" in data
    assert "thermal_zone_2" in data
    assert "thermal_zone_1" not in data


def test_worker_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker долго отвечает → query_msacpi возвращает None по timeout."""
    # WMI «зависает» на 1 секунду — query с timeout 0.1 с не дождётся.
    wmi_mod = _FakeWmiModule(zones=[_FakeWmiZone(3131)], delay_seconds=1.0)
    _install_fakes(monkeypatch, _FakePythoncom(), wmi_mod)

    worker = wmi_temps._WmiWorker()
    assert worker.start(init_timeout=2.0) is True
    data = worker.query_msacpi(timeout=0.1)
    assert data is None
    # После timeout worker не failed — можно попробовать снова.
    assert worker.failed is False


def test_worker_query_returns_none_when_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если worker failed — query сразу возвращает None, без request'а."""
    _install_fakes(monkeypatch, pythoncom=None, wmi_mod=None)
    worker = wmi_temps._WmiWorker()
    worker.start(init_timeout=2.0)
    assert worker.failed is True
    assert worker.query_msacpi(timeout=0.05) is None


# ─── Singleton accessor ────────────────────────────────────────────────────


def test_get_wmi_worker_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный ``_get_wmi_worker`` возвращает тот же объект."""
    _install_fakes(
        monkeypatch, _FakePythoncom(), _FakeWmiModule(zones=[_FakeWmiZone(3131)])
    )
    a = wmi_temps._get_wmi_worker()
    b = wmi_temps._get_wmi_worker()
    assert a is b
    assert a is not None
    assert a.failed is False


def test_get_wmi_worker_returns_none_when_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed singleton → ``_get_wmi_worker`` возвращает None."""
    _install_fakes(monkeypatch, pythoncom=None, wmi_mod=None)
    a = wmi_temps._get_wmi_worker()
    assert a is None
    # Повторный вызов — тоже None, без попытки нового init.
    b = wmi_temps._get_wmi_worker()
    assert b is None


# ─── Интеграция с read_msacpi_thermal_zone ─────────────────────────────────


def test_read_msacpi_uses_worker_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_msacpi_thermal_zone`` использует worker если он работает."""
    _install_fakes(
        monkeypatch, _FakePythoncom(), _FakeWmiModule(zones=[_FakeWmiZone(3131)])
    )
    # Subprocess не должен вызываться — мы идём через worker.
    call_count = {"subprocess": 0}

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        call_count["subprocess"] += 1
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = wmi_temps.read_msacpi_thermal_zone()
    assert result == {"thermal_zone_0": pytest.approx(40.0, abs=0.1)}
    assert call_count["subprocess"] == 0


def test_read_msacpi_falls_back_to_cim_when_worker_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker failed + legacy ``import wmi`` тоже падает → CIM-fallback вызывается."""
    # Worker init fails: pythoncom отсутствует.
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    # Legacy `import wmi` тоже падает.
    monkeypatch.setitem(sys.modules, "wmi", None)
    # CIM-subprocess отвечает корректным JSON.
    payload = '[{"CurrentTemperature":3131}]'
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=payload, returncode=0),
    )

    result = wmi_temps.read_msacpi_thermal_zone()
    assert result == {"thermal_zone_0": pytest.approx(40.0, abs=0.1)}
    # _WMI_PACKAGE_BROKEN взведён — safety-net инвариант сохранён.
    assert wmi_temps._WMI_PACKAGE_BROKEN is True


def test_reset_wmi_worker_for_tests_nullifies_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тестовый хелпер действительно обнуляет singleton."""
    _install_fakes(
        monkeypatch, _FakePythoncom(), _FakeWmiModule(zones=[_FakeWmiZone(3131)])
    )
    a = wmi_temps._get_wmi_worker()
    assert a is not None
    wmi_temps._reset_wmi_worker_for_tests()
    # После сброса singleton инициализируется заново; вернёт новый объект.
    b = wmi_temps._get_wmi_worker()
    assert b is not None
    assert b is not a


# ─── Thread safety smoke test ──────────────────────────────────────────────


def test_get_wmi_worker_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Параллельные вызовы _get_wmi_worker не создают дубликаты thread'ов.

    Smoke test (не строгий race detection) — 10 потоков одновременно
    дёргают accessor; должен быть ровно один singleton.
    """
    _install_fakes(
        monkeypatch, _FakePythoncom(), _FakeWmiModule(zones=[_FakeWmiZone(3131)])
    )
    seen: list[Any] = []
    seen_lock = threading.Lock()

    def _grab() -> None:
        w = wmi_temps._get_wmi_worker()
        with seen_lock:
            seen.append(w)

    threads = [threading.Thread(target=_grab) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Все 10 потоков увидели один и тот же singleton.
    assert len({id(w) for w in seen}) == 1
    assert seen[0] is not None
