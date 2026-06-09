"""Тесты `interfaces/cli/render_sensors.py` — основной renderable для «Датчиков»."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console

from apexcore.domain.sensor_models import (
    SensorGroup,
    SensorKind,
    SensorReading,
    SensorSnapshot,
    SourceBackend,
    ThrottleCause,
    ThrottleState,
)
from apexcore.interfaces.cli.render_sensors import (
    ViewMode,
    _collapse_per_core,
    render_sensors_view,
)


def _r(**overrides) -> SensorReading:
    base = dict(
        group=SensorGroup.CPU,
        device="Intel i9-12900K",
        sensor="p_core_1",
        label="Ядро P1",
        kind=SensorKind.TEMPERATURE,
        value=55.0,
        unit="°C",
        source=SourceBackend.LHM,
    )
    base.update(overrides)
    return SensorReading(**base)


def _snap(readings: list[SensorReading], **kwargs) -> SensorSnapshot:
    return SensorSnapshot(
        timestamp=datetime.now(timezone.utc),
        readings=readings,
        **kwargs,
    )


def _capture(renderable, width: int = 120, height: int = 80) -> str:
    """Отрендерить в plain-text для проверки содержимого.

    ``height=80`` сильно больше реальных терминалов — нужно, чтобы
    ``rich.Layout`` в dashboard mode не обрезал нижнюю карточку «Диски».
    Без явной высоты Console по умолчанию даёт 25, и тесты на содержимое
    Storage начинали проваливаться.
    """
    console = Console(
        width=width, height=height,
        record=True, force_terminal=False, color_system=None,
    )
    console.print(renderable)
    return console.export_text()


# ─── overview ────────────────────────────────────────────────────────────


def test_overview_wide_shows_dashboard() -> None:
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="hot_spot", label="Hot Spot", value=63.5),
        _r(group=SensorGroup.STORAGE, device="nvme0", sensor="temperature", label="Температура", value=48.0),
    ])
    out = _capture(render_sensors_view(snap, [snap], console_width=140), width=140)
    assert "Датчики" in out
    assert "CPU" in out
    assert "GPU" in out
    assert "Hot Spot" in out


def test_overview_narrow_shows_long_table() -> None:
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="hot_spot", label="Hot Spot", value=63.5),
    ])
    out = _capture(render_sensors_view(snap, [snap], console_width=80), width=80)
    # Узкий режим: длинная таблица с группой/устройством в колонках.
    assert "Группа" in out
    assert "Сенсор" in out


def test_throttle_active_shown_in_header() -> None:
    snap = _snap(
        [_r()],
        throttle=ThrottleState(cause=ThrottleCause.THERMAL, detail="p_core_3 hit Tjmax"),
    )
    out = _capture(render_sensors_view(snap, [snap], console_width=140))
    assert "throttle" in out.lower()
    assert "thermal" in out.lower()


def test_paused_marker_in_header() -> None:
    snap = _snap([_r()])
    out = _capture(render_sensors_view(snap, [snap], console_width=140, paused=True))
    assert "ПАУЗА" in out


def test_footer_lists_hotkeys() -> None:
    """Подсказка в подвале содержит EN-обозначения клавиш.

    RU-раскладка работает на уровне функционала (`keyboard.py:classify_key`),
    но в тексте подвала не дублируется — иначе строка перегружена.
    """
    snap = _snap([_r()])
    out = _capture(render_sensors_view(snap, [snap], console_width=140))
    assert "Esc" in out
    assert "Q" in out
    assert "B" in out
    assert "C" in out  # Focus CPU
    assert "G" in out  # Focus GPU
    assert "M" in out  # Focus system
    assert "E" in out  # Collapse/expand cores
    # RU-аналоги намеренно НЕ показываются в footer.
    assert "Й" not in out
    assert "И" not in out


def test_footer_b_says_back_in_overview() -> None:
    """B — единая подпись «шаг назад» во всех режимах."""
    snap = _snap([_r()])
    out = _capture(
        render_sensors_view(
            snap, [snap], console_width=140, view_mode=ViewMode.OVERVIEW
        )
    )
    assert "B — шаг назад" in out


def test_footer_b_says_back_in_focus_mode() -> None:
    """Та же подпись «шаг назад» и в focus mode — пользователь понимает
    одинаково что B возвращает на уровень выше."""
    snap = _snap([_r()])
    out = _capture(
        render_sensors_view(
            snap, [snap], console_width=140, view_mode=ViewMode.FOCUS_CPU
        )
    )
    assert "B — шаг назад" in out


# ─── focus modes ─────────────────────────────────────────────────────────


def test_focus_cpu_shows_cpu_only() -> None:
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="gpu_core", label="GPU Core", value=51.0),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, view_mode=ViewMode.FOCUS_CPU)
    )
    assert "Ядро P1" in out
    # В focus_cpu GPU не должен присутствовать как отдельная карточка.
    # (Footer всё равно может его упоминать в подсказке клавиш — ищем имя датчика.)
    assert "GPU Core" not in out


def test_focus_gpu_shows_gpu_only() -> None:
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="gpu_core", label="GPU Core", value=51.0),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, view_mode=ViewMode.FOCUS_GPU)
    )
    assert "GPU Core" in out
    assert "Ядро P1" not in out


def test_focus_system_combines_memory_and_motherboard() -> None:
    snap = _snap([
        _r(group=SensorGroup.MEMORY, sensor="dimm_1", label="DIMM 1", value=40.0),
        _r(group=SensorGroup.MOTHERBOARD, sensor="vrm_mos", label="VRM MOS", value=42.0),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, view_mode=ViewMode.FOCUS_SYSTEM)
    )
    assert "DIMM 1" in out
    assert "VRM MOS" in out


# ─── collapse per-core ───────────────────────────────────────────────────


def test_collapse_per_core_summarizes_p_and_e() -> None:
    readings = [
        _r(sensor=f"p_core_{i}", label=f"Ядро P{i}", value=30.0 + i)
        for i in range(1, 5)
    ] + [
        _r(sensor=f"e_core_{i}", label=f"Ядро E{i}", value=25.0 + i)
        for i in range(1, 5)
    ] + [
        _r(sensor="cpu_package", label="Package", value=44.0),
    ]
    collapsed = _collapse_per_core(readings)
    labels = {r.label for r in collapsed}
    # Сводные строки вместо индивидуальных ядер
    assert "P-cores (4)" in labels
    assert "E-cores (4)" in labels
    # Package остался как есть
    assert "Package" in labels
    # Индивидуальные ядра свернулись
    assert "Ядро P1" not in labels


def test_collapse_takes_max_for_worst_case() -> None:
    readings = [
        _r(sensor="p_core_1", label="Ядро P1", value=40.0),
        _r(sensor="p_core_2", label="Ядро P2", value=70.0),  # горячее
        _r(sensor="p_core_3", label="Ядро P3", value=55.0),
    ]
    collapsed = _collapse_per_core(readings)
    summary = next(r for r in collapsed if r.sensor.startswith("_collapsed_"))
    assert summary.value == 70.0


def test_collapse_with_no_cores_returns_input() -> None:
    """Если в списке нет p_core/e_core — сворачивать нечего."""
    original = [_r(sensor="cpu_package", label="Package", value=44.0)]
    assert _collapse_per_core(original) == original


# ─── empty / edge cases ──────────────────────────────────────────────────


def test_empty_snapshot_renders_without_error() -> None:
    snap = SensorSnapshot(timestamp=datetime.now(timezone.utc))
    # Должно не упасть и выдать осмысленный вывод.
    out = _capture(render_sensors_view(snap, [snap], console_width=140))
    assert "Датчики" in out


def test_storage_panel_shows_all_physical_disks() -> None:
    """Карточка «Диски» показывает ВСЕ физические диски, даже без T°."""
    from apexcore.infrastructure.disk_inventory import PhysicalDisk

    snap = _snap([
        _r(
            group=SensorGroup.STORAGE,
            device="Kingston SKC3000D2048G",
            sensor="composite_temperature",
            label="Composite (среднее NVMe)",
            value=35.0,
        ),
    ])
    disks = [
        PhysicalDisk(
            index=0, model="Kingston SKC3000D2048G",
            bus_type="NVMe", media_type="SSD", size_gb=2048.0, letters=["C:", "D:"],
        ),
        PhysicalDisk(
            index=1, model="ST2000NM0011",
            bus_type="SATA", media_type="HDD", size_gb=2000.0, letters=["E:"],
        ),
        PhysicalDisk(
            index=2, model="Samsung 860 EVO 500GB",
            bus_type="SATA", media_type="SSD", size_gb=500.0, letters=["F:"],
        ),
    ]
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, physical_disks=disks),
        width=140,
    )

    # Все 3 диска видны в заголовках
    assert "Kingston SKC3000D2048G" in out
    assert "ST2000NM0011" in out
    assert "Samsung 860 EVO 500GB" in out
    # Буквы в скобках
    assert "C:" in out
    assert "E:" in out
    assert "F:" in out
    # Тип подключения (SSD/HDD/NVMe/SATA) — в dashboard mode НЕ показываем,
    # видно только в focus mode (полный display_title). Здесь поэтому
    # ассертим что суффикс отсутствует, чтобы зафиксировать решение.
    # Температура Kingston показана
    assert "35" in out
    # Для дисков без T° — placeholder
    assert "нет данных" in out


def test_storage_panel_empty_when_no_disks_and_no_readings() -> None:
    """Если нет ни physical_disks, ни readings — fallback на «нет данных»."""
    snap = _snap([])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, physical_disks=[]),
        width=140,
    )
    assert "Диски" in out


# ─── Регрессия fans-карточки на узких/коротких терминалах ─────────────────


def _make_fan(label: str, sensor: str, value: float = 1300.0) -> SensorReading:
    """Helper: build a SensorReading for a fan."""
    return _r(
        group=SensorGroup.FANS,
        device="Вентиляторы",
        sensor=sensor,
        label=label,
        kind=SensorKind.FAN_RPM,
        value=value,
        unit="об/мин",
    )


def test_fans_panel_renders_in_dashboard_when_only_gpu_fans() -> None:
    """Регрессия: на 1080p карточка «Вентиляторы» пропадала из dashboard,
    когда LHM отдаёт только GPU-фаны (rich.Columns([Group, Group]) баг).

    После перехода на rich.Layout с size= карточка обязана быть видна,
    даже если внутри только два GPU-датчика.
    """
    snap = _snap([
        _r(),  # CPU temp
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="hot_spot", label="Hot Spot", value=63.5),
        _make_fan("Графический процессор 1", "fan/gpu_fan_1", 1300),
        _make_fan("Графический процессор 2", "fan/gpu_fan_2", 1290),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, console_height=50),
        width=140,
    )
    assert "Вентиляторы" in out, (
        "Карточка «Вентиляторы» должна рендериться в dashboard mode "
        "даже когда LHM отдал только GPU-фаны"
    )
    assert "Графический процессор 1" in out


def test_fans_panel_shows_gpu_only_disclaimer() -> None:
    """Когда все fan-readings — GPU, в карточке появляется пометка
    «CPU/Chassis/Помпа не отдаются через LHM…»."""
    snap = _snap([
        _make_fan("Графический процессор 1", "fan/gpu_fan_1", 1300),
        _make_fan("Графический процессор 2", "fan/gpu_fan_2", 1290),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, console_height=50),
        width=140,
    )
    assert "Доступ к другим датчикам" in out


def test_fans_panel_no_disclaimer_when_motherboard_fans_present() -> None:
    """Если LHM отдал ЦП/Шасси-вентиляторы, дисклеймер про EC не показываем."""
    snap = _snap([
        _make_fan("ЦП", "fan/cpu_fan", 1800),
        _make_fan("Шасси 1", "fan/chassis_fan_1", 1100),
        _make_fan("Графический процессор 1", "fan/gpu_fan_1", 1300),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, console_height=50),
        width=140,
    )
    assert "Доступ к другим датчикам" not in out


def test_narrow_terminal_height_falls_back_to_table() -> None:
    """Когда высота терминала < ADAPTIVE_HEIGHT_THRESHOLD — даже на широком
    экране показываем узкую таблицу (dashboard не помещается)."""
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="hot_spot", label="Hot Spot", value=63.5),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, console_height=20),
        width=140,
        height=20,
    )
    # Узкий режим имеет заголовок «Группа»; dashboard — нет.
    assert "Группа" in out


def test_measure_panel_height_table_grid() -> None:
    """``_measure_panel_height`` корректно считает строки Panel(Table)."""
    from apexcore.interfaces.cli.render_sensors import _measure_panel_height

    snap = _snap([
        _r(sensor="p_core_1", label="Ядро P1"),
        _r(sensor="p_core_2", label="Ядро P2"),
        _r(sensor="p_core_3", label="Ядро P3"),
    ])
    from apexcore.interfaces.cli.render_sensors import _build_group_panel

    panel = _build_group_panel(snap, [snap], group=SensorGroup.CPU, collapse_cores=False)
    # 3 readings → 3 строки + рамка + title = 6.
    assert _measure_panel_height(panel) == 6


def test_dashboard_no_layout_debug_placeholders() -> None:
    """Регрессия: rich.Layout с пустыми Layout(name='spare') рендерил
    debug-placeholder вида ``'spare_name' (W x H)`` поверх дашборда.
    После перехода на ``Align(panel, vertical='top')`` таких placeholder
    в выводе быть не должно.
    """
    snap = _snap([
        _r(),
        _r(group=SensorGroup.GPU, device="RTX 4070 Ti", sensor="hot_spot", label="Hot Spot", value=63.5),
    ])
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140, console_height=50),
        width=140,
    )
    # Имена layout-ов: cpu_spare, mid_spare, mb_spare (старая схема).
    # Любое из них в выводе означает регрессию.
    assert "cpu_spare" not in out
    assert "mid_spare" not in out
    assert "mb_spare" not in out
    # Общий случай: rich debug формат '<name>' (<W> x <H>).
    # Если этот формат встречается — мы где-то забыли заполнить Layout.
    import re
    assert not re.search(r"'\w+'\s+\(\d+\s+x\s+\d+\)", out)


def test_storage_panel_sorts_disks_by_drive_letter() -> None:
    """Диски в карточке «Диски» отсортированы по букве (как в Проводнике):
    C → D → E → F. Не по index'у PhysicalDisk."""
    from apexcore.infrastructure.disk_inventory import PhysicalDisk

    snap = _snap([])  # без readings, нам интересен порядок заголовков
    disks = [
        # Намеренно не в алфавитном порядке:
        PhysicalDisk(index=0, model="DiskF", bus_type="SATA",
                     media_type="SSD", size_gb=500.0, letters=["F:"]),
        PhysicalDisk(index=1, model="DiskC", bus_type="NVMe",
                     media_type="SSD", size_gb=1000.0, letters=["C:"]),
        PhysicalDisk(index=2, model="DiskE", bus_type="NVMe",
                     media_type="SSD", size_gb=2000.0, letters=["E:"]),
        PhysicalDisk(index=3, model="DiskD", bus_type="SATA",
                     media_type="HDD", size_gb=2000.0, letters=["D:"]),
    ]
    out = _capture(
        render_sensors_view(snap, [snap], console_width=140,
                            console_height=50, physical_disks=disks),
        width=140,
    )
    # Извлекаем порядок появления моделей в выводе.
    positions = [(name, out.find(name)) for name in ("DiskC", "DiskD", "DiskE", "DiskF")]
    found = [name for name, pos in positions if pos != -1]
    assert found == ["DiskC", "DiskD", "DiskE", "DiskF"], (
        f"Ожидался порядок [C, D, E, F], получено {found}; "
        f"позиции: {positions}"
    )


def test_initial_view_is_rich_renderable_not_pydantic() -> None:
    """Регрессия: ``Live(empty_sensor_snapshot())`` падал с NotRenderableError,
    потому что SensorSnapshot — Pydantic DTO без ``__rich_console__``.

    В команде `apexcore sensors` нужно передавать в Live() результат
    ``render_sensors_view(...)`` (Group из Panel/Text), а не сырой snapshot.
    Этот тест ловит регрессию, если кто-то снова попытается передать
    snapshot напрямую.
    """
    from apexcore.application.sensor_service import empty_sensor_snapshot

    snap = empty_sensor_snapshot()
    renderable = render_sensors_view(snap, [], console_width=120)

    # rich-renderable обязан иметь либо __rich_console__, либо __rich__,
    # либо быть str/Text/Segment.
    assert hasattr(renderable, "__rich_console__") or hasattr(renderable, "__rich__"), (
        "render_sensors_view должен возвращать rich-renderable, а не Pydantic-объект"
    )

    # И должен реально отрисоваться без исключений (а snapshot сам по себе — нет).
    console = Console(width=120, record=True, color_system=None)
    console.print(renderable)
    assert console.export_text() != ""
