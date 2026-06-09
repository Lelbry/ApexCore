"""Параметризованные регрессии для классификации ``DegradedReason``.

Закрывает P1.2 (HVCI/SAC test fixtures) — проверяет приоритет
классификации причин в ``_classify_lhm_no_cpu_reason`` и корректные
формулировки advice в ``diagnose_sensors`` для каждого сценария.

Все probe-функции мокаются — никаких реальных winreg/subprocess вызовов.
Тесты должны проходить на любой ОС (включая CI без Windows-API).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from apexcore.application import diagnostics_sensors as diag_mod
from apexcore.application.diagnostics_sensors import _classify_lhm_no_cpu_reason
from apexcore.domain.sensor_models import DegradedReason, ProbeResult
from apexcore.infrastructure.sensors import lhm as lhm_mod
from apexcore.infrastructure.sensors import probe as probe_mod


def _make_probe(**overrides: object) -> ProbeResult:
    """Соберать ``ProbeResult`` с дефолтами «всё ок» + патчи."""
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


# ─── Параметризованные сценарии для _classify_lhm_no_cpu_reason ────────────

# Каждая строка: (имя сценария, probe-overrides, ожидаемый DegradedReason).
# Тесты ниже выполняют их **с DLL в наличии** (чтобы NO_LHM_DLL не выиграл).
CLASSIFY_SCENARIOS = [
    pytest.param(
        {"hvci_enabled": True, "sac_enabled": True},
        DegradedReason.HVCI_BLOCKED,
        id="hvci-beats-sac",
    ),
    pytest.param(
        {"sac_enabled": True, "defender_quarantine_winring0": True},
        DegradedReason.SAC_BLOCKED,
        id="sac-beats-defender",
    ),
    pytest.param(
        {
            "defender_quarantine_winring0": True,
            "av_vendor": "Avast Antivirus",
        },
        DegradedReason.DEFENDER_BLOCKED,
        id="defender-beats-av",
    ),
    pytest.param(
        {"av_vendor": "Kaspersky Internet Security"},
        DegradedReason.AV_BLOCKED,
        id="av-only",
    ),
    pytest.param(
        {"is_admin": False},
        DegradedReason.NO_ADMIN,
        id="no-admin",
    ),
    pytest.param(
        {},
        DegradedReason.CPU_UNSUPPORTED,
        id="all-clear-fallback-cpu-unsupported",
    ),
]


@pytest.mark.parametrize("overrides, expected", CLASSIFY_SCENARIOS)
def test_classify_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, object],
    expected: DegradedReason,
) -> None:
    """Проверка приоритета causes: HVCI > SAC > Defender > AV > NO_ADMIN > CPU_UNSUPPORTED."""
    # DLL «есть» — иначе сработает NO_LHM_DLL поверх probe.
    fake_dll = tmp_path / "lib" / "LibreHardwareMonitorLib.dll"
    fake_dll.parent.mkdir(parents=True, exist_ok=True)
    fake_dll.write_bytes(b"\x4d\x5a")  # MZ — фиктивный PE header
    monkeypatch.setattr(lhm_mod, "_LIB_DLL", fake_dll)

    # Probe-результат — кастомный для сценария.
    probe = _make_probe(**overrides)
    monkeypatch.setattr(probe_mod, "run_full_probe", lambda *_args, **_kw: probe)

    assert _classify_lhm_no_cpu_reason() is expected


def test_classify_no_lhm_dll_beats_any_probe_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Регрессия**: NO_LHM_DLL имеет высший приоритет — даже над HVCI."""
    # DLL «отсутствует».
    monkeypatch.setattr(
        lhm_mod, "_LIB_DLL", Path("Z:\\nonexistent\\lib\\LibreHardwareMonitorLib.dll")
    )
    # Probe «всё плохо» — но это не должно перебить NO_LHM_DLL.
    probe = _make_probe(
        hvci_enabled=True,
        sac_enabled=True,
        defender_quarantine_winring0=True,
        av_vendor="Avast",
        is_admin=False,
    )
    monkeypatch.setattr(probe_mod, "run_full_probe", lambda *_args, **_kw: probe)

    assert _classify_lhm_no_cpu_reason() is DegradedReason.NO_LHM_DLL


def test_classify_probe_failure_returns_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если probe бросает — classify не падает, возвращает UNKNOWN."""
    fake_dll = tmp_path / "lib" / "LibreHardwareMonitorLib.dll"
    fake_dll.parent.mkdir(parents=True, exist_ok=True)
    fake_dll.write_bytes(b"\x4d\x5a")
    monkeypatch.setattr(lhm_mod, "_LIB_DLL", fake_dll)

    def boom(*_args: object, **_kw: object) -> ProbeResult:
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(probe_mod, "run_full_probe", boom)

    assert _classify_lhm_no_cpu_reason() is DegradedReason.UNKNOWN


# ─── Интеграция: diagnose_sensors → degraded_reasons + advice ──────────────

# Каждый сценарий: probe-overrides + ожидаемая подстрока в любой advice-строке.
# Это проверяет UX-формулировки из плана §5.1.
ADVICE_SCENARIOS = [
    pytest.param(
        {"hvci_enabled": True},
        DegradedReason.HVCI_BLOCKED,
        "PawnIO",
        id="hvci-advice-mentions-pawnio",
    ),
    pytest.param(
        {"sac_enabled": True},
        DegradedReason.SAC_BLOCKED,
        "PawnIO",
        id="sac-advice-mentions-pawnio",
    ),
    pytest.param(
        {"defender_quarantine_winring0": True},
        DegradedReason.DEFENDER_BLOCKED,
        "CVE-2020-14979",
        id="defender-advice-mentions-cve",
    ),
    pytest.param(
        {"av_vendor": "Avast Antivirus"},
        DegradedReason.AV_BLOCKED,
        "Avast",
        id="av-advice-mentions-vendor-name",
    ),
    pytest.param(
        {"is_admin": False},
        DegradedReason.NO_ADMIN,
        "ОДИН РАЗ",
        id="no-admin-advice-mentions-single-admin-run",
    ),
]


@pytest.mark.parametrize("overrides, expected_reason, advice_substr", ADVICE_SCENARIOS)
def test_diagnose_advice_for_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, object],
    expected_reason: DegradedReason,
    advice_substr: str,
) -> None:
    """Для каждого ``DegradedReason`` хотя бы одна строка ``advice`` упоминает ключевое слово.

    Это контракт UX из плана §5.1: пользователь должен получить конкретное
    действие (PawnIO/HWiNFO/admin/...) для своей причины, а не generic «не работает».
    """
    fake_dll = tmp_path / "lib" / "LibreHardwareMonitorLib.dll"
    fake_dll.parent.mkdir(parents=True, exist_ok=True)
    fake_dll.write_bytes(b"\x4d\x5a")
    monkeypatch.setattr(lhm_mod, "_LIB_DLL", fake_dll)

    probe = _make_probe(**overrides)
    monkeypatch.setattr(probe_mod, "run_full_probe", lambda *_args, **_kw: probe)

    # Мокаем _check_lhm_runtime так, чтобы он вернул классификацию.
    # Иначе он попытается реально дёрнуть LHM и наша подмена _LIB_DLL не
    # сработает, потому что singleton _computer уже инициализирован.
    from apexcore.application.diagnostics_sensors import BackendStatus

    classified = _classify_lhm_no_cpu_reason()

    def fake_check_lhm_runtime() -> BackendStatus:
        return BackendStatus(
            name="LHM runtime (pythonnet)",
            ok=False,
            sensor_count=0,
            sample={},
            detail=f"CPU-сенсоров нет ({classified.short()})",
            reason=classified,
        )

    monkeypatch.setattr(
        diag_mod, "_check_lhm_runtime", fake_check_lhm_runtime
    )

    report = diag_mod.diagnose_sensors()

    # Главное: классифицирована правильная причина.
    assert expected_reason in report.degraded_reasons, (
        f"Сценарий {overrides}: ожидали {expected_reason} в degraded_reasons, "
        f"получили {report.degraded_reasons}"
    )
    # Главное-2: advice содержит конкретное упоминание ключевого слова.
    full_advice = " ".join(report.advice)
    assert advice_substr in full_advice, (
        f"Сценарий {overrides}: ожидали в advice подстроку '{advice_substr}', "
        f"но advice = {report.advice!r}"
    )


def test_diagnose_no_admin_yields_admin_run_advice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Регрессия: при NO_ADMIN advice упоминает однократный запуск от админа."""
    fake_dll = tmp_path / "lib" / "LibreHardwareMonitorLib.dll"
    fake_dll.parent.mkdir(parents=True, exist_ok=True)
    fake_dll.write_bytes(b"\x4d\x5a")
    monkeypatch.setattr(lhm_mod, "_LIB_DLL", fake_dll)

    probe = _make_probe(is_admin=False)
    monkeypatch.setattr(probe_mod, "run_full_probe", lambda *_args, **_kw: probe)

    from apexcore.application.diagnostics_sensors import BackendStatus

    monkeypatch.setattr(
        diag_mod,
        "_check_lhm_runtime",
        lambda: BackendStatus(
            name="LHM runtime (pythonnet)",
            ok=False,
            detail="CPU-сенсоров нет (нужны admin-права)",
            reason=DegradedReason.NO_ADMIN,
        ),
    )

    report = diag_mod.diagnose_sensors()

    assert DegradedReason.NO_ADMIN in report.degraded_reasons
    full = " ".join(report.advice)
    # Должно упомянуть «ОДИН РАЗ» (явная формулировка из advice).
    assert "ОДИН РАЗ" in full or "admin" in full.lower()


def test_classify_with_real_probe_does_not_throw() -> None:
    """**Smoke**: вызов с реальным probe не падает (любая ОС).

    Не проверяем конкретный DegradedReason — он зависит от ОС/железа CI.
    Проверяем только что функция отрабатывает без исключений.
    """
    result = _classify_lhm_no_cpu_reason()
    assert isinstance(result, DegradedReason)
