"""Тесты гибридного pipeline температур в ``WindowsAdapter`` (Windows only).

Реальные источники (LHM, psutil.sensors_temperatures, PowerShell) полностью
мокаются — мы проверяем только порядок fallback'ов и приоритеты.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

if sys.platform != "win32":  # pragma: no cover
    pytest.skip("WindowsAdapter тестируется только под Win32", allow_module_level=True)

import psutil

from apexcore.infrastructure.adapters.windows import WindowsAdapter
from apexcore.infrastructure.sensors import lhm, nvidia_ml, smartctl, wmi_temps


@pytest.fixture
def adapter() -> WindowsAdapter:
    return WindowsAdapter()


@pytest.fixture(autouse=True)
def _silence_real_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждый тест явно настраивает источник; по умолчанию все молчат.

    Hot-path адаптера читает Temperature + Voltage за один проход через
    :func:`lhm.read_lhm_temperatures_and_voltages`; именно её и подменяем
    в тестах. Обёртка :func:`lhm.read_lhm_temperatures` оставлена для
    обратной совместимости callsite'ов (diagnostics, watchdog), но в
    адаптере уже не вызывается напрямую.

    NVML и smartctl (M3, см. docs/research/sensor_dashboard_brief.md)
    тоже мокаются по умолчанию, иначе реальная NVIDIA-карта/smartctl
    на дев-машине подмешают свои значения в mock-тесты.
    """
    monkeypatch.setattr(lhm, "read_lhm_temperatures_and_voltages", lambda: ({}, {}))
    monkeypatch.setattr(lhm, "read_lhm_cpu_clocks", lambda: {})
    monkeypatch.setattr(lhm, "read_lhm_fans", lambda: {})
    # _read_sensors() помимо temperatures+voltages дёргает ещё read_lhm_cpu_power
    # (kludge через voltages-dict с префиксом cpu_power/, см. windows.py:226).
    # Если не замокать — на машине с подложенной LHM DLL в voltages протекут
    # реальные cpu_power/{cores,memory,package,platform}=0.0 и тест упадёт.
    monkeypatch.setattr(lhm, "read_lhm_cpu_power", lambda: {})
    monkeypatch.setattr(wmi_temps, "read_perf_counter_thermal_zone", lambda: {})
    monkeypatch.setattr(wmi_temps, "read_msacpi_thermal_zone", lambda: {})
    monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {}, raising=False)
    monkeypatch.setattr(nvidia_ml, "read_nvml_temperatures", lambda: {})
    monkeypatch.setattr(nvidia_ml, "read_nvml_power", lambda: {})
    monkeypatch.setattr(nvidia_ml, "read_nvml_frequencies", lambda: {})
    monkeypatch.setattr(smartctl, "read_smartctl_temperatures", lambda: {})


# ────────── приоритеты ──────────


def test_lhm_wins_when_present(monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter) -> None:
    monkeypatch.setattr(
        lhm,
        "read_lhm_temperatures_and_voltages",
        lambda: ({"cpu/package": 65.0, "gpu/temperature": 51.0}, {}),
    )
    # psutil тоже отдаёт данные, но LHM приоритетнее и до psutil дело не доходит.
    monkeypatch.setattr(
        psutil,
        "sensors_temperatures",
        lambda: {"acpi": [_FakePsutilEntry("zone0", 99.9)]},
        raising=False,
    )

    temps = adapter._read_temperatures()

    assert temps == {"cpu/package": 65.0, "gpu/temperature": 51.0}
    # Ключ от psutil не должен затереть LHM.
    assert "zone0" not in temps


def test_lhm_voltages_propagate_to_snapshot(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    """LHM-вольтаж должен прийти в кортеже из :meth:`_read_sensors`."""
    monkeypatch.setattr(
        lhm,
        "read_lhm_temperatures_and_voltages",
        lambda: (
            {"cpu/cpu_package": 60.0},
            {"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95},
        ),
    )

    temps, voltages = adapter._read_sensors()

    assert temps == {"cpu/cpu_package": 60.0}
    assert voltages == {"cpu/cpu_core": 1.275, "gpunvidia/gpu_core": 0.95}


def test_psutil_used_when_lhm_empty(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    monkeypatch.setattr(lhm, "read_lhm_temperatures_and_voltages", lambda: ({}, {}))
    monkeypatch.setattr(
        psutil,
        "sensors_temperatures",
        lambda: {"acpi": [_FakePsutilEntry("zone0", 47.5)]},
        raising=False,
    )

    temps = adapter._read_temperatures()

    assert temps == {"zone0": 47.5}


def test_perf_counter_used_when_lhm_and_psutil_empty(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    monkeypatch.setattr(lhm, "read_lhm_temperatures_and_voltages", lambda: ({}, {}))
    monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {}, raising=False)
    monkeypatch.setattr(
        wmi_temps,
        "read_perf_counter_thermal_zone",
        lambda: {"\\thermal zone(_total)\\temperature": 42.0},
    )

    temps = adapter._read_temperatures()

    assert temps == {"\\thermal zone(_total)\\temperature": 42.0}


def test_msacpi_used_as_last_resort(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    monkeypatch.setattr(
        wmi_temps,
        "read_msacpi_thermal_zone",
        lambda: {"thermal_zone_0": 38.0},
    )

    temps = adapter._read_temperatures()

    assert temps == {"thermal_zone_0": 38.0}


def test_all_sources_empty_returns_empty_dict(adapter: WindowsAdapter) -> None:
    """Полная деградация — пустой словарь, никаких исключений."""
    assert adapter._read_temperatures() == {}


def test_psutil_raising_is_caught(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    """psutil.sensors_temperatures на Windows может бросать NotImplementedError."""

    def boom() -> dict[str, list[Any]]:
        raise NotImplementedError("not on this platform")

    monkeypatch.setattr(psutil, "sensors_temperatures", boom, raising=False)
    monkeypatch.setattr(
        wmi_temps,
        "read_perf_counter_thermal_zone",
        lambda: {"counter_0": 50.0},
    )

    temps = adapter._read_temperatures()

    # Не падаем; идём дальше по pipeline.
    assert temps == {"counter_0": 50.0}


def test_get_available_temps_reflects_pipeline(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    """``get_available_temps()`` базового адаптера зовёт _read_temperatures()."""
    monkeypatch.setattr(
        lhm,
        "read_lhm_temperatures_and_voltages",
        lambda: ({"cpu/package": 60.0, "cpu/core_0": 58.0}, {}),
    )

    keys = adapter.get_available_temps()

    assert set(keys) == {"cpu/package", "cpu/core_0"}


def test_smartctl_replaces_lhm_storage_temps_to_avoid_dups(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    """Regression: «Накопители» не должны показывать дубли LHM + smartctl.

    Раньше LHM публиковал ``storage/composite_temperature`` + ``storage/temperature_2``,
    а smartctl поверх — ``storage/nvme0/temperature``. Проверка `k not in temps`
    дубли не отлавливала (разные формы ключей), и в UI «Накопители» один
    физический диск появлялся 2-3 раза. При наличии smartctl-данных LHM
    storage 2-сегментные ключи должны быть выкинуты.

    CPU/GPU/fan-LHM ключи и storage-имена дисков (для frontend) при этом
    сохраняются.
    """
    monkeypatch.setattr(
        lhm,
        "read_lhm_temperatures_and_voltages",
        lambda: (
            {
                "cpu/package": 65.0,                        # сохранить
                "gpunvidia/gpu_core": 50.0,                 # сохранить
                "storage/composite_temperature": 34.0,      # выкинуть — есть smartctl
                "storage/temperature_2": 61.9,              # выкинуть — есть smartctl
            },
            {},
        ),
    )
    monkeypatch.setattr(
        smartctl,
        "read_smartctl_temperatures",
        lambda: {
            "storage/nvme0/temperature": 35.0,
            "storage/drive1/temperature": 31.0,
        },
    )

    temps = adapter._read_temperatures()

    # smartctl ключи присутствуют.
    assert temps["storage/nvme0/temperature"] == 35.0
    assert temps["storage/drive1/temperature"] == 31.0
    # 2-сегментные LHM storage/* выкинуты.
    assert "storage/composite_temperature" not in temps
    assert "storage/temperature_2" not in temps
    # CPU/GPU LHM-ключи сохранены.
    assert temps["cpu/package"] == 65.0
    assert temps["gpunvidia/gpu_core"] == 50.0


def test_lhm_storage_kept_when_no_smartctl(
    monkeypatch: pytest.MonkeyPatch, adapter: WindowsAdapter
) -> None:
    """Без smartctl LHM storage/* остаётся (Linux/Astra без smartctl-capability,
    либо Windows где smartctl ничего не вернул)."""
    monkeypatch.setattr(
        lhm,
        "read_lhm_temperatures_and_voltages",
        lambda: (
            {"storage/composite_temperature": 40.0},
            {},
        ),
    )
    # smartctl уже замокан в _silence_real_sensors как пустой.

    temps = adapter._read_temperatures()

    assert temps == {"storage/composite_temperature": 40.0}


# ────────── вспомогательные ──────────


class _FakePsutilEntry:
    """Минимальная имитация psutil._common.shwtemp."""

    def __init__(self, label: str, current: float, high: float | None = None) -> None:
        self.label = label
        self.current = current
        self.high = high
        self.critical = None
