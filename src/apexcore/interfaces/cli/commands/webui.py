"""CLI-команда `apexcore webui` — запустить FastAPI веб-визуализацию.

Порт и хост по умолчанию берутся из пользовательских настроек
(``data_dir/menu_settings.yaml``); ``--port`` / ``--host`` остаются override
без сохранения. Это позволяет:

- Web Settings UI меняет порт → сохраняется в YAML → следующий запуск
  ``apexcore webui`` подхватывает.
- ``apexcore webui --port 9000`` запускает на 9000 один раз, не трогая YAML.
"""

from __future__ import annotations

import typer

from apexcore.interfaces.cli.menu.settings_store import load_menu_settings


def webui(
    host: str | None = typer.Option(
        None,
        "--host",
        help="Адрес (override; по умолчанию из настроек, обычно 127.0.0.1).",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        help="Порт (override; по умолчанию из настроек, обычно 8765).",
    ),
    reload: bool = typer.Option(False, "--reload", help="Авто-перезагрузка при разработке."),
) -> None:
    """Поднять локальный веб-сервер с дашбордом."""
    try:
        from apexcore.interfaces.webui.server import serve
    except ImportError as exc:
        from apexcore.interfaces.cli.render import console

        console.print(f"[red]Не удалось импортировать webui: {exc}[/]")
        console.print(
            "Установите extras: [bold]pip install -e \".[webui]\"[/]"
        )
        raise typer.Exit(code=2) from exc

    # Если CLI не передал --host/--port — читаем сохранённые в menu_settings.yaml.
    # Это даёт паритет с Web Settings: пользователь поменял порт в браузере → новый
    # запуск подхватывает, не нужно помнить аргументы CLI.
    if host is None or port is None:
        saved = load_menu_settings()
        if host is None:
            host = saved.webui_host
        if port is None:
            port = saved.webui_port

    serve(host=host, port=port, reload=reload)
