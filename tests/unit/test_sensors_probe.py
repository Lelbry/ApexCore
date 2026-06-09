"""Unit-тесты для probe-фазы (``infrastructure.sensors.probe``).

Каждая probe-функция мокается через monkeypatch — никаких реальных
winreg / subprocess / ctypes-вызовов. Цели:

- проверить, что probe не падает при отсутствии источников (graceful);
- проверить классификацию архитектуры (x64/ARM64/x86);
- проверить кэш module-level;
- проверить, что на Linux probe возвращает skeleton без Windows-полей.
"""

from __future__ import annotations

import platform

import pytest

from apexcore.domain.sensor_models import ProbeResult
from apexcore.infrastructure.sensors import probe


@pytest.fixture(autouse=True)
def _reset_probe_cache() -> None:
    """Перед каждым тестом сбрасываем module-level cache."""
    probe.reset_cache()
    yield
    probe.reset_cache()


def test_probe_architecture_x64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert probe.probe_architecture() == "x64"


def test_probe_architecture_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    assert probe.probe_architecture() == "ARM64"


def test_probe_architecture_x86(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "i686")
    assert probe.probe_architecture() == "x86"


def test_probe_architecture_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "RISCV64")
    assert probe.probe_architecture() == "riscv64"


def test_run_full_probe_returns_probe_result_skeleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe не должен падать и возвращает ``ProbeResult``."""
    # Мокаем все Windows-only функции, чтобы тест работал и на Linux/CI.
    monkeypatch.setattr(probe, "probe_dotnet_runtimes", lambda: ["4.8"])
    monkeypatch.setattr(probe, "probe_hvci_status", lambda: False)
    monkeypatch.setattr(probe, "probe_sac_status", lambda: False)
    monkeypatch.setattr(probe, "probe_vbl_status", lambda: False)
    monkeypatch.setattr(
        probe,
        "probe_shm_available",
        lambda: {"hwinfo": False, "coretemp": False, "aida64": False},
    )
    monkeypatch.setattr(probe, "probe_av_vendor", lambda: None)
    monkeypatch.setattr(probe, "probe_defender_quarantine", lambda: False)
    monkeypatch.setattr(probe, "probe_admin", lambda: False)
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    result = probe.run_full_probe()

    assert isinstance(result, ProbeResult)
    assert result.architecture in {"x64", "ARM64", "x86", "amd64", "unknown"}
    assert "hwinfo" in result.shm_available
    assert "coretemp" in result.shm_available
    assert "aida64" in result.shm_available


def test_run_full_probe_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Второй вызов ``run_full_probe()`` не должен дёргать probe заново."""
    call_counter = {"n": 0}

    def counting_admin() -> bool:
        call_counter["n"] += 1
        return False

    monkeypatch.setattr(probe, "probe_admin", counting_admin)
    monkeypatch.setattr(probe, "probe_dotnet_runtimes", lambda: [])
    monkeypatch.setattr(probe, "probe_hvci_status", lambda: False)
    monkeypatch.setattr(probe, "probe_sac_status", lambda: False)
    monkeypatch.setattr(probe, "probe_vbl_status", lambda: False)
    monkeypatch.setattr(
        probe,
        "probe_shm_available",
        lambda: {"hwinfo": False, "coretemp": False, "aida64": False},
    )
    monkeypatch.setattr(probe, "probe_av_vendor", lambda: None)
    monkeypatch.setattr(probe, "probe_defender_quarantine", lambda: False)

    probe.run_full_probe()
    probe.run_full_probe()
    probe.run_full_probe()

    # На Windows probe_admin вызовется один раз (благодаря кэшу).
    # На Linux не вызовется вообще (skeleton-ветка).
    if platform.system().lower() == "windows":
        assert call_counter["n"] == 1


def test_run_full_probe_force_resets_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force=True`` пересоздаёт ``ProbeResult``."""
    call_counter = {"n": 0}

    def counting_admin() -> bool:
        call_counter["n"] += 1
        return False

    monkeypatch.setattr(probe, "probe_admin", counting_admin)
    monkeypatch.setattr(probe, "probe_dotnet_runtimes", lambda: [])
    monkeypatch.setattr(probe, "probe_hvci_status", lambda: False)
    monkeypatch.setattr(probe, "probe_sac_status", lambda: False)
    monkeypatch.setattr(probe, "probe_vbl_status", lambda: False)
    monkeypatch.setattr(
        probe,
        "probe_shm_available",
        lambda: {"hwinfo": False, "coretemp": False, "aida64": False},
    )
    monkeypatch.setattr(probe, "probe_av_vendor", lambda: None)
    monkeypatch.setattr(probe, "probe_defender_quarantine", lambda: False)

    probe.run_full_probe()
    probe.run_full_probe(force=True)

    if platform.system().lower() == "windows":
        assert call_counter["n"] == 2


def test_probe_shm_available_non_windows() -> None:
    """На не-Windows все SHM = False (нет OpenFileMapping)."""
    if platform.system().lower() == "windows":
        pytest.skip("test-case для Linux/macOS")
    result = probe.probe_shm_available()
    assert result == {"hwinfo": False, "coretemp": False, "aida64": False}


def test_probe_admin_non_windows() -> None:
    """На Linux ``probe_admin`` не падает и возвращает bool."""
    if platform.system().lower() == "windows":
        pytest.skip("test-case для Linux/macOS")
    assert isinstance(probe.probe_admin(), bool)


def test_probe_hvci_status_returns_bool() -> None:
    """``probe_hvci_status`` всегда возвращает bool (graceful на любой ОС)."""
    assert isinstance(probe.probe_hvci_status(), bool)


def test_probe_dotnet_runtimes_returns_list() -> None:
    """``probe_dotnet_runtimes`` возвращает список (может быть пустым)."""
    result = probe.probe_dotnet_runtimes()
    assert isinstance(result, list)
    for v in result:
        assert isinstance(v, str)


# ─── Параметризованные winreg-тесты (P1.2 fixtures) ─────────────────────────


class _FakeWinregKey:
    """Контекст-менеджер, имитирующий winreg-key с заданным DWORD."""

    def __init__(self, value: int | None) -> None:
        self.value = value

    def __enter__(self) -> _FakeWinregKey:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.fixture
def _winreg_dword_mock(monkeypatch: pytest.MonkeyPatch):
    """Подменить ``winreg.OpenKey`` + ``QueryValueEx`` фиксированным значением.

    Возвращает функцию ``set_value(int | None)``:
    - int → ключ открывается и возвращает (value, REG_DWORD);
    - None → ``winreg.OpenKey`` бросает OSError (ключ отсутствует).
    """
    if platform.system().lower() != "windows":
        pytest.skip("winreg недоступен — тест Windows-only")
    import winreg  # type: ignore

    state: dict[str, int | None] = {"value": None}

    def fake_open_key(*_args: object, **_kw: object) -> _FakeWinregKey:
        if state["value"] is None:
            raise OSError("simulated: key not found")
        return _FakeWinregKey(state["value"])

    def fake_query_value_ex(key: _FakeWinregKey, _name: str) -> tuple[int, int]:
        assert isinstance(key, _FakeWinregKey)
        return (int(key.value), 4)  # REG_DWORD = 4

    monkeypatch.setattr(winreg, "OpenKey", fake_open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", fake_query_value_ex)

    def set_value(v: int | None) -> None:
        state["value"] = v

    return set_value


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(1, True, id="enabled"),
        pytest.param(0, False, id="disabled"),
        pytest.param(None, False, id="key-missing"),
        pytest.param(2, True, id="non-zero-also-truthy"),
    ],
)
def test_probe_hvci_status(_winreg_dword_mock, value: int | None, expected: bool) -> None:
    """``probe_hvci_status`` корректно интерпретирует DWORD из реестра."""
    _winreg_dword_mock(value)
    assert probe.probe_hvci_status() is expected


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(1, True, id="sac-enabled"),
        pytest.param(0, False, id="sac-disabled"),
        pytest.param(None, False, id="sac-key-missing"),
    ],
)
def test_probe_sac_status(_winreg_dword_mock, value: int | None, expected: bool) -> None:
    """``probe_sac_status`` корректно интерпретирует VerifiedAndReputablePolicyState."""
    _winreg_dword_mock(value)
    assert probe.probe_sac_status() is expected


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(1, True, id="vbl-enabled-default-since-22h2"),
        pytest.param(0, False, id="vbl-disabled-explicitly"),
        pytest.param(None, False, id="vbl-key-missing-old-build"),
    ],
)
def test_probe_vbl_status(_winreg_dword_mock, value: int | None, expected: bool) -> None:
    """``probe_vbl_status`` корректно интерпретирует Vulnerable Driver Blocklist."""
    _winreg_dword_mock(value)
    assert probe.probe_vbl_status() is expected


@pytest.mark.parametrize(
    "machine_value, expected",
    [
        pytest.param("AMD64", "x64", id="windows-x64"),
        pytest.param("x86_64", "x64", id="linux-x64"),
        pytest.param("ARM64", "ARM64", id="snapdragon-arm64"),
        pytest.param("aarch64", "ARM64", id="linux-arm64"),
        pytest.param("x86", "x86", id="32bit-x86"),
        pytest.param("i686", "x86", id="32bit-i686"),
        pytest.param("RISCV64", "riscv64", id="unknown-platform-lowered"),
        pytest.param("", "unknown", id="empty-machine-string"),
    ],
)
def test_probe_architecture_parametrized(
    monkeypatch: pytest.MonkeyPatch, machine_value: str, expected: str
) -> None:
    """Параметризованная проверка normalize'а ``platform.machine()``."""
    monkeypatch.setattr(platform, "machine", lambda: machine_value)
    assert probe.probe_architecture() == expected


def test_probe_shm_available_keys_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``probe_shm_available`` всегда возвращает ключи hwinfo/coretemp/aida64."""
    result = probe.probe_shm_available()
    assert set(result.keys()) == {"hwinfo", "coretemp", "aida64"}
    for v in result.values():
        assert isinstance(v, bool)


@pytest.mark.parametrize(
    "all_overrides, expected_hvci, expected_sac, expected_vbl",
    [
        pytest.param(
            {"hvci_enabled": True}, True, False, False, id="hvci-only"
        ),
        pytest.param(
            {"sac_enabled": True}, False, True, False, id="sac-only"
        ),
        pytest.param(
            {"vbl_enabled": True}, False, False, True, id="vbl-only"
        ),
        pytest.param(
            {"hvci_enabled": True, "sac_enabled": True, "vbl_enabled": True},
            True,
            True,
            True,
            id="all-three-secured-core",
        ),
    ],
)
def test_run_full_probe_preserves_security_flags(
    monkeypatch: pytest.MonkeyPatch,
    all_overrides: dict[str, bool],
    expected_hvci: bool,
    expected_sac: bool,
    expected_vbl: bool,
) -> None:
    """Probe-flags из individual функций корректно попадают в ``ProbeResult``."""
    monkeypatch.setattr(probe, "probe_dotnet_runtimes", lambda: ["4.8"])
    monkeypatch.setattr(probe, "probe_admin", lambda: True)
    monkeypatch.setattr(
        probe, "probe_hvci_status", lambda: all_overrides.get("hvci_enabled", False)
    )
    monkeypatch.setattr(
        probe, "probe_sac_status", lambda: all_overrides.get("sac_enabled", False)
    )
    monkeypatch.setattr(
        probe, "probe_vbl_status", lambda: all_overrides.get("vbl_enabled", False)
    )
    monkeypatch.setattr(
        probe,
        "probe_shm_available",
        lambda: {"hwinfo": False, "coretemp": False, "aida64": False},
    )
    monkeypatch.setattr(probe, "probe_av_vendor", lambda: None)
    monkeypatch.setattr(probe, "probe_defender_quarantine", lambda: False)
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    result = probe.run_full_probe(force=True)

    assert result.hvci_enabled is expected_hvci
    assert result.sac_enabled is expected_sac
    assert result.vbl_enabled is expected_vbl
