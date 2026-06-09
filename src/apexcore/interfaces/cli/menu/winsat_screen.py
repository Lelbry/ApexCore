"""Экран меню «Аналог Windows Winsat».

Доступен только на Windows — на Linux пункт скрывается из ``HomeScreen``,
а вход в этот экран в обход проверки печатает «недоступно на этой ОС».

Интерфейс копирует паттерн :class:`RamCacheScreen`:
1. Запустить полную оценку (winsat formal)
2. Показать последний результат
3. Список последних прогонов
4. О методике расчёта (показать docs/winsat.md)
"""

from __future__ import annotations

import sys

from rich.panel import Panel

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
    render_winsat_progress,
    render_winsat_report,
    render_winsat_welcome,
)


class WinsatScreen(Screen):
    """Меню Winsat-аналога — флагман модуля «Наследие Winsat»."""

    title = "Аналог Windows Winsat"
    subtitle = (
        "Точная Winsat-шкала 1.0–9.9. CPU + RAM + Disk. "
        "Graphics/D3D будут в следующем релизе."
    )

    def render_extra(self) -> None:
        """Показать ASCII-баннер при каждой отрисовке экрана."""
        if sys.platform != "win32":
            console.print(
                Panel.fit(
                    "[red]Аналог Winsat недоступен на этой ОС.[/]\n"
                    "[dim]Модуль работает только на Windows.[/]",
                    border_style="red",
                )
            )
            return
        render_winsat_welcome()

    def items(self) -> list[MenuItem]:
        return [
            MenuItem("1", "Запустить полную оценку (winsat formal)", self._run_formal),
            MenuItem("2", "Показать последний результат", self._query_last),
            MenuItem("3", "О методике расчёта", self._about),
            MenuItem("b", "Назад", lambda: back(), accent="dim"),
            MenuItem("q", "Выход", lambda: quit_app(), accent="red"),
        ]

    # ─── Действия ─────────────────────────────────────────────────────────

    def _run_formal(self) -> NavResult:
        if sys.platform != "win32":
            return stay("[red]Доступно только на Windows.[/]")

        from apexcore.application.winsat_service import WinsatService
        from apexcore.infrastructure.adapters import AdapterFactory
        from apexcore.infrastructure.persistence import SqliteWinsatRepository
        from apexcore.shared.config import load_settings

        s = settings_store.load_menu_settings()
        # Используем ту же длительность, что и для micro-теста (по умолчанию ≥5 с).
        duration = max(s.durations.micro, 5.0)

        settings = load_settings()
        adapter = AdapterFactory.detect()
        repo = SqliteWinsatRepository(settings.db_path)
        service = WinsatService(adapter, repo=repo)

        console.rule("[bold]Запуск Winsat-оценки[/]")
        console.print(
            f"[dim]По {duration:.1f} с на тест × 5 подтестов ≈ {duration * 5:.0f} с суммарно. "
            f"CPU: {adapter.get_system_info().cpu_model}.[/]"
        )
        console.print(
            "[bold yellow]Ctrl+C[/] — прервать с сохранением промежуточных значений."
        )
        console.print()

        progress, advance = render_winsat_progress()
        with cancellable() as token, progress:
            report = service.run_formal(
                duration_sec_per_test=duration,
                cancel_token=token,
                on_progress=advance,
            )

        console.print()
        render_winsat_report(report)

        if report.cancelled:
            console.print("[yellow]Прогон был отменён пользователем.[/]")
        else:
            console.print(
                f"\n[dim]Сохранено в БД: {str(report.id)[:8]}…[/]"
            )

        _wait_enter()
        return stay()

    def _query_last(self) -> NavResult:
        if sys.platform != "win32":
            return stay("[red]Доступно только на Windows.[/]")

        from apexcore.infrastructure.persistence import SqliteWinsatRepository
        from apexcore.shared.config import load_settings

        settings = load_settings()
        repo = SqliteWinsatRepository(settings.db_path)
        runs = repo.list_runs(limit=1)
        if not runs:
            return stay(
                "[yellow]Нет сохранённых winsat-прогонов.[/] "
                "Запустите пункт 1 для первой оценки."
            )

        console.rule("[bold]Последний winsat-прогон[/]")
        render_winsat_report(runs[0])
        _wait_enter()
        return stay()

    def _about(self) -> NavResult:
        text = (
            "[bold]Аналог Windows System Assessment Tool[/]\n\n"
            "Этот модуль воспроизводит формат [bold]Get-CimInstance Win32_Winsat[/]:\n"
            "  • [cyan]CPUScore[/]      — гармоническое среднее AES-256 + SHA-1.\n"
            "  • [cyan]MemoryScore[/]   — пропускная способность DRAM (np.sum, 256 МБ).\n"
            "  • [cyan]DiskScore[/]     — min(Sequential 64K, Random 16K read).\n"
            "  • [dim]GraphicsScore[/]  — N/A в MVP, будет в следующем релизе.\n"
            "  • [dim]D3DScore[/]       — N/A в MVP, будет в следующем релизе.\n"
            "  • [bold]WinSPRLevel[/]   — минимум среди PASS-подскоров.\n\n"
            "[dim]Шкала 1.0–9.9 откалибрована под типичные конфигурации\n"
            "(см. data/winsat_thresholds.yaml и docs/winsat.md).[/]"
        )
        console.print(Panel.fit(text, title="О методике расчёта", border_style="cyan"))
        _wait_enter()
        return stay()


__all__ = ["WinsatScreen"]
