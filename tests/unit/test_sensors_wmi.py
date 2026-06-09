"""Тесты WMI/CIM-источников температур (без реального PowerShell)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from apexcore.infrastructure.sensors import wmi_temps


@pytest.fixture(autouse=True)
def _reset_wmi_state() -> None:
    """Сбросить module-level state между тестами.

    Сбрасываем:

    - ``_WMI_PACKAGE_BROKEN`` — иначе первый тест с заблокированным
      импортом ``wmi`` навсегда выставит флаг в True, и последующие
      тесты не смогут проверить branch с успешным импортом.
    - ``_WMI_WORKER_INSTANCE`` — singleton worker'а (P1.3); без сброса
      первый тест помечает singleton ``failed`` навсегда и последующие
      тесты не могут exercise worker-путь. Daemon-thread предыдущего
      worker'а тихо умрёт при завершении процесса.
    """
    wmi_temps._WMI_PACKAGE_BROKEN = False
    wmi_temps._reset_wmi_worker_for_tests()


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _patch_powershell(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], _FakeCompleted | Exception],
) -> list[list[str]]:
    """Заменить ``subprocess.run`` на фейк; возвращает список перехваченных команд."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> _FakeCompleted:
        captured.append(list(cmd))
        result = handler(cmd)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


# ────────── perf-counter Thermal Zone ──────────


def test_perf_counter_returns_dict_for_well_formed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        '[{"Path":"\\\\thermal zone information(_total)\\\\temperature",'
        '"CookedValue":104.0},'
        '{"Path":"\\\\thermal zone information(\\\\_tz.tzs0)\\\\temperature",'
        '"CookedValue":113.0}]'
    )
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=payload))

    result = wmi_temps.read_perf_counter_thermal_zone()

    assert len(result) == 2
    # 104°F = 40°C
    assert result["\\thermal zone information(_total)\\temperature"] == pytest.approx(40.0, abs=0.1)
    # 113°F = 45°C
    assert any(abs(v - 45.0) < 0.1 for v in result.values())


def test_perf_counter_handles_single_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если ConvertTo-Json получил один объект, он отдаёт dict, а не list."""
    payload = '{"Path":"\\\\thermal zone(only)\\\\temperature","CookedValue":86.0}'
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=payload))

    result = wmi_temps.read_perf_counter_thermal_zone()

    assert len(result) == 1
    # 86°F = 30°C
    assert next(iter(result.values())) == pytest.approx(30.0, abs=0.1)


def test_perf_counter_returns_empty_on_empty_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=""))
    assert wmi_temps.read_perf_counter_thermal_zone() == {}


def test_perf_counter_returns_empty_on_garbage_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout="not a json"))
    assert wmi_temps.read_perf_counter_thermal_zone() == {}


def test_perf_counter_returns_empty_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_powershell(
        monkeypatch,
        lambda _cmd: subprocess.TimeoutExpired(cmd=_cmd, timeout=3.0),
    )
    assert wmi_temps.read_perf_counter_thermal_zone() == {}


def test_perf_counter_returns_empty_on_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    """PowerShell может отсутствовать (минимальный Server Core)."""
    _patch_powershell(monkeypatch, lambda _cmd: FileNotFoundError("powershell"))
    assert wmi_temps.read_perf_counter_thermal_zone() == {}


def test_perf_counter_skips_rows_without_value(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        '[{"Path":"\\\\zone(a)\\\\temperature","CookedValue":104.0},'
        '{"Path":"\\\\zone(b)\\\\temperature","CookedValue":null}]'
    )
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=payload))

    result = wmi_temps.read_perf_counter_thermal_zone()

    assert len(result) == 1
    assert "\\zone(a)\\temperature" in result


# ────────── MSAcpi через CIM (PowerShell fallback) ──────────


def test_msacpi_via_cim_decodes_decikelvin(monkeypatch: pytest.MonkeyPatch) -> None:
    """MSAcpi отдаёт CurrentTemperature в десятых долях кельвина."""
    # 313.15 K = 40°C → 3131.5 десятых долей
    payload = '[{"CurrentTemperature":3131},{"CurrentTemperature":3231}]'
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=payload))

    # Гарантируем, что Python-пакет ``wmi`` импортируется как None,
    # чтобы пойти в CIM-fallback.
    monkeypatch.setattr(
        "builtins.__import__",
        _import_blocker({"wmi"}, fallback=__builtins__["__import__"]),
    )

    result = wmi_temps.read_msacpi_thermal_zone()

    assert len(result) == 2
    assert result["thermal_zone_0"] == pytest.approx(40.0, abs=0.1)
    assert result["thermal_zone_1"] == pytest.approx(50.0, abs=0.1)


def test_msacpi_via_cim_empty_when_no_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=""))
    monkeypatch.setattr(
        "builtins.__import__",
        _import_blocker({"wmi"}, fallback=__builtins__["__import__"]),
    )

    assert wmi_temps.read_msacpi_thermal_zone() == {}


def test_msacpi_via_cim_handles_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout="<<<garbage>>>"))
    monkeypatch.setattr(
        "builtins.__import__",
        _import_blocker({"wmi"}, fallback=__builtins__["__import__"]),
    )
    assert wmi_temps.read_msacpi_thermal_zone() == {}


def _import_blocker(blocked: set[str], fallback: Callable[..., Any]) -> Callable[..., Any]:
    """Заставляет ``import name`` для name из ``blocked`` падать с ImportError."""

    def _patched(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in blocked:
            raise ImportError(f"blocked by test: {name}")
        return fallback(name, *args, **kwargs)

    return _patched


def _import_raiser(
    blocked: set[str],
    error: BaseException,
    fallback: Callable[..., Any],
) -> Callable[..., Any]:
    """Как ``_import_blocker``, но бросает произвольное исключение (не ImportError).

    Нужно для регрессионного теста на «плавающую» COM-ошибку:
    ``import wmi`` на module-level дёргает ``GetObject("winmgmts:")``,
    который в фоновом потоке без COM-апартмента бросает
    ``com_error MK_E_SYNTAX``. Это **не** ``ImportError`` — раньше код
    ловил только ``ImportError``, и com_error прорывался в TelemetryService.
    """

    def _patched(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in blocked:
            raise error
        return fallback(name, *args, **kwargs)

    return _patched


# ────────── Регрессия: COM-ошибка при импорте wmi ──────────


class _FakeComError(OSError):
    """Имитация ``pywintypes.com_error`` — не ``ImportError``, обычное OSError."""


def test_msacpi_falls_back_to_cim_on_com_error_during_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import wmi`` падает с COM-ошибкой → ожидаем CIM-fallback.

    Исторически код ловил только ``ImportError``, и плавающая COM-ошибка
    из module-level ``wmi.py`` (``GetObject("winmgmts:")``) прорывалась
    в ``TelemetryService._run`` как «Сбор метрик завершился ошибкой».
    Этот тест охраняет фикс в ``read_msacpi_thermal_zone``.
    """
    com_error = _FakeComError("(-2147221020, 'Синтаксическая ошибка', None, None)")
    payload = '[{"CurrentTemperature":3131}]'  # 40°C через CIM
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=payload))
    monkeypatch.setattr(
        "builtins.__import__",
        _import_raiser({"wmi"}, com_error, fallback=__builtins__["__import__"]),
    )

    result = wmi_temps.read_msacpi_thermal_zone()

    assert result == {"thermal_zone_0": pytest.approx(40.0, abs=0.1)}
    # После первой неудачи флаг должен быть взведён, чтобы повторные
    # тики телеметрии шли сразу в CIM, минуя дорогостоящий перепопытку.
    assert wmi_temps._WMI_PACKAGE_BROKEN is True


def test_msacpi_uses_cached_broken_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если флаг уже выставлен — не пытаемся даже импортировать wmi."""
    wmi_temps._WMI_PACKAGE_BROKEN = True
    import_calls: list[str] = []
    # ВАЖНО: сохраняем оригинальный __import__ ДО monkeypatch.setattr,
    # иначе fake_import будет рекурсивно вызывать сам себя.
    real_import = __builtins__["__import__"]

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        import_calls.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    _patch_powershell(monkeypatch, lambda _cmd: _FakeCompleted(stdout=""))

    wmi_temps.read_msacpi_thermal_zone()

    assert "wmi" not in import_calls, (
        "После взведённого флага попытки import wmi не должно быть"
    )
