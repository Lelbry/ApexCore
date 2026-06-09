"""Юнит-тесты ``interfaces/cli/menu/stress_menu.py`` (без запуска реальной нагрузки).

Проверяет служебные функции форматирования и сами входные точки на наличие
(импортируемость, сигнатура). Полный e2e-прогон стресса требует сильной
нагрузки на железо и не делается в CI.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from rich.console import Console

from apexcore.domain.models import MetricSnapshot
from apexcore.interfaces.cli.menu import stress_menu


def _render(table) -> str:
    """Сконвертировать Rich Table в plain-text для assert'ов."""
    console = Console(width=200, record=True, force_terminal=False, color_system=None)
    console.print(table)
    return console.export_text()


def test_fmt_time_minutes_seconds():
    assert stress_menu._fmt_time(0) == "00:00"
    assert stress_menu._fmt_time(5) == "00:05"
    assert stress_menu._fmt_time(65) == "01:05"
    assert stress_menu._fmt_time(599) == "09:59"


def test_fmt_time_hours():
    assert stress_menu._fmt_time(3600) == "01:00:00"
    assert stress_menu._fmt_time(3661) == "01:01:01"
    assert stress_menu._fmt_time(36000) == "10:00:00"


def test_fmt_time_negative_zero():
    """Отрицательное время (часы перематывают в 0) не должно крашить."""
    assert stress_menu._fmt_time(-5) == "00:00"


def test_live_table_no_snap_shows_dashes():
    tbl = stress_menu._build_stress_live_table(
        snap=None,
        gpu_data=None,
        cpu_temp_history=[],
        cpu_load_history=[],
        ram_load_history=[],
        gpu_temp_history=[],
        gpu_load_history=[],
    )
    out = _render(tbl)
    # CPU/RAM строки всегда есть, значения — прочерки при отсутствии snap.
    assert "CPU" in out
    assert "RAM" in out
    # Без snap нет ни загрузки, ни температуры, ни Vcore.
    assert "—" in out


def test_live_table_with_snap_shows_cpu_ram_values():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=98.5,
        ram_percent=42.0,
        temperatures={"cpu/package": 85.4, "gpu/temp": 70.0},
        frequencies={"cpu_avg": 4500.0},
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data=None,
        cpu_temp_history=[85.4],
        cpu_load_history=[98.5],
        ram_load_history=[42.0],
        gpu_temp_history=[],
        gpu_load_history=[],
    )
    out = _render(tbl)
    assert "98.5" in out  # CPU%
    assert "85.4" in out  # T° CPU pkg
    assert "42.0" in out  # RAM%
    # Атавизм частоты ЦП — не показываем.
    assert "4500" not in out
    assert "МГц" not in out
    # Английский лейбл (единообразие с GPU/VRAM).
    assert "CPU" in out
    assert "RAM" in out


def test_live_table_skips_acpi_thermal_zone_for_cpu():
    """Регрессия: ACPI thermal_zone_* не должен показываться как CPU-temp."""
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=80.0,
        ram_percent=50.0,
        temperatures={"thermal_zone_0": 28.0},  # ACPI чипсет/корпус
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data=None,
        cpu_temp_history=[],
        cpu_load_history=[80.0],
        ram_load_history=[50.0],
        gpu_temp_history=[],
        gpu_load_history=[],
    )
    out = _render(tbl)
    assert "28.0" not in out  # ACPI thermal_zone не как CPU-temp
    assert "80.0" in out


def test_live_table_without_gpu_has_constant_height():
    """Регрессия: при ``gpu_available=False`` GPU/VRAM строк нет, и
    их добавление через `gpu_data` НЕ срабатывает. Это гарантирует
    константную высоту renderable, без которой Live в conhost оставляет
    «призраков» предыдущих кадров (визуальный баг: «Стресс-нагрузка»
    дублируется в шапке).
    """
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=80.0, ram_percent=30.0, temperatures={"cpu/package": 70.0},
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data={"load_pct": 50.0, "temp_c": 60.0, "mem_used_gb": 1.0, "mem_total_gb": 8.0},
        cpu_temp_history=[70.0],
        cpu_load_history=[80.0],
        ram_load_history=[30.0],
        gpu_temp_history=[],
        gpu_load_history=[],
        gpu_available=False,
    )
    out = _render(tbl)
    assert "VRAM" not in out
    assert "(фон)" not in out


def test_live_table_with_gpu_data_renders_gpu_row():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=80.0,
        ram_percent=30.0,
        temperatures={"cpu/package": 70.0},
    )
    gpu_data = {
        "name": "GeForce RTX 4070 Ti",
        "temp_c": 72.5,
        "load_pct": 95.0,
        "mem_used_gb": 4.3,
        "mem_total_gb": 12.0,
    }
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data=gpu_data,
        cpu_temp_history=[70.0],
        cpu_load_history=[80.0],
        ram_load_history=[30.0],
        gpu_temp_history=[72.5],
        gpu_load_history=[95.0],
        gpu_available=True,
    )
    out = _render(tbl)
    assert "GPU" in out
    assert "95.0" in out  # GPU load
    assert "72.5" in out  # GPU temp
    # VRAM строка отдельная.
    assert "VRAM" in out
    assert "4.3" in out
    assert "12.0" in out
    # Пометка «фон» (GPU не входит в план CPU+RAM-стресса).
    assert "фон" in out


def test_gpu_poller_unavailable_when_no_nvidia_smi(monkeypatch):
    """Без nvidia-smi поллер не запускает thread и report-поля остаются None."""
    monkeypatch.setattr(stress_menu, "_poll_nvidia_smi", lambda: None)
    poller = stress_menu.GpuPoller()
    poller.start()
    try:
        assert poller.available is False
        assert poller.latest() is None
        peaks = poller.peaks()
        assert peaks["peak_temp_c"] is None
        assert peaks["peak_load_pct"] is None
    finally:
        poller.stop()


def test_gpu_poller_tracks_peaks(monkeypatch):
    """Поллер корректно держит максимумы по T°/нагрузке/VRAM между опросами."""
    sample_seq = iter(
        [
            {
                "name": "GPU-A",
                "temp_c": 60.0,
                "load_pct": 50.0,
                "mem_used_gb": 2.0,
                "mem_total_gb": 12.0,
            },
            # Второй замер — почти все метрики выше.
            {
                "name": "GPU-A",
                "temp_c": 75.0,
                "load_pct": 40.0,
                "mem_used_gb": 3.5,
                "mem_total_gb": 12.0,
            },
        ]
    )

    def fake_poll() -> dict[str, object] | None:
        try:
            return next(sample_seq)
        except StopIteration:
            return None

    monkeypatch.setattr(stress_menu, "_poll_nvidia_smi", fake_poll)
    poller = stress_menu.GpuPoller()
    poller.start()  # сразу первый замер пишется в peaks
    # Эмулируем второй замер вручную — без ожидания фонового цикла.
    second = fake_poll()
    with poller._lock:
        poller._latest = second
        if second["temp_c"] > poller._peak_temp:
            poller._peak_temp = second["temp_c"]
        if second["load_pct"] > poller._peak_load:
            poller._peak_load = second["load_pct"]
        if second["mem_used_gb"] > poller._peak_mem:
            poller._peak_mem = second["mem_used_gb"]
    try:
        assert poller.available is True
        peaks = poller.peaks()
        assert peaks["peak_temp_c"] == 75.0  # выше второго замера
        assert peaks["peak_load_pct"] == 50.0  # первый был выше
        assert peaks["peak_mem_gb"] == 3.5
        assert peaks["mem_total_gb"] == 12.0
        assert peaks["name"] == "GPU-A"
    finally:
        poller.stop()


def test_extract_voltage_picks_max_matching_token():
    """``_extract_voltage`` фильтрует по префиксу + ключевым словам и берёт max."""
    voltages = {
        "cpu/cpu_core": 1.275,
        "cpu/cpu_soc": 1.10,
        "cpu/cpu_vid": 1.30,
        "gpunvidia/gpu_core": 0.95,
        "mainboard/3v3": 3.30,  # мейнборд-рейл — не должно зацепиться
        "memory/dimm_vdd": 1.35,
    }
    # CPU: префикс ``cpu`` (cpu_*); токены core/vcore/vdd/vid. max = 1.30 (VID)
    assert stress_menu._extract_voltage(voltages, "cpu") == pytest.approx(1.30)
    # GPU: префикс ``gpu`` (gpunvidia матчится по startswith).
    assert stress_menu._extract_voltage(voltages, "gpu") == pytest.approx(0.95)
    # Memory: DIMM Vdd.
    assert stress_menu._extract_voltage(voltages, "memory") == pytest.approx(1.35)
    # Мейнборд-рейлы НЕ матчатся (нет core/vcore/vdd/vid в нормализованном имени).
    assert stress_menu._extract_voltage(voltages, "mainboard") is None


def test_extract_voltage_empty_returns_none():
    assert stress_menu._extract_voltage({}, "cpu") is None
    # Нет нужных токенов в имени — фильтруем.
    assert stress_menu._extract_voltage({"cpu/something_else": 1.2}, "cpu") is None


def test_extract_voltage_memory_finds_dram_in_superio_and_motherboard():
    """DRAM-напряжение часто публикуется LHM не в memory/..., а в superio/...
    или motherboard/...; для prefix="memory" должны искать в этих источниках
    тоже, фильтруя по DIMM/DRAM-токенам и диапазону 0.8–2.0 В.
    """
    voltages = {
        # Классический memory/... — должен находиться.
        "memory/dimm_vdd": 1.35,
        # Super I/O публикует DIMM_VDD / DRAM_VOLTAGE — должны ловить.
        "superio/dimm_vdd": 1.20,
        "superio/dram_voltage": 1.30,
        # Motherboard может публиковать VDIMM/VDDQ (DDR5).
        "motherboard/vddq": 1.10,
        # Рейлы материнки — НЕ должны попадать (вне диапазона + не DIMM/DRAM).
        "superio/3vcc": 3.30,
        "motherboard/12v": 12.15,
        "motherboard/5vsb": 5.05,
    }
    # max из всех DRAM-кандидатов = 1.35 (memory/dimm_vdd).
    assert stress_menu._extract_voltage(voltages, "memory") == pytest.approx(1.35)


def test_extract_voltage_memory_only_superio_source():
    """Если memory/... пустой, всё равно ловим DRAM-напряжение из superio/..."""
    voltages = {
        "superio/dimm_vdd": 1.30,
        "superio/3vcc": 3.30,  # не пройдёт по диапазону
        "motherboard/12v": 12.0,  # не пройдёт по диапазону
    }
    assert stress_menu._extract_voltage(voltages, "memory") == pytest.approx(1.30)


def test_extract_voltage_memory_ignores_out_of_range():
    """Значение в правильном источнике с правильным токеном, но вне 0.8–2.0 В,
    отбрасывается (защита от ложных срабатываний)."""
    voltages = {
        "superio/dimm_vdd": 5.0,  # подозрительно высокое, отбрасываем
        "motherboard/vdd_aux": 0.5,  # подозрительно низкое, отбрасываем
    }
    assert stress_menu._extract_voltage(voltages, "memory") is None


def test_live_table_shows_vcore_when_available():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=80.0,
        ram_percent=50.0,
        temperatures={"cpu/package": 65.0},
        voltages={
            "cpu/cpu_core": 1.275,
            "gpunvidia/gpu_core": 0.95,
            "memory/dimm_vdd": 1.35,
        },
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data={
            "name": "RTX 4070",
            "load_pct": 70.0,
            "temp_c": 55.0,
            "mem_used_gb": 4.0,
            "mem_total_gb": 12.0,
        },
        cpu_temp_history=[65.0],
        cpu_load_history=[80.0],
        ram_load_history=[50.0],
        gpu_temp_history=[55.0],
        gpu_load_history=[70.0],
        gpu_available=True,
    )
    out = _render(tbl)
    assert "1.275" in out  # CPU Vcore
    assert "0.950" in out  # GPU Vcore
    assert "1.350" in out  # RAM DIMM Vdd


def test_live_table_no_vcore_column_dashes_when_absent():
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=80.0,
        ram_percent=50.0,
        temperatures={"cpu/package": 65.0},
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data=None,
        cpu_temp_history=[65.0],
        cpu_load_history=[80.0],
        ram_load_history=[50.0],
        gpu_temp_history=[],
        gpu_load_history=[],
    )
    out = _render(tbl)
    # Vcore значений нет → колонка содержит "—" (один или несколько).
    assert "—" in out


def test_summarize_history_collects_voltages():
    """``_summarize_metrics_history`` агрегирует Vcore CPU/GPU/RAM."""
    now = datetime.now(timezone.utc)
    history = [
        MetricSnapshot(
            timestamp=now,
            cpu_percent=70.0,
            ram_percent=40.0,
            voltages={"cpu/cpu_core": 1.20, "gpunvidia/gpu_core": 0.90},
        ),
        MetricSnapshot(
            timestamp=now,
            cpu_percent=80.0,
            ram_percent=42.0,
            voltages={"cpu/cpu_core": 1.30, "gpunvidia/gpu_core": 1.00},
        ),
    ]
    summary = stress_menu._summarize_metrics_history(history)
    assert summary["cpu_vcore_avg"] == pytest.approx(1.25)
    assert summary["cpu_vcore_peak"] == pytest.approx(1.30)
    assert summary["gpu_vcore_avg"] == pytest.approx(0.95)
    assert summary["gpu_vcore_peak"] == pytest.approx(1.00)
    # RAM voltage отсутствует — None.
    assert summary["ram_vcore_avg"] is None
    assert summary["ram_vcore_peak"] is None


def test_summarize_history_empty_voltages():
    """Пустая история отдаёт всё None, включая новые ключи Vcore."""
    summary = stress_menu._summarize_metrics_history([])
    for key in (
        "cpu_vcore_avg",
        "cpu_vcore_peak",
        "gpu_vcore_avg",
        "gpu_vcore_peak",
        "ram_vcore_avg",
        "ram_vcore_peak",
    ):
        assert summary[key] is None


def test_live_table_filters_non_cpu_temps():
    """В таблице live-статуса T° CPU = только из CPU-сенсоров, не GPU/SSD."""
    snap = MetricSnapshot(
        timestamp=datetime.now(timezone.utc),
        cpu_percent=50.0,
        ram_percent=10.0,
        temperatures={
            "gpu/temperature": 95.0,  # не CPU
            "nvme/composite": 60.0,
            "cpu/package": 70.0,  # это
        },
    )
    tbl = stress_menu._build_stress_live_table(
        snap=snap,
        gpu_data=None,
        cpu_temp_history=[70.0],
        cpu_load_history=[50.0],
        ram_load_history=[10.0],
        gpu_temp_history=[],
        gpu_load_history=[],
    )
    out = _render(tbl)
    assert "70.0" in out
    # GPU/NVMe температуры НЕ должны попасть в строку CPU.
    # Они не пройдут через _is_cpu_temp_key.
    assert "95.0" not in out
    assert "60.0" not in out


def test_public_api_importable():
    """Sanity-check: главные точки входа модуля доступны и callable."""
    assert callable(stress_menu.run_timed_stress)
    assert callable(stress_menu.run_infinite_stress)
    assert callable(stress_menu.show_engines_table)


# ─── _detect_cpu_temp_source (issue #20) ─────────────────────────────────────


class _FakeDiagnostics:
    """Минимальный stand-in для SensorDiagnostics — без зависимости от dataclass."""

    def __init__(
        self,
        *,
        has_cpu_temperature: bool,
        cpu_temp_source: str | None = None,
        advice: list[str] | None = None,
    ) -> None:
        self.has_cpu_temperature = has_cpu_temperature
        self.cpu_temp_source = cpu_temp_source
        self.advice = advice or []


def test_detect_cpu_temp_source_returns_source_when_cpu_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При работающем датчике helper возвращает (True, source, []) без советов."""
    fake = _FakeDiagnostics(
        has_cpu_temperature=True,
        cpu_temp_source="LibreHardwareMonitor (DTS ядер CPU)",
        advice=["этот совет не должен мозолить глаза"],
    )
    monkeypatch.setattr(stress_menu, "diagnose_sensors", lambda: fake)

    ok, source, advice = stress_menu._detect_cpu_temp_source(object())

    assert ok is True
    assert source == "LibreHardwareMonitor (DTS ядер CPU)"
    assert advice == []


def test_detect_cpu_temp_source_returns_advice_when_no_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При отсутствии CPU-температуры helper отдаёт actionable-совет."""
    fake = _FakeDiagnostics(
        has_cpu_temperature=False,
        cpu_temp_source=None,
        advice=[
            "Запустите apexcore ОДИН РАЗ от имени администратора.",
            "Проверьте антивирус: WinRing0 может быть в карантине.",
        ],
    )
    monkeypatch.setattr(stress_menu, "diagnose_sensors", lambda: fake)

    ok, message, advice = stress_menu._detect_cpu_temp_source(object())

    assert ok is False
    assert "недоступна" in message  # короткое описание, не просто пусто
    assert advice == [
        "Запустите apexcore ОДИН РАЗ от имени администратора.",
        "Проверьте антивирус: WinRing0 может быть в карантине.",
    ]


def test_detect_cpu_temp_source_swallows_diagnose_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Падение diagnose_sensors не должно ронять старт стресса (graceful)."""
    def boom() -> object:
        raise RuntimeError("диагностика заглохла")

    monkeypatch.setattr(stress_menu, "diagnose_sensors", boom)

    ok, message, advice = stress_menu._detect_cpu_temp_source(object())

    assert ok is False
    assert message  # любое непустое сообщение для UI
    assert advice == []


# ─── Одноразовое отключение всех защит ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_safety_flag() -> None:
    """Сбросить module-level флаг ``_DISABLE_ALL_SAFETY_NEXT_RUN`` между тестами.

    Без этого один тест мог бы оставить флаг в True, и следующий тест,
    проверяющий «по умолчанию выключено», стал бы flaky.
    """
    stress_menu._DISABLE_ALL_SAFETY_NEXT_RUN = False
    yield
    stress_menu._DISABLE_ALL_SAFETY_NEXT_RUN = False


def test_safety_flag_default_enabled():
    """По умолчанию защита включена — флаг отключения False."""
    assert stress_menu.is_safety_disabled_next_run() is False


def test_toggle_inverts_state():
    """toggle() переключает в обе стороны."""
    assert stress_menu.toggle_safety_disabled_next_run() is True
    assert stress_menu.is_safety_disabled_next_run() is True
    assert stress_menu.toggle_safety_disabled_next_run() is False
    assert stress_menu.is_safety_disabled_next_run() is False


def test_consume_resets_to_false():
    """consume() сбрасывает флаг — это и есть «одноразовость»."""
    stress_menu.toggle_safety_disabled_next_run()
    assert stress_menu.is_safety_disabled_next_run() is True
    _ = stress_menu.consume_safety_disabled()
    assert stress_menu.is_safety_disabled_next_run() is False


def test_consume_returns_previous_state():
    """consume() возвращает то, что было ДО сброса."""
    # Защита была включена → consume вернёт False.
    assert stress_menu.consume_safety_disabled() is False
    # Защита отключена → consume вернёт True (и сразу сбросит).
    stress_menu.toggle_safety_disabled_next_run()
    assert stress_menu.consume_safety_disabled() is True
    # Повторный consume — уже False.
    assert stress_menu.consume_safety_disabled() is False
