"""Regression-тесты для ``doctor --repair`` self-repair логики.

Главная проверка: путь к ``scripts/fetch_lhm.ps1`` корректно вычисляется
от ``doctor.py`` через ``Path(__file__).parents[5]``. Регрессия — после
P0 был баг с ``parents[4]``, который указывал на ``src/`` вместо
````, и self-repair падал с «Скрипт не найден».
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.platform != "win32":  # pragma: no cover
    pytest.skip("doctor --repair тестируется только на Win32", allow_module_level=True)


def test_fetch_lhm_script_path_resolves_to_new_app_scripts() -> None:
    """``parents[5]`` от doctor.py указывает на , не на src/.

    Регрессия: с P0 был баг ``parents[4]`` → искало `src/scripts/`.
    Должно быть `parents[5]` → `scripts/`.
    """
    from apexcore.interfaces.cli.commands import doctor

    doctor_path = Path(doctor.__file__).resolve()
    new_app_root = doctor_path.parents[5]
    script = new_app_root / "scripts" / "fetch_lhm.ps1"
    assert script.exists(), (
        f"fetch_lhm.ps1 не найден на пути {script}. Проверьте глубину "
        f"parents[N] в doctor._run_fetch_lhm_script."
    )


def test_doctor_module_has_repair_flag() -> None:
    """``apexcore doctor --repair`` существует как опция Typer."""
    import inspect

    from apexcore.interfaces.cli.commands.doctor import doctor

    # Проверяем что параметр `repair` присутствует в сигнатуре.
    sig = inspect.signature(doctor)
    assert "repair" in sig.parameters, (
        "Параметр --repair удалён из apexcore doctor — это регрессия P0.6."
    )


def test_classify_no_cpu_reason_priorities_dll_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NO_LHM_DLL`` имеет приоритет над probe-сигналами (HVCI/SAC/...).

    Регрессия: при отсутствии DLL `_check_lhm_runtime` сначала писал
    «Smart App Control блокирует» через probe, хотя реальная причина —
    отсутствие DLL.
    """
    from apexcore.application.diagnostics_sensors import (
        _classify_lhm_no_cpu_reason,
    )
    from apexcore.domain.sensor_models import DegradedReason
    from apexcore.infrastructure.sensors import lhm

    # Мокаем что DLL отсутствует.
    fake_dll = Path("Z:\\nonexistent\\lib\\LibreHardwareMonitorLib.dll")
    monkeypatch.setattr(lhm, "_LIB_DLL", fake_dll)

    # Даже с активным SAC должен вернуть NO_LHM_DLL (приоритет DLL).
    reason = _classify_lhm_no_cpu_reason()
    assert reason is DegradedReason.NO_LHM_DLL, (
        f"Ожидали NO_LHM_DLL (DLL отсутствует) — получили {reason}. "
        "Регрессия: probe-сигналы перебили DLL-приоритет."
    )
