"""Экран меню «Общая оценка производительности системы».

Два пункта:
1. Комплексный бенчмарк (CPU + RAM + Boot-диск) — выдаёт балл в шкале
   ×10 000, см. ``docs/general_benchmark.md``.
2. Аналог Windows Winsat — переехал сюда с главного меню (Windows-only).
   На Linux пункт скрывается.

История прогонов комплексного бенчмарка отображается в общем разделе
«История ваших тестов» (HistoryScreen), отдельного пункта здесь нет.
"""

from __future__ import annotations

import sys

from rich.panel import Panel

from apexcore.interfaces.cli.menu.cancel import cancellable
from apexcore.interfaces.cli.menu.nav import (
    MenuItem,
    NavResult,
    Screen,
    _wait_enter,
    back,
    push,
    quit_app,
    stay,
)
from apexcore.interfaces.cli.render import (
    console,
    render_general_benchmark_report,
)


class BenchmarkScreen(Screen):
    """Подменю «Общая оценка производительности системы»."""

    title = "Общая оценка производительности системы"
    subtitle = (
        "Композитный балл системы в целом и устаревший аналог этому — "
        "Windows Winsat. Подходит для сравнения систем с идентичными "
        "компонентами для выявления проблем с производительностью."
    )

    def render_extra(self) -> None:
        console.print(
            Panel.fit(
                "[bold]Что здесь:[/]\n"
                "  • [cyan]Комплексный бенчмарк[/] — прогон "
                "CPU + RAM + Boot-диск, балл до 10 000 (теоретический идеал).\n"
                "  • [cyan]Аналог старой утилиты Windows Winsat[/] — "
                "официальная шкала 1.0–9.9 по каждому комплектующему "
                "(только Windows).",
                border_style="cyan",
            )
        )

    def items(self) -> list[MenuItem]:
        items: list[MenuItem] = [
            MenuItem(
                "1",
                "Комплексный бенчмарк (CPU + RAM + Boot-диск)",
                self._run_general,
            ),
        ]
        if sys.platform == "win32":
            items.append(
                MenuItem("2", "Аналог Windows Winsat", self._winsat)
            )
        items.append(MenuItem("b", "Назад на главный", lambda: back(), accent="dim"))
        items.append(MenuItem("q", "Выход", lambda: quit_app(), accent="red"))
        return items

    # ─── 1. Комплексный бенчмарк ────────────────────────────────────────────

    def _run_general(self) -> NavResult:
        from apexcore.application.general_benchmark import (
            GeneralBenchmarkOrchestrator,
            GeneralBenchmarkParams,
        )
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.infrastructure.persistence import (
            SqliteGeneralBenchmarkRepository,
        )
        from apexcore.shared.config import load_settings

        params = GeneralBenchmarkParams()
        cpu_total = 2 * params.cpu_phase_duration_sec
        disk_total = 2 * params.disk_read_duration_sec + 5.0
        cooldown_total = 2 * params.cooldown_sec
        total_est = cpu_total + disk_total + cooldown_total

        console.rule("[bold]Запуск комплексного бенчмарка[/]")
        console.print(
            f"[dim]Ожидаемое время: ~{total_est:.0f} с "
            f"(DGEMM {params.cpu_phase_duration_sec:.0f}с → "
            f"cooldown {params.cooldown_sec:.0f}с → "
            f"STREAM {params.cpu_phase_duration_sec:.0f}с → "
            f"cooldown {params.cooldown_sec:.0f}с → диск ~"
            f"{disk_total:.0f}с).[/]"
        )
        console.print(
            "[bold yellow]Ctrl+C[/] — прервать с сохранением промежуточных данных."
        )
        console.print()

        adapter = AdapterFactory.detect()
        orchestrator = GeneralBenchmarkOrchestrator(adapter)

        try:
            with (
                cancellable() as token,
                console.status("[cyan]Прогрев…[/]", spinner="dots") as status,
            ):
                def on_progress(phase: str, idx: int, total: int) -> None:
                    pretty = {
                        "dgemm": "DGEMM (CPU+RAM compute)",
                        "stream": "STREAM (RAM bandwidth)",
                        "disk_seq_read": "Диск: последовательное чтение",
                        "disk_random_read": "Диск: случайное чтение",
                        "disk_seq_write": "Диск: последовательная запись",
                    }.get(phase, phase)
                    status.update(
                        f"[cyan]Фаза {idx}/{total}: [bold]{pretty}[/cyan][/]"
                    )

                report = orchestrator.run(
                    params=params,
                    cancel_token=token,
                    on_progress=on_progress,
                )
        except KeyboardInterrupt:
            return stay("[yellow]Бенчмарк отменён пользователем.[/]")

        # Тихо сохраняем в БД ДО рендера, чтобы в выводе не было «Сохранено
        # в БД: …». Ошибку логируем в notes report'а — пользователь увидит
        # её в блоке «Заметки» если что.
        try:
            settings = load_settings()
            repo = SqliteGeneralBenchmarkRepository(settings.db_path)
            repo.save(report)
            repo.close()
        except Exception as exc:
            report.notes.append(f"Не удалось сохранить в БД: {exc}")

        console.print()
        render_general_benchmark_report(report)

        _wait_enter()
        return stay()

    # ─── 2. Winsat ──────────────────────────────────────────────────────────

    def _winsat(self) -> NavResult:
        if sys.platform != "win32":
            return stay("[red]Доступно только на Windows.[/]")
        from apexcore.interfaces.cli.menu.winsat_screen import WinsatScreen

        return push(WinsatScreen())


__all__ = ["BenchmarkScreen"]
