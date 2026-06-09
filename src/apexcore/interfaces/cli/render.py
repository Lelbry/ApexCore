"""Утилиты вывода CLI: общие rich-таблицы и форматирование."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from apexcore.domain.cache import LevelName, OperationName, RamCacheReport
from apexcore.domain.models import (
    BenchmarkResult,
    MetricSnapshot,
    MicroBenchSuiteResult,
    OverallScore,
    SingleMultiResult,
    StressResult,
    SystemInfo,
    ThermalStabilityResult,
)
from apexcore.domain.winsat import WinsatReport, WinsatStatus, WinsatSubscore
from apexcore.interfaces.cli.messages import (
    RAMCACHE_FOOTNOTES,
    WINSAT_BANNER,
    WINSAT_FOOTNOTES,
    WINSAT_METHODOLOGY,
    WINSAT_NA_NOTE,
    WINSAT_STAGE_LABELS,
)
from apexcore.shared.units import humanize_throughput

if TYPE_CHECKING:
    from apexcore.application.cpu_ranking import RankingMatch

console = Console()


def _init_theme_from_env() -> None:
    """Применяем тему при первом импорте render.py.

    Дефолт — dark (текущее поведение, никаких регрессов). ENV-переменная
    APEXCORE_THEME=light переключает на светлую палитру для скриншотов и
    печати. CLI-флаг ``apexcore --theme=light`` перезапишет это в callback
    main.py до запуска подкоманды.

    Импорт лениво — иначе цикл с theme.py, который сам трогает console
    через apply_theme().
    """
    from apexcore.interfaces.cli.theme import apply_theme, detect_default_theme

    apply_theme(detect_default_theme())


_init_theme_from_env()


_ARCH_NAME_MAP: dict[str, str] = {
    # 64-bit Intel/AMD → Microsoft-style 'x64'
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    # 32-bit Intel/AMD → 'x86' (явно показывает 32-bit пользователю Windows)
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    # 64-bit ARM
    "aarch64": "ARM64",
    "arm64": "ARM64",
    # 32-bit ARM
    "armv6l": "ARM",
    "armv7l": "ARM",
}


def _normalize_arch(arch: str | None) -> str:
    """Привести `platform.machine()` к коротким именам Microsoft-стиля.

    На Windows пользователи привыкли к 'x64' / 'x86' / 'ARM64' / 'ARM' —
    эти имена однозначно говорят о разрядности (`x86 == 32-bit`,
    `x64 == 64-bit`). На Linux `x86_64` тоже частая запись, но 'x64' читается
    короче и единообразно. Незнакомые значения (`mips`, `riscv64`, ...)
    отдаём как есть — лучше показать сырое имя, чем угадать.
    """
    if not arch:
        return "—"
    return _ARCH_NAME_MAP.get(arch.lower(), arch)


def _add_frequency_rows(
    tbl: Table,
    info: SystemInfo,
    base_clock_ghz_legacy: float | None,
) -> None:
    """Добавить строки «Частота …» в таблицу с учётом hybrid/non-hybrid.

    Приоритет источников: P/E-поля из SystemInfo → общая база из SystemInfo →
    legacy `base_clock_ghz` параметр. Минимальная разница, начиная с которой
    P и E считаются «реально разными» — 50 МГц (защита от шумов sysfs).
    """
    p_mhz = info.cpu_base_p_mhz
    e_mhz = info.cpu_base_e_mhz
    if p_mhz and e_mhz and abs(p_mhz - e_mhz) >= 50:
        tbl.add_row("Частота P-ядер", f"базовая {p_mhz / 1000:.2f} ГГц")
        tbl.add_row("Частота E-ядер", f"базовая {e_mhz / 1000:.2f} ГГц")
        return
    common_mhz = info.cpu_base_mhz
    if common_mhz and common_mhz > 0:
        tbl.add_row("Базовая частота CPU", f"{common_mhz / 1000:.2f} ГГц")
        return
    if base_clock_ghz_legacy is not None and base_clock_ghz_legacy > 0:
        tbl.add_row("Базовая частота CPU", f"{base_clock_ghz_legacy:.2f} ГГц")


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Подобрать форму русского существительного по числу.

    1, 21, 31, ... → one (поток); 2-4, 22-24, ... → few (потока);
    остальное, включая 11-14, → many (потоков).
    """
    n = abs(n)
    if n % 100 in (11, 12, 13, 14):
        return many
    rem = n % 10
    if rem == 1:
        return one
    if rem in (2, 3, 4):
        return few
    return many


# ─── Надёжная очистка экрана на Windows ─────────────────────────────────────
#
# Rich `console.clear()` отправляет ANSI escape ``\x1b[2J\x1b[H``. На современном
# Windows Terminal это работает корректно. Но в classic conhost.exe (заголовок
# окна «Администратор: Windows PowerShell») этот escape очищает только видимую
# область и не всегда затирает «хвосты» строк — особенно если новый вывод
# содержит ``Panel.fit`` (узкие панели), а предыдущий был широким (например,
# вывод ``pip show``). Это создаёт визуальное «наслоение» меню.
#
# Подменяем ``console.clear`` обёрткой, которая на Windows дополнительно
# вызывает нативный ``cls`` через ``os.system``. Это гарантирует полную
# очистку экрана и скроллбэка во всех вариантах хост-консоли (conhost,
# Windows Terminal, VS Code terminal и т.д.).
def _hard_clear(home: bool = True) -> None:
    """Полная очистка экрана: нативный ``cls`` на Windows + ANSI escape.

    Вызывается каждый раз, когда меню перерисовывает экран
    (``MenuLoop.render``). Без этого хвосты от прошлых выводов остаются
    висеть на строках в classic conhost.
    """
    if sys.platform == "win32":
        # ``os.system('cls')`` — нативная очистка через cmd, гарантированно
        # затирает экран и скроллбэк во всех Windows-консолях.
        os.system("cls")
    else:
        # На Linux/macOS Rich-clear работает корректно, плюс ``clear``
        # как страховка для нестандартных терминалов.
        os.system("clear")


# Подмена выполняется один раз при импорте модуля. Все callsite'ы используют
# тот же ``console`` объект (``from apexcore.interfaces.cli.render import console``),
# поэтому патч действует глобально.
console.clear = _hard_clear  # type: ignore[method-assign]


def render_system_info(
    info: SystemInfo,
    *,
    base_clock_ghz: float | None = None,
    turbo_clock_ghz: float | None = None,
    sensor_driver_active: bool | None = None,
    capability_summary: str | None = None,
) -> None:
    """Отрисовать SystemInfo в виде таблицы.

    Базовая частота берётся в порядке приоритета:

    1. ``info.cpu_base_p_mhz`` + ``info.cpu_base_e_mhz`` — две строки
       «Частота P-ядер» / «Частота E-ядер» (Intel hybrid 12th Gen+).
    2. ``info.cpu_base_mhz`` — одна строка «Базовая частота CPU» (реестр/sysfs,
       работает на всех сборках).
    3. Legacy-параметр ``base_clock_ghz`` — fallback для старых вызывающих,
       которые ещё не используют поля SystemInfo.

    Параметр ``turbo_clock_ghz`` сейчас игнорируется: турбо/живая частота
    показываются в дашборде «Sensors», а не в статической карточке `info`.
    Сохраняем сигнатуру для обратной совместимости.

    ``sensor_driver_active`` — состояние драйвера термосенсоров (`True`/`False`
    → строка с цветным маркером; `None` → строка не выводится).

    ``capability_summary`` — короткая capability-строка из
    ``application.diagnostics_sensors.build_capability_summary``. Если
    передана, добавляется строкой «Capability» в таблицу. См. P1.1 в плане.
    """
    _ = turbo_clock_ghz  # сознательно не используется; см. docstring
    tbl = Table(title="Система", show_header=False)
    tbl.add_column("Поле", style="bold cyan")
    tbl.add_column("Значение")
    tbl.add_row("ОС", f"{info.os_name} {info.os_version}")
    tbl.add_row("Архитектура", _normalize_arch(info.cpu_arch))
    tbl.add_row("Хост", info.hostname or "—")
    tbl.add_row("CPU", info.cpu_model)
    cores = info.cpu_cores
    hybrid_fields = (cores.p_cores, cores.e_cores, cores.p_threads, cores.e_threads)
    if all(v is not None for v in hybrid_fields):
        p_word = _ru_plural(cores.p_threads, "поток", "потока", "потоков")
        e_word = _ru_plural(cores.e_threads, "поток", "потока", "потоков")
        cores_str = (
            f"P {cores.p_cores} / {cores.p_threads} {p_word} + "
            f"E {cores.e_cores} / {cores.e_threads} {e_word} "
            f"(всего {cores.physical} / {cores.logical})"
        )
    else:
        cores_word = _ru_plural(cores.physical, "ядро", "ядра", "ядер")
        threads_word = _ru_plural(cores.logical, "поток", "потока", "потоков")
        cores_str = f"{cores.physical} {cores_word} / {cores.logical} {threads_word}"
    tbl.add_row("Ядра", cores_str)
    _add_frequency_rows(tbl, info, base_clock_ghz)
    tbl.add_row("RAM", f"{info.ram_total_gb:.1f} ГБ")
    if info.gpu_list:
        tbl.add_row("GPU", "\n".join(info.gpu_list))
    else:
        tbl.add_row("GPU", "не обнаружены")
    if sensor_driver_active is True:
        tbl.add_row("Драйвер термосенсоров", "[green]✓ активен[/]")
    elif sensor_driver_active is False:
        tbl.add_row(
            "Драйвер термосенсоров",
            "[red]✗ не активен[/] [dim](см. `apexcore doctor`)[/]",
        )
    if capability_summary:
        tbl.add_row("Capability", capability_summary)
    # Локальное время без часового пояса — RTZ-2 «зима» в выводе ОС-локали
    # выглядит загадочно и не нужно пользователю.
    tbl.add_row("Дата и время", info.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
    console.print(tbl)


# Маппинг префикса LHM-ключа на группу железа. Используется в _group_temps
# для разделения вывода по источнику (CPU/GPU/Материнка/RAM/NVMe). Подробности —
# в docs/research/sensor_dashboard_brief.md § 6.4.
_LHM_PREFIX_TO_GROUP: dict[str, str] = {
    "cpu": "cpu",
    "gpunvidia": "gpu",
    "gpuintel": "gpu",
    "gpuamd": "gpu",
    "motherboard": "mb",
    "memory": "ram",
    "storage": "nvme",
}

_GROUP_LABELS: dict[str, str] = {
    "cpu": "CPU",
    "gpu": "GPU",
    "nvme": "NVMe",
    "mb": "Материнка",
    "ram": "RAM",
}

# Порядок групп в выводе — самые важные первыми.
_GROUP_ORDER: tuple[str, ...] = ("cpu", "gpu", "nvme", "mb", "ram")


def _group_temps(temps: dict[str, float]) -> dict[str, float]:
    """Сгруппировать температуры по hardware-группе, вернув max по каждой.

    Отсеивает outliers (вне 15–130 °C — например, ``motherboard/pcie_x1`` на
    Z690 отдаёт 11 °C, явно битый/зарезервированный сенсор). Ключи без
    распознаваемого префикса (ACPI thermal zone и пр.) игнорируются —
    они не должны попадать в «T° CPU».
    """
    groups: dict[str, float] = {}
    for key, value in temps.items():
        if value < 15.0 or value > 130.0:
            continue
        prefix = key.split("/", 1)[0] if "/" in key else ""
        group = _LHM_PREFIX_TO_GROUP.get(prefix)
        if group is None:
            continue
        if group not in groups or value > groups[group]:
            groups[group] = value
    return groups


def render_metric_snapshot(snap: MetricSnapshot) -> None:
    """Краткая строка с текущими метриками."""
    parts: list[str] = []
    parts.append(f"CPU [bold]{snap.cpu_percent:5.1f}%[/]")
    parts.append(f"RAM [bold]{snap.ram_percent:5.1f}%[/]  ({snap.ram_used_gb:5.1f} ГБ)")
    if snap.frequencies.get("cpu_avg"):
        base = snap.frequencies.get("cpu_base")
        cur = snap.frequencies["cpu_avg"]
        if base and base > 0:
            parts.append(f"freq {cur:.0f}/{base:.0f} МГц")
        else:
            parts.append(f"freq {cur:.0f} МГц")
    if snap.cpu_throttled:
        parts.append("[red]throttle[/]")
    if snap.temperatures:
        groups = _group_temps(snap.temperatures)
        temp_chunks = [
            f"{_GROUP_LABELS[g]} [bold]{groups[g]:.1f}°[/]"
            for g in _GROUP_ORDER
            if g in groups
        ]
        if temp_chunks:
            parts.append(" ".join(temp_chunks))
    else:
        # P0.7: degraded mode — показать конкретную причину вместо пустоты.
        reason_text = _cpu_temp_degraded_inline()
        if reason_text:
            parts.append(reason_text)
    parts.append(
        f"disk R {snap.disk_read_mb:5.1f} / W {snap.disk_write_mb:5.1f} МБ"
    )
    parts.append(snap.timestamp.astimezone().strftime("%H:%M:%S"))
    console.print(" │ ".join(parts))


def _cpu_temp_degraded_inline() -> str | None:
    """Inline-причина «нет CPU temp» для краткой строки.

    Возвращает строку вида ``[dim]CPU temp: нет данных (HVCI блокирует драйвер)[/]``
    или ``None`` если не Windows / причина не классифицируется. Это
    «дешёвая» UX-обёртка над probe-фазой — без перепрогона диагностики.
    """
    import platform

    if platform.system().lower() != "windows":
        return None
    try:
        from apexcore.infrastructure.adapters.windows import (
            get_last_cpu_temp_source,
        )

        source, quality = get_last_cpu_temp_source()
        if source is not None and quality == "silicon":
            return None  # данные есть — не баннер
        if quality == "approximate":
            return "[yellow]CPU temp: ACPI zone (approximate)[/]"
        # Полностью нет данных — пытаемся вытащить причину.
        from apexcore.application.diagnostics_sensors import (
            _classify_lhm_no_cpu_reason,
        )

        reason = _classify_lhm_no_cpu_reason()
        return f"[dim]CPU temp: нет данных ({reason.short()})[/]"
    except Exception:
        return None


def render_metric_table(snapshots: list[MetricSnapshot], max_rows: int = 30) -> None:
    """Полноценная таблица последних N снимков (для финального отчёта monitor)."""
    rows = snapshots[-max_rows:]
    if not rows:
        console.print("[yellow]Метрик не собрано[/]")
        return
    # Какие группы температур присутствуют хотя бы в одном снимке — динамически
    # добавляем столбцы только для них, чтобы таблица не пухла пустыми колонками.
    present_groups: list[str] = []
    grouped_rows: list[dict[str, float]] = [_group_temps(s.temperatures) for s in rows]
    for g in _GROUP_ORDER:
        if any(g in gr for gr in grouped_rows):
            present_groups.append(g)

    tbl = Table(title=f"Метрики ({len(rows)} последних снимков)", show_lines=False)
    tbl.add_column("Время", style="dim", width=8)
    tbl.add_column("CPU%", justify="right")
    tbl.add_column("RAM%", justify="right")
    tbl.add_column("Freq, МГц", justify="right")
    for g in present_groups:
        tbl.add_column(f"{_GROUP_LABELS[g]}°", justify="right")
    tbl.add_column("Disk R/W МБ", justify="right")
    tbl.add_column("Throt.", justify="center")
    for s, groups in zip(rows, grouped_rows, strict=True):
        ts = s.timestamp.astimezone().strftime("%H:%M:%S")
        freq = f"{s.frequencies.get('cpu_avg', 0):.0f}" if s.frequencies else "—"
        temp_cells = [f"{groups[g]:.1f}" if g in groups else "—" for g in present_groups]
        disk = f"{s.disk_read_mb:.1f}/{s.disk_write_mb:.1f}"
        throt = "[red]✓[/]" if s.cpu_throttled else "·"
        tbl.add_row(ts, f"{s.cpu_percent:.1f}", f"{s.ram_percent:.1f}", freq, *temp_cells, disk, throt)
    console.print(tbl)


def render_metric_summary(snapshots: list[MetricSnapshot]) -> None:
    """Сводная статистика по серии метрик."""
    if not snapshots:
        return
    cpu = [s.cpu_percent for s in snapshots]
    ram = [s.ram_percent for s in snapshots]
    freqs = [
        s.frequencies["cpu_avg"]
        for s in snapshots
        if s.frequencies.get("cpu_avg") is not None
    ]
    throt = sum(1 for s in snapshots if s.cpu_throttled)

    # Собираем серии температур по каждой группе отдельно, чтобы строки сводки
    # явно показывали «CPU 44–47°C» отдельно от «GPU 51–64°C», а не один смешанный
    # max по всем сенсорам. См. docs/research/sensor_dashboard_brief.md § 6.4.
    per_group_temps: dict[str, list[float]] = {}
    for s in snapshots:
        for g, value in _group_temps(s.temperatures).items():
            per_group_temps.setdefault(g, []).append(value)

    tbl = Table(title="Сводка", show_header=True)
    tbl.add_column("Метрика", style="bold")
    tbl.add_column("min", justify="right")
    tbl.add_column("avg", justify="right")
    tbl.add_column("max", justify="right")
    tbl.add_row("CPU, %", f"{min(cpu):.1f}", f"{sum(cpu)/len(cpu):.1f}", f"{max(cpu):.1f}")
    tbl.add_row("RAM, %", f"{min(ram):.1f}", f"{sum(ram)/len(ram):.1f}", f"{max(ram):.1f}")
    if freqs:
        tbl.add_row(
            "Freq, МГц",
            f"{min(freqs):.0f}",
            f"{sum(freqs)/len(freqs):.0f}",
            f"{max(freqs):.0f}",
        )
    for g in _GROUP_ORDER:
        values = per_group_temps.get(g)
        if not values:
            continue
        tbl.add_row(
            f"{_GROUP_LABELS[g]}, °C",
            f"{min(values):.1f}",
            f"{sum(values)/len(values):.1f}",
            f"{max(values):.1f}",
        )
    tbl.add_row("Тротлинг (отсчётов)", "", f"{throt}/{len(snapshots)}", "")
    console.print(tbl)


def render_stress_result(res: StressResult) -> None:
    """Карточка результата одного стресс-движка."""
    tbl = Table(title=f"Стресс {res.engine}", show_header=False)
    tbl.add_column("Поле", style="bold cyan")
    tbl.add_column("Значение")
    tbl.add_row("Категория", res.category)
    tbl.add_row("Длительность", f"{res.duration_actual_sec:.2f} с")
    tbl.add_row("Потоков", str(res.threads))
    tbl.add_row("Throughput", humanize_throughput(res.throughput, res.throughput_unit))
    if res.error_count:
        tbl.add_row("Ошибки", f"[red]{res.error_count}[/]")
    if res.extra:
        tbl.add_row("Доп.", ", ".join(f"{k}={v}" for k, v in res.extra.items()))
    console.print(tbl)


def render_bench_result(result: BenchmarkResult) -> None:
    """Финальный отчёт по прогону `bench run`."""
    tbl = Table(title=f"Прогон {result.id}", show_header=True)
    tbl.add_column("Параметр", style="bold")
    tbl.add_column("Значение")
    tbl.add_row("Профиль", result.config.profile_name)
    tbl.add_row("Старт", result.start_time.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
    duration = (result.end_time - result.start_time).total_seconds()
    tbl.add_row("Длительность", f"{duration:.1f} с")
    tbl.add_row("Снимков метрик", str(len(result.metrics_history)))
    tbl.add_row("Стресс-фаз", str(len(result.stress_results)))
    tbl.add_row("Итоговый балл", f"[bold green]{result.final_score:.3f}[/]")
    tbl.add_row("Статус", result.status)
    console.print(tbl)
    for sr in result.stress_results:
        render_stress_result(sr)


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ─── Scoring v2 рендер ──────────────────────────────────────────────────────


_PRESET_LABELS = {
    "fast":     "Быстрый",
    "standard": "Стандартный (median-of-3)",
    "accurate": "Точный (с 95% CI)",
}


def render_overall_score(overall: OverallScore, *, preset: str | None = None) -> None:
    """Отрисовать per-category разбивку расширенного теста CPU (scoring v2).

    Единый «итоговый балл» (overall_score = 1000·ratio) удалён в 0.9.x как
    устаревший: micro-прогон — это детальный анализ по категориям (доля от
    архитектурного пика Roofline), а не системный балл (его дают Стресс /
    Общая оценка / Winsat). Показываем только subscores по категориям.
    """
    if not overall.subscores:
        return
    console.print()
    console.rule("[bold cyan]Расширенный тест CPU · доля от архитектурного пика[/]")

    sub_tbl = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    sub_tbl.add_column("Категория", style="bold cyan")
    sub_tbl.add_column("Доля от пика (Roofline)", justify="right", style="bold")

    for key in ("R_MEM", "R_CPU_compute"):
        if key in overall.subscores:
            v = overall.subscores[key]
            sub_tbl.add_row(key, f"{v:.3f} ({v * 100:.1f}%)")

    for key in ("r_memory", "r_flops", "r_integer", "r_crypto", "r_fractal"):
        if key in overall.subscores:
            v = overall.subscores[key]
            sub_tbl.add_row(f"  {key}", f"{v:.3f} ({v * 100:.1f}%)")
    console.print(sub_tbl)

    flags: list[str] = []
    preset_label = _PRESET_LABELS.get(preset or "", "")
    if preset_label:
        flags.append(f"[dim]{preset_label}[/]")
    if overall.n_runs > 1:
        flags.append(f"[dim]n={overall.n_runs}[/]")
    if overall.provisional:
        flags.append("[yellow]provisional[/]")
    if "roofline_partial" in overall.notes or "partial_reference" in overall.notes:
        flags.append("[yellow]roofline partial[/]")
    flags.append(f"[dim]v{overall.scoring_version}[/]")
    flags.append(f"[dim]ref: {overall.reference_id}[/]")

    console.print()
    console.print("  " + " · ".join(flags))


def render_thermal_stability(thermal: ThermalStabilityResult) -> None:
    """Отрисовать метрику стабильности под нагрузкой (scoring v2 §7)."""
    console.print()
    console.rule("[bold]Стабильность под нагрузкой[/]")

    if thermal.frame_rate_stability_pct is None:
        console.print("[yellow]Данных по частотам недостаточно для оценки стабильности[/]")
        return

    pct = thermal.frame_rate_stability_pct
    verdict = "[bold green]PASS[/]" if thermal.pass_threshold_97 else "[bold red]FAIL[/]"
    console.print(
        f"  Frame-Rate-Stability: [bold]{pct:.1f}%[/]  {verdict}  "
        f"[dim](порог 97%, по UL 3DMark Stress Test)[/]"
    )

    tbl = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    tbl.add_column("Метрика", style="bold cyan")
    tbl.add_column("Значение")
    # Намеренно не показываем «Частота CPU min/max» — на большинстве сборок
    # OS-адаптер отдаёт только base clock из /proc/cpuinfo и cpu_model_name,
    # из-за чего min == max и значение визуально статично. См.
    # ARCHITECTURE.md секцию «CLI-меню → атавизм частоты».
    if thermal.temp_max_c is not None:
        tbl.add_row("Температура макс.", f"{thermal.temp_max_c:.1f} °C")
    if thermal.temp_avg_c is not None:
        tbl.add_row("Температура сред.", f"{thermal.temp_avg_c:.1f} °C")
    tbl.add_row(
        "Тротлинг",
        "[red]зафиксирован[/]" if thermal.throttle_observed else "не зафиксирован",
    )
    if thermal.tsc is not None:
        tbl.add_row("TSC (деградация)", f"{thermal.tsc:.3f} ({thermal.tsc * 100:.1f}%)")
    tbl.add_row("Снимков телеметрии", str(thermal.samples))
    console.print(tbl)


def render_microbench_suite(suite: MicroBenchSuiteResult) -> None:
    """Финальная таблица расширенного тестирования процессора.

    Группируется по категориям (memory / flops / integer / crypto / fractal)
    с разделителями между группами. Для каждого теста показывается значение,
    единицы измерения, фактическая длительность и число прогонов внутри теста.
    """
    cpu = suite.system_info.cpu_model
    cores = suite.system_info.cpu_cores
    duration_total = (suite.end_time - suite.start_time).total_seconds()

    console.rule(f"[bold]Расширенное тестирование процессора[/]   {cpu}")
    console.print(
        f"[dim]Ядра: физ. {cores.physical}, лог. {cores.logical}    "
        f"Всего: {duration_total:.1f} с    "
        f"На тест: {suite.duration_sec_per_test:.1f} с    "
        f"Потоков: {suite.threads or 'auto'}[/]"
    )

    tbl = Table(show_header=True, show_lines=False, header_style="bold")
    tbl.add_column("Тест", style="bold cyan", no_wrap=True)
    tbl.add_column("Категория", style="dim")
    tbl.add_column("Значение", justify="right", style="bold green")
    tbl.add_column("Ед.", justify="left")
    tbl.add_column("Время, с", justify="right", style="dim")
    tbl.add_column("Прогонов", justify="right", style="dim")

    last_category: str | None = None
    for res in suite.results:
        if last_category is not None and last_category != res.category:
            tbl.add_section()
        last_category = res.category
        if res.error:
            tbl.add_row(
                res.name,
                res.category,
                f"[red]ERROR[/] [dim]{res.error[:30]}[/]",
                res.unit,
                "—",
                "—",
            )
            continue
        val = res.value
        if val >= 1000:
            value_str = f"{val:,.0f}".replace(",", " ")
        elif val >= 100:
            value_str = f"{val:.1f}"
        elif val >= 10:
            value_str = f"{val:.2f}"
        else:
            value_str = f"{val:.3f}"
        tbl.add_row(
            res.name,
            res.category,
            value_str,
            res.unit,
            f"{res.duration_actual_sec:.2f}",
            str(res.iterations),
        )
    console.print(tbl)


# ─── Ram&Cache рендер ───────────────────────────────────────────────────────


def format_buffer_size(bytes_size: int) -> str:
    """Сжатый формат: «32 КБ», «1.0 МБ», «256 МБ»."""
    if bytes_size >= 1024 * 1024:
        mb = bytes_size / (1024 * 1024)
        if mb >= 100:
            return f"{mb:.0f} МБ"
        if mb >= 10:
            return f"{mb:.1f} МБ"
        return f"{mb:.2f} МБ"
    if bytes_size >= 1024:
        return f"{bytes_size // 1024} КБ"
    return f"{bytes_size} Б"


def _format_throughput_mbps(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 100:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _format_latency_ns(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 100:
        return f"{value:.1f}"
    if value >= 10:
        return f"{value:.2f}"
    return f"{value:.3f}"


def render_ram_cache_report(report: RamCacheReport) -> None:
    """Финальная таблица 4×4 + сноска ¹²³⁴ с описаниями метрик.

    Колонки: Read · Write · Copy · Latency. Строки: DRAM · L3 · L2 · L1.
    Под таблицей идёт пояснение каждой метрики на русском (см. messages.py)
    и предупреждение про ограничения Python/NumPy без numba.
    """
    cpu = report.system_info.cpu_model
    cores = report.system_info.cpu_cores
    duration_total = (report.ended_at - report.started_at).total_seconds()

    console.rule(
        f"[bold]Расширенный тест ОЗУ и кеша (Ram&Cache)[/]   {cpu}"
    )
    console.print(
        f"[dim]Ядра: физ. {cores.physical}, лог. {cores.logical}    "
        f"Всего: {duration_total:.1f} с    "
        f"На метрику: {report.duration_sec_per_metric:.1f} с[/]"
    )

    by_level_op: dict[tuple[LevelName, OperationName], object] = {}
    for m in report.metrics:
        by_level_op[(m.level, m.operation)] = m

    tbl = Table(show_header=True, show_lines=False, header_style="bold")
    tbl.add_column("Уровень", style="bold cyan", no_wrap=True)
    tbl.add_column("Read¹, MB/s", justify="right", style="bold green")
    tbl.add_column("Write², MB/s", justify="right", style="bold green")
    tbl.add_column("Copy³, MB/s", justify="right", style="bold green")
    tbl.add_column("Latency⁴, ns", justify="right", style="bold yellow")

    levels_in_order: list[LevelName] = ["DRAM", "L3", "L2", "L1"]

    for level in levels_in_order:
        cells: list[str] = []
        for op in ("read", "write", "copy", "latency"):
            metric = by_level_op.get((level, op))  # type: ignore[arg-type]
            if metric is None:
                cells.append("—")
                continue
            if metric.error:  # type: ignore[union-attr]
                cells.append("[red]—[/]")
                continue
            if op == "latency":
                cells.append(_format_latency_ns(metric.value))  # type: ignore[union-attr]
            else:
                cells.append(_format_throughput_mbps(metric.value))  # type: ignore[union-attr]

        tbl.add_row(level, *cells)

    console.print(tbl)

    # Сноска ¹²³⁴
    console.print()
    for marker, name, desc in RAMCACHE_FOOTNOTES:
        console.print(f"[bold]{marker} {name}[/] — [dim]{desc}[/]")

    if report.cancelled:
        console.print()
        console.print("[yellow]Прогон был отменён пользователем — часть значений отсутствует.[/]")


def render_ram_cache_progress(total_measurements: int):
    """Создать Rich-Progress + колбэки для Ram&Cache.

    Возвращает кортеж ``(progress, advance, finish)``:
      - ``progress`` — context manager Rich (``with progress: …``), внутри
        живёт прогресс-бар со спиннером, счётчиком M/N, прошедшим и
        оставшимся временем.
      - ``advance(level, op, idx, total)`` — совместим с сигнатурой
        :data:`RamCacheService.ProgressCallback`. Обновляет текущее описание
        и счётчик выполненных измерений.
      - ``finish(cancelled)`` — закрыть бар по окончании прогона: либо
        «готово» (completed=N), либо «прервано пользователем».
    """
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}", justify="left"),
        BarColumn(
            bar_width=None,
            complete_style="cyan",
            finished_style="bright_green",
            pulse_style="cyan",
        ),
        MofNCompleteColumn(),
        TextColumn("[dim]•[/]"),
        TimeElapsedColumn(),
        TextColumn("[dim]осталось[/]"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    task_id = progress.add_task(
        "[dim]инициализация…[/]", total=total_measurements
    )

    def advance(level: str, op: str, idx: int, total: int) -> None:
        # ``idx`` 1-based и приходит ПЕРЕД стартом измерения. Поэтому
        # выполнено уже ``idx - 1``, а ``level · op`` — то, что сейчас
        # запускается. Описание окрашиваем под уровень кеша.
        level_color = {
            "DRAM": "magenta",
            "L3": "yellow",
            "L2": "green",
            "L1": "bright_cyan",
        }.get(level, "cyan")
        progress.update(
            task_id,
            completed=idx - 1,
            description=f"[{level_color}]{level}[/] · [bold]{op}[/]",
        )

    def finish(cancelled: bool) -> None:
        if cancelled:
            progress.update(
                task_id, description="[yellow]прервано пользователем[/]"
            )
        else:
            progress.update(
                task_id,
                completed=total_measurements,
                description="[green]готово[/]",
            )

    return progress, advance, finish


# ─────────────────────────── Stress orchestrator ───────────────────────────


def render_safety_report(report: object) -> None:
    """Краткий блок с pre-flight проверками SafetyGate.

    Аргумент типизирован как ``object`` чтобы избежать циклического импорта
    с ``application/safety_gate.py`` (этот модуль импортирует render).
    """
    on_battery = bool(getattr(report, "on_battery", False))
    battery = getattr(report, "battery_percent", None)
    is_vm = bool(getattr(report, "is_virtualized", False))
    vm_kind = getattr(report, "virtualization_kind", None)
    free_ram = getattr(report, "free_ram_gb", None)
    block_reasons = list(getattr(report, "block_reasons", []) or [])
    warn_reasons = list(getattr(report, "warn_reasons", []) or [])

    tbl = Table(
        title="Проверки перед запуском",
        show_header=False,
        box=None,
    )
    tbl.add_column("Параметр", style="bold cyan")
    tbl.add_column("Значение")

    if battery is not None:
        if on_battery:
            tbl.add_row(
                "Питание",
                f"[red]на батарее[/], заряд {battery:.0f}% — "
                "[dim]ноутбук под нагрузкой быстро сядет; "
                "при низком заряде запуск может быть заблокирован[/]",
            )
        else:
            tbl.add_row(
                "Питание",
                f"[green]AC подключён[/], заряд {battery:.0f}% — "
                "[dim]ограничений по питанию нет[/]",
            )
    else:
        tbl.add_row(
            "Питание",
            "[green]стационарный ПК[/] "
            "[dim](батарея не обнаружена — питание от сети; "
            "ограничений по батарее не накладываем)[/]",
        )
    # Строку про виртуализацию выводим только когда VM обнаружена —
    # пользователю важно знать что watchdog не сможет работать. На
    # обычном железе эта строка лишний шум.
    if is_vm:
        tbl.add_row(
            "Виртуализация",
            f"[red]{vm_kind or 'неизвестная среда'}[/] "
            "[dim](в VM нет прямого доступа к датчикам CPU/GPU; "
            "watchdog работать не будет — следите за температурой вручную)[/]",
        )
    if free_ram is not None:
        tbl.add_row("Свободно RAM", f"{free_ram:.1f} ГБ")
    console.print(tbl)
    if block_reasons:
        console.print()
        console.print("[bold red]Запуск заблокирован:[/]")
        for r in block_reasons:
            console.print(f"  • {r}")
    if warn_reasons:
        console.print()
        console.print("[bold yellow]Предупреждения:[/]")
        for r in warn_reasons:
            console.print(f"  • {r}")


def render_stress_final_report(report: object) -> None:
    """Сводный отчёт после стресс-нагрузки.

    Принимает StressFinalReport как ``object`` (см. примечание про
    циклический импорт). Ориентирован на пользователя: технические имена
    движков заменены на человекочитаемые, sub_results-флаги
    («no_watchdog_trigger» и т.п.) скрыты — вместо них показываются
    пиковые температуры с указанием тепловых лимитов.
    """

    duration = float(getattr(report, "duration_actual_sec", 0.0))
    requested = float(getattr(report, "requested_duration_sec", 0.0))
    triggered = bool(getattr(report, "watchdog_triggered", False))
    trigger = getattr(report, "watchdog_trigger", None)
    parallel = getattr(report, "parallel", None)
    thermal = getattr(report, "thermal", None)
    verdict = getattr(report, "verdict", None)
    safety = getattr(report, "safety", None)

    cpu_avg_load = getattr(report, "cpu_avg_load_pct", None)
    cpu_peak_load = getattr(report, "cpu_peak_load_pct", None)
    cpu_avg_temp = getattr(report, "cpu_avg_temp_c", None)
    cpu_peak_temp = getattr(report, "cpu_peak_temp_c", None)
    cpu_limit = getattr(report, "cpu_thermal_limit_c", None)
    cpu_avg_vcore = getattr(report, "cpu_avg_vcore_v", None)
    cpu_peak_vcore = getattr(report, "cpu_peak_vcore_v", None)
    ram_avg_load = getattr(report, "ram_avg_load_pct", None)
    ram_peak_load = getattr(report, "ram_peak_load_pct", None)
    ram_avg_temp = getattr(report, "ram_avg_temp_c", None)
    ram_peak_temp = getattr(report, "ram_peak_temp_c", None)
    ram_avg_vcore = getattr(report, "ram_avg_vcore_v", None)
    ram_peak_vcore = getattr(report, "ram_peak_vcore_v", None)
    gpu_avg_temp = getattr(report, "gpu_avg_temp_c", None)
    gpu_peak_temp = getattr(report, "gpu_peak_temp_c", None)
    gpu_avg_load = getattr(report, "gpu_avg_load_pct", None)
    gpu_peak_load = getattr(report, "gpu_peak_load_pct", None)
    gpu_peak_mem = getattr(report, "gpu_peak_mem_gb", None)
    gpu_mem_total = getattr(report, "gpu_mem_total_gb", None)
    gpu_limit = getattr(report, "gpu_thermal_limit_c", None)
    gpu_avg_vcore = getattr(report, "gpu_avg_vcore_v", None)
    gpu_peak_vcore = getattr(report, "gpu_peak_vcore_v", None)
    cpu_avg_power_w = getattr(report, "cpu_avg_power_w", None)
    cpu_peak_power_w = getattr(report, "cpu_peak_power_w", None)
    gpu_avg_power_w = getattr(report, "gpu_avg_power_w", None)
    gpu_peak_power_w = getattr(report, "gpu_peak_power_w", None)

    # Этап 1: «нет данных» — диагностика источника CPU-температуры и
    # понимание, был ли GPU частью плана. Поля опциональны: для совместимости
    # со старыми сериализациями отчёта дефолты безопасные (ok=True, GPU не
    # стрессировался).
    cpu_temp_source_ok = bool(getattr(report, "cpu_temp_source_ok", True))
    gpu_was_stressed = bool(getattr(report, "gpu_was_stressed", False))

    # Этапы 3a/3b: Roofline-пики и стресс-балл «в попугаях». Поля плоские —
    # см. ``StressFinalReport``. Все опциональны: для старых сериализаций
    # дефолты None, рендер тогда просто не показывает соответствующие блоки.
    stress_score = getattr(report, "stress_score", None)
    stress_r_dgemm = getattr(report, "stress_r_dgemm", None)
    stress_r_stream = getattr(report, "stress_r_stream", None)
    stress_r_stability = getattr(report, "stress_r_stability", None)
    stress_r_thermal = getattr(report, "stress_r_thermal", None)
    stress_t_max_c = getattr(report, "stress_t_max_c", None)
    stress_tjmax_c = getattr(report, "stress_tjmax_c", None)
    stress_duration_sec = getattr(report, "stress_duration_sec", None)
    roofline_dgemm_peak = getattr(report, "roofline_dgemm_peak_gflops", None)
    roofline_stream_peak = getattr(report, "roofline_stream_peak_gb_s", None)
    roofline_simd_level = getattr(report, "roofline_simd_level", None)
    roofline_clock_ghz = getattr(report, "roofline_clock_ghz", None)
    roofline_dram_mts = getattr(report, "roofline_dram_mts", None)
    roofline_dram_modules = getattr(report, "roofline_dram_modules", None)

    # Шапка вердикта.
    passed = bool(verdict is not None and getattr(verdict, "passed", False))
    if passed:
        console.rule("[bold green]СТРЕСС-ТЕСТ СИСТЕМЫ: ПРОЙДЕНО[/]")
    elif triggered:
        console.rule("[bold red]СТРЕСС-ТЕСТ СИСТЕМЫ: ОСТАНОВЛЕН ЗАЩИТОЙ[/]")
    else:
        console.rule("[bold red]СТРЕСС-ТЕСТ СИСТЕМЫ: НЕ ПРОЙДЕНО[/]")
    if verdict is not None:
        reason = getattr(verdict, "reason", "?")
        console.print(f"[bold]Вердикт:[/] {reason}")

    # Срабатывание watchdog (если случилось) — отдельным блоком.
    if triggered and trigger is not None:
        console.print()
        msg = getattr(trigger, "message", "термальный лимит достигнут")
        console.print(f"[bold red]⚠ {msg}[/]")

    # Длительность прогона. В бесконечном режиме «запрошено» — это
    # технический потолок (24 ч), который пользователь не задавал —
    # показывать его не нужно, иначе путает. Порог 12 ч с запасом
    # отделяет реальные timed-прогоны (макс. 1-2 ч) от инфинит-маркера.
    console.print()
    if requested >= 12 * 3600.0:
        console.print(
            f"[bold]Длительность нагрузки:[/] {duration:.1f} с"
        )
    else:
        console.print(
            f"[bold]Длительность нагрузки:[/] {duration:.1f} с "
            f"(запрошено {requested:.0f} с)"
        )

    # Производительность: голые результаты прогона + утилизация Roofline-пика
    # в одной строке на каждую нагрузку. Таблица «Результаты нагрузки»
    # сознательно убрана — пользователь сказал, что она перегружала отчёт
    # и нужны только сами числа + контекст «много это или мало».
    dgemm_measured: float | None = None
    stream_measured: float | None = None
    total_errors = 0
    cancelled = False
    if parallel is not None:
        for r in getattr(parallel, "results", []) or []:
            total_errors += getattr(r, "error_count", 0) or 0
            unit = getattr(r, "throughput_unit", None)
            tput = getattr(r, "throughput", None)
            if tput is None or tput <= 0:
                continue
            if unit == "GFLOPS":
                dgemm_measured = float(tput)
            elif unit == "GB/s":
                stream_measured = float(tput)
        cancelled = bool(getattr(parallel, "cancelled", False))

    perf_lines: list[str] = []
    if dgemm_measured is not None:
        ctx_bits = []
        if stress_r_dgemm is not None:
            ctx_bits.append(f"{stress_r_dgemm * 100.0:.1f} % от пика")
        if roofline_simd_level:
            ctx_bits.append(roofline_simd_level.upper())
        if roofline_clock_ghz:
            ctx_bits.append(f"~{roofline_clock_ghz:.1f} ГГц")
        if roofline_dgemm_peak is not None:
            ctx_bits.append(f"~{roofline_dgemm_peak:.0f} GFLOPS DP")
        ctx = f"  [dim]({' · '.join(ctx_bits)})[/]" if ctx_bits else ""
        perf_lines.append(
            f"[bold cyan]DGEMM:[/] {dgemm_measured:,.2f} GFLOPS".replace(",", " ") + ctx
        )
    if stream_measured is not None:
        ctx_bits = []
        if stress_r_stream is not None:
            ctx_bits.append(f"{stress_r_stream * 100.0:.1f} % от пика")
        if roofline_dram_mts and roofline_dram_modules:
            ctx_bits.append(
                f"DDR @ {roofline_dram_mts:.0f} MT/s × "
                f"{roofline_dram_modules} модуля"
            )
        if roofline_stream_peak is not None:
            ctx_bits.append(f"~{roofline_stream_peak:.1f} GB/s")
        ctx = f"  [dim]({' · '.join(ctx_bits)})[/]" if ctx_bits else ""
        perf_lines.append(
            f"[bold cyan]STREAM:[/] {stream_measured:,.2f} GB/s".replace(",", " ") + ctx
        )
    if perf_lines:
        console.print()
        console.print("[bold]Производительность:[/]")
        for line in perf_lines:
            console.print(f"  {line}")
    if total_errors > 0:
        console.print(
            f"[red]Verify-ошибок: {total_errors} "
            f"(см. подробности в БД через `apexcore runs show`)[/]"
        )
    if cancelled and not triggered:
        console.print(
            "[yellow]Прогон был остановлен пользователем (Ctrl+C).[/]"
        )

    # Оценка под нагрузкой — крупное число + однострочное пояснение что
    # меряет балл. Лейбл выбран после анализа в
    # `docs/research/stress_score_validity.md` (балл смешивает
    # производительность и охлаждение — заголовок это явно обозначает).
    # Точная формула, 4 компонента (DGEMM, STREAM, стабильность,
    # thermal headroom) и шкала живут в `docs/stress_score.md`.
    #
    # Если duration_sec < RELIABLE_DURATION_SEC (90 сек) — балл всё равно
    # выводится, но с warning «оценка приближённая, для точности 10–60 мин»
    # (запрос пользователя 2026-05-17: «даже если тест шел меньше 90 секунд
    # все равно выведи баллы и явно укажите пользователю»).
    from apexcore.application.stress_score import RELIABLE_DURATION_SEC

    short_run = (
        stress_duration_sec is not None
        and stress_duration_sec < RELIABLE_DURATION_SEC
    )
    if stress_score is not None:
        console.print()
        panel_body = f"[bold cyan]{stress_score:,.0f}[/]".replace(",", " ")
        if short_run:
            panel_body += (
                f"\n[yellow]⚠ Прогон {stress_duration_sec:.0f} с короче "
                f"{RELIABLE_DURATION_SEC:.0f} с — оценка приближённая.[/]"
                "\n[dim]Для честной оценки устойчивой производительности "
                "используйте 10–60 минут.[/]"
            )
        else:
            panel_body += (
                "\n[dim]Производительность CPU+RAM с учётом стабильности "
                "частот и теплового запаса. Чистая оценка производительности "
                "— в разделе меню «Общая оценка производительности системы».[/]"
            )
        console.print(
            Panel(
                panel_body,
                title="Оценка под нагрузкой (CPU+RAM+охлаждение)",
                border_style="cyan",
                expand=False,
            )
        )
    elif (
        stress_r_dgemm is not None
        or stress_r_stream is not None
        or stress_r_stability is not None
        or stress_r_thermal is not None
    ):
        missing: list[str] = []
        if stress_r_dgemm is None:
            missing.append("DGEMM (Roofline-пик)")
        if stress_r_stream is None:
            missing.append("STREAM (DRAM-пик)")
        if stress_r_stability is None:
            missing.append("стабильность частот CPU")
        if stress_r_thermal is None:
            # Различаем причины: нет CPU temp, нет TJmax.
            if stress_t_max_c is None:
                missing.append(
                    "температура CPU (требуется PawnIO/LHM/sensord)"
                )
            elif stress_tjmax_c is None:
                missing.append("TJmax (CPU не распознан таблицей)")
            else:
                missing.append("thermal headroom (r_thermal)")
        console.print()
        console.print(
            "[yellow]Оценка под нагрузкой недоступна:[/] "
            f"не рассчитаны компоненты — {', '.join(missing)}."
        )

    # Пустая строка перед «Сводкой по компонентам» — иначе сообщение
    # «Оценка недоступна: ...» сливается с заголовком таблицы (visual
    # bug, замечено пользователем 2026-05-17).
    console.print()

    # Единая сводная таблица. Парные колонки (ср/пик) объединены в одну
    # ячейку «X / Y», иначе при 9–10 колонках таблица расползается за края
    # типичного PowerShell-окна и Rich обрезает правый край.
    # «Лимит T°» убран — он уже показан выше в шапке прогона.
    # Колонка «Тренд» убрана из финала — тренды пользователь уже видел в
    # Live во время прогона; в итоговой Сводке они избыточны.
    summary = Table(title="Сводка по компонентам")
    summary.add_column("Компонент", style="bold cyan", no_wrap=True)
    summary.add_column("Загрузка, %\nср/пик", justify="right", no_wrap=True)
    summary.add_column("T°, °C\nср/пик", justify="right", no_wrap=True)
    summary.add_column("Напряжение, В\nср/пик", justify="right", no_wrap=True)
    summary.add_column("Потребление, Вт\nср/пик", justify="right", no_wrap=True)
    summary.add_column("Статус")

    def _fmt_pct(v: float | None) -> str:
        return f"{v:.1f} %" if v is not None else "—"

    def _fmt_temp(v: float | None) -> str:
        return f"{v:.1f} °C" if v is not None else "—"

    def _fmt_volt(v: float | None) -> str:
        return f"{v:.3f} В" if v is not None else "—"

    # Pair-форматы: единица измерения уже в заголовке колонки → не
    # дублируем; пробелы вокруг «/» убраны для экономии 4 символов на
    # значение. Это критично — иначе таблица не помещается в 160 столбцов.
    def _fmt_pair_pct(avg: float | None, peak: float | None) -> str:
        if avg is None and peak is None:
            return "—"
        a = f"{avg:.1f}" if avg is not None else "—"
        p = f"{peak:.1f}" if peak is not None else "—"
        return f"{a}/{p}"

    def _fmt_pair_temp(avg: float | None, peak: float | None) -> str:
        if avg is None and peak is None:
            return "—"
        a = f"{avg:.1f}" if avg is not None else "—"
        p = f"{peak:.1f}" if peak is not None else "—"
        return f"{a}/{p}"

    def _fmt_pair_volt(avg: float | None, peak: float | None) -> str:
        if avg is None and peak is None:
            return "—"
        a = f"{avg:.2f}" if avg is not None else "—"
        p = f"{peak:.2f}" if peak is not None else "—"
        return f"{a}/{p}"

    def _fmt_pair_power(avg: float | None, peak: float | None) -> str:
        if avg is None and peak is None:
            return "—"
        a = f"{avg:.0f}" if avg is not None else "—"
        p = f"{peak:.0f}" if peak is not None else "—"
        return f"{a}/{p}"

    def _temp_status(peak: float | None, limit: float | None) -> str:
        if peak is None or limit is None:
            return "—"
        return (
            "[green]лимит не превышен[/]"
            if peak < limit
            else "[red]ЛИМИТ ПРЕВЫШЕН[/]"
        )

    summary.add_row(
        "ЦП",
        _fmt_pair_pct(cpu_avg_load, cpu_peak_load),
        _fmt_pair_temp(cpu_avg_temp, cpu_peak_temp),
        _fmt_pair_volt(cpu_avg_vcore, cpu_peak_vcore),
        _fmt_pair_power(cpu_avg_power_w, cpu_peak_power_w),
        _temp_status(cpu_peak_temp, cpu_limit),
    )
    summary.add_row(
        "Оперативная память",
        _fmt_pair_pct(ram_avg_load, ram_peak_load),
        _fmt_pair_temp(ram_avg_temp, ram_peak_temp),
        _fmt_pair_volt(ram_avg_vcore, ram_peak_vcore),
        "—",
        "—",
    )
    if gpu_peak_temp is not None or gpu_peak_load is not None:
        # Модель GPU уже отображается в шапке прогона
        # («Видеокарта: <gpu_name> — телеметрия активна»); в Сводке нужен
        # короткий лейбл, иначе колонка «Компонент» расширяется и таблица
        # ломает все остальные.
        if gpu_was_stressed:
            gpu_label = "Видеокарта"
            gpu_status_str = _temp_status(gpu_peak_temp, gpu_limit)
        else:
            gpu_label = "Видеокарта [dim](фон)[/]"
            gpu_status_str = "[dim]фон[/]"
        summary.add_row(
            gpu_label,
            _fmt_pair_pct(gpu_avg_load, gpu_peak_load),
            _fmt_pair_temp(gpu_avg_temp, gpu_peak_temp),
            _fmt_pair_volt(gpu_avg_vcore, gpu_peak_vcore),
            # GPU power: NVML (`nvml/<N>/power_w`) на NVIDIA без admin,
            # либо LHM `gpu_power/*` если когда-нибудь добавим. На AMD/Intel
            # iGPU обычно None — таблица покажет «—» честно.
            _fmt_pair_power(gpu_avg_power_w, gpu_peak_power_w),
            gpu_status_str,
        )
    console.print(summary)

    # Этап 1: если CPU-температуры не было, выводим короткую напоминалку
    # (без полного advice — он уже показан в шапке прогона, пользователь
    # уже видел его минуту назад). Полная диагностика — `apexcore doctor`.
    cpu_temp_missing = cpu_avg_temp is None and cpu_peak_temp is None
    if cpu_temp_missing and not cpu_temp_source_ok:
        console.print()
        console.print(
            "[dim yellow]Температура CPU не считывалась "
            "(см. подсказку в начале прогона; полная диагностика: "
            "apexcore doctor)[/]"
        )

    # Краткие дополнительные факты, не помещающиеся в таблицу:
    extras: list[tuple[str, str]] = []
    if thermal is not None:
        # Этап 1: семантика «нет данных» vs «не зафиксирован». Тротлинг по
        # частоте (psutil.cpu_freq) без температуры не позволяет различить
        # термальный и power-throttle, поэтому без CPU-температуры
        # выводим «нет данных» — пользователь не должен думать, что система
        # прошла тепловой тест, если температурного датчика не было.
        has_cpu_temps = cpu_avg_temp is not None or cpu_peak_temp is not None
        if not has_cpu_temps:
            extras.append(("Тротлинг ЦП", "[dim]нет данных[/]"))
        elif getattr(thermal, "throttle_observed", False):
            extras.append(("Тротлинг ЦП", "[red]зафиксирован[/]"))
        else:
            extras.append(("Тротлинг ЦП", "[green]не зафиксирован[/]"))
    if gpu_peak_mem is not None and gpu_mem_total:
        extras.append(
            (
                "Пиковая занятость видеопамяти",
                f"{gpu_peak_mem:.1f} / {gpu_mem_total:.1f} ГБ",
            )
        )
    if extras:
        extra_tbl = Table(show_header=False, box=None)
        extra_tbl.add_column(style="bold cyan")
        extra_tbl.add_column()
        for k, v in extras:
            extra_tbl.add_row(k, v)
        console.print(extra_tbl)

    # Cooling-sanity и safety-warnings (если есть). Показываем только
    # человекочитаемые предупреждения, без технических деталей.
    if safety is not None:
        warn_reasons = list(getattr(safety, "warn_reasons", []) or [])
        cooling_ok = getattr(safety, "cooling_sanity_ok", None)
        if warn_reasons or cooling_ok is False:
            console.print()
            console.print("[bold yellow]Предупреждения:[/]")
            for w in warn_reasons:
                console.print(f"  • {w}")


def render_sensor_diagnostics(report: object) -> None:
    """Печать структурированного отчёта о датчиках температуры.

    Принимает ``SensorDiagnostics`` как ``object`` чтобы избежать
    циклического импорта с ``application/diagnostics_sensors.py``.
    """
    cpu_ok = bool(getattr(report, "has_cpu_temperature", False))
    gpu_ok = bool(getattr(report, "has_gpu_temperature", False))
    cpu_src = getattr(report, "cpu_temp_source", None)
    gpu_src = getattr(report, "gpu_temp_source", None)
    backends = list(getattr(report, "backends", []) or [])
    advice = list(getattr(report, "advice", []) or [])

    # Заголовок-сводка.
    if cpu_ok:
        console.rule("[bold green]Датчики температуры: драйвер активен[/]")
    else:
        console.rule("[bold yellow]Датчики температуры: драйвер не активен[/]")

    summary = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    summary.add_column(style="bold cyan")
    summary.add_column()
    summary.add_row(
        "CPU",
        f"[green]✓ {cpu_src}[/]" if cpu_ok else "[red]✗ источник не определён[/]",
    )
    summary.add_row(
        "GPU",
        f"[green]✓ {gpu_src}[/]" if gpu_ok else "[yellow]✗ нет данных[/]",
    )
    console.print(summary)

    # Подробная таблица бэкендов.
    tbl = Table(title="Источники температурных данных", show_lines=False)
    tbl.add_column("Бэкенд", style="bold cyan")
    tbl.add_column("Статус", justify="center")
    tbl.add_column("Сенсоров", justify="right")
    tbl.add_column("Подробности")
    for b in backends:
        ok = bool(getattr(b, "ok", False))
        n = int(getattr(b, "sensor_count", 0) or 0)
        status = "[green]✓[/]" if ok else "[yellow]✗[/]"
        tbl.add_row(
            getattr(b, "name", "?"),
            status,
            str(n) if n > 0 else "—",
            getattr(b, "detail", "") or "",
        )
    console.print(tbl)

    # Если есть конкретные DegradedReason — показать перевод (P0.5).
    degraded_reasons = list(getattr(report, "degraded_reasons", []) or [])
    if degraded_reasons:
        console.print()
        console.print("[bold]Обнаруженные проблемы:[/]")
        for r in degraded_reasons:
            short = r.short() if hasattr(r, "short") else str(r)
            console.print(f"  • [yellow]{r.value if hasattr(r, 'value') else r}[/]: {short}")

    # Советы — только если есть.
    if advice:
        console.print()
        console.print("[bold]Что сделать:[/]")
        for i, a in enumerate(advice, start=1):
            console.print(f"  [bold cyan]{i}.[/] {a}")


def render_engine_availability_table(engines_by_role: dict[str, list[tuple[str, object]]]) -> None:
    """Таблица доступности движков по ролям (stability_cpu / stability_ram / benchmark).

    Принимает результат ``pick_stability_engines_by_role(registry)``.
    """
    from apexcore.infrastructure.stress.registry import (
        ENGINE_DESCRIPTIONS,
        ENGINE_ROLES,
        build_default_registry,
    )

    role_titles = {
        "stability_cpu": "CPU stress (с verify)",
        "stability_ram": "RAM stress (с verify)",
        "benchmark": "Бенчмарки",
    }
    registry = build_default_registry()
    for role, items in engines_by_role.items():
        if not items:
            console.print(f"[yellow]{role_titles.get(role, role)}: ничего не доступно[/]")
            continue
        tbl = Table(title=role_titles.get(role, role))
        tbl.add_column("№", style="dim", justify="right")
        tbl.add_column("Имя", style="bold cyan")
        tbl.add_column("Категория")
        tbl.add_column("Доступен")
        tbl.add_column("Описание")
        for idx, (name, eng) in enumerate(items, start=1):
            available = "[green]✓[/]" if eng.is_available() else "[red]✗[/]"
            tbl.add_row(
                str(idx), name, eng.category, available,
                ENGINE_DESCRIPTIONS.get(name, ""),
            )
        console.print(tbl)
        console.print()
    # Дополнительно — список недоступных stability-движков, чтобы пользователь видел,
    # что можно поставить (apt install stress-ng / prime95 в PATH).
    unavailable: list[tuple[str, str]] = []
    for name, role in ENGINE_ROLES.items():
        if role == "benchmark":
            continue
        eng = registry.get(name)
        if eng is None:
            continue
        if not eng.is_available():
            unavailable.append((name, ENGINE_DESCRIPTIONS.get(name, "")))
    if unavailable:
        console.print("[bold]Недоступно (попробуйте установить):[/]")
        for name, desc in unavailable:
            console.print(f"  • {name} — [dim]{desc}[/]")
        console.print()


# ─── Winsat-аналог: визуальный модуль ──────────────────────────────────────
#
# Меню «Наследие Winsat» оформлено как флагман нового модуля. Для рендера
# используются только стандартные Rich-компоненты (Panel, Columns, Progress,
# Table) — без новых зависимостей. ASCII-баннер живёт в messages.py
# (константа WINSAT_BANNER), сноски — в WINSAT_FOOTNOTES.

def _score_color(score: float, status: WinsatStatus) -> str:
    """Цвет Rich-разметки для оценки 1.0–9.9 (или ``dim white`` для NA/ERROR)."""
    if status != WinsatStatus.PASS:
        return "dim white"
    if score >= 9.0:
        return "bold bright_green"
    if score >= 7.0:
        return "green"
    if score >= 5.0:
        return "yellow"
    if score >= 3.0:
        return "orange3"
    return "bold red"


def _score_bar(score: float, *, width: int = 10) -> str:
    """Мини-баркод ``████░░`` для визуализации оценки 0–9.9 на ширине ``width``."""
    if score <= 0:
        return "░" * width
    filled = max(1, min(width, round(score / 9.9 * width)))
    return "█" * filled + "░" * (width - filled)


def _stars(score: float, *, total: int = 9) -> str:
    """Звёздочки ``★★★★★★★★☆`` для WinSPRLevel (8.7 → 8 ★ + ☆)."""
    n = max(0, min(total, round(score)))
    return "★" * n + "☆" * (total - n)


def _format_score(sub: WinsatSubscore) -> str:
    """Текст подоценки: «9.5» или «N/A» — с цветовой разметкой."""
    if sub.status == WinsatStatus.PASS:
        color = _score_color(sub.score, sub.status)
        return f"[{color}]{sub.score:.1f}[/]"
    if sub.status == WinsatStatus.NA:
        return "[dim white]N/A[/]"
    if sub.status == WinsatStatus.NOT_SUPPORTED_ON_OS:
        return "[dim white]N/A (OS)[/]"
    return "[dim red]ERR[/]"


def render_winsat_welcome() -> None:
    """Показать приветственный баннер при входе в winsat-меню или CLI."""
    from rich.align import Align
    from rich.panel import Panel
    from rich.text import Text

    text = Text(WINSAT_BANNER, style="cyan")
    panel = Panel(
        Align.center(text),
        title="[bold]apexcore · winsat[/]",
        subtitle="[dim]аналог Windows System Assessment Tool[/]",
        border_style="cyan",
    )
    console.print(panel)


def render_winsat_progress():
    """Создать Rich-Progress + callback ``advance(stage, idx, total)``.

    Возвращает кортеж ``(progress, advance_fn)``. Контекст ``progress`` —
    стандартный context manager Rich (используется через ``with progress:``).
    Callback совместим с :data:`WinsatService.ProgressCallback` сигнатурой.
    """
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
    )

    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}", justify="left"),
        BarColumn(bar_width=30, complete_style="green", finished_style="bright_green"),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    task_ids: dict[str, TaskID] = {}
    finished: set[str] = set()

    # Создаём задачи заранее с total=1 (на каждый stage). Описания берём из messages.
    for stage_key, label in WINSAT_STAGE_LABELS.items():
        task_ids[stage_key] = progress.add_task(label, total=1, start=False)

    def advance(stage: str, idx: int, total: int) -> None:
        # Закрываем все предыдущие — это сигнал «текущий start».
        for prev in WINSAT_STAGE_LABELS:
            if prev == stage:
                break
            if prev not in finished:
                progress.update(task_ids[prev], completed=1)
                finished.add(prev)
        if stage in task_ids:
            progress.start_task(task_ids[stage])
            progress.update(task_ids[stage], description=WINSAT_STAGE_LABELS[stage])

    return progress, advance


def render_winsat_report(report: WinsatReport) -> None:
    """Отрисовать итоговую таблицу winsat-отчёта (две панели + WinSPR-панель).

    Структура:
      1. ASCII-баннер
      2. Слева — Win32_Winsat-стиль (CPUScore/D3DScore/...).
         Справа — детали (метрика, единица, цветная оценка, ⭐ для score≥9.0).
      3. Финальная панель с WinSPRLevel (★-визуализация).
      4. Сноски о методике (WINSAT_FOOTNOTES).
    """
    # Columns, Panel, Table, Text импортированы на module-уровне; локальный
    # импорт здесь больше не нужен после рефакторинга шапки.

    render_winsat_welcome()
    console.print(
        Panel(
            WINSAT_METHODOLOGY,
            title="[bold]Как устроена оценка[/]",
            border_style="cyan",
        )
    )
    if report.cancelled:
        console.print(
            "[bold yellow]⚠ Прогон был прерван — оценка частична.[/]\n"
        )

    # ── Левая панель: Win32_Winsat-стиль ──────────────────────────────────
    left = Table(show_header=False, box=None, pad_edge=False)
    left.add_column(style="bold cyan")
    left.add_column()
    left.add_column(style="dim")

    win32_rows: list[tuple[str, WinsatSubscore]] = [
        ("CPUScore", report.cpu_score),
        ("D3DScore", report.d3d_score),
        ("DiskScore", report.disk_score),
        ("GraphicsScore", report.graphics_score),
        ("MemoryScore", report.memory_score),
    ]
    for label, sub in win32_rows:
        score_text = _format_score(sub)
        bar = _score_bar(sub.score) if sub.status == WinsatStatus.PASS else "      "
        left.add_row(f"{label:<14}", f": {score_text}", bar)

    # WinSPRLevel в той же колонке, но визуально выделен.
    winspr_color = _score_color(report.winspr_level, WinsatStatus.PASS)
    left.add_row(
        f"{'WinSPRLevel':<14}",
        f": [{winspr_color}]{report.winspr_level:.1f}[/]",
        _score_bar(report.winspr_level),
    )

    panel_left = Panel(left, title="[bold]Win32_Winsat[/]", border_style="cyan")

    # ── Правая панель: подробности ────────────────────────────────────────
    right = Table(show_header=False, box=None, pad_edge=False)
    right.add_column(style="bold")
    right.add_column()
    right.add_column(justify="right")

    detail_rows: list[tuple[str, WinsatSubscore]] = [
        ("⚡ CPU", report.cpu_score),
        ("🧠 Memory", report.memory_score),
        ("💾 Disk", report.disk_score),
        ("🎮 Graphics", report.graphics_score),
        ("🎨 D3D", report.d3d_score),
    ]
    for label, sub in detail_rows:
        if sub.status == WinsatStatus.PASS:
            metric_text = f"{sub.metric_name}\n[dim]{sub.metric_value:,.0f} {sub.metric_unit}[/]"
            score_text = _format_score(sub)
            star = " ⭐" if sub.score >= 9.0 else ""
            right.add_row(label, metric_text, f"{score_text}{star}")
        else:
            note = sub.note or WINSAT_NA_NOTE
            right.add_row(label, f"[dim]{note}[/]", _format_score(sub))

    panel_right = Panel(right, title="[bold]Подробности[/]", border_style="cyan")

    console.print(Columns([panel_left, panel_right], expand=False, equal=False))
    console.print()

    # ── WinSPRLevel-панель ────────────────────────────────────────────────
    winspr_text = Text()
    winspr_text.append("\n")
    winspr_text.append(
        f"  {report.winspr_level:.1f}  ",
        style="bold bright_green" if report.winspr_level >= 7.0 else "bold yellow",
    )
    winspr_text.append("\n\n")
    winspr_text.append(_stars(report.winspr_level), style="bright_yellow")
    winspr_text.append("  из  ", style="dim")
    winspr_text.append("★★★★★★★★★", style="bright_yellow")
    winspr_text.append("\nминимум среди подоценок", style="dim")

    from rich.align import Align

    panel_winspr = Panel(
        Align.center(winspr_text),
        title="[bold]Индекс производительности Windows[/]",
        border_style="bright_green" if report.winspr_level >= 7.0 else "yellow",
    )
    console.print(panel_winspr)
    console.print()

    # ── Сноски о методике ────────────────────────────────────────────────
    foot_table = Table(show_header=False, box=None, pad_edge=False)
    foot_table.add_column(style="bold cyan", justify="right")
    foot_table.add_column(style="bold")
    foot_table.add_column()
    for marker, label, text in WINSAT_FOOTNOTES:
        foot_table.add_row(marker, label, f"[dim]{text}[/]")
    console.print(Panel(foot_table, title="[bold]Методика[/]", border_style="dim"))


# ──────────────── Single-Core vs Multi-Core (новый пункт меню) ────────────────


def _format_metric(value: float, unit: str) -> tuple[str, str]:
    """Разбить значение на крупное число и подпись с единицами.

    Возвращает (число_строкой, единицы). Логика: если 100+ — 1 знак,
    если 10+ — 2 знака, иначе 3 знака. Достаточно компактно и читаемо.
    """
    if value >= 1000:
        s = f"{value:,.0f}".replace(",", " ")
    elif value >= 100:
        s = f"{value:.1f}"
    elif value >= 10:
        s = f"{value:.2f}"
    else:
        s = f"{value:.3f}"
    return s, unit


def _bench_column(
    title: str,
    badge: str,
    big_value: str,
    unit: str,
    rows: list[tuple[str, str]],
    *,
    accent: str,
) -> Group:
    """Один «столбик» карточки (без своей рамки) — заголовок + крупное число + параметры.

    Не рисует Panel вокруг себя: внешнюю рамку даёт общая карточка
    «Тест Single-Core / Multi-Core». Это избавляет от проблем legacy
    Windows-renderer, который теряет рамку правого Panel внутри Columns.
    """
    grid = Table.grid(expand=True)
    grid.add_column(justify="center")
    grid.add_row(Text(title, style=f"bold {accent}"))
    grid.add_row(Text(badge, style="dim"))
    grid.add_row("")
    grid.add_row(Text(big_value, style=f"bold {accent}"))
    grid.add_row(Text(unit, style=accent))
    grid.add_row("")
    params = Table.grid(padding=(0, 1))
    params.add_column(style="dim", justify="right")
    params.add_column()
    for k, v in rows:
        params.add_row(f"{k}:", v)
    grid.add_row(Align.center(params))
    return Group(grid)


def _bar(value: float, max_value: float, *, width: int = 32, accent: str = "cyan") -> Text:
    """ASCII-бар из ▇/░ длиной ``width``, заполненный пропорционально value/max."""
    ratio = 0.0 if max_value <= 0 else max(0.0, min(1.0, value / max_value))
    filled = round(ratio * width)
    bar = Text()
    bar.append("▇" * filled, style=f"bold {accent}")
    bar.append("░" * (width - filled), style="dim")
    return bar


def _efficiency_color(efficiency: float | None) -> str:
    if efficiency is None:
        return "dim"
    if efficiency >= 0.80:
        return "green"
    if efficiency >= 0.50:
        return "yellow"
    return "red"


def render_single_multi_result(
    result: SingleMultiResult,
    ranking: RankingMatch | None = None,
) -> None:
    """Отрисовать результат сравнения Single-Core vs Multi-Core.

    Одна внешняя карточка (рамка) с двумя цветными колонками внутри
    (Single | Multi), горизонтальным разделителем и сводкой:
    ускорение, эффективность, ASCII-бар сравнения.

    Если передан ``ranking`` — снизу добавляется секция «Положение
    среди популярных CPU» с рейтинговой позицией. Параметр опционален
    и со значением ``None`` рендер ведёт себя как раньше (обратная
    совместимость для тестов и внешних вызовов).

    Почему единая рамка, а не две Panel в Columns: legacy Windows
    renderer (conhost.exe) теряет правую рамку при выводе двух Panel
    в Columns/Table.grid. Одна внешняя Panel рисуется надёжно везде.
    """
    # ── Левая колонка: Single ────────────────────────────────────────────
    s_val, s_unit = _format_metric(result.single.value, result.single.unit)
    if result.pinned_cpu is not None:
        kind = result.pinned_kind or "ядро"
        s_badge = f"Привязка: CPU {result.pinned_cpu} ({kind})"
    else:
        s_badge = "Привязка: scheduler (affinity недоступна)"
    single_block = _bench_column(
        "Single-Core",
        s_badge,
        s_val,
        s_unit,
        rows=[
            ("Ядро", "1"),
            ("Поток", "1"),
            ("Длительность", f"{result.single.duration_actual_sec:.1f} с"),
        ],
        accent="cyan",
    )

    # ── Правая колонка: Multi ────────────────────────────────────────────
    m_val, m_unit = _format_metric(result.multi.value, result.multi.unit)
    multi_block = _bench_column(
        "Multi-Core",
        _multi_badge(result),
        m_val,
        m_unit,
        rows=[
            ("Ядра", _multi_cores_label(result)),
            ("Потоков", str(result.cores_used_multi)),
            ("Длительность", f"{result.multi.duration_actual_sec:.1f} с"),
        ],
        accent="magenta",
    )

    # Две колонки в Table.grid с вертикальным разделителем посередине.
    body = Table.grid(expand=True, padding=(0, 2))
    body.add_column(ratio=1, justify="center")
    body.add_column(width=1, justify="center")
    body.add_column(ratio=1, justify="center")
    body.add_row(single_block, Text("│", style="dim"), multi_block)

    # ── Сводка под разделителем ──────────────────────────────────────────
    summary = _summary_grid(result)

    parts: list = [body, Rule(style="dim"), summary]
    if ranking is not None:
        parts += [Rule(style="dim"), _ranking_grid(ranking)]
    inner = Group(*parts)
    console.print(
        Panel(
            inner,
            title="[bold]Тест Single-Core / Multi-Core[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _multi_badge(result: SingleMultiResult) -> str:
    """«Все ядра: 16 (P 8 + E 8)» для hybrid, иначе «Все ядра: N»."""
    if result.physical_cores is None:
        return f"Все логические CPU: {result.cores_used_multi}"
    if result.physical_p_cores and result.physical_e_cores:
        return (
            f"Все ядра: {result.physical_cores} "
            f"(P {result.physical_p_cores} + E {result.physical_e_cores})"
        )
    return f"Все ядра: {result.physical_cores}"


def _multi_cores_label(result: SingleMultiResult) -> str:
    """Краткая подпись для строки «Ядра» в Multi-карточке."""
    if result.physical_cores is None:
        return "—"
    if result.physical_p_cores and result.physical_e_cores:
        return (
            f"{result.physical_cores} "
            f"({result.physical_p_cores}P + {result.physical_e_cores}E)"
        )
    return str(result.physical_cores)


def _summary_grid(result: SingleMultiResult) -> Table:
    """Сводка: ускорение, эффективность, ASCII-бар Single vs Multi. Без своей рамки."""
    sp = result.speedup
    ef = result.efficiency

    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold cyan", justify="right", min_width=14)
    body.add_column()

    if sp is not None:
        body.add_row(
            "Ускорение",
            Text.from_markup(f"[bold green]×{sp:.2f}[/]  (Multi / Single)"),
        )
    else:
        body.add_row("Ускорение", Text.from_markup("[dim]не определено[/]"))

    if ef is not None and sp is not None:
        color = _efficiency_color(ef)
        ef_label = _efficiency_label(ef)
        body.add_row(
            "Эффективность",
            Text.from_markup(
                f"[bold {color}]{ef * 100:.0f}%[/]  "
                f"[dim]({ef_label}, ×{sp:.2f} / {result.cores_used_multi} потоков)[/]"
            ),
        )

    # ── ASCII-бар сравнения (Single как доля Multi) ──────────────────────
    if result.multi.value > 0 and result.single.value > 0:
        single_bar = _bar(result.single.value, result.multi.value, accent="cyan")
        multi_bar = _bar(result.multi.value, result.multi.value, accent="magenta")
        body.add_row("", "")  # пустая строка отступа
        body.add_row(
            Text.from_markup("[cyan]Single[/]"),
            Text.assemble(single_bar, Text.from_markup(f"  [cyan]{result.single.value:,.1f}[/]")),
        )
        body.add_row(
            Text.from_markup("[magenta]Multi[/]"),
            Text.assemble(multi_bar, Text.from_markup(f"  [magenta]{result.multi.value:,.1f}[/]")),
        )

    return body


def _efficiency_label(efficiency: float) -> str:
    """Текстовая интерпретация числа efficiency (зелёное/жёлтое/красное)."""
    if efficiency >= 0.80:
        return "отличное масштабирование"
    if efficiency >= 0.60:
        return "хорошее"
    if efficiency >= 0.40:
        return "умеренное"
    return "слабое"


def _percentile_color(percentile: int) -> str:
    """Цвет для рейтинговой позиции: чем ниже percentile, тем зеленее.

    Привязка к мягким порогам ±10%/±25% из плана: топ 25% → green,
    25-75% → yellow, > 75% → red. Логично для «топ N%».
    """
    if percentile <= 25:
        return "green"
    if percentile <= 75:
        return "yellow"
    return "red"


def _ranking_grid(ranking: RankingMatch) -> Table:
    """Секция «Положение среди популярных CPU» под основной сводкой.

    Три режима:
    - exact:        точное совпадение по cpu_pattern.
    - approx_cores: ближайший по топологии (P/E ядрам).
    - none:         CPU не нашёлся в публичной базе.
    Во всех случаях идёт постоянная сноска об ограниченности базы.
    """
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right", min_width=14)
    grid.add_column()

    grid.add_row(
        Text.from_markup("[bold]Положение среди популярных CPU[/]"),
        "",
    )

    total = ranking.total or 0

    if ranking.kind in ("exact", "approx_cores") and ranking.entry is not None:
        entry = ranking.entry
        if ranking.kind == "exact":
            note = "[dim](точное совпадение)[/]"
        else:
            note = (
                f"[dim](ближайший по ядрам, расхождение "
                f"{ranking.core_distance})[/]"
            )
        grid.add_row(
            "CPU",
            Text.from_markup(f"[bold]{entry.display_name}[/]  {note}"),
        )
        if (
            ranking.multi_rank is not None
            and ranking.multi_percentile is not None
        ):
            color = _percentile_color(ranking.multi_percentile)
            grid.add_row(
                "Multi-Core",
                Text.from_markup(
                    f"[bold {color}]Топ {ranking.multi_percentile}%[/]  "
                    f"[dim]({ranking.multi_rank} место из {total})[/]"
                ),
            )
        if (
            ranking.single_rank is not None
            and ranking.single_percentile is not None
        ):
            color = _percentile_color(ranking.single_percentile)
            grid.add_row(
                "Single-Core",
                Text.from_markup(
                    f"[bold {color}]Топ {ranking.single_percentile}%[/]  "
                    f"[dim]({ranking.single_rank} место из {total})[/]"
                ),
            )
    else:
        grid.add_row(
            "CPU",
            Text.from_markup(
                "[yellow]не найден в публичной базе[/] "
                f"[dim](~{total} популярных моделей)[/]"
            ),
        )

    grid.add_row("", "")
    grid.add_row(
        "",
        Text.from_markup(
            "[dim]База: ~"
            f"{total} популярных десктопных CPU (Intel 9–14gen, "
            "AMD Ryzen 3000–7000). Самые новые или специализированные "
            "модели могут отсутствовать. Это ориентир, не точный замер.[/]"
        ),
    )

    return grid


# ──────────────── General Benchmark («Оценки общей производительности») ────────


def render_general_benchmark_report(report: object) -> None:
    """Финальный отчёт комплексного бенчмарка CPU + RAM + Boot-диск.

    Порядок секций:
    1. Заголовок (зелёная rule).
    2. Таблица «Итоги по подсистемам» (box.ROUNDED): подсистема / результат /
       максимум железа / % от максимума. Имя загрузочного диска — в
       заголовке секции «ОС диск», отдельной строки нет.
    3. Panel-пояснение «Что значит % от максимума».
    4. Заметки (notes) — если есть.
    5. Финальная плашка «Ваш итоговый балл».
    6. Panel-пояснение шкалы баллов.
    7. dim-rule — визуальный разделитель перед «Enter — продолжить».

    Принимает ``GeneralBenchmarkReport`` как ``object`` — чтобы не тащить
    cross-domain импорт здесь (тот же паттерн, что у
    ``render_stress_final_report``).
    """
    score = getattr(report, "score", None)
    dgemm_g = getattr(report, "dgemm_gflops", None)
    stream_g = getattr(report, "stream_gb_s", None)
    seq_r = getattr(report, "disk_seq_read_mb_s", None)
    rnd_r = getattr(report, "disk_random_read_mb_s", None)
    seq_w = getattr(report, "disk_seq_write_mb_s", None)
    dgemm_peak = getattr(report, "dgemm_peak_gflops", None)
    stream_peak = getattr(report, "stream_peak_gb_s", None)
    seq_r_peak = getattr(report, "disk_seq_read_peak_mb_s", None)
    rnd_r_peak = getattr(report, "disk_random_read_peak_mb_s", None)
    seq_w_peak = getattr(report, "disk_seq_write_peak_mb_s", None)
    r_dgemm = getattr(report, "r_dgemm", None)
    r_stream = getattr(report, "r_stream", None)
    r_disk = getattr(report, "r_disk", None)
    media_label = getattr(report, "disk_media_label", None) or ""
    disk_model = getattr(report, "disk_model", None)
    cancelled = bool(getattr(report, "cancelled", False))
    notes = list(getattr(report, "notes", []) or [])

    if cancelled:
        console.rule("[bold yellow]ОБЩАЯ ОЦЕНКА: ПРЕРВАНО ПОЛЬЗОВАТЕЛЕМ[/]")
    else:
        console.rule("[bold green]ОБЩАЯ ОЦЕНКА ПРОИЗВОДИТЕЛЬНОСТИ СИСТЕМЫ[/]")

    # ─── Таблица «Итоги по подсистемам» ────────────────────────────────────
    console.print()
    tbl = Table(
        title="[bold]Итоги по подсистемам[/]",
        box=box.ROUNDED,
        header_style="bold cyan",
        title_justify="center",
        pad_edge=True,
        show_lines=False,
    )
    # min_width на «Подсистему» = 28 (хватит для «Запись последовательная»
    # с отступом). На колонки со значениями no_wrap не ставим: при тесной
    # консоли заголовки переносятся, но числа остаются читаемы.
    tbl.add_column("Подсистема", style="bold", min_width=28, no_wrap=True)
    tbl.add_column("Результат", justify="right")
    tbl.add_column("Максимум", justify="right", style="dim")
    tbl.add_column("% от максимума", justify="right", style="bold green")

    def _row(name: str, val: float | None, peak: float | None, unit: str) -> None:
        result_cell = f"{val:.2f} {unit}" if val is not None else "—"
        peak_cell = f"{peak:.0f} {unit}" if peak is not None else "—"
        if val is not None and peak and peak > 0:
            pct = min(val / peak, 1.0) * 100.0
            # Цветим процент по уровню реализации потенциала: красный <40,
            # жёлтый 40-70, зелёный ≥70. Заголовочный bold green из колонки
            # перебивается inline-разметкой.
            if pct >= 70.0:
                util_cell = f"[bold green]{pct:.1f} %[/]"
            elif pct >= 40.0:
                util_cell = f"[bold yellow]{pct:.1f} %[/]"
            else:
                util_cell = f"[bold red]{pct:.1f} %[/]"
        else:
            util_cell = "—"
        tbl.add_row(name, result_cell, peak_cell, util_cell)

    _row("CPU + RAM", dgemm_g, dgemm_peak, "GFLOPS")
    _row("RAM", stream_g, stream_peak, "GB/s")

    # Диск — отдельной секцией с заголовком (модель и тип в имени группы).
    tbl.add_section()
    disk_title_parts = ["ОС диск"]
    if disk_model:
        disk_title_parts.append(disk_model)
    if media_label:
        disk_title_parts.append(media_label)
    disk_title = " · ".join(disk_title_parts)
    # Заголовок-разделитель: одна выделенная ячейка, остальные пустые.
    tbl.add_row(f"[bold yellow]{disk_title}[/]", "", "", "")
    _row("  Чтение последовательное", seq_r, seq_r_peak, "MB/s")
    _row("  Чтение случайное", rnd_r, rnd_r_peak, "MB/s")
    _row("  Запись последовательная", seq_w, seq_w_peak, "MB/s")

    console.print(tbl)

    # ─── Пояснение колонки «% от максимума» ──────────────────────────────
    console.print()
    console.print(
        Panel(
            "[bold]Что показывает «% от максимума»[/]\n"
            "Насколько ваше железо приблизилось к своему теоретическому "
            "пределу. Например, 60 % означает, что подсистема выдала 60 % "
            "от того, что в принципе возможно на данной модели CPU / RAM / "
            "диска. У реальных систем 100 % часто недостижимо — всегда "
            "есть потери на кеш-промахи, прерывания ОС, контроллер памяти."
            "\n\n"
            "[dim]Зелёный ≥ 70 % — хорошая реализация потенциала; "
            "жёлтый 40–70 % — средняя; красный < 40 % — есть резерв.[/]",
            border_style="dim cyan",
            expand=True,
        )
    )

    # ─── Заметки (warnings / errors) ──────────────────────────────────────
    if notes:
        console.print()
        console.print("[bold yellow]Заметки:[/]")
        for n in notes:
            console.print(f"  • {n}")

    # ─── Финальная плашка с баллом ────────────────────────────────────────
    console.print()
    if score is not None:
        score_str = f"{score:,.0f}".replace(",", " ")
        console.print(
            Align.center(
                Panel(
                    f"[bold cyan]{score_str}[/]",
                    title="[bold]Ваш итоговый балл[/]",
                    border_style="bold cyan",
                    padding=(1, 6),
                    expand=False,
                )
            )
        )
    else:
        missing: list[str] = []
        if r_dgemm is None:
            missing.append("CPU")
        if r_stream is None:
            missing.append("RAM")
        if r_disk is None:
            missing.append("диск")
        console.print(
            "[yellow]Итоговый балл недоступен[/] — нет данных по: "
            f"{', '.join(missing) if missing else 'неизвестно'}."
        )

    # ─── Пояснение шкалы — отдельным блоком, не слипается с плашкой ───────
    console.print()
    console.print(
        Panel(
            "[bold]Как читать балл[/]\n"
            "  • [green]7000–10 000[/] — мощная актуальная конфигурация\n"
            "  • [green]5000–7000[/]  — хороший современный десктоп\n"
            "  • [yellow]3500–5000[/]  — средний десктоп / рабочая станция\n"
            "  • [red]< 3500[/]      — слабая, устаревшая или виртуальная среда\n"
            "  • [dim]10 000[/]        — теоретический потолок (на реальном "
            "железе обычно недостижим)",
            title="[bold]Шкала баллов[/]",
            border_style="dim",
            expand=True,
        )
    )

    # ─── Визуальный разделитель перед «Enter — продолжить» ────────────────
    console.print()
    console.rule(style="dim")
