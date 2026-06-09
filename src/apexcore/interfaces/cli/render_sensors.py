"""Рендеринг раздела «Датчики» (M5).

Производит rich-renderable, который команда ``apexcore sensors`` оборачивает
в ``rich.live.Live``. Сам по себе не печатает в консоль — это позволяет
использовать его с ``Live``, ``Layout``, в тестах, и в будущем — в
вебе через ``rich.console.Console.capture()``.

Адаптивность:

- ``width >= 110`` → dashboard карточками (CPU/GPU/Memory+MB/Storage).
- ``width < 110`` → одна длинная таблица «Сенсор / Тек / Мин / Макс / Среднее / Тренд».

Focus modes (см. ``keyboard.py:KeyAction``):

- ``OVERVIEW`` — дефолт (адаптивный layout).
- ``FOCUS_CPU`` / ``FOCUS_GPU`` / ``FOCUS_SYSTEM`` — одна группа во весь
  экран с расширенными per-sensor sparkline'ами.

Toggles:

- ``collapse_cores=True`` — per-core в CPU-карточке сворачиваются в одну
  строку «Ядер 16: 30-44°C avg 36°C».
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from apexcore.domain.sensor_models import (
    SensorGroup,
    SensorKind,
    SensorReading,
    SensorSnapshot,
)
from apexcore.infrastructure.disk_inventory import PhysicalDisk
from apexcore.interfaces.cli.sparkline import sparkline

# Ширина терминала для переключения dashboard ↔ table.
ADAPTIVE_WIDTH_THRESHOLD = 110

# Минимум строк терминала для dashboard mode. Если меньше — narrow table
# (одна вертикальная таблица). Windows Terminal на 1080p обычно даёт ~50 строк,
# но в маленьком окне или с увеличенным шрифтом может быть и 20–25. В таких
# случаях dashboard физически не помещается и карточки обрезает.
ADAPTIVE_HEIGHT_THRESHOLD = 30

# Sparkline-окно (отсчётов). При rate=0.5с это ~6 секунд.
SPARKLINE_WINDOW = 12


# ─── ViewMode ──────────────────────────────────────────────────────────────


class ViewMode:
    """Что показывать на экране. Управляется hotkeys в ``commands/sensors.py``."""

    OVERVIEW = "overview"
    FOCUS_CPU = "focus_cpu"
    FOCUS_GPU = "focus_gpu"
    FOCUS_SYSTEM = "focus_system"  # Memory + Motherboard объединённо
    FOCUS_FANS = "focus_fans"  # Вентиляторы CPU/шасси/помпа/GPU


# ─── Точка входа ───────────────────────────────────────────────────────────


def render_sensors_view(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
    *,
    console_width: int,
    console_height: int | None = None,
    view_mode: str = ViewMode.OVERVIEW,
    collapse_cores: bool = False,
    paused: bool = False,
    active_backends: list[str] | None = None,
    physical_disks: list[PhysicalDisk] | None = None,
) -> RenderableType:
    """Сконструировать renderable экрана «Датчики».

    :param latest: последний snapshot — основной источник значений «сейчас».
    :param history: предыдущие N snapshot'ов (для sparkline и min/max в сводке).
    :param console_width: ``Console.size.width`` — определяет dashboard/table.
    :param console_height: ``Console.size.height`` — нужна dashboard'у чтобы
        правильно распределить вертикальное место между правыми карточками
        (GPU/Вентиляторы/Диски). Если не передана — dashboard всё равно
        рисует, но Storage может «висеть» ниже фактической высоты содержимого.
    :param view_mode: один из ``ViewMode.*``.
    :param collapse_cores: свернуть per-core блок в одну строку.
    :param paused: показать индикатор «⏸ Пауза» в шапке.
    :param active_backends: список вида ``["LHM ✓", "pynvml ✓", "smartctl ✗"]``
        для строки диагностики в шапке.
    :param physical_disks: полный список физических дисков системы
        (через ``infrastructure.disk_inventory.list_physical_disks``).
        Раздел «Диски» отображает все диски, даже если для них нет
        температурных показаний.
    """
    physical_disks = physical_disks or []
    header = _render_header(latest, paused=paused, active_backends=active_backends or [])
    footer = _render_footer(view_mode, collapse_cores=collapse_cores)

    # Если высота терминала меньше порога — dashboard физически не вмещается
    # (карточки обрезает и пользователь не видит низ). В таком случае
    # переключаемся на узкую таблицу.
    height_ok = console_height is None or console_height >= ADAPTIVE_HEIGHT_THRESHOLD
    is_dashboard = (
        view_mode == ViewMode.OVERVIEW
        and console_width >= ADAPTIVE_WIDTH_THRESHOLD
        and height_ok
    )

    if view_mode == ViewMode.FOCUS_CPU:
        body = _render_focus(latest, history, group=SensorGroup.CPU, collapse_cores=collapse_cores)
    elif view_mode == ViewMode.FOCUS_GPU:
        body = _render_focus(latest, history, group=SensorGroup.GPU, collapse_cores=False)
    elif view_mode == ViewMode.FOCUS_SYSTEM:
        body = _render_focus_system(latest, history)
    elif view_mode == ViewMode.FOCUS_FANS:
        body = _render_focus(latest, history, group=SensorGroup.FANS, collapse_cores=False)
    elif is_dashboard:
        body = _render_dashboard(
            latest, history,
            collapse_cores=collapse_cores,
            physical_disks=physical_disks,
        )
    else:
        body = _render_narrow_table(latest, history)

    # Внешний Layout с фиксированной высотой header/footer — гарантирует
    # что подсказка hotkeys всегда видна. В dashboard mode body был
    # rich.Layout, который при использовании внутри Group съедал всю
    # доступную высоту и отодвигал footer за низ терминала.
    if is_dashboard and console_height is not None:
        outer = Layout()
        outer.split_column(
            Layout(header, name="header", size=_measure_renderable_height(header)),
            Layout(body, name="body"),
            Layout(footer, name="footer", size=_measure_renderable_height(footer)),
        )
        return outer
    return Group(header, body, footer)


def _measure_renderable_height(renderable: RenderableType) -> int:
    """Высота renderable. Для Panel — content lines + 2 (рамка)."""
    if isinstance(renderable, Panel):
        return _measure_panel_height(renderable) - 1  # без title-pad
    return _count_renderable_lines(renderable)


# ─── Header / Footer ────────────────────────────────────────────────────────


def _render_header(
    latest: SensorSnapshot,
    *,
    paused: bool,
    active_backends: list[str],
) -> RenderableType:
    # active_backends остался в сигнатуре для обратной совместимости
    # с командой `apexcore sensors`, но в шапке больше не показывается
    # (служебная инфа, не нужная в UI — см. ниже).
    _ = active_backends
    """Шапка раздела «Датчики»: заголовок + время + индикаторы паузы/throttle.

    Раньше под заголовком была строка статусов backends (``LHM ✓ pynvml ✓
    smartctl ✓ throttle ✓``) — для пользователя это служебная инфа,
    нужная скорее в ``apexcore doctor``. В шапке оставлены только время,
    индикатор паузы и предупреждение о throttle (если активен).
    """
    ts = latest.timestamp.astimezone().strftime("%H:%M:%S")
    pause_marker = "  [bold yellow]⏸ ПАУЗА[/]" if paused else ""
    throttle = latest.throttle
    if throttle.active:
        throttle_chunk = f"  [bold red]⚠ throttle: {throttle.cause.value}[/]"
        if throttle.detail:
            throttle_chunk += f" [dim]({throttle.detail})[/]"
    else:
        throttle_chunk = ""
    title = f"[bold cyan]Датчики[/]  ·  {ts}{pause_marker}{throttle_chunk}"
    return Panel(Text.from_markup(title), border_style="cyan", padding=(0, 1))


def _render_footer(view_mode: str, *, collapse_cores: bool) -> RenderableType:
    """Подсказка с hotkeys в подвале экрана.

    Отображаются только EN-обозначения клавиш — RU-раскладка работает
    автоматически (см. ``keyboard.py:classify_key``), и дублирование
    `Esc/Q/Й` визуально перегружало строку.
    """
    cores_state = "развернуть" if collapse_cores else "свернуть"
    # B — «шаг назад»: из focus возврат в OVERVIEW, из OVERVIEW — выход в
    # меню. Единая семантика, единая подпись — лишний контекст
    # перегружает футер.
    back_label = "[bold]B[/] — шаг назад"
    # Подсказки в формате «KEY — действие» (тире отделяет клавишу от
    # пояснения, читабельнее чем сплошной поток слов).
    # A/0 (OVERVIEW) убрали — B из focus и так возвращает в общий вид,
    # отдельный шорткат был избыточен.
    keys = [
        "[bold]Esc/Q[/] — выход",
        back_label,
        "[bold]Ctrl+C[/] — стоп",
        f"[bold]E[/] — {cores_state} ядра",
        "[bold]C[/] — CPU фокус",
        "[bold]G[/] — GPU фокус",
        "[bold]M[/] — материнка фокус",
        "[bold]F[/] — вентиляторы фокус",
        "[bold]P[/] — пауза",
    ]
    mode_chunk = ""
    if view_mode != ViewMode.OVERVIEW:
        mode_chunk = f"  [yellow]режим: {view_mode}[/]"
    return Panel(
        Text.from_markup("  ".join(keys) + mode_chunk),
        border_style="dim",
        padding=(0, 1),
    )


# ─── Dashboard (≥110 cols) ──────────────────────────────────────────────────


def _render_dashboard(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
    *,
    collapse_cores: bool,
    physical_disks: list[PhysicalDisk],
) -> RenderableType:
    """Карточная сетка: три колонки разной ширины через ``rich.Layout``.

    - Колонка 1 (ratio=4): CPU (узкая — данные «Ядро/VID + значение + спарклайн»).
    - Колонка 2 (ratio=5): GPU (size=N), Вентиляторы (size=N), Диски (size=N).
      Все три карточки прижаты к верху, под ними пустое место.
    - Колонка 3 (ratio=4): Материнская плата (вся высота колонки).

    По фидбэку: Storage перенесён из 3-й колонки во 2-ю под Вентиляторы — на
    1080p терминала так компактнее (хвост MB-сенсоров и хвост Storage больше
    не сжимали footer с hotkeys). Все три карточки в среднем столбце
    имеют ``size=`` по фактической высоте — нет пустых «провалов» между
    GPU/Fans/Storage.
    """
    cpu_panel = _build_group_panel(
        latest, history, group=SensorGroup.CPU, collapse_cores=collapse_cores
    )
    gpu_panel = _build_group_panel(
        latest, history, group=SensorGroup.GPU, collapse_cores=False
    )
    mb_panel = _build_system_panel(latest, history)
    storage_panel = _build_storage_panel(latest, history, physical_disks=physical_disks)
    fans_panel = _build_fans_panel(latest, history)

    # Ratio 4:5:4 — средняя колонка чуть шире, потому что GPU-labels
    # «GPU температура (NVML)» (22 ch) и Fans «Графический процессор 1»
    # (21 ch) длиннее всех остальных. На 4:4:4 они переносятся на
    # вторую строку, что портит компактность.
    layout = Layout()
    layout.split_row(
        Layout(name="col_cpu", ratio=4),
        Layout(name="col_gpu_fans_storage", ratio=5),
        Layout(name="col_mb", ratio=4),
    )
    # Все карточки оборачиваем в Align(vertical="top") — это прижимает
    # Panel к верху колонки и оставляет «прозрачную» пустоту под ней.
    # Без Align rich.Layout растягивает единственный renderable в cell
    # на всю высоту — Panel рисует длинный пустой «ящик» (визуальный
    # мусор, жаловался пользователь). Прежний подход через split_column
    # с filler-Layout показывал debug-placeholder 'cpu_spare (W x H)' —
    # Align чище и компактнее.
    #
    # Автоматический resize: rich.Align не накладывает ограничение на
    # высоту, поэтому при росте числа readings Panel сам становится
    # выше. Если на маленьком терминале CPU c 30+ ядер не помещается —
    # пользователь жмёт `E` (collapse cores).
    layout["col_cpu"].update(Align(cpu_panel, vertical="top"))
    layout["col_gpu_fans_storage"].update(
        Align(Group(gpu_panel, fans_panel, storage_panel), vertical="top")
    )
    layout["col_mb"].update(Align(mb_panel, vertical="top"))
    return layout


def _measure_panel_height(panel: Panel) -> int:
    """Грубая оценка высоты Panel в строках для ``Layout(size=...)``.

    Panel = 2 строки рамки + 1 строка title + строки содержимого.
    Содержимое — Table.grid (число rows) или Group из Text + Table.
    Не используем ``Console.measure`` потому что Layout/Live ещё не знают
    финальной ширины в момент построения; нам важна именно высота, а она
    в Table.grid линейна по числу строк.
    """
    body = panel.renderable
    inner_lines = _count_renderable_lines(body)
    return inner_lines + 3  # рамка сверху + title + рамка снизу


def _count_renderable_lines(renderable: RenderableType) -> int:
    """Сколько строк займёт renderable (без учёта ширины)."""
    if isinstance(renderable, Table):
        # Table.grid: число rows = len(renderable.rows).
        # Заголовка нет (show_header=False по умолчанию у grid).
        return len(renderable.rows)
    if isinstance(renderable, Text):
        # Многострочный Text — считаем переносы.
        return max(1, renderable.plain.count("\n") + 1)
    if isinstance(renderable, Group):
        return sum(_count_renderable_lines(r) for r in renderable.renderables)
    # Неизвестный renderable — консервативно 1 строка.
    return 1


def _build_fans_panel(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
) -> Panel:
    """Карточка «Вентиляторы»: RPM по всем датчикам (CPU/Шасси/Помпа/GPU).

    Если LHM отдаёт только GPU-вентиляторы (типичная ситуация на некоторых
    Z690/Z790 с Nuvoton/ITE SuperIO-чипами — CPU/Chassis fans не публикуются
    в LHM, хотя AIDA64/HWiNFO их видит через прямой EC-доступ), добавляем
    в карточку строку-дисклеймер чтобы у пользователя не было вопроса
    «почему всего 2 строки».
    """
    readings = latest.by_group(SensorGroup.FANS)
    if not readings:
        return Panel(
            Text.from_markup("[dim]нет данных от LHM (без админа или нет fan-датчиков)[/]"),
            title="Вентиляторы",
            border_style="dim",
            padding=(0, 1),
        )
    tbl = _build_readings_table(readings, history, collapse_cores=False)
    if _only_gpu_fans(readings):
        # Нейтральный однострочный текст без упоминания backend'а (LHM):
        # для пользователя важно знать что данные недоступны, а не
        # какой именно компонент их не отдаёт.
        body: RenderableType = Group(
            tbl,
            Text.from_markup("[dim]Доступ к другим датчикам отсутствует.[/]"),
        )
    else:
        body = tbl
    return Panel(body, title="Вентиляторы", border_style="cyan", padding=(0, 1))


def _only_gpu_fans(readings: list[SensorReading]) -> bool:
    """``True`` если все fan-readings — GPU (LHM не отдал CPU/Chassis/Pump)."""
    if not readings:
        return False
    for r in readings:
        label_low = r.label.lower()
        sensor_low = r.sensor.lower()
        if "gpu" in label_low or "графич" in label_low or "gpu" in sensor_low:
            continue
        return False
    return True


def _build_storage_panel(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
    *,
    physical_disks: list[PhysicalDisk],
) -> Panel:
    """Карточка «Диски» с подсекцией на каждый физический диск.

    Стратегия:

    1. Берём список физических дисков из ``physical_disks`` (через WMI/lsblk).
       Это единый источник истины «какие диски есть».
    2. Читаем readings из SensorSnapshot (LHM + smartctl).
    3. Для каждого диска ищем его readings по совпадению модели (fuzzy
       lowercase substring match) или по smartctl-индексу.
    4. Если у диска нет readings — рисуем строку «нет данных» с пояснением.

    Если physical_disks пуст (на Linux без lsblk, или при ошибке) — fallback
    на старое поведение (плоская таблица из readings).
    """
    storage_readings = latest.by_group(SensorGroup.STORAGE)

    # Если физический список не дан и нет показаний — пусто.
    if not physical_disks and not storage_readings:
        return _empty_group_panel(SensorGroup.STORAGE)

    # Без списка дисков — fallback на старое поведение.
    if not physical_disks:
        return _build_group_panel(
            latest, history,
            group=SensorGroup.STORAGE,
            collapse_cores=False,
        )

    # Компактная таблица: одна строка на диск (модель/буква + primary T°).
    # Без sparkline и без типа SSD/HDD — для дисков температура меняется
    # медленно (по сравнению с CPU/GPU), и история мало даёт. Полные
    # данные (включая sparkline и тип) видны в focus mode (B → focus).
    tbl = Table.grid(padding=(0, 1))
    tbl.add_column("Диск", style="bold")
    tbl.add_column("Тек", justify="right")

    # Сортируем диски по первой букве (как Проводник Windows: C, D, E, F,
    # потом G и т.д.). Диски без letters попадают в конец списка через
    # сортировочный ключ ``"~"`` (после всех ASCII-букв). На Linux,
    # где letters обычно пуст, порядок будет по mount-point (если есть)
    # или по index.
    def _sort_key(d: PhysicalDisk) -> tuple[str, int]:
        if d.letters:
            return (d.letters[0].upper(), d.index)
        return ("~", d.index)
    sorted_disks = sorted(physical_disks, key=_sort_key)

    matched_readings: set[int] = set()  # id() readings, уже привязанных к диску
    for disk in sorted_disks:
        disk_readings = _readings_for_disk(disk, storage_readings)
        for r in disk_readings:
            matched_readings.add(id(r))
        primary = _pick_primary_storage_reading(disk_readings)
        if primary:
            r = primary[0]
            value_str = _format_value(r)
            tbl.add_row(
                disk.display_title_compact,
                _color_value(r, value_str),
            )
        else:
            tbl.add_row(
                disk.display_title_compact,
                "[dim]—[/]",
            )

    body: RenderableType = tbl

    # Readings без match (например, LHM знает диск, которого нет в WMI) —
    # покажем их под отдельной «прочее» подсекцией.
    orphan = [r for r in storage_readings if id(r) not in matched_readings]
    if orphan:
        body = Group(
            tbl,
            Text.from_markup("[bold dim]Прочие источники T° дисков[/]"),
            _build_readings_table(orphan, history, collapse_cores=False),
        )

    return Panel(
        body,
        title="Диски",
        border_style=_group_color(SensorGroup.STORAGE),
        padding=(0, 1),
    )


def _pick_primary_storage_reading(
    readings: list[SensorReading],
) -> list[SensorReading]:
    """Один primary датчик температуры на диск (для dashboard mode).

    Логика как в AIDA64/HWiNFO: если LHM публикует «Composite» (NVMe Health
    усреднённое значение) — берём его, иначе первый Temperature-сенсор.
    Остальные датчики (Температура 2/3/... — отдельные NAND-чипы и
    контроллер) видны в narrow mode и в focus mode.
    """
    if not readings:
        return []
    composite = next(
        (r for r in readings if "composite" in r.sensor.lower()),
        None,
    )
    if composite is not None:
        return [composite]
    temps = [r for r in readings if r.kind is SensorKind.TEMPERATURE]
    return [temps[0]] if temps else []


def _readings_for_disk(
    disk: PhysicalDisk,
    storage_readings: list[SensorReading],
) -> list[SensorReading]:
    """Найти все readings, относящиеся к этому физическому диску.

    Матчинг по моделям: smartctl формирует device типа ``"Kingston SSD ... · SSD M.2 NVMe"``,
    LHM — ``"KINGSTON SKC3000D2048G"``. Сравниваем lowercase + первые «значащие»
    токены (число + первое слово модели).
    """
    disk_model_norm = _normalize_model(disk.model)
    out: list[SensorReading] = []
    for r in storage_readings:
        reading_model_norm = _normalize_model(r.device)
        if not reading_model_norm or not disk_model_norm:
            continue
        # Совпадение — одно содержит другое (например smartctl device-имя
        # «KINGSTON SKC3000D2048G · SSD NVMe» содержит «KINGSTON SKC3000D2048G»).
        if (
            disk_model_norm in reading_model_norm
            or reading_model_norm in disk_model_norm
        ):
            out.append(r)
    return out


def _normalize_model(name: str) -> str:
    """Привести имя модели к compact-форме для матчинга.

    «KINGSTON SKC3000D2048G» → «kingstonskc3000d2048g»
    «Kingston SSD 980 PRO 1TB» → «kingstonssd980pro1tb»
    """
    return "".join(c for c in name.lower() if c.isalnum())


def _build_group_panel(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
    *,
    group: SensorGroup,
    collapse_cores: bool,
) -> Panel:
    readings = latest.by_group(group)
    if not readings:
        return _empty_group_panel(group)
    # Для CPU поднимаем «Частота» и «Мощность» в начало карточки —
    # это самое полезное на первый взгляд. Остальное (per-core temps,
    # Vcore, VID) идёт следом в исходном порядке.
    if group is SensorGroup.CPU:
        readings = _reorder_cpu_readings(readings)
    device_name = readings[0].device
    title = f"{_group_label(group)} · {device_name}"
    tbl = _build_readings_table(readings, history, collapse_cores=collapse_cores)
    return Panel(tbl, title=title, border_style=_group_color(group), padding=(0, 1))


def _reorder_cpu_readings(readings: list[SensorReading]) -> list[SensorReading]:
    """Поднять «Частоту» и «Мощность» в начало списка CPU-readings.

    Порядок: FREQUENCY → POWER → всё остальное (TEMPERATURE/VOLTAGE
    в исходной последовательности — Макс. ядро, Среднее по ядрам,
    Ядро P/E, Package, Vcore, VID).
    """
    freq = [r for r in readings if r.kind is SensorKind.FREQUENCY]
    power = [r for r in readings if r.kind is SensorKind.POWER]
    rest = [
        r for r in readings
        if r.kind is not SensorKind.FREQUENCY and r.kind is not SensorKind.POWER
    ]
    return freq + power + rest


def _build_system_panel(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
) -> Panel:
    """Объединённая карточка Memory + Motherboard (одна строка панелей не лезет).

    Заголовок «Материнская плата» — данные практически полностью описывают MB
    (сокет, чипсет, VRM, напряжения по линиям, CMOS-батарея), плюс пара
    DIMM-температур и DIMM-напряжение из ``SensorGroup.MEMORY``.
    """
    mem = latest.by_group(SensorGroup.MEMORY)
    mb = latest.by_group(SensorGroup.MOTHERBOARD)
    if not mem and not mb:
        return _empty_group_panel(SensorGroup.MOTHERBOARD, title="Материнская плата")
    combined = mem + mb
    tbl = _build_readings_table(combined, history, collapse_cores=False)
    # ``bold magenta`` вместо просто ``magenta`` — на стандартной палитре
    # Windows PowerShell обычный magenta рендерится как тёмный пурпур и
    # сливается с тёмно-синим фоном; bold даёт яркую розовую рамку,
    # сравнимую по контрасту с cyan/green/yellow у соседних карточек.
    return Panel(
        tbl,
        title="Материнская плата",
        border_style="bold magenta",
        padding=(0, 1),
    )


def _empty_group_panel(group: SensorGroup, *, title: str | None = None) -> Panel:
    label = title or _group_label(group)
    return Panel(
        Text.from_markup("[dim]нет данных[/]"),
        title=label,
        border_style="dim",
        padding=(0, 1),
    )


# ─── Таблица показаний (используется в карточках и в narrow-mode) ───────────


def _build_readings_table(
    readings: list[SensorReading],
    history: list[SensorSnapshot],
    *,
    collapse_cores: bool,
    show_min_max_avg: bool = False,
) -> Table:
    """Универсальная rich-таблица для списка `SensorReading`.

    В dashboard-режиме (карточки) `show_min_max_avg=False` — экономим место.
    В narrow-table режиме (узкие терминалы) `show_min_max_avg=True`.
    """
    if collapse_cores:
        readings = _collapse_per_core(readings)

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column("Сенсор", style="bold")
    tbl.add_column("Тек", justify="right")
    if show_min_max_avg:
        tbl.add_column("Мин", justify="right")
        tbl.add_column("Макс", justify="right")
        tbl.add_column("Сред", justify="right")
    tbl.add_column("Тренд", justify="left")

    for r in readings:
        history_values = _history_for_sensor(history, r)
        spark = sparkline(history_values, width=SPARKLINE_WINDOW)
        value_str = _format_value(r)
        row = [r.label, _color_value(r, value_str)]
        if show_min_max_avg and history_values:
            vmin = min(history_values)
            vmax = max(history_values)
            vavg = sum(history_values) / len(history_values)
            row.extend(
                [
                    f"{vmin:.1f}",
                    f"{vmax:.1f}",
                    f"{vavg:.1f}",
                ]
            )
        elif show_min_max_avg:
            row.extend(["—", "—", "—"])
        row.append(f"[cyan]{spark}[/]")
        tbl.add_row(*row)
    return tbl


def _collapse_per_core(readings: list[SensorReading]) -> list[SensorReading]:
    """Свернуть `cpu/p_core_*` и `cpu/e_core_*` в одну сводную строку каждого вида.

    Используется когда пользователь нажал [C/С]. Все остальные показания
    (cpu_package, VRM, voltages) сохраняются как есть.
    """
    collapsed: list[SensorReading] = []
    p_cores: list[SensorReading] = []
    e_cores: list[SensorReading] = []
    for r in readings:
        if r.kind is SensorKind.TEMPERATURE and r.sensor.startswith("p_core_"):
            p_cores.append(r)
        elif r.kind is SensorKind.TEMPERATURE and r.sensor.startswith("e_core_"):
            e_cores.append(r)
        else:
            collapsed.append(r)

    for label_prefix, group in (("P-cores", p_cores), ("E-cores", e_cores)):
        if not group:
            continue
        values = [r.value for r in group]
        sample = group[0]
        summary = SensorReading(
            group=sample.group,
            device=sample.device,
            sensor=f"_collapsed_{label_prefix}",
            label=f"{label_prefix} ({len(group)})",
            kind=SensorKind.TEMPERATURE,
            value=max(values),  # худшее значение
            unit=sample.unit,
            source=sample.source,
        )
        collapsed.append(summary)
    return collapsed


# ─── Focus modes ───────────────────────────────────────────────────────────


def _render_focus(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
    *,
    group: SensorGroup,
    collapse_cores: bool,
) -> RenderableType:
    readings = latest.by_group(group)
    if not readings:
        return Panel(
            Text.from_markup(f"[dim]Нет данных для {_group_label(group)}.[/]"),
            title=_group_label(group),
            border_style="dim",
        )
    device_name = readings[0].device
    tbl = _build_readings_table(
        readings, history, collapse_cores=collapse_cores, show_min_max_avg=True
    )
    return Panel(
        tbl,
        title=f"{_group_label(group)} · {device_name}",
        border_style=_group_color(group),
        padding=(0, 1),
    )


def _render_focus_system(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
) -> RenderableType:
    """Объединённый focus для Memory + Motherboard + Storage."""
    combined = (
        latest.by_group(SensorGroup.MEMORY)
        + latest.by_group(SensorGroup.MOTHERBOARD)
        + latest.by_group(SensorGroup.STORAGE)
    )
    if not combined:
        return Panel(
            Text.from_markup("[dim]Нет данных по системным сенсорам.[/]"),
            title="Материнская плата",
        )
    tbl = _build_readings_table(
        combined, history, collapse_cores=False, show_min_max_avg=True
    )
    return Panel(tbl, title="Материнская плата", border_style="magenta", padding=(0, 1))


# ─── Narrow mode (<110 cols) ──────────────────────────────────────────────


def _render_narrow_table(
    latest: SensorSnapshot,
    history: list[SensorSnapshot],
) -> RenderableType:
    """Одна большая таблица — все группы по очереди (CPU → GPU → MB → MEM → Storage)."""
    if not latest.readings:
        return Panel(
            Text.from_markup("[dim]Нет показаний датчиков.[/]"),
            title="Датчики",
        )
    tbl = Table(title="Все сенсоры", show_header=True, show_lines=False)
    tbl.add_column("Группа", style="dim")
    tbl.add_column("Устройство", style="dim")
    tbl.add_column("Сенсор", style="bold")
    tbl.add_column("Тек", justify="right")
    tbl.add_column("Мин", justify="right")
    tbl.add_column("Макс", justify="right")
    tbl.add_column("Сред", justify="right")
    tbl.add_column("Тренд", justify="left")
    for group in (
        SensorGroup.CPU,
        SensorGroup.GPU,
        SensorGroup.MOTHERBOARD,
        SensorGroup.MEMORY,
        SensorGroup.STORAGE,
        SensorGroup.FANS,
    ):
        for r in latest.by_group(group):
            history_values = _history_for_sensor(history, r)
            spark = sparkline(history_values, width=8) if history_values else "·" * 8
            value_str = _format_value(r)
            if history_values:
                vmin = f"{min(history_values):.1f}"
                vmax = f"{max(history_values):.1f}"
                vavg = f"{sum(history_values) / len(history_values):.1f}"
            else:
                vmin = vmax = vavg = "—"
            tbl.add_row(
                _group_label(r.group),
                r.device,
                r.label,
                _color_value(r, value_str),
                vmin, vmax, vavg,
                f"[cyan]{spark}[/]",
            )
    return tbl


# ─── helpers ──────────────────────────────────────────────────────────────


def _history_for_sensor(history: list[SensorSnapshot], r: SensorReading) -> list[float]:
    """Вытащить серию значений конкретного сенсора из истории."""
    out: list[float] = []
    for s in history:
        # Один O(N) проход по readings — приемлемо при 30-50 сенсоров × 12 окне.
        for rd in s.readings:
            if rd.sensor == r.sensor and rd.device == r.device and rd.kind is r.kind:
                out.append(rd.value)
                break
    return out


def _format_value(r: SensorReading) -> str:
    """Форматировать значение в зависимости от величины."""
    # Между значением и единицей пробел — кроме градусов (°), они
    # традиционно пишутся слитно (45.0°).
    if r.kind is SensorKind.LOAD:
        return f"{r.value:.0f} %"
    if r.kind is SensorKind.FREQUENCY:
        return f"{r.value:.0f} МГц"
    if r.kind is SensorKind.POWER:
        return f"{r.value:.1f} Вт"
    if r.kind is SensorKind.VOLTAGE:
        return f"{r.value:.3f} В"
    if r.kind is SensorKind.FAN_RPM:
        return f"{r.value:.0f} об"
    # TEMPERATURE — слитно с °, USAGE_BYTES — без единицы.
    return f"{r.value:.1f}°" if r.kind is SensorKind.TEMPERATURE else f"{r.value:.1f}"


def _color_value(r: SensorReading, formatted: str) -> str:
    """Подкрасить значение по threshold_warn/crit. Если порогов нет — нейтральный."""
    if r.kind is not SensorKind.TEMPERATURE:
        return formatted
    if r.threshold_crit is not None and r.value >= r.threshold_crit:
        return f"[bold red]{formatted}[/]"
    if r.threshold_warn is not None and r.value >= r.threshold_warn:
        return f"[yellow]{formatted}[/]"
    if r.threshold_crit is not None and r.value >= r.threshold_crit - 15:
        # Зелёный если рядом с порогом нет — оставляем нейтральным;
        # явно красить «green» не будем, чтобы избежать визуального шума.
        return formatted
    return formatted


def _group_label(group: SensorGroup) -> str:
    return {
        SensorGroup.CPU: "CPU",
        SensorGroup.GPU: "GPU",
        SensorGroup.MEMORY: "Память",
        SensorGroup.MOTHERBOARD: "Материнка",
        SensorGroup.STORAGE: "Диски",
        SensorGroup.FANS: "Вентиляторы",
        SensorGroup.POWER_SUPPLY: "БП",
    }[group]


def _group_color(group: SensorGroup) -> str:
    return {
        SensorGroup.CPU: "blue",
        SensorGroup.GPU: "green",
        SensorGroup.MEMORY: "magenta",
        SensorGroup.MOTHERBOARD: "magenta",
        SensorGroup.STORAGE: "yellow",
        SensorGroup.FANS: "cyan",
        SensorGroup.POWER_SUPPLY: "red",
    }[group]


def format_active_backends(diag) -> list[str]:
    """Сформировать строки вида ``LHM ✓``, ``pynvml ✓``, ``smartctl ✗`` из
    `SensorDiagnostics` для шапки. Принимает duck-типовой объект с
    атрибутом ``backends``.

    Вынесено отдельно чтобы render_sensors не импортировал
    application/diagnostics_sensors напрямую (избегаем циклов).
    """
    if not diag or not hasattr(diag, "backends"):
        return []
    interesting = {"LHM runtime (pythonnet)", "pynvml", "smartctl", "throttle_detector"}
    out: list[str] = []
    for b in diag.backends:
        if b.name not in interesting:
            continue
        short = {
            "LHM runtime (pythonnet)": "LHM",
            "pynvml": "pynvml",
            "smartctl": "smartctl",
            "throttle_detector": "throttle",
        }[b.name]
        mark = "[green]✓[/]" if b.ok else "[red]✗[/]"
        out.append(f"{short} {mark}")
    return out
