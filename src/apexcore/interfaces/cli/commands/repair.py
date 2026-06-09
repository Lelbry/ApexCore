"""CLI-команда ``apexcore repair-drivers`` — переустановка PawnIO + sensord.

Запускается когда после установки apexcore'а CPU-температура не подтянулась
(`apexcore doctor` показывает `cpu_unsupported` или `degraded`):

1. Self-elevation через UAC если не админ (`Start-Process -Verb RunAs`)
2. Открывает **видимое** PowerShell-окно (НЕ runhidden) — пользователь
   увидит вывод и ошибки в реальном времени.
3. По шагам:
   - Перезапускает `{app}\\drivers\\PawnIO_setup.exe -install -silent` если
     PawnIO_setup.exe лежит в инсталляции; иначе пропускает.
   - Запускает `install_pawnio_service.ps1` (без -NoPrompt → видимо).
   - Запускает `install_sensord_bundle.ps1` (тоже видимо).
   - В конце дёргает `apexcore doctor` чтобы пользователь увидел эффект.

Не делает скачивания PawnIO MSI из интернета — если бандл потерян, печатает
инструкцию по ручной установке с https://pawnio.eu.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

logger = logging.getLogger(__name__)


def _pause_if_console() -> None:
    """Пауза, чтобы пользователь успел прочитать ошибку до закрытия окна.

    Web-UI спавнит ``repair-drivers`` в ``CREATE_NEW_CONSOLE`` — без паузы
    окно с ошибкой мгновенно закрывается (пользователь видит лишь красный
    flash). В обычном CLI (терминал уже открыт) тоже не мешает.
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n[Enter] чтобы закрыть…")
    except Exception:
        pass


def _find_install_root() -> Path | None:
    """Каталог `{app}\\scripts` лежит рядом с PyInstaller-frozen `apexcore.exe`.

    Возвращает Path до корня установки или ``None`` если запущено из
    editable-install (когда ``__file__`` смотрит в `src/apexcore/...`).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # editable-install — `repair-drivers` ещё имеет смысл если рядом dev `dist/`,
    # но в первую очередь команда для production.
    candidate = Path(sys.executable).parent
    if (candidate / "scripts" / "install_pawnio_service.ps1").exists():
        return candidate
    return None


def _powershell_exe() -> str:
    """pwsh если есть (PS7 быстрее запускается), иначе встроенный."""
    for name in ("pwsh", "powershell.exe", "powershell"):
        full = shutil.which(name)
        if full:
            return full
    return "powershell.exe"


def repair_drivers() -> None:
    """Перезапустить PawnIO MSI + переустановить apexcore_sensord."""
    install_root = _find_install_root()
    if install_root is None:
        typer.secho(
            "Не удалось найти каталог установки. "
            "Запусти команду из установленного apexcore, не из editable-install.",
            fg=typer.colors.RED,
            err=True,
        )
        _pause_if_console()
        raise typer.Exit(code=1)

    scripts_dir = install_root / "scripts"
    pawnio_setup = install_root / "drivers" / "PawnIO_setup.exe"
    install_pawnio = scripts_dir / "install_pawnio_service.ps1"
    install_sensord = scripts_dir / "install_sensord_bundle.ps1"

    if not install_pawnio.exists() or not install_sensord.exists():
        typer.secho(
            f"Не найдены скрипты установки в {scripts_dir}. "
            "Возможно apexcore установлен не из v0.8.7+ инсталлера.",
            fg=typer.colors.RED,
            err=True,
        )
        _pause_if_console()
        raise typer.Exit(code=1)

    typer.echo("")
    typer.secho("=== apexcore repair-drivers ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"install: {install_root}")
    typer.echo(f"PawnIO_setup.exe: {'есть' if pawnio_setup.exists() else 'НЕТ'}")
    typer.echo("")

    # Собираем shell-скрипт который запустится в видимом PS-окне под UAC.
    # Каждый шаг с try/catch — ошибка одного не прерывает остальные. В конце
    # Read-Host чтобы пользователь успел прочитать вывод.
    parts: list[str] = []
    parts.append("$ErrorActionPreference = 'Continue'")
    parts.append("Write-Host '=== apexcore repair-drivers ===' -ForegroundColor Cyan")
    parts.append("Write-Host ''")

    if pawnio_setup.exists():
        ps = str(pawnio_setup).replace("'", "''")
        parts.append(f"Write-Host '[1/4] PawnIO_setup.exe -install -silent ...' -ForegroundColor Yellow")
        parts.append(f"& '{ps}' -install -silent")
        parts.append("if ($LASTEXITCODE -ne 0) { Write-Host \"  PawnIO_setup exit=$LASTEXITCODE\" -ForegroundColor Red }")
    else:
        parts.append("Write-Host '[1/4] PawnIO_setup.exe не найден в drivers/ — пропускаю' -ForegroundColor Yellow")
        parts.append("Write-Host '  Скачай вручную с https://pawnio.eu и запусти PawnIO_setup.exe -install -silent' -ForegroundColor Yellow")

    parts.append("Write-Host ''")
    ip = str(install_pawnio).replace("'", "''")
    parts.append(f"Write-Host '[2/5] install_pawnio_service.ps1 ...' -ForegroundColor Yellow")
    parts.append(f"& '{ip}'")

    parts.append("Write-Host ''")
    isn = str(install_sensord).replace("'", "''")
    app_root = str(install_root).replace("'", "''")
    parts.append(f"Write-Host '[3/5] install_sensord_bundle.ps1 ...' -ForegroundColor Yellow")
    parts.append(f"& '{isn}' -InstallDir '{app_root}'")

    # 4/5 — sensord selftest (console-mode init). Если 3/5 не смог запустить
    # сервис, selftest точно покажет на каком этапе falls
    sensord_exe = install_root / "apexcore-sensord" / "apexcore-sensord.exe"
    parts.append("Write-Host ''")
    parts.append("Write-Host '[4/5] apexcore-sensord.exe selftest ...' -ForegroundColor Yellow")
    if sensord_exe.exists():
        se = str(sensord_exe).replace("'", "''")
        parts.append(f"& '{se}' selftest")
    else:
        parts.append("Write-Host '  sensord.exe не найден — пропускаю selftest' -ForegroundColor Red")

    parts.append("Write-Host ''")
    parts.append("Write-Host '[5/5] apexcore doctor ...' -ForegroundColor Yellow")
    apexcore_exe = install_root / "apexcore.exe"
    if apexcore_exe.exists():
        be = str(apexcore_exe).replace("'", "''")
        parts.append(f"& '{be}' doctor")
    else:
        parts.append("Write-Host '  apexcore.exe не найден в каталоге установки — пропускаю doctor' -ForegroundColor Red")

    parts.append("Write-Host ''")
    parts.append("Write-Host '=== готово ===' -ForegroundColor Green")
    parts.append("Write-Host 'Закрой это окно и запусти apexcore заново (БЕЗ админа).'")
    parts.append("[void](Read-Host '[Enter] чтобы закрыть')")

    powershell = _powershell_exe()

    # Пишем шаги в temp .ps1 и запускаем его через -File. Раньше скрипт
    # передавался как -Command "<...>"; строки с Write-Host "..." (литеральные
    # кавычки внутри) ломали тройное экранирование → spawned PS падал с
    # parse-error (красный flash + мгновенное закрытие окна). В .ps1-файле
    # двойные кавычки внутри строк валидны. UTF-8 С BOM — иначе PS 5.1 (ru-RU)
    # читает кириллицу как mojibake.
    script = "\n".join(parts)
    ps1_fd, ps1_path = tempfile.mkstemp(prefix="apexcore-repair-", suffix=".ps1")
    os.close(ps1_fd)
    Path(ps1_path).write_text(script, encoding="utf-8-sig")

    # В bootstrap — только путь к .ps1 (никакого вложенного скрипта).
    bootstrap = (
        f"Start-Process -FilePath '{powershell}' "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','{ps1_path}' "
        f"-Verb RunAs"
    )

    typer.echo("Открываю окно admin PowerShell (UAC спросит подтверждение)...")
    try:
        # Не-elevated PS дёргает Start-Process -RunAs → elevated окно с .ps1.
        subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", bootstrap],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        typer.secho(f"Не удалось запустить PowerShell: {e}", fg=typer.colors.RED, err=True)
        _pause_if_console()
        raise typer.Exit(code=1) from e
    except FileNotFoundError as e:
        typer.secho(f"PowerShell не найден: {e}", fg=typer.colors.RED, err=True)
        _pause_if_console()
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.secho(
        "Окно установки открыто отдельно. После его завершения запусти "
        "`apexcore doctor` в новой PowerShell и проверь что CPU температура появилась.",
        fg=typer.colors.GREEN,
    )
