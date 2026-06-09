"""Регрессии для ``build_capability_summary`` (P1.1).

Helper рендерит одну строку «Capability: …» в `apexcore info`, поэтому
покрытие — самые частые сценарии Windows + degraded + Linux.

Все источники мокаются: ``get_last_cpu_temp_source`` (windows side-channel),
``nvidia_ml.is_available`` + ``read_nvml_device_names`` (GPU probe),
``platform.system`` (ОС). Тесты должны проходить на любой ОС.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from apexcore.application import diagnostics_sensors as diag_mod
from apexcore.application.diagnostics_sensors import build_capability_summary
from apexcore.domain.models import MetricSnapshot
from apexcore.domain.sensor_models import DegradedReason, ProbeResult, SourceBackend
from apexcore.infrastructure.sensors import lhm as lhm_mod
from apexcore.infrastructure.sensors import nvidia_ml as nvml_mod
from apexcore.infrastructure.sensors import probe as probe_mod


def _snap(voltages: dict[str, float] | None = None) -> MetricSnapshot:
    """Минимальный snapshot для проверки Vcore-суффикса."""
    return MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0,
        cpu_per_core_percent=[],
        ram_percent=20.0,
        temperatures={"cpu/package": 45.0},
        frequencies={"cpu_avg": 3200.0},
        voltages=voltages or {},
    )


def _patch_windows(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: SourceBackend | None,
    quality: str = "silicon",
    gpu_present: bool = True,
) -> None:
    """Привести модуль к состоянию «Windows + кастомный side-channel + NVML"""
    monkeypatch.setattr(diag_mod.platform, "system", lambda: "Windows")

    # Side-channel из windows-адаптера. Импорт ленивый внутри helper'а —
    # патчим модуль windows напрямую.
    from apexcore.infrastructure.adapters import windows as win_mod

    monkeypatch.setattr(win_mod, "_last_cpu_temp_source", source)
    monkeypatch.setattr(win_mod, "_last_cpu_temp_quality", quality)

    # NVML.
    monkeypatch.setattr(nvml_mod, "is_available", lambda: gpu_present)
    monkeypatch.setattr(
        nvml_mod,
        "read_nvml_device_names",
        lambda: ({0: "NVIDIA GeForce RTX 4090"} if gpu_present else {}),
    )


# ─── Параметризованные сценарии Windows: source × quality × GPU ────────────


WINDOWS_SCENARIOS = [
    pytest.param(
        SourceBackend.HWINFO_SHM,
        "silicon",
        True,
        {"cpu/vcore": 1.21},
        "HWiNFO SHM+NVML (silicon CPU/GPU, Vcore доступен)",
        id="hwinfo+nvml+vcore",
    ),
    pytest.param(
        SourceBackend.HWINFO_SHM,
        "silicon",
        True,
        {},
        "HWiNFO SHM+NVML (silicon CPU/GPU, Vcore недоступен)",
        id="hwinfo+nvml-no-vcore",
    ),
    pytest.param(
        SourceBackend.CORETEMP_SHM,
        "silicon",
        True,
        {},
        "CoreTemp SHM+NVML (silicon CPU/GPU, Vcore недоступен)",
        id="coretemp+nvml-no-vcore",
    ),
    pytest.param(
        SourceBackend.LHM,
        "silicon",
        True,
        {"cpu/cpu_core": 1.2},
        "LHM+NVML (silicon CPU/GPU, Vcore доступен)",
        id="lhm+nvml+vcore",
    ),
    pytest.param(
        SourceBackend.LHM,
        "silicon",
        False,
        {},
        "LHM (silicon CPU, Vcore недоступен)",
        id="lhm-no-gpu",
    ),
    pytest.param(
        SourceBackend.PERF_COUNTER,
        "approximate",
        True,
        {},
        "ACPI zone+NVML (approximate CPU/GPU, Vcore недоступен)",
        id="acpi-fake-zone+nvml",
    ),
    pytest.param(
        SourceBackend.WMI,
        "approximate",
        False,
        {},
        "WMI MSAcpi (approximate CPU, Vcore недоступен)",
        id="wmi-msacpi-fake",
    ),
    pytest.param(
        SourceBackend.PSUTIL,
        "silicon",
        True,
        {},
        "psutil+NVML (silicon CPU/GPU, Vcore недоступен)",
        id="psutil-fallback",
    ),
]


@pytest.mark.parametrize(
    "source, quality, gpu_present, voltages, expected", WINDOWS_SCENARIOS
)
def test_capability_summary_windows_scenarios(
    monkeypatch: pytest.MonkeyPatch,
    source: SourceBackend,
    quality: str,
    gpu_present: bool,
    voltages: dict[str, float],
    expected: str,
) -> None:
    _patch_windows(
        monkeypatch, source=source, quality=quality, gpu_present=gpu_present
    )
    snap = _snap(voltages=voltages)
    assert build_capability_summary(snap) == expected


def test_capability_summary_windows_no_snap_omits_vcore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без snap helper не врёт про наличие Vcore — суффикса просто нет."""
    _patch_windows(monkeypatch, source=SourceBackend.HWINFO_SHM, gpu_present=True)
    out = build_capability_summary(None)
    assert out == "HWiNFO SHM+NVML (silicon CPU/GPU)"
    assert "Vcore" not in out


# ─── Degraded: source=None → классификация причины через probe ─────────────


def _make_probe(**overrides: object) -> ProbeResult:
    defaults: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc),
        "architecture": "x64",
        "is_admin": True,
        "dotnet_versions": ["4.8", "9.0.0"],
        "hvci_enabled": False,
        "sac_enabled": False,
        "vbl_enabled": False,
        "defender_quarantine_winring0": False,
        "av_vendor": None,
        "shm_available": {"hwinfo": False, "coretemp": False, "aida64": False},
    }
    defaults.update(overrides)
    return ProbeResult(**defaults)  # type: ignore[arg-type]


def test_capability_summary_windows_degraded_hvci(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Source=None + HVCI active → строка про HVCI."""
    _patch_windows(monkeypatch, source=None, quality="unavailable", gpu_present=False)

    # DLL «есть», иначе classify вернёт NO_LHM_DLL.
    fake_dll = tmp_path / "lib" / "LibreHardwareMonitorLib.dll"
    fake_dll.parent.mkdir(parents=True, exist_ok=True)
    fake_dll.write_bytes(b"\x4d\x5a")
    monkeypatch.setattr(lhm_mod, "_LIB_DLL", fake_dll)

    monkeypatch.setattr(
        probe_mod, "run_full_probe", lambda *_a, **_kw: _make_probe(hvci_enabled=True)
    )

    out = build_capability_summary(_snap())
    assert "Источников нет" in out
    assert DegradedReason.HVCI_BLOCKED.short() in out
    assert "apexcore doctor" in out


def test_capability_summary_windows_degraded_no_dll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source=None + DLL отсутствует → NO_LHM_DLL имеет приоритет."""
    _patch_windows(monkeypatch, source=None, quality="unavailable", gpu_present=False)
    monkeypatch.setattr(
        lhm_mod, "_LIB_DLL", Path("Z:\\nonexistent\\LibreHardwareMonitorLib.dll")
    )
    # Probe не должна влиять — classify выйдет на DLL первой.
    monkeypatch.setattr(
        probe_mod,
        "run_full_probe",
        lambda *_a, **_kw: _make_probe(hvci_enabled=True),
    )
    out = build_capability_summary(_snap())
    assert DegradedReason.NO_LHM_DLL.short() in out


# ─── Linux: hwmon-фоллбэк ──────────────────────────────────────────────────


def test_capability_summary_linux_with_cpu_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diag_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(nvml_mod, "is_available", lambda: True)
    monkeypatch.setattr(
        nvml_mod, "read_nvml_device_names", lambda: {0: "NVIDIA GeForce RTX 4090"}
    )
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0,
        ram_percent=20.0,
        temperatures={"coretemp/core 0": 50.0},
        frequencies={},
        voltages={},
    )
    out = build_capability_summary(snap)
    assert out == "hwmon+NVML (silicon CPU/GPU, Vcore недоступен)"


def test_capability_summary_linux_without_cpu_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diag_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(nvml_mod, "is_available", lambda: False)
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=10.0,
        ram_percent=20.0,
        temperatures={},
        frequencies={},
        voltages={},
    )
    out = build_capability_summary(snap)
    assert "Источников нет" in out
    assert "lm-sensors" in out


# ─── Защита от exception: helper никогда не должен ронять `apexcore info` ──


def test_capability_summary_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если impl-функция бросает — helper возвращает строку-заглушку."""
    def boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(diag_mod, "_build_capability_summary_impl", boom)
    assert build_capability_summary(_snap()) == "Capability недоступна"
