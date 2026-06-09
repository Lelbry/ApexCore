"""CLI-команда ``apexcore sensors`` — новый раздел «Датчики» (M5 issue #3).

Интерактивный TUI поверх ``SensorService``:

- Адаптивный layout (dashboard ≥110 cols / длинная таблица <110).
- Hotkeys (см. ``interfaces/cli/keyboard.py``): Esc/Q/Й — выход, B/И — назад,
  C/С — свернуть per-core, T/Е G/П M/Ь A/Ф — focus modes, P/З — пауза,
  Ctrl+C — стоп.
- Threshold colour-coding (только если железо публикует пороги).
- Throttle с расшифровкой причины.

Старый ``apexcore monitor`` живёт параллельно с deprecation-warning
(см. ``main.py``).
"""

from __future__ import annotations

import contextlib
import time

import typer
from rich.live import Live

from apexcore.application.sensor_service import (
    InMemorySensorBus,
    SensorService,
    empty_sensor_snapshot,
)
from apexcore.domain.sensor_models import SensorSnapshot
from apexcore.infrastructure.adapters import AdapterFactory
from apexcore.infrastructure.disk_inventory import list_physical_disks
from apexcore.infrastructure.sensors import lhm, nvidia_ml, smartctl
from apexcore.interfaces.cli.keyboard import (
    KeyAction,
    KeyboardListener,
    classify_key,
)
from apexcore.interfaces.cli.render import console
from apexcore.interfaces.cli.render_sensors import (
    ViewMode,
    format_active_backends,
    render_sensors_view,
)

# Период перерисовки UI. Семплер тикает свой rate отдельно, UI просто
# показывает последний snapshot — слишком частая перерисовка не нужна.
_UI_REFRESH_SEC = 0.25


def sensors(
    duration: float | None = typer.Option(
        None, "--duration", "-d",
        help="Длительность работы, секунд. По умолчанию — без ограничения (выход по Esc/Q).",
    ),
    rate: float = typer.Option(
        0.5, "--rate", "-r",
        help="Интервал между отсчётами, секунд (минимум 0.05).",
    ),
    once: bool = typer.Option(
        False, "--once",
        help="Снять один снимок и распечатать (без интерактивного TUI).",
    ),
) -> None:
    """Раздел «Датчики»: live-просмотр CPU/GPU/Memory/MB/Storage с группировкой."""
    adapter = AdapterFactory.detect()

    # Подписи устройств: для NVIDIA берём имя из NVML, остальные fallback'и
    # в parse_legacy_key выдадут «NVIDIA GPU» / «Intel GPU» и т.д.
    gpu_devices: dict[str, str] = {}
    try:
        nvml_names = nvidia_ml.read_nvml_device_names()
        if nvml_names:
            first = next(iter(nvml_names.values()))
            gpu_devices["nvml"] = first
            gpu_devices["gpunvidia"] = first  # LHM использует тот же ярлык
    except Exception:
        pass

    # Tjmax-пороги для CPU-температурных threshold_crit.
    tjmax_by_key: dict[str, float] = {}
    try:
        tjmax_by_key = lhm.read_lhm_tjmax() or {}
    except Exception:
        tjmax_by_key = {}

    # Storage metadata: имена дисков из LHM + model/type из smartctl.
    # Снимаем один раз — модели/типы статичны для процесса.
    storage_lhm_names: dict[str, str] = {}
    try:
        storage_lhm_names = lhm.read_lhm_storage_names() or {}
    except Exception:
        storage_lhm_names = {}
    storage_smartctl_info: dict[str, dict[str, str]] = {}
    try:
        storage_smartctl_info = smartctl.read_smartctl_devices_info() or {}
    except Exception:
        storage_smartctl_info = {}

    # Полный список физических дисков (WMI на Windows, lsblk на Linux).
    # Используется чтобы карточка «Диски» показала все накопители, а не
    # только те у которых LHM/smartctl дали температуру.
    try:
        physical_disks = list_physical_disks()
    except Exception:
        physical_disks = []

    if once:
        _print_once(
            adapter,
            gpu_devices=gpu_devices,
            tjmax_by_key=tjmax_by_key,
            storage_lhm_names=storage_lhm_names,
            storage_smartctl_info=storage_smartctl_info,
            physical_disks=physical_disks,
        )
        return

    bus = InMemorySensorBus()
    service = SensorService(
        adapter=adapter,
        bus=bus,
        sampling_rate_sec=rate,
        gpu_devices=gpu_devices,
        tjmax_by_key=tjmax_by_key,
        storage_lhm_names=storage_lhm_names,
        storage_smartctl_info=storage_smartctl_info,
    )

    # Активные backend'ы — снимаем один раз для шапки. Свежесть не критична.
    backends_line: list[str] = []
    try:
        from apexcore.application.diagnostics_sensors import diagnose_sensors

        backends_line = format_active_backends(diagnose_sensors())
    except Exception:
        backends_line = []

    service.start()
    started_at = time.monotonic()
    state = _UIState()

    # Первый renderable для Live — отрендеренный «пустой» вид (рамка с
    # шапкой + footer + «нет данных»), пока семплер ещё не собрал тик.
    initial_view = render_sensors_view(
        empty_sensor_snapshot(),
        [],
        console_width=console.size.width,
        console_height=console.size.height,
        active_backends=backends_line,
        physical_disks=physical_disks,
    )

    try:
        with KeyboardListener() as kb, Live(
            initial_view,
            console=console,
            refresh_per_second=4,
            screen=False,
        ) as live:
            while True:
                # 1) hotkeys
                exit_requested = False
                while kb.has_key():
                    action = classify_key(kb.read_key())
                    if action is None:
                        continue
                    if action is KeyAction.QUIT:
                        # Esc/Q — всегда выход в меню.
                        exit_requested = True
                        break
                    if action is KeyAction.BACK:
                        # B — из focus-режима возврат в OVERVIEW, из OVERVIEW — выход.
                        if state.view_mode != ViewMode.OVERVIEW:
                            state.view_mode = ViewMode.OVERVIEW
                        else:
                            exit_requested = True
                            break
                        continue
                    if action is KeyAction.PAUSE:
                        state.paused = not state.paused
                    elif action is KeyAction.COLLAPSE_CORES:
                        state.collapse_cores = not state.collapse_cores
                    elif action is KeyAction.FOCUS_CPU:
                        state.view_mode = ViewMode.FOCUS_CPU
                    elif action is KeyAction.FOCUS_GPU:
                        state.view_mode = ViewMode.FOCUS_GPU
                    elif action is KeyAction.FOCUS_SYSTEM:
                        state.view_mode = ViewMode.FOCUS_SYSTEM
                    elif action is KeyAction.FOCUS_FANS:
                        state.view_mode = ViewMode.FOCUS_FANS
                    elif action is KeyAction.OVERVIEW:
                        state.view_mode = ViewMode.OVERVIEW
                if exit_requested:
                    break

                # 2) длительность
                if duration is not None and (time.monotonic() - started_at) >= duration:
                    break

                # 3) обновить экран
                latest = service.latest() if not state.paused else state.frozen_latest
                history = service.history()
                if latest is None:
                    latest = empty_sensor_snapshot()
                if state.paused and state.frozen_latest is None:
                    state.frozen_latest = service.latest() or latest
                    latest = state.frozen_latest
                live.update(
                    render_sensors_view(
                        latest,
                        history,
                        console_width=console.size.width,
                        console_height=console.size.height,
                        view_mode=state.view_mode,
                        collapse_cores=state.collapse_cores,
                        paused=state.paused,
                        active_backends=backends_line,
                        physical_disks=physical_disks,
                    )
                )
                if not state.paused:
                    state.frozen_latest = None
                time.sleep(_UI_REFRESH_SEC)
    except KeyboardInterrupt:
        _shutdown(service, None)
        _print_session_summary(service, started_at, interrupted=True)
        return
    # Вышли из loop через break (Esc/Q/B или --duration). Live уже закрылся
    # через `with`-выход. Останавливаем сэмплер и печатаем сводку.
    _shutdown(service, None)
    _print_session_summary(service, started_at, interrupted=False)


# ─── helpers ──────────────────────────────────────────────────────────────


class _UIState:
    __slots__ = ("collapse_cores", "frozen_latest", "paused", "view_mode")

    def __init__(self) -> None:
        self.view_mode: str = ViewMode.OVERVIEW
        self.collapse_cores: bool = False
        self.paused: bool = False
        # Замороженный snapshot для паузы — не теряем «момент» при возобновлении.
        self.frozen_latest: SensorSnapshot | None = None


def _shutdown(service: SensorService, live: Live | None) -> None:
    if live is not None:
        with contextlib.suppress(Exception):
            live.stop()
    with contextlib.suppress(Exception):
        service.stop()


def _print_session_summary(
    service: SensorService,
    started_at: float,
    *,
    interrupted: bool,
) -> None:
    """Короткая сводка после остановки сессии «Датчиков».

    Показывает длительность работы и число собранных снимков. Подсказка
    с hotkeys для следующего запуска уже видна в подвале основного
    меню — здесь её не дублируем.
    """
    duration = time.monotonic() - started_at
    history = service.history()
    samples = len(history)
    reason = "прервано пользователем" if interrupted else "остановлено"
    console.print(
        f"[dim]Сессия «Датчики» {reason}. "
        f"Длительность {duration:.1f} с, снимков: {samples}.[/]"
    )


def _print_once(
    adapter,
    *,
    gpu_devices,
    tjmax_by_key,
    storage_lhm_names,
    storage_smartctl_info,
    physical_disks,
) -> None:
    """Снять один snapshot и распечатать — для скриптов и `--once`."""
    bus = InMemorySensorBus()
    service = SensorService(
        adapter=adapter, bus=bus,
        gpu_devices=gpu_devices,
        tjmax_by_key=tjmax_by_key,
        storage_lhm_names=storage_lhm_names,
        storage_smartctl_info=storage_smartctl_info,
    )
    snap = service.make_snapshot() or empty_sensor_snapshot()
    console.print(
        render_sensors_view(
            snap,
            [snap],
            console_width=console.size.width,
            console_height=console.size.height,
            view_mode=ViewMode.OVERVIEW,
            physical_disks=physical_disks,
        )
    )
