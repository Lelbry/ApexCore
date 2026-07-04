"""Конкретные экраны меню apexcore.

Дерево экранов
--------------
- ``HomeScreen``                — главный экран
- ``CpuTestsScreen``            — расширенное тестирование процессора
- ``SelectMicroTestsScreen``    — экран выбора подмножества тестов CPU
- ``RamCacheScreen``            — расширенный тест ОЗУ и кеша
- ``SelectRamCacheTestsScreen`` — экран выбора подмножества тестов Ram&Cache
- ``StressScreen``              — стресс-тесты и полный прогон
- ``WinsatScreen``              — Аналог Windows Winsat (только Windows; в отдельном модуле)
- ``HistoryScreen``             — единая лента прогонов (stress / scoring v2 / winsat)
- ``SettingsScreen``            — настройки (длительности, потоки, путь)
- ``DurationsScreen``           — выбор программы для изменения длительности
"""

from __future__ import annotations

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from apexcore.interfaces.cli.menu import settings_store
from apexcore.interfaces.cli.menu.cancel import cancellable
from apexcore.interfaces.cli.menu.nav import (
    MenuItem,
    NavResult,
    Screen,
    _confirm,
    _wait_enter,
    back,
    push,
    quit_app,
    stay,
)
from apexcore.interfaces.cli.render import console

# ─────────────────────────── HOME ──────────────────────────────────────────


class HomeScreen(Screen):
    title = "Главный экран"
    subtitle = "apexcore — оценка производительности компьютера (Windows / Astra Linux)"

    def items(self) -> list[MenuItem]:
        items: list[MenuItem] = [
            MenuItem("1", "Информация о системе", self._info),
            MenuItem("2", "Датчики (мониторинг в реальном времени)", self._sensors),
            MenuItem("3", "Стресс-тест системы", self._stress),
            MenuItem(
                "4",
                "Общая оценка производительности системы",
                self._benchmark,
            ),
            MenuItem("5", "Оценка производительности GPU", self._gpu),
            MenuItem("6", "Расширенное тестирование процессора", self._cpu),
            MenuItem(
                "7",
                "Расширенный тест оперативной памяти и кеша (Ram & CPU Cache)",
                self._ram_cache,
            ),
            MenuItem("8", "История ваших тестов", self._history),
            MenuItem("9", "Web UI (localhost)", self._webui),
            MenuItem("10", "Настройки", self._settings),
            MenuItem("q", "Выход", self._quit, accent="red"),
        ]
        return items

    def _info(self) -> NavResult:
        from apexcore.interfaces.cli.commands.info import info as info_fn

        console.rule("[bold]Информация о системе[/]")
        info_fn()
        _wait_enter()
        return stay()

    def _sensors(self) -> NavResult:
        """Раздел «Датчики» (M5) — live-просмотр с группировкой и hotkeys."""
        from apexcore.interfaces.cli.commands.sensors import sensors as sensors_fn

        s = settings_store.load_menu_settings()
        console.rule("[bold]Датчики[/]")
        console.print(
            f"[dim]Шаг опроса {s.sampling_rate_sec:.2f} с (см. Настройки).[/]"
        )
        try:
            # Без --duration — пользователь сам управляет временем через hotkeys.
            sensors_fn(duration=None, rate=s.sampling_rate_sec, once=False)
        except KeyboardInterrupt:
            console.print("[yellow]Датчики: остановлено пользователем[/]")
        _wait_enter()
        return stay()

    def _cpu(self) -> NavResult:
        return push(CpuTestsScreen())

    def _ram_cache(self) -> NavResult:
        return push(RamCacheScreen())

    def _stress(self) -> NavResult:
        return push(StressScreen())

    def _winsat(self) -> NavResult:
        # Сохранён для обратной совместимости (тесты, прямой вызов из CLI).
        # В главном меню пункт переехал в BenchmarkScreen.
        from apexcore.interfaces.cli.menu.winsat_screen import WinsatScreen

        return push(WinsatScreen())

    def _benchmark(self) -> NavResult:
        from apexcore.interfaces.cli.menu.benchmark_screen import BenchmarkScreen

        return push(BenchmarkScreen())

    def _gpu(self) -> NavResult:
        from apexcore.interfaces.cli.menu.gpu_screen import GpuScreen

        return push(GpuScreen())

    def _history(self) -> NavResult:
        return push(HistoryScreen())

    def _webui(self) -> NavResult:
        from apexcore.interfaces.cli.commands.webui import webui as webui_fn

        console.rule("[bold]Web UI[/]")
        port = _ask_int("Порт", 8765)
        console.print(f"[cyan]После запуска открой: http://127.0.0.1:{port}[/]")
        console.print("[dim]Ctrl+C — остановить сервер[/]")
        try:
            webui_fn(host="127.0.0.1", port=port, reload=False)
        except KeyboardInterrupt:
            console.print("[yellow]Сервер остановлен[/]")
        _wait_enter()
        return stay()

    def _settings(self) -> NavResult:
        return push(SettingsScreen())

    def _quit(self) -> NavResult:
        if _confirm("Выйти из apexcore? (y/n)"):
            return quit_app()
        return stay()


# ─────────────────────────── CPU ───────────────────────────────────────────


class CpuTestsScreen(Screen):
    title = "Расширенное тестирование процессора"
    subtitle = (
        "12 микробенчмарков: память, FLOPS, IOPS, AES-256, SHA-1, фракталы. "
        "Аналог CPU-таблиц в AIDA64 / Phoronix."
    )

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("1", "Посмотреть список тестов", self._list),
            MenuItem("2", "Запустить все тесты", self._run_all),
            MenuItem("3", "Запустить выбранные тесты", self._run_some),
            MenuItem("4", "Тест Single-Core / Multi-Core", self._run_single_multi),
            MenuItem("b", "Назад на главный", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def _list(self) -> NavResult:
        from apexcore.interfaces.cli.commands.micro import list_tests

        console.rule("[bold]Список расширенных тестов CPU[/]")
        list_tests()
        _wait_enter()
        return stay()

    def _run_all(self) -> NavResult:
        return self._do_run(selected=None)

    def _run_some(self) -> NavResult:
        return push(SelectMicroTestsScreen(parent=self))

    def _run_single_multi(self) -> NavResult:
        """Запустить парный замер Single-Core (1 поток, прибит к P-ядру)
        и Multi-Core (все логические CPU) на одном бенче.

        После показа результата спрашиваем — Enter (назад в меню) или
        r (повторить тест). Полезно для оценки стабильности результата
        и наблюдения за разогревом кулера.
        """
        from rich.prompt import Prompt

        from apexcore.application.cpu_ranking import match_cpu_ranking
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.interfaces.cli.render import render_single_multi_result

        # SystemInfo нужен только для матчинга CPU — берём один раз,
        # WMI/registry на Windows стоят дороже, чем сам матч.
        try:
            sys_info = AdapterFactory.detect().get_system_info()
        except Exception as exc:
            console.log(f"[dim]get_system_info failed: {exc}[/]")
            sys_info = None

        while True:
            outcome = self._single_multi_one_pass()
            if isinstance(outcome, NavResult):
                return outcome

            ranking = None
            if sys_info is not None:
                try:
                    ranking = match_cpu_ranking(
                        sys_info.cpu_model, sys_info.cpu_cores
                    )
                except Exception as exc:
                    console.log(f"[dim]match_cpu_ranking failed: {exc}[/]")

            console.print()
            render_single_multi_result(outcome, ranking=ranking)
            console.print()
            try:
                choice = Prompt.ask(
                    "[dim]Enter — назад в меню, [bold cyan]r[/] — повторить тест[/]",
                    default="",
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return stay()
            # Поддерживаем русскую раскладку: 'к' и 'К' — это та же клавиша r.
            if choice in {"r", "к"}:
                console.clear()
                continue
            return stay()

    def _single_multi_one_pass(self):
        """Один прогон Single/Multi. Возвращает ``SingleMultiResult`` либо
        ``NavResult`` (если прерывание/ошибка — сразу обратно в меню)."""
        import psutil

        from apexcore.application.single_multi_compare import run_single_multi_compare
        from apexcore.infrastructure.microbench.integer import Int64IopsBench

        s = settings_store.load_menu_settings()
        duration = s.durations.single_multi
        total_threads = psutil.cpu_count(logical=True) or 1

        console.rule("[bold]Тест Single-Core / Multi-Core[/]")
        console.print(
            "[dim]Два замера CPU: сначала 1 поток на одном P-ядре, "
            f"потом {total_threads} потоков на все ядра. "
            f"По ~{duration:.1f} с каждый (изменить можно в «Настройках»).[/]"
        )
        console.print("[bold yellow]Ctrl+C[/] — отменить и вернуться в меню.")
        console.print()

        bench = Int64IopsBench()
        try:
            with cancellable() as token, console.status(
                "[cyan]Прогрев бенча…[/]", spinner="dots"
            ) as status:
                def progress(stage: str) -> None:
                    status.update(f"[cyan]Замер: [bold]{stage}[/cyan][/]")

                return run_single_multi_compare(
                    bench=bench,
                    duration_sec=duration,
                    total_threads=total_threads,
                    cancel_token=token,
                    progress_cb=progress,
                )
        except KeyboardInterrupt:
            return stay("[yellow]Тест отменён пользователем.[/]")
        except Exception as exc:
            return stay(f"[red]Ошибка теста: {exc}[/]")

    def _do_run(self, selected: set[str] | None) -> NavResult:
        from apexcore.interfaces.cli.render import render_microbench_suite

        suite = self._run_pass(selected)
        if suite is None:
            return stay("[red]Нет тестов для выполнения.[/]")
        console.print()
        render_microbench_suite(suite)
        if any(r.error == "отменено пользователем" for r in suite.results):
            console.print("[yellow]Прогон был отменён пользователем.[/]")
        _wait_enter()
        return stay()

    def _run_pass(self, selected: set[str] | None):
        """Один прогон выбранных тестов. Возвращает Suite или None.

        В отличие от ``_do_run``, не делает рендер таблицы и не ждёт Enter —
        используется ``SelectMicroTestsScreen`` для накопительного цикла.
        """
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.infrastructure.microbench import build_default_microbench_registry
        from apexcore.interfaces.cli.menu.runners import run_microbench_suite

        s = settings_store.load_menu_settings()
        tests = build_default_microbench_registry()
        if selected is not None:
            tests = [t for t in tests if t.name in selected]
        if not tests:
            return None

        adapter = AdapterFactory.detect()
        sys_info = adapter.get_system_info()

        n = len(tests)
        total_estimate = s.durations.micro * n
        console.rule(f"[bold]Запуск {n} тестов[/]")
        console.print(
            f"[dim]≈ {total_estimate:.0f} с суммарно, по {s.durations.micro:.1f} с на тест. "
            f"CPU: {sys_info.cpu_model}.[/]"
        )
        console.print(
            "[bold yellow]Ctrl+C[/] — отменить выполнение и вернуться в меню."
        )
        console.print()

        with cancellable() as token:
            return run_microbench_suite(
                tests=tests,
                duration_sec=s.durations.micro,
                threads=s.threads,
                sys_info=sys_info,
                cancel_token=token,
            )


class SelectMicroTestsScreen(Screen):
    title = "Выбор тестов CPU"
    subtitle = (
        "Введи номера через запятую (1,2), диапазон (1-3) или имена тестов "
        "и нажми Enter."
    )

    def __init__(self, parent: CpuTestsScreen) -> None:
        super().__init__()
        self._parent = parent
        # Накопленные результаты по name (последний прогон перезаписывает старый).
        self._accumulated: dict = {}
        self._sys_info = None
        self._duration_sec_per_test: float | None = None
        self._threads: int | None = None
        self._first_start = None

    def _names(self) -> list[str]:
        from apexcore.infrastructure.microbench import build_default_microbench_registry

        return [t.name for t in build_default_microbench_registry()]

    def render_extra(self) -> None:
        names = self._names()
        tbl = Table(show_header=False, box=None)
        tbl.add_column(style="bold cyan")
        tbl.add_column()
        for i, name in enumerate(names, 1):
            tbl.add_row(f"{i}", name)
        console.print(tbl)
        console.print(
            "[dim]Введи номера через запятую (1,2), диапазон (1-3) или имена "
            "тестов и нажми Enter. Пустой Enter — назад.[/]"
        )

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("b", "Назад", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def _parse_input(self, choice: str, names: list[str]) -> set[str]:
        wanted: set[str] = set()
        for token in choice.split(","):
            t = token.strip()
            if not t:
                continue
            rng = _parse_range_token(t, len(names))
            if rng is not None:
                for idx in rng:
                    wanted.add(names[idx])
                continue
            if t.isdigit():
                idx = int(t) - 1
                if 0 <= idx < len(names):
                    wanted.add(names[idx])
            elif t in names:
                wanted.add(t)
        return wanted

    def handle_unknown_input(self, choice: str) -> NavResult | None:
        names = self._names()
        if not choice.strip():
            return back()
        selected = self._parse_input(choice, names)
        if not selected:
            return stay("[yellow]Не распознано ни одного имени теста.[/]")
        return self._run_loop(selected, names)

    def _run_loop(self, initial_selected: set[str], names: list[str]) -> NavResult:
        from apexcore.domain.models import MicroBenchSuiteResult
        from apexcore.interfaces.cli.render import render_microbench_suite

        selected = initial_selected
        while True:
            try:
                suite = self._parent._run_pass(selected)
            except KeyboardInterrupt:
                return stay("[yellow]Прогон отменён пользователем[/]")
            if suite is None:
                return stay("[red]Нет тестов для выполнения.[/]")
            for r in suite.results:
                self._accumulated[r.name] = r
            if self._sys_info is None:
                self._sys_info = suite.system_info
                self._duration_sec_per_test = suite.duration_sec_per_test
                self._threads = suite.threads
                self._first_start = suite.start_time

            ordered = [self._accumulated[n] for n in names if n in self._accumulated]
            merged = MicroBenchSuiteResult(
                system_info=self._sys_info,
                results=ordered,
                start_time=self._first_start,
                end_time=suite.end_time,
                duration_sec_per_test=self._duration_sec_per_test or 0.0,
                threads=self._threads or 0,
            )
            self._redraw_screen()
            render_microbench_suite(merged)
            console.print()
            if any(r.error == "отменено пользователем" for r in suite.results):
                console.print("[yellow]Последний прогон был отменён пользователем.[/]")
            try:
                raw = Prompt.ask(
                    "Тесты или Enter — назад",
                    default="",
                ).strip()
            except EOFError:
                return back()
            if not raw:
                return back()
            new_selected = self._parse_input(raw, names)
            if not new_selected:
                console.print("[yellow]Не распознано ни одного имени теста.[/]")
                continue
            selected = new_selected

    def _redraw_screen(self) -> None:
        console.clear()
        self.render_header(self._breadcrumbs_at_push)
        self.render_extra()
        console.print()
        self.render_items()
        console.print()


# ─────────────────────────── RAM&CACHE ─────────────────────────────────────


class RamCacheScreen(Screen):
    title = "Расширенный тест оперативной памяти и кеша (Ram&Cache)"
    subtitle = (
        "16 измерений: Read / Write / Copy / Latency для DRAM и L1/L2/L3 кеша процессора. "
        "Подходит для оценки пропускной способности и задержек подсистемы памяти."
    )

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("1", "Посмотреть список тестов", self._list),
            MenuItem("2", "Запустить все тесты", self._run_all),
            MenuItem("3", "Запустить выбранные тесты", self._run_some),
            MenuItem("b", "Назад на главный", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def _list(self) -> NavResult:
        from apexcore.interfaces.cli.commands.ram_cache import list_tests

        console.rule("[bold]Список измерений ОЗУ и кеша[/]")
        list_tests()
        _wait_enter()
        return stay()

    def _run_all(self) -> NavResult:
        return self._do_run(selected=None)

    def _run_some(self) -> NavResult:
        return push(SelectRamCacheTestsScreen(parent=self))

    def _do_run(self, selected: set | None) -> NavResult:
        from apexcore.interfaces.cli.render import render_ram_cache_report

        report = self._run_pass(selected)
        if report is None:
            return stay("[red]Нет тестов для выполнения.[/]")
        console.print()
        render_ram_cache_report(report)
        if report.cancelled:
            console.print("[yellow]Прогон был отменён пользователем.[/]")
        _wait_enter()
        return stay()

    def _run_pass(self, selected: set | None):
        """Один прогон выбранных измерений. Возвращает RamCacheReport или None.

        В отличие от ``_do_run``, не делает рендер таблицы и не ждёт Enter —
        используется ``SelectRamCacheTestsScreen`` для накопительного цикла.
        """
        from apexcore.application.ram_cache_service import RamCacheService
        from apexcore.infrastructure.adapters import AdapterFactory

        s = settings_store.load_menu_settings()
        adapter = AdapterFactory.detect()
        service = RamCacheService(adapter)

        n = 16 if selected is None else len(selected)
        if n == 0:
            return None
        total_estimate = s.durations.ram_cache * n
        console.rule(f"[bold]Запуск {n} измерений[/]")
        console.print(
            f"[dim]≈ {total_estimate:.0f} с суммарно, по {s.durations.ram_cache:.1f} с "
            f"на измерение. CPU: {adapter.get_system_info().cpu_model}.[/]"
        )
        console.print("[bold yellow]Ctrl+C[/] — отменить (уже снятые значения сохранятся).")
        console.print()

        from apexcore.interfaces.cli.render import render_ram_cache_progress

        progress, advance, finish = render_ram_cache_progress(n)
        with cancellable() as token, progress:
            report = service.run(
                duration_sec_per_metric=s.durations.ram_cache,
                cancel_token=token,
                on_progress=advance,
                selected_pairs=selected,
            )
            finish(report.cancelled)
            return report


class SelectRamCacheTestsScreen(Screen):
    title = "Выбор тестов Ram&Cache"
    subtitle = (
        "Введи номера через запятую (1,3), диапазон (1-3) или имена тестов "
        "(например dram_latency) и нажми Enter."
    )

    def __init__(self, parent: RamCacheScreen) -> None:
        super().__init__()
        self._parent = parent
        # Накопленные метрики по (level, operation).
        self._accumulated: dict = {}
        self._sys_info = None
        self._topology = None
        self._backend_default = None
        self._duration_sec_per_metric: float | None = None
        self._first_start = None

    def _names(self) -> list[str]:
        from apexcore.application.ram_cache_service import all_test_names

        return all_test_names()

    def render_extra(self) -> None:
        names = self._names()
        tbl = Table(show_header=False, box=None)
        tbl.add_column(style="bold cyan", justify="right")
        tbl.add_column()
        for i, name in enumerate(names, 1):
            tbl.add_row(f"{i}", name)
        console.print(tbl)
        console.print(
            "[dim]Введи номера через запятую (1,3), диапазон (1-3) или имена "
            "тестов (например dram_latency) и нажми Enter. Пустой Enter — назад.[/]"
        )

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("b", "Назад", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def _parse_input(self, choice: str, names: list[str]) -> tuple[set, list[str]]:
        """Возвращает (wanted_pairs, unknown_tokens)."""
        from apexcore.application.ram_cache_service import parse_test_name

        wanted: set = set()
        unknown: list[str] = []
        for token in choice.split(","):
            t = token.strip()
            if not t:
                continue
            rng = _parse_range_token(t, len(names))
            if rng is not None:
                for idx in rng:
                    pair = parse_test_name(names[idx])
                    if pair is not None:
                        wanted.add(pair)
                continue
            if t.isdigit():
                idx = int(t) - 1
                if 0 <= idx < len(names):
                    pair = parse_test_name(names[idx])
                    if pair is not None:
                        wanted.add(pair)
                else:
                    unknown.append(t)
                continue
            pair = parse_test_name(t)
            if pair is None:
                unknown.append(t)
            else:
                wanted.add(pair)
        return wanted, unknown

    def handle_unknown_input(self, choice: str) -> NavResult | None:
        names = self._names()
        if not choice.strip():
            return back()
        wanted, unknown = self._parse_input(choice, names)
        if unknown:
            return stay(
                f"[yellow]Не распознаны: {', '.join(unknown)}. "
                f"См. полный список через пункт «1» родительского экрана.[/]"
            )
        if not wanted:
            return stay("[yellow]Не выбран ни один тест.[/]")
        return self._run_loop(wanted, names)

    def _run_loop(self, initial_selected: set, names: list[str]) -> NavResult:
        from apexcore.domain.cache import RamCacheReport
        from apexcore.interfaces.cli.render import render_ram_cache_report

        selected = initial_selected
        while True:
            try:
                report = self._parent._run_pass(selected)
            except KeyboardInterrupt:
                return stay("[yellow]Прогон отменён пользователем[/]")
            if report is None:
                return stay("[red]Нет тестов для выполнения.[/]")
            for m in report.metrics:
                self._accumulated[(m.level, m.operation)] = m
            if self._sys_info is None:
                self._sys_info = report.system_info
                self._topology = report.topology
                self._backend_default = report.backend_default
                self._duration_sec_per_metric = report.duration_sec_per_metric
                self._first_start = report.started_at

            ordered_metrics = list(self._accumulated.values())
            merged = RamCacheReport(
                system_info=self._sys_info,
                topology=self._topology,
                metrics=ordered_metrics,
                started_at=self._first_start,
                ended_at=report.ended_at,
                duration_sec_per_metric=self._duration_sec_per_metric or 0.0,
                backend_default=self._backend_default,
                cancelled=report.cancelled,
            )
            self._redraw_screen()
            render_ram_cache_report(merged)
            console.print()
            if report.cancelled:
                console.print("[yellow]Последний прогон был отменён пользователем.[/]")
            try:
                raw = Prompt.ask(
                    "Тесты или Enter — назад",
                    default="",
                ).strip()
            except EOFError:
                return back()
            if not raw:
                return back()
            new_selected, new_unknown = self._parse_input(raw, names)
            if new_unknown:
                console.print(
                    f"[yellow]Не распознаны: {', '.join(new_unknown)}.[/]"
                )
                continue
            if not new_selected:
                console.print("[yellow]Не выбран ни один тест.[/]")
                continue
            selected = new_selected

    def _redraw_screen(self) -> None:
        console.clear()
        self.render_header(self._breadcrumbs_at_push)
        self.render_extra()
        console.print()
        self.render_items()
        console.print()


# ─────────────────────────── STRESS ────────────────────────────────────────


class StressScreen(Screen):
    title = "Стресс-тест системы"
    subtitle = (
        "Параллельная нагрузка CPU + RAM с термальной защитой. "
        "Аналог AIDA64 System Stability Test / Prime95 Blend."
    )

    def items(self) -> list[MenuItem]:
        from apexcore.interfaces.cli.menu.stress_menu import (
            is_safety_disabled_next_run,
        )

        if is_safety_disabled_next_run():
            safety_label = (
                "[bold red]Термальная защита: ✗ ВЫКЛ на следующий прогон[/]"
            )
        else:
            safety_label = "Термальная защита: [green]✓ вкл[/]"

        return [
            MenuItem(
                "1",
                "Стресс-тест на время  (указать минуты)",
                self._run_timed,
            ),
            MenuItem(
                "2",
                "Бесконечный стресс-тест  (для оценки стабильности; Ctrl+C — стоп)",
                self._run_infinite,
            ),
            MenuItem("3", "Доступные движки нагрузки на этой ОС", self._engines),
            MenuItem("4", safety_label, self._toggle_safety),
            MenuItem("5", "История прогонов нагрузки", self._history),
            MenuItem("b", "Назад на главный", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    # ─── 1. Стресс-нагрузка на время ───────────────────────────────────────

    def _run_timed(self) -> NavResult:
        from apexcore.interfaces.cli.menu.stress_menu import run_timed_stress

        try:
            raw = Prompt.ask(
                "Сколько минут запускать нагрузку",
                default="10",
            )
        except EOFError:
            return back()
        try:
            minutes = float(raw.strip().replace(",", "."))
        except ValueError:
            return stay("[red]Введите число (например, 10 или 1.5).[/]")
        if minutes < 0.1:
            return stay("[red]Минимум — 0.1 мин (6 секунд).[/]")
        if minutes > 24 * 60:
            return stay("[red]Максимум — 24 часа (1440 мин).[/]")

        run_timed_stress(minutes)
        _wait_enter()
        return stay()

    # ─── 2. Бесконечная стресс-нагрузка ────────────────────────────────────

    def _run_infinite(self) -> NavResult:
        from apexcore.interfaces.cli.menu.stress_menu import run_infinite_stress

        run_infinite_stress()
        _wait_enter()
        return stay()

    # ─── 3. Список движков ─────────────────────────────────────────────────

    def _engines(self) -> NavResult:
        from apexcore.interfaces.cli.menu.stress_menu import show_engines_table

        show_engines_table()
        _wait_enter()
        return stay()

    # ─── 4. Toggle термальной защиты ───────────────────────────────────────

    def _toggle_safety(self) -> NavResult:
        from apexcore.interfaces.cli.menu.stress_menu import (
            is_safety_disabled_next_run,
            toggle_safety_disabled_next_run,
        )

        # Если уже выключена — включаем обратно без подтверждений
        # (это безопасное действие).
        if is_safety_disabled_next_run():
            toggle_safety_disabled_next_run()
            return stay("[green]Термальная защита включена обратно.[/]")

        # Включена — выключаем с двойным подтверждением.
        console.print()
        console.print(
            "[bold red]ВНИМАНИЕ:[/] вы собираетесь отключить ВСЕ защиты на "
            "следующий прогон стресс-нагрузки:\n"
            "  • Термальный watchdog (авто-стоп при перегреве CPU)\n"
            "  • Cooling-sanity (предупреждение о проблеме охлаждения)\n"
            "  • Pre-flight проверки (батарея / виртуализация / свободная RAM)\n"
            "  • Лимит температуры GPU\n\n"
            "После одного прогона защиты автоматически включатся обратно."
        )
        if not _confirm("Действительно отключить? (y/n)"):
            return stay("[yellow]Отмена.[/]")
        if not _confirm(
            "ПОДТВЕРДИТЕ ЕЩЁ РАЗ — это может повредить оборудование. (y/n)"
        ):
            return stay("[yellow]Отмена.[/]")
        toggle_safety_disabled_next_run()
        return stay(
            "[bold red]Защита отключена на следующий прогон.[/] "
            "Состояние видно в пункте меню."
        )

    # ─── 5. История ─────────────────────────────────────────────────────────

    def _history(self) -> NavResult:
        return push(HistoryScreen())


# ─────────────────────────── HISTORY ───────────────────────────────────────


class HistoryScreen(Screen):
    """Единый экран ленты прогонов: stress + Тест CPU (scoring v2) + winsat.

    UX (итерация 2):
    - При входе сразу таблица с нумерацией 1..N (по умолчанию 5 строк).
    - Ввод числа → детали прогона с интерактивным footer'ом (экспорт + назад).
    - `m` / `ь` — показать больше (+5 к лимиту); `r` / `к` — обновить;
      `b` / `и` — назад; `q` / `й` — выход (RU-эквиваленты на тех же клавишах).
    - Экспорт убран с ленты и доступен из режима просмотра деталей —
      `s` (JSON) / `s csv` (CSV) одним нажатием в текущую папку.

    Подменю-обёртки над compare/diagnose/trend убраны в итерации 1 (issues
    #10/#11/#12) — эти возможности переедут в будущий web-фронт.
    """

    title = "История прогонов"
    subtitle = "Просматривайте сохранённые прогоны и экспортируйте отчёты"

    # Дефолт 5 — компактно для первого взгляда; «Показать больше» (+5)
    # инкрементит этот лимит сколько угодно раз.
    _DEFAULT_LIMIT = 5
    _MORE_STEP = 5

    # RU-эквиваленты на тех же физических клавишах (как у глобальных
    # BACK_KEYS/QUIT_KEYS из nav.py). `b`/`q`/`h` уже принимаются каркасом.
    _REFRESH_KEYS = frozenset({"r", "к"})
    _MORE_KEYS = frozenset({"m", "ь"})
    # Опции экспорта внутри режима деталей.
    _EXPORT_JSON_KEYS = frozenset({"s", "ы"})
    _EXPORT_CSV_KEYS = frozenset({"s csv", "ы csv"})

    def __init__(self) -> None:
        super().__init__()
        self._limit = self._DEFAULT_LIMIT
        self._listing: list = []  # list[RunRef], заполняется в render_extra

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("m", f"Показать больше (+{self._MORE_STEP})", self._more),
            MenuItem("r", "Обновить ленту", self._refresh, accent="cyan"),
            MenuItem("b", "Назад на главный", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def render_extra(self) -> None:
        from apexcore.interfaces.cli.commands.runs import (
            collect_unified_listing,
            render_unified_listing,
        )

        self._listing = collect_unified_listing(limit=self._limit)
        render_unified_listing(self._listing)
        if self._listing:
            # Пояснение разных шкал — снимает удивление «почему у Стресса 2000,
            # а у Теста CPU 239».
            console.print()
            console.print(
                "[dim]Шкалы: Общая оценка ~4000–8000  ·  Стресс ~1500–3500  ·  "
                "Тест CPU 0–1000  ·  Winsat 1.0–9.9[/]"
            )
            # Пустая строка отделяет инструкцию от пояснения и приближает её
            # к prompt-у каркаса меню — чтобы взгляд не «слипал» две dim-строки.
            console.print()
            console.print("[bold cyan]Введите номер прогона[/]")

    def handle_unknown_input(self, choice: str):
        """Перехват ввода, не совпавшего с пунктами меню:

        - число → детали соответствующего прогона;
        - ``к`` → обновить ленту (RU-эквивалент `r`);
        - ``ь`` → показать больше (RU-эквивалент `m`).
        """
        text = choice.strip().lower()
        if not text:
            return None
        if text in self._REFRESH_KEYS:
            return self._refresh()
        if text in self._MORE_KEYS:
            return self._more()
        if text.isdigit():
            return self._show_by_index(int(text))
        return None

    def _refresh(self) -> NavResult:
        return stay("[dim]Лента обновлена.[/]")

    def _more(self) -> NavResult:
        self._limit += self._MORE_STEP
        return stay(f"[dim]Лимит ленты: {self._limit}.[/]")

    def _show_by_index(self, idx: int) -> NavResult:
        """Открыть детали прогона №idx + интерактивный footer.

        Footer-цикл: пользователь может несколько раз сохранять отчёт в разные
        форматы, не выходя из деталей; пустой Enter / b / и → назад к ленте.
        """
        from apexcore.interfaces.cli.commands.runs import (
            export_run_by_ref,
            show_run_by_ref,
        )

        if not self._listing:
            return stay("[yellow]Лента пуста.[/]")
        if idx < 1 or idx > len(self._listing):
            return stay(
                f"[yellow]Номер вне диапазона: 1…{len(self._listing)}[/]"
            )
        ref = self._listing[idx - 1]
        console.rule(f"[bold]Прогон №{idx}[/]")
        show_run_by_ref(ref)

        flash: str | None = None
        while True:
            console.print()
            try:
                ans = Prompt.ask(
                    "[dim][bold]Enter[/] — назад  ·  "
                    "[bold]s[/] — сохранить JSON  ·  "
                    "[bold]s csv[/] — сохранить CSV[/]",
                    default="",
                )
            except EOFError:
                return stay(flash)
            text = ans.strip().lower()
            # Пустой ввод или явное «назад» → к ленте.
            if not text or text in {"b", "и", "back", "назад"}:
                return stay(flash)
            if text in self._EXPORT_CSV_KEYS:
                path = export_run_by_ref(ref, fmt="csv", out=None)
                if path is not None:
                    flash = f"[green]Сохранено: {path}[/]"
                continue
            if text in self._EXPORT_JSON_KEYS:
                path = export_run_by_ref(ref, fmt="json", out=None)
                if path is not None:
                    flash = f"[green]Сохранено: {path}[/]"
                continue
            console.print(
                "[yellow]Неизвестная команда.[/] Enter — назад, "
                "s — JSON, s csv — CSV."
            )


# ─────────────────────────── SETTINGS ──────────────────────────────────────


class SettingsScreen(Screen):
    title = "Настройки"
    subtitle = "Длительности тестов, потоки, путь к файлу настроек"

    def render_extra(self) -> None:
        s = settings_store.load_menu_settings()
        defaults = settings_store.MenuSettings()
        path = settings_store.settings_path()
        tbl = Table(title="Текущие значения", show_header=False, box=None)
        tbl.add_column(style="bold cyan")
        tbl.add_column()

        def _dur_row(label: str, field: str) -> None:
            tbl.add_row(label, _duration_value_text(s, defaults, field))

        _dur_row("Микробенчмарк CPU (на тест)", "micro")
        _dur_row("Тест Single-Core / Multi-Core (на замер)", "single_multi")
        _dur_row("Стресс-движок", "stress_engine")
        _dur_row("Полный прогон (фаза)", "bench")
        _dur_row("Расширенный тест ОЗУ и кеша (на измерение)", "ram_cache")

        tbl.add_row(
            "Частота сбора телеметрии",
            _scalar_value_text(
                f"{s.sampling_rate_sec:.2f} с",
                changed=s.sampling_rate_sec != defaults.sampling_rate_sec,
            ),
        )
        threads_str = f"{s.threads}" + (" (auto)" if s.threads == 0 else "")
        tbl.add_row(
            "Потоков",
            _scalar_value_text(threads_str, changed=s.threads != defaults.threads),
        )
        tbl.add_row(
            "Sparkline в графиках",
            _scalar_value_text(
                s.sparkline_style,
                changed=s.sparkline_style != defaults.sparkline_style,
            ),
        )
        tbl.add_row(
            "Тема Rich-консоли (TUI)",
            _scalar_value_text(
                s.cli_theme,
                changed=s.cli_theme != defaults.cli_theme,
            ),
        )
        tbl.add_row("Файл настроек", str(path))
        console.print(tbl)

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("1", "Изменить длительность тестирования", self._durations),
            MenuItem("2", "Изменить частоту сбора телеметрии", self._rate),
            MenuItem("3", "Изменить число потоков (0 = авто)", self._threads),
            MenuItem(
                "4", "Изменить стиль sparkline (auto/unicode/ascii)", self._sparkline
            ),
            MenuItem(
                "5",
                "Изменить тему Rich-консоли (dark/light)",
                self._theme,
            ),
            MenuItem("6", "Сбросить настройки к значениям по умолчанию", self._reset),
            MenuItem(
                "7",
                "Диагностика датчиков температуры (doctor)",
                self._doctor,
            ),
            MenuItem(
                "8",
                "Открыть папку настроек (для ручного редактирования файла)",
                self._open_folder,
            ),
            MenuItem("b", "Назад на главный", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    def _durations(self) -> NavResult:
        return push(DurationsScreen())

    def _rate(self) -> NavResult:
        s = settings_store.load_menu_settings()
        new = _ask_float(
            f"Частота сбора телеметрии, с (минимум 0.05, текущая {s.sampling_rate_sec:.2f})",
            s.sampling_rate_sec,
        )
        if new < 0.05 or new > 60.0:
            return stay("[red]Допустимый диапазон: 0.05…60 с.[/]")
        s.sampling_rate_sec = new
        settings_store.save_menu_settings(s)
        return stay(f"[green]Частота сбора телеметрии: {new:.2f} с[/]")

    def _threads(self) -> NavResult:
        s = settings_store.load_menu_settings()
        new = _ask_int(
            f"Потоков (0 = авто, текущее {s.threads})",
            s.threads,
        )
        if new < 0 or new > 1024:
            return stay("[red]Допустимый диапазон: 0…1024.[/]")
        s.threads = new
        settings_store.save_menu_settings(s)
        return stay(f"[green]Потоков: {new}{' (auto)' if new == 0 else ''}[/]")

    def _sparkline(self) -> NavResult:
        s = settings_store.load_menu_settings()
        styles = settings_store.SPARKLINE_STYLES
        try:
            raw = Prompt.ask(
                f"Стиль sparkline ({'/'.join(styles)}, текущий {s.sparkline_style})",
                default=s.sparkline_style,
            ).strip().lower()
        except EOFError:
            return stay()
        if raw not in styles:
            return stay(f"[red]Допустимо: {', '.join(styles)}[/]")
        s.sparkline_style = raw
        settings_store.save_menu_settings(s)
        settings_store.apply_sparkline_env(raw)
        return stay(
            f"[green]Стиль sparkline: {raw}[/] "
            "[dim](вступает в силу сразу для новых графиков)[/]"
        )

    def _theme(self) -> NavResult:
        from apexcore.interfaces.cli.theme import apply_theme

        s = settings_store.load_menu_settings()
        # Тема — бинарный toggle (dark ↔ light). Чтобы не заставлять
        # пользователя набирать слово «light», спрашиваем Y/N: «текущая
        # X, переключить на Y?». Дефолт «no» — случайный Enter не
        # переключит тему.
        current_label = "тёмная" if s.cli_theme == "dark" else "светлая"
        target = "light" if s.cli_theme == "dark" else "dark"
        target_label = "светлая" if target == "light" else "тёмная"
        if not _confirm(
            f"Сейчас тема — [bold]{current_label}[/]. Переключить на "
            f"[bold]{target_label}[/]? (y/n)"
        ):
            return stay()
        s.cli_theme = target
        settings_store.save_menu_settings(s)
        # Применяем мгновенно — следующий render таблиц/панелей уже будет
        # в новой палитре, не дожидаясь рестарта `apexcore menu`.
        apply_theme(target)
        raw = target  # для совместимости с нижестоящим кодом
        hint = (
            "светлая нужна для печати/яркого окружения"
            if raw == "light"
            else "тёмная — дефолт"
        )
        return stay(f"[green]Тема Rich-консоли: {raw}[/] [dim]({hint})[/]")

    def _reset(self) -> NavResult:
        if not _confirm("Сбросить все настройки к значениям по умолчанию? (y/n)"):
            return stay()
        settings_store.reset_to_defaults()
        return stay("[green]Настройки сброшены к значениям по умолчанию.[/]")

    def _doctor(self) -> NavResult:
        from apexcore.application.diagnostics_sensors import diagnose_sensors
        from apexcore.interfaces.cli.render import render_sensor_diagnostics

        console.clear()
        report = diagnose_sensors()
        render_sensor_diagnostics(report)
        _wait_enter()
        return stay()

    def _open_folder(self) -> NavResult:
        """Открыть каталог настроек в системном файловом менеджере.

        Полезно для ручного редактирования `menu_settings.yaml` — пользователь
        получает окно Explorer/Nautilus/Finder, выделенный файл сразу под рукой.
        Перед открытием папка гарантированно создаётся
        (`settings_store.settings_dir` делает mkdir).
        """
        try:
            folder = settings_store.open_settings_dir()
        except (OSError, FileNotFoundError) as exc:
            return stay(
                f"[red]Не удалось открыть папку настроек: {exc}[/] "
                f"[dim]({settings_store.settings_dir()})[/]"
            )
        return stay(f"[green]Открыта папка настроек:[/] {folder}")


class DurationsScreen(Screen):
    title = "Длительность тестирования"
    subtitle = (
        "Выбери программу — увидишь её текущую длительность и сможешь ввести новую"
    )

    def render_extra(self) -> None:
        s = settings_store.load_menu_settings()
        defaults = settings_store.DurationSettings()
        tbl = Table(title="Программы и текущие длительности", show_header=True, box=None)
        tbl.add_column("№", style="bold cyan", justify="right")
        tbl.add_column("Программа")
        tbl.add_column("Команда", style="dim")
        tbl.add_column("Длительность", justify="right")
        tbl.add_column("Полный прогон", justify="right", style="dim")
        tbl.add_column("Минимум", justify="right", style="dim")
        for i, p in enumerate(settings_store.PROGRAMS, 1):
            current = getattr(s.durations, p.field)
            default = getattr(defaults, p.field)
            duration_text = _scalar_value_text(
                f"{current:.1f} с", changed=current != default
            )
            full = settings_store.full_run_duration(p.field, current)
            full_text = (
                f"~{full[0]:.0f} с ({full[1]} {full[2]})" if full is not None else "—"
            )
            tbl.add_row(
                str(i),
                p.label,
                p.technical,
                duration_text,
                full_text,
                f"{p.min_value:.1f} с",
            )
        console.print(tbl)
        console.print(
            "[dim]* — значение изменено относительно дефолта[/]"
        )

    def items(self) -> list[MenuItem]:
        items = []
        for i, p in enumerate(settings_store.PROGRAMS, 1):
            items.append(
                MenuItem(str(i), f"Изменить: {p.label}", _editor_for(p)),
            )
        items.append(MenuItem("b", "Назад", lambda: back(), accent="dim"))
        items.append(MenuItem("q", "Выход", lambda: quit_app(), accent="red"))
        return items


def _editor_for(program: settings_store.ProgramDescriptor):
    """Создать handler редактирования длительности конкретной программы."""

    def handler() -> NavResult:
        s = settings_store.load_menu_settings()
        current = getattr(s.durations, program.field)
        console.rule(f"[bold]{program.label}[/]")
        console.print(
            Panel.fit(
                f"[bold]{program.description}[/]\n\n"
                f"Текущее значение: [bold green]{current:.1f} с[/]\n"
                f"Минимум: {program.min_value:.1f} с, "
                f"максимум: {settings_store.MAX_DURATION:.0f} с\n"
                f"Команда CLI: [dim]{program.technical}[/]",
                border_style="cyan",
            )
        )
        try:
            raw = Prompt.ask(
                "Новая длительность (с) — пусто оставляет без изменений",
                default="",
            )
        except EOFError:
            return back()
        if not raw.strip():
            return stay()
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            return stay("[red]Не удалось распознать число.[/]")
        try:
            settings_store.update_duration(program.field, value)
        except ValueError as exc:
            return stay(f"[red]{exc}[/]")
        return stay(f"[green]{program.label}: {value:.1f} с[/]")

    return handler


# ─── Утилиты ввода ──────────────────────────────────────────────────────────


def _parse_range_token(token: str, count: int) -> list[int] | None:
    """Распарсить токен вида ``"N-M"`` как диапазон 1-based номеров.

    Возвращает список 0-based индексов в пределах ``[0, count)``,
    либо ``None`` если токен не похож на диапазон. ``"3-1"`` нормализуется
    к ``[0,1,2]``. Out-of-range номера тихо отбрасываются.
    """
    if token.count("-") != 1:
        return None
    a, b = token.split("-", 1)
    a, b = a.strip(), b.strip()
    if not (a.isdigit() and b.isdigit()):
        return None
    start, end = int(a), int(b)
    if start > end:
        start, end = end, start
    return [i - 1 for i in range(start, end + 1) if 0 < i <= count]


def _scalar_value_text(formatted: str, *, changed: bool) -> Text:
    """Текст значения в карточке Настроек. Изменённое vs default — звёздочкой и зелёным."""
    if changed:
        return Text(f"{formatted} *", style="bold green")
    return Text(formatted)


def _duration_value_text(
    settings: settings_store.MenuSettings,
    defaults: settings_store.MenuSettings,
    field: str,
) -> Text:
    """Текст длительности: значение, опционально *, подсказка полного прогона.

    Пример вывода: `5.0 с *  · полный набор ~60 с (12 тестов)`
    """
    current = getattr(settings.durations, field)
    default = getattr(defaults.durations, field)
    text = _scalar_value_text(f"{current:.1f} с", changed=current != default)
    hint = settings_store.full_run_duration(field, current)
    if hint is not None:
        total, count, unit = hint
        text.append(f"  · полный набор ~{total:.0f} с ({count} {unit})", style="dim")
    return text


def _ask_int(prompt: str, default: int) -> int:
    try:
        raw = Prompt.ask(prompt, default=str(default))
    except EOFError:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _ask_float(prompt: str, default: float) -> float:
    try:
        raw = Prompt.ask(prompt, default=str(default))
    except EOFError:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


__all__ = [
    "CpuTestsScreen",
    "DurationsScreen",
    "HistoryScreen",
    "HomeScreen",
    "RamCacheScreen",
    "SelectMicroTestsScreen",
    "SelectRamCacheTestsScreen",
    "SettingsScreen",
    "StressScreen",
]
