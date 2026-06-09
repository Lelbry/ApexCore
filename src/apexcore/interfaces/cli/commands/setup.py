"""CLI-команда ``apexcore setup`` — первый-запуск wizard в браузере.

Стартует FastAPI WebUI на свободном (или сохранённом) порту, открывает
браузер на ``/setup``, ждёт пока пользователь не нажмёт «Завершить» (что
оставляет marker-файл ``~/.config/apexcore/setup_completed``).

На Astra Linux это основная точка входа после ``apt install`` — postinst
печатает подсказку «выполните apexcore setup». На Windows этот же wizard
работает в WebView2 bootstrapper'е инсталлера, поэтому ручной запуск
``apexcore setup`` нужен только если пользователь хочет переконфигурировать.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import webbrowser
from pathlib import Path

import typer

logger = logging.getLogger(__name__)


def _port_is_free(host: str, port: int) -> bool:
    """Проверить что порт свободен (TCP bind)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def setup(
    host: str = typer.Option("127.0.0.1", "--host", help="Адрес локального WebUI."),
    port: int = typer.Option(8765, "--port", help="Порт WebUI (default 8765)."),
    no_browser: bool = typer.Option(
        False, "--no-browser",
        help="Не открывать браузер автоматически (пишет URL в консоль).",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Запустить wizard даже если setup уже отмечен завершённым.",
    ),
) -> None:
    """Запустить первый-запуск wizard в браузере (для Astra Linux после .deb)."""
    from apexcore.interfaces.cli.render import console

    # Idempotent: если уже был setup_completed — спрашиваем, кроме --force.
    try:
        from apexcore.interfaces.webui.setup_router import (
            SETUP_MARKER_PATH,
            is_setup_completed,
        )
    except ImportError as exc:
        console.print(f"[red]WebUI не установлен: {exc}[/]")
        console.print('Установите extras: [bold]pip install -e ".[webui]"[/]')
        raise typer.Exit(code=2) from exc

    if is_setup_completed() and not force:
        console.print(
            f"[yellow]Setup уже выполнен ({SETUP_MARKER_PATH}).[/]"
            " Запустите [bold]apexcore setup --force[/] чтобы пройти заново."
        )
        return

    try:
        from apexcore.interfaces.webui.server import serve
    except ImportError as exc:
        console.print(f"[red]Web UI требует extras 'webui': {exc}[/]")
        raise typer.Exit(code=2) from exc

    # Найдём свободный порт (если 8765 занят — берём следующий)
    chosen_port = port
    if not _port_is_free(host, chosen_port):
        for candidate in range(port + 1, port + 30):
            if _port_is_free(host, candidate):
                chosen_port = candidate
                break
        else:
            console.print(f"[red]Не удалось найти свободный порт в диапазоне {port}..{port + 30}[/]")
            raise typer.Exit(code=1)
    if chosen_port != port:
        console.print(f"[yellow]Порт {port} занят, использую {chosen_port}.[/]")

    setup_url = f"http://{host}:{chosen_port}/setup"

    # Открыть браузер через 1.5 c — даём серверу время подняться.
    def _open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(setup_url)
        except Exception as exc:
            logger.debug("webbrowser.open failed: %s", exc)

    if not no_browser:
        threading.Thread(target=_open_browser, daemon=True).start()
        console.print(
            f"[bold green]ApexCore Setup[/] открывается в браузере: "
            f"[cyan]{setup_url}[/]"
        )
    else:
        console.print(
            f"[bold]ApexCore Setup[/] доступен по адресу: [cyan]{setup_url}[/]"
        )

    console.print("[dim]Ctrl+C для остановки сервера после завершения wizard'а.[/]")

    # Блокирующий запуск uvicorn (закроется по Ctrl+C)
    try:
        serve(host=host, port=chosen_port, reload=False)
    except KeyboardInterrupt:
        console.print("\n[dim]Сервер остановлен.[/]")
