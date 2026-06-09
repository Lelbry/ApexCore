"""Тесты `application/throttle_detector.py`.

Mock-стратегия: подменяем `lhm._get_computer` на синтетический объект с
заданным набором CPU-сенсоров (для Windows-ветки). Для Linux-ветки —
готовим временный каталог с файлами `thermal_throttle/*_throttle_count`
через `tmp_path` и подменяем `Path("/sys/devices/system/cpu")` через
monkeypatch.

Кросс-платформенные ремарки: тесты Windows-ветки выполняются только на
Windows (используют реальный `LibreHardwareMonitor.Hardware` через
pythonnet). Linux-тесты можно прогонять на любой ОС — там только sysfs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from apexcore.application import throttle_detector
from apexcore.application.throttle_detector import ThrottleCause, read_throttle_state


@pytest.fixture(autouse=True)
def _reset_throttle_state() -> None:
    throttle_detector._reset_for_tests()
    yield
    throttle_detector._reset_for_tests()


# ────────── базовые случаи ──────────


def test_idle_no_lhm_no_freqs_returns_none() -> None:
    """Без LHM/sysfs и без частот — cause=NONE."""
    state = read_throttle_state()
    assert state.cause is ThrottleCause.NONE
    assert state.active is False


def test_heuristic_triggers_when_freq_ratio_below_threshold() -> None:
    """Старый heuristic: avg/max < 0.85 → cause=OTHER."""
    state = read_throttle_state(cpu_avg_mhz=2000, cpu_max_mhz=5000)
    assert state.cause is ThrottleCause.OTHER
    assert "0.40" in state.detail


def test_heuristic_silent_when_above_threshold() -> None:
    state = read_throttle_state(cpu_avg_mhz=4500, cpu_max_mhz=5000)
    assert state.cause is ThrottleCause.NONE


def test_heuristic_silent_when_max_zero() -> None:
    state = read_throttle_state(cpu_avg_mhz=3200, cpu_max_mhz=0)
    assert state.cause is ThrottleCause.NONE


def test_heuristic_disabled_for_hybrid_intel() -> None:
    """Гетерогенный Intel (Alder/Raptor Lake) — heuristic не срабатывает.

    На i9-12900K avg=mean(P+E)≈3.8 GHz и max=P-core boost=5.2 GHz дают
    ratio≈0.73 — гомогенный heuristic ложно бы сигналил throttle.
    Регрессия для скриншота 2026-05-17 (`Тротлинг ЦП — зафиксирован`
    при отсутствии реального throttle).
    """
    state = read_throttle_state(
        cpu_avg_mhz=3800,
        cpu_max_mhz=5200,
        cpu_model="12th Gen Intel(R) Core(TM) i9-12900K",
    )
    assert state.cause is ThrottleCause.NONE


def test_heuristic_still_active_for_non_hybrid_intel() -> None:
    """Не-гибридный CPU (Ryzen, Skylake) — heuristic работает как раньше."""
    state = read_throttle_state(
        cpu_avg_mhz=2000,
        cpu_max_mhz=5000,
        cpu_model="AMD Ryzen 7 5800X 8-Core Processor",
    )
    assert state.cause is ThrottleCause.OTHER


def test_heuristic_active_without_cpu_model() -> None:
    """Если cpu_model не задан — старое поведение (для backward-compat)."""
    state = read_throttle_state(cpu_avg_mhz=2000, cpu_max_mhz=5000)
    assert state.cause is ThrottleCause.OTHER


def test_heuristic_disabled_on_idle_amd_apu() -> None:
    """Регрессия для скриншота 2026-05-22 (Astra Ryzen 6800H, WebUI красный
    баннер «троттлинг активен · freq ratio 0.73 < 0.85» на idle).

    На AMD APU (Ryzen U/HS/HX-серии) при idle CPU частоты падают в
    powersave/P-state idle — это **штатное** энергосбережение, а не
    thermal throttle. Без проверки cpu_percent heuristic ложно срабатывал
    на ratio ~0.5 и пугал пользователя красным баннером.
    """
    state = read_throttle_state(
        cpu_avg_mhz=2400,
        cpu_max_mhz=4790,
        cpu_model="AMD Ryzen 7 6800H with Radeon Graphics",
        cpu_percent=4.0,
    )
    assert state.cause is ThrottleCause.NONE


def test_heuristic_still_triggers_under_real_load() -> None:
    """Под реальной CPU-нагрузкой ratio < 0.85 — heuristic срабатывает.

    Гарантия что cpu_percent-гард не слишком агрессивен: при стресс-тесте
    (cpu_percent ≈ 95-100%) низкий ratio всё равно классифицируется как
    throttle.
    """
    state = read_throttle_state(
        cpu_avg_mhz=2000,
        cpu_max_mhz=5000,
        cpu_model="AMD Ryzen 7 5800X 8-Core Processor",
        cpu_percent=95.0,
    )
    assert state.cause is ThrottleCause.OTHER


def test_heuristic_active_at_threshold_boundary() -> None:
    """На границе cpu_percent = 80 — trigger срабатывает (>=, не >)."""
    state = read_throttle_state(
        cpu_avg_mhz=2000,
        cpu_max_mhz=5000,
        cpu_percent=80.0,
    )
    assert state.cause is ThrottleCause.OTHER


def test_heuristic_disabled_just_below_threshold() -> None:
    """cpu_percent=79.9 → heuristic пропускает (граничный случай)."""
    state = read_throttle_state(
        cpu_avg_mhz=2000,
        cpu_max_mhz=5000,
        cpu_percent=79.9,
    )
    assert state.cause is ThrottleCause.NONE


def test_heuristic_legacy_caller_without_cpu_percent_unchanged() -> None:
    """Legacy-вызов без cpu_percent сохраняет старое поведение.

    Защита от регрессии для внешних callerов которые ещё не обновили
    сигнатуру (например тестовые фикстуры или интеграционные тесты).
    """
    state = read_throttle_state(cpu_avg_mhz=2000, cpu_max_mhz=5000)
    assert state.cause is ThrottleCause.OTHER


# ────────── классификатор имён ──────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CPU Clock Throttle - Thermal", ThrottleCause.THERMAL),
        ("Clock Throttle Power Limit", ThrottleCause.POWER),
        ("Clock Throttle Current Limit", ThrottleCause.CURRENT),
        ("Clock Throttle VR Thermal", ThrottleCause.VR_THERMAL),
        ("Clock Throttle EDP Other", ThrottleCause.OTHER),
        ("Clock Throttle Unknown", ThrottleCause.OTHER),
    ],
)
def test_classify_throttle_name(name: str, expected: ThrottleCause) -> None:
    assert throttle_detector._classify_throttle_name(name.lower()) is expected


# ────────── Linux: sysfs counter'ы ──────────


def _make_fake_sysfs(tmp_path: Path, counters: dict[str, int]) -> Path:
    """Сконструировать `tmp_path/cpu*/thermal_throttle/{core,package}_throttle_count`."""
    root = tmp_path / "cpu_root"
    root.mkdir()
    seen_cpus: set[str] = set()
    for key, value in counters.items():
        cpu, _, counter = key.partition("/")
        seen_cpus.add(cpu)
        d = root / cpu / "thermal_throttle"
        d.mkdir(parents=True, exist_ok=True)
        (d / counter).write_text(str(value))
    # Для каждого CPU создаём пустой каталог thermal_throttle если ещё нет
    for cpu in seen_cpus:
        (root / cpu / "thermal_throttle").mkdir(parents=True, exist_ok=True)
    return root


def test_linux_first_tick_no_baseline_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Первый тик — baseline сохраняется, но cause=NONE."""
    monkeypatch.setattr(sys, "platform", "linux")
    fake_root = _make_fake_sysfs(
        tmp_path, {"cpu0/core_throttle_count": 5, "cpu0/package_throttle_count": 2}
    )
    monkeypatch.setattr(throttle_detector, "Path", _path_factory(fake_root))

    state = read_throttle_state()
    assert state.cause is ThrottleCause.NONE


def test_linux_thermal_counter_increment_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Второй тик с возросшим counter'ом → cause=THERMAL."""
    monkeypatch.setattr(sys, "platform", "linux")
    fake_root = _make_fake_sysfs(
        tmp_path, {"cpu0/core_throttle_count": 5, "cpu0/package_throttle_count": 2}
    )
    monkeypatch.setattr(throttle_detector, "Path", _path_factory(fake_root))

    # baseline
    read_throttle_state()

    # Через тик — counter вырос
    (fake_root / "cpu0" / "thermal_throttle" / "core_throttle_count").write_text("8")

    state = read_throttle_state()
    assert state.cause is ThrottleCause.THERMAL
    assert "cpu0/core_throttle_count +3" in state.detail


def test_linux_no_thermal_throttle_dir_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если /sys/devices/system/cpu/cpu*/thermal_throttle отсутствует — graceful."""
    monkeypatch.setattr(sys, "platform", "linux")
    root = tmp_path / "empty"
    root.mkdir()
    (root / "cpu0").mkdir()  # есть cpu0, но нет thermal_throttle
    monkeypatch.setattr(throttle_detector, "Path", _path_factory(root))

    state = read_throttle_state()
    assert state.cause is ThrottleCause.NONE


# ────────── helpers ──────────


def _path_factory(fake_root: Path):
    """Возвращает функцию-замену `Path`, которая для пути `/sys/devices/...`
    подменяет корень на `fake_root`, а для остальных путей работает обычно.
    """
    real_path = Path

    def fake_path(arg: Any) -> Path:
        s = str(arg)
        if s == "/sys/devices/system/cpu":
            return fake_root
        return real_path(arg)

    return fake_path
