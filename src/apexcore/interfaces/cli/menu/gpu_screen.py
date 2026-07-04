"""Экран меню «Оценка производительности GPU» (OpenCL, Roofline ×10 000).

Три действия:
1. Список обнаруженных GPU-устройств.
2. Полный бенчмарк выбранного GPU (FP32 + FP64 + VRAM + PCIe) — балл до
   10 000 с сохранением в БД. Прогресс отменяем по Ctrl+C.
3. Одиночный замер одной нагрузки (FP32/FP64/VRAM/PCIe) без скоринга.

Если OpenCL/GPU не обнаружен, экран всё равно показывается, но с явным
состоянием «GPU/OpenCL не обнаружен» и подсказкой. Длительности фаз берутся
из пользовательских настроек (``menu_settings.yaml``: ``gpu_compute`` и
``gpu_pcie``), как и у остальных тестов.

История прогонов GPU-бенчмарка отображается в общем разделе «История ваших
тестов» (HistoryScreen), отдельного пункта здесь нет — по образцу
:class:`BenchmarkScreen`.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from apexcore.interfaces.cli.menu import settings_store
from apexcore.interfaces.cli.menu.cancel import cancellable
from apexcore.interfaces.cli.menu.nav import (
    MenuItem,
    NavResult,
    Screen,
    _wait_enter,
    back,
    quit_app,
    stay,
)
from apexcore.interfaces.cli.render import (
    console,
    render_gpu_report,
    render_gpu_stress_report,
)

# Человекочитаемые подписи фаз для прогресс-статуса полного прогона.
_PHASE_LABELS = {
    "fp32": "FP32 (вычисления)",
    "fp64": "FP64 (вычисления)",
    "mem_bandwidth": "Пропускная способность VRAM",
    "pcie_h2d": "PCIe host→device",
    "pcie_d2h": "PCIe device→host",
}

# Псевдонимы нагрузок для одиночного замера (совпадают с GpuWorkloadKind).
_WORKLOAD_ALIASES = {
    "fp32": "fp32",
    "fp64": "fp64",
    "mem": "mem_bandwidth",
    "vram": "mem_bandwidth",
    "pcie": "pcie_h2d",
    "h2d": "pcie_h2d",
    "d2h": "pcie_d2h",
}


class GpuScreen(Screen):
    """Подменю «Оценка производительности GPU»."""

    title = "Оценка производительности GPU"
    subtitle = (
        "Бенчмарк графического процессора через OpenCL: FP32/FP64/VRAM/PCIe, "
        "итоговый балл по Roofline (до 10 000). Кроссвендорный путь."
    )

    def render_extra(self) -> None:
        if not self._backend_available():
            console.print(
                Panel.fit(
                    "[bold yellow]GPU/OpenCL не обнаружен[/]\n"
                    "ICD-loader не загрузился или в системе нет OpenCL-устройств. "
                    "Бенчмарк запустить нельзя.\n\n"
                    "[dim]На дискретных GPU установите свежий драйвер — он "
                    "приносит OpenCL runtime. Проверить список: пункт «1».[/]",
                    border_style="yellow",
                )
            )
            return
        console.print(
            Panel.fit(
                "[bold]Что здесь:[/]\n"
                "  • [cyan]Список устройств[/] — какие GPU видит OpenCL.\n"
                "  • [cyan]Полный бенчмарк[/] — FP32 + FP64 + VRAM + PCIe, "
                "балл до 10 000 (в балл входят FP32 и VRAM).\n"
                "  • [cyan]Стресс-тест[/] — длительная FP32-нагрузка на "
                "термостабильность: нагрев, троттлинг, обвал частоты "
                "(вердикт PASS/WARN/FAIL).\n"
                "  • [cyan]Одиночный замер[/] — сырая скорость одной нагрузки "
                "без скоринга.",
                border_style="cyan",
            )
        )

    def items(self) -> list[MenuItem]:
        items: list[MenuItem] = [
            MenuItem("1", "Список GPU-устройств", self._list),
        ]
        if self._backend_available():
            items.append(
                MenuItem("2", "Полный бенчмарк GPU (FP32 + FP64 + VRAM + PCIe)", self._run_full)
            )
            items.append(
                MenuItem("3", "Стресс-тест GPU (термостабильность)", self._run_stress)
            )
            items.append(
                MenuItem("4", "Одиночный замер одной нагрузки (без балла)", self._run_test)
            )
        items.append(MenuItem("b", "Назад на главный", lambda: back(), accent="dim"))
        items.append(MenuItem("q", "Выход", lambda: quit_app(), accent="red"))
        return items

    # ─── Вспомогательное ───────────────────────────────────────────────────

    def _backend_available(self) -> bool:
        """Доступен ли GPU-бэкенд. Ошибку загрузки трактуем как «нет GPU»."""
        try:
            from apexcore.infrastructure.gpu import build_default_gpu_backend

            return build_default_gpu_backend().is_available()
        except Exception:
            return False

    # ─── 1. Список устройств ────────────────────────────────────────────────

    def _list(self) -> NavResult:
        from apexcore.interfaces.cli.commands.gpu import list_devices

        console.rule("[bold]GPU-устройства[/]")
        list_devices()
        _wait_enter()
        return stay()

    # ─── 2. Полный бенчмарк ─────────────────────────────────────────────────

    def _run_full(self) -> NavResult:
        from apexcore.application.gpu_benchmark import (
            GpuBenchmarkOrchestrator,
            GpuBenchmarkParams,
        )
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.infrastructure.gpu import build_default_gpu_backend
        from apexcore.infrastructure.persistence import (
            SqliteGpuBenchmarkRepository,
        )
        from apexcore.shared.config import load_settings

        backend = build_default_gpu_backend()
        if not backend.is_available():
            return stay("[yellow]GPU/OpenCL не обнаружен — прогон невозможен.[/]")

        devices = backend.list_devices()
        if not devices:
            return stay("[yellow]OpenCL доступен, но устройств не найдено.[/]")

        device_index = self._ask_device(devices)
        if device_index is None:
            return stay()

        s = settings_store.load_menu_settings()
        params = GpuBenchmarkParams(
            fp32_duration_sec=s.durations.gpu_compute,
            fp64_duration_sec=s.durations.gpu_compute,
            mem_duration_sec=s.durations.gpu_compute,
            pcie_duration_sec=s.durations.gpu_pcie,
        )
        total_est = (
            3 * params.fp32_duration_sec
            + 2 * params.pcie_duration_sec
            + 3 * params.cooldown_sec
        )

        console.rule("[bold]Запуск полного GPU-бенчмарка[/]")
        console.print(
            f"[dim]Устройство: {devices[device_index].name}. "
            f"Ожидаемое время ~{total_est:.0f} с "
            "(FP32 → FP64 → VRAM → PCIe с остыванием между фазами).[/]"
        )
        console.print("[bold yellow]Ctrl+C[/] — прервать с сохранением снятых фаз.")
        console.print()

        adapter = AdapterFactory.detect()
        orchestrator = GpuBenchmarkOrchestrator(adapter, backend)

        try:
            with (
                cancellable() as token,
                console.status("[cyan]Прогрев…[/]", spinner="dots") as status,
            ):
                def on_progress(phase: str, idx: int, total: int) -> None:
                    pretty = _PHASE_LABELS.get(phase, phase)
                    status.update(
                        f"[cyan]Фаза {idx}/{total}: [bold]{pretty}[/cyan][/]"
                    )

                report = orchestrator.run(
                    device_index=device_index,
                    params=params,
                    cancel_token=token,
                    on_progress=on_progress,
                )
        except KeyboardInterrupt:
            return stay("[yellow]Бенчмарк отменён пользователем.[/]")

        # Тихо сохраняем в БД до рендера; ошибку кладём в notes отчёта.
        try:
            settings = load_settings()
            repo = SqliteGpuBenchmarkRepository(settings.db_path)
            repo.save(report)
            repo.close()
        except Exception as exc:
            report.notes.append(f"Не удалось сохранить в БД: {exc}")

        console.print()
        render_gpu_report(report)

        _wait_enter()
        return stay()

    # ─── 3. Стресс-тест (термостабильность) ──────────────────────────────────

    def _run_stress(self) -> NavResult:
        from apexcore.application.gpu_stress import GpuStressOrchestrator
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.infrastructure.gpu import build_default_gpu_backend
        from apexcore.infrastructure.persistence import (
            SqliteGpuStressRepository,
        )
        from apexcore.shared.config import load_settings

        backend = build_default_gpu_backend()
        if not backend.is_available():
            return stay("[yellow]GPU/OpenCL не обнаружен — стресс-тест невозможен.[/]")

        devices = backend.list_devices()
        if not devices:
            return stay("[yellow]OpenCL доступен, но устройств не найдено.[/]")

        device_index = self._ask_device(devices)
        if device_index is None:
            return stay()

        s = settings_store.load_menu_settings()
        duration = s.durations.gpu_stress

        console.rule("[bold]Запуск GPU-стресс-теста (термостабильность)[/]")
        console.print(
            f"[dim]Устройство: {devices[device_index].name}. "
            f"Длительность ~{duration:.0f} с "
            "(максимальная FP32-нагрузка + посекундная телеметрия).[/]"
        )
        console.print("[bold yellow]Ctrl+C[/] — прервать с оценкой по снятым данным.")
        console.print()

        adapter = AdapterFactory.detect()
        orchestrator = GpuStressOrchestrator(adapter, backend)

        try:
            with (
                cancellable() as token,
                console.status("[cyan]Прогрев…[/]", spinner="dots") as status,
            ):
                def on_progress(elapsed_sec: float, duration_sec: float) -> None:
                    status.update(
                        f"[cyan]Нагрузка: [bold]{elapsed_sec:.0f}[/] / "
                        f"{duration_sec:.0f} с[/cyan]"
                    )

                report = orchestrator.run(
                    device_index=device_index,
                    duration_sec=duration,
                    cancel_token=token,
                    on_progress=on_progress,
                )
        except KeyboardInterrupt:
            return stay("[yellow]Стресс-тест отменён пользователем.[/]")

        # Тихо сохраняем в БД до рендера; ошибку кладём в notes отчёта.
        try:
            settings = load_settings()
            repo = SqliteGpuStressRepository(settings.db_path)
            repo.save(report)
            repo.close()
        except Exception as exc:
            report.notes.append(f"Не удалось сохранить в БД: {exc}")

        console.print()
        render_gpu_stress_report(report)

        _wait_enter()
        return stay()

    # ─── 4. Одиночный замер ─────────────────────────────────────────────────

    def _run_test(self) -> NavResult:
        from apexcore.domain.gpu import GpuWorkloadKind
        from apexcore.infrastructure.gpu import build_default_gpu_backend

        backend = build_default_gpu_backend()
        if not backend.is_available():
            return stay("[yellow]GPU/OpenCL не обнаружен — замер невозможен.[/]")

        devices = backend.list_devices()
        if not devices:
            return stay("[yellow]OpenCL доступен, но устройств не найдено.[/]")

        device_index = self._ask_device(devices)
        if device_index is None:
            return stay()

        try:
            raw = Prompt.ask(
                "Нагрузка [dim](fp32 / fp64 / mem / pcie=h2d / d2h)[/]",
                default="fp32",
            ).strip().lower()
        except EOFError:
            return stay()
        kind_value = _WORKLOAD_ALIASES.get(raw)
        if kind_value is None:
            return stay(
                f"[red]Неизвестная нагрузка: {raw!r}.[/] "
                "Допустимо: fp32, fp64, mem, pcie, d2h."
            )
        kind = GpuWorkloadKind(kind_value)

        dev = devices[device_index]
        if not backend.supports(device_index, kind):
            return stay(
                f"[yellow]«{dev.name}» не поддерживает нагрузку {kind.value} — "
                "замер пропущен.[/]"
            )

        s = settings_store.load_menu_settings()
        # PCIe-замеры короче compute — берём соответствующую настройку.
        duration = (
            s.durations.gpu_pcie
            if kind in (GpuWorkloadKind.PCIE_H2D, GpuWorkloadKind.PCIE_D2H)
            else s.durations.gpu_compute
        )

        console.rule("[bold]Одиночный замер GPU[/]")
        console.print(
            f"[dim]{kind.value} на «{dev.name}», ~{duration:.1f} с. "
            "Балл не считается.[/]"
        )
        console.print("[bold yellow]Ctrl+C[/] — прервать.")
        console.print()

        try:
            with cancellable() as token, console.status(
                "[cyan]Идёт замер…[/]", spinner="dots"
            ):
                m = backend.measure(device_index, kind, duration, token)
        except KeyboardInterrupt:
            return stay("[yellow]Замер отменён пользователем.[/]")

        if not m.throughput or m.throughput <= 0:
            return stay(
                "[yellow]Нулевой throughput — измерение не удалось "
                "(кернел не запустился?).[/]"
            )

        console.print()
        console.print(
            f"[bold green]{m.throughput:.2f} {m.unit}[/]  "
            f"[dim](итераций: {m.iterations}, факт. время: {m.duration_sec:.2f} с)[/]"
        )
        _wait_enter()
        return stay()

    # ─── Выбор устройства ───────────────────────────────────────────────────

    def _ask_device(self, devices: list) -> int | None:
        """Спросить индекс устройства. Возвращает None при отмене/ошибке ввода.

        Если устройство одно — выбираем его без вопроса.
        """
        if len(devices) == 1:
            return 0
        tbl = Table(show_header=False, box=None)
        tbl.add_column(style="bold cyan", justify="right")
        tbl.add_column()
        for d in devices:
            vram_gb = (d.global_mem_mb or 0) / 1024.0
            suffix = f"  [dim]({vram_gb:.0f} ГБ VRAM)[/]" if d.global_mem_mb else ""
            tbl.add_row(str(d.index), f"{d.name}{suffix}")
        console.print(tbl)
        try:
            raw = Prompt.ask("Индекс устройства", default="0").strip()
        except EOFError:
            return None
        if not raw.isdigit():
            console.print("[red]Введите номер устройства из списка.[/]")
            return None
        idx = int(raw)
        if idx < 0 or idx >= len(devices):
            console.print(f"[red]Номер вне диапазона: 0…{len(devices) - 1}.[/]")
            return None
        return idx


__all__ = ["GpuScreen"]
