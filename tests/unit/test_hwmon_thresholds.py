"""Юнит-тесты ``infrastructure/sensors/hwmon_thresholds.py``."""

from __future__ import annotations

from pathlib import Path

from apexcore.infrastructure.sensors import hwmon_thresholds


def _setup_fake_hwmon(tmp_path: Path) -> Path:
    """Создать фейковую структуру /sys/class/hwmon/."""
    root = tmp_path / "hwmon"
    root.mkdir()
    return root


def test_returns_empty_when_root_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", tmp_path / "nonexistent")
    assert hwmon_thresholds.read_hwmon_tjmax() == {}


def test_reads_coretemp_crit(monkeypatch, tmp_path):
    root = _setup_fake_hwmon(tmp_path)
    hwmon0 = root / "hwmon0"
    hwmon0.mkdir()
    (hwmon0 / "name").write_text("coretemp\n", encoding="utf-8")
    (hwmon0 / "temp1_crit").write_text("100000\n", encoding="utf-8")
    (hwmon0 / "temp2_crit").write_text("100000\n", encoding="utf-8")
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", root)
    result = hwmon_thresholds.read_hwmon_tjmax()
    assert result == {"coretemp/temp1": 100.0, "coretemp/temp2": 100.0}


def test_falls_back_to_temp_max_when_no_crit(monkeypatch, tmp_path):
    root = _setup_fake_hwmon(tmp_path)
    hwmon0 = root / "hwmon0"
    hwmon0.mkdir()
    (hwmon0 / "name").write_text("k10temp\n", encoding="utf-8")
    (hwmon0 / "temp1_max").write_text("95000\n", encoding="utf-8")
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", root)
    result = hwmon_thresholds.read_hwmon_tjmax()
    assert result == {"k10temp/temp1": 95.0}


def test_skips_non_cpu_hwmon(monkeypatch, tmp_path):
    root = _setup_fake_hwmon(tmp_path)
    # nvme — не CPU.
    hwmon_nvme = root / "hwmon0"
    hwmon_nvme.mkdir()
    (hwmon_nvme / "name").write_text("nvme\n", encoding="utf-8")
    (hwmon_nvme / "temp1_crit").write_text("85000\n", encoding="utf-8")
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", root)
    result = hwmon_thresholds.read_hwmon_tjmax()
    assert result == {}


def test_filters_implausible_values(monkeypatch, tmp_path):
    root = _setup_fake_hwmon(tmp_path)
    hwmon0 = root / "hwmon0"
    hwmon0.mkdir()
    (hwmon0 / "name").write_text("coretemp\n", encoding="utf-8")
    # 200°C — нереально высокое значение, фильтруем.
    (hwmon0 / "temp1_crit").write_text("200000\n", encoding="utf-8")
    # 30°C — нереально низкое.
    (hwmon0 / "temp2_crit").write_text("30000\n", encoding="utf-8")
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", root)
    result = hwmon_thresholds.read_hwmon_tjmax()
    assert result == {}


def test_best_tjmax_returns_minimum():
    thresholds = {"a": 100.0, "b": 95.0, "c": 105.0}
    assert hwmon_thresholds.best_tjmax(thresholds) == 95.0


def test_best_tjmax_falls_back_when_empty():
    assert hwmon_thresholds.best_tjmax({}, fallback=88.0) == 88.0


def test_best_tjmax_filters_implausible_before_min():
    # 50°C — слишком низко (отбраковка).
    thresholds = {"good": 100.0, "bad": 50.0}
    assert hwmon_thresholds.best_tjmax(thresholds) == 100.0


def test_handles_permission_error_gracefully(monkeypatch, tmp_path):
    """На Astra SE с MAC может быть PermissionError — функция просто молчит."""
    root = _setup_fake_hwmon(tmp_path)
    hwmon0 = root / "hwmon0"
    hwmon0.mkdir()
    (hwmon0 / "name").write_text("coretemp\n", encoding="utf-8")
    crit = hwmon0 / "temp1_crit"
    crit.write_text("100000\n", encoding="utf-8")
    monkeypatch.setattr(hwmon_thresholds, "_HWMON_ROOT", root)

    # Симулируем PermissionError при чтении (через monkeypatch на Path.read_text).
    real_read = Path.read_text

    def fake_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "temp1_crit":
            raise PermissionError("Astra SE MAC")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)
    result = hwmon_thresholds.read_hwmon_tjmax()
    assert result == {}
