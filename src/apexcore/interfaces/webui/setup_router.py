"""FastAPI router для первого-запуска wizard'а на /setup.

Использует ту же `static/setup/` HTML/CSS/JS-кодовую базу, что и Windows
WebView2 bootstrapper. На Linux/Astra — wizard работает в браузере, делает
реальные post-install шаги (probes, capabilities, sensors-detect) через
`/ws/setup`.

Поведение деградации: если запрошенная утилита (pkexec, nvidia-smi, lspci)
недоступна — wizard продолжит работать, в лог пойдёт мягкое предупреждение.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Setup router требует extras 'webui'. Установите: pip install -e \".[webui]\""
    ) from exc

from apexcore import __version__ as _apexcore_version

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SETUP_STATIC_DIR = STATIC_DIR / "setup"

# Файл-маркер «setup пройден». Используется при следующих запусках,
# чтобы не показывать wizard повторно.
SETUP_MARKER_PATH = Path.home() / ".config" / "apexcore" / "setup_completed"


def _mark_setup_completed() -> None:
    try:
        SETUP_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETUP_MARKER_PATH.write_text(_apexcore_version, encoding="utf-8")
    except OSError as exc:
        # Если маркер не записан — wizard покажется при следующем запуске
        # повторно. Это нужно видеть (warning, не debug), чтобы можно было
        # диагностировать (права на ~/.config/apexcore, read-only FS).
        logger.warning("Не удалось записать setup_completed marker: %s", exc)


def is_setup_completed() -> bool:
    """Проверить наличие маркер-файла. Используется CLI для idempotent-запуска."""
    return SETUP_MARKER_PATH.exists()


# ─── Запуск CLI в терминале (Linux finish-шаг) ──────────────────────────────

# Кандидаты терминал-эмуляторов в порядке предпочтения + флаг «выполнить
# команду». На Astra (Fly DE) x-terminal-emulator → konsole, fly-term —
# родной терминал. Большинство принимает «-e <cmd>»; xfce4-terminal надёжнее
# через «-x», gnome-terminal — через «--».
_LINUX_TERMINALS: tuple[tuple[str, str], ...] = (
    ("x-terminal-emulator", "-e"),
    ("fly-term", "-e"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("mate-terminal", "-e"),
    ("gnome-terminal", "--"),
    ("xterm", "-e"),
)


def _launch_cli_terminal_linux() -> str:
    """Открыть терминал-эмулятор с интерактивным меню `apexcore`.

    Возвращает строку-результат (для лога). Никогда не бросает: если нет
    графической сессии ($DISPLAY/$WAYLAND_DISPLAY) или не найден ни один
    терминал — просто возвращает мягкое сообщение, ничего не спавнит.

    Права root не требуются: базовые сенсоры (CPU/GPU/диск через hwmon)
    доступны обычному пользователю, а capability для dmidecode мастер уже
    выставил на шаге установки через pkexec.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "launch_cli: нет графической сессии ($DISPLAY пуст) — пропускаем"

    apex = shutil.which("apexcore")
    if apex is None:
        if Path("/usr/bin/apexcore").exists():
            apex = "/usr/bin/apexcore"
        else:
            return "launch_cli: исполняемый apexcore не найден — пропускаем"

    for term, flag in _LINUX_TERMINALS:
        term_path = shutil.which(term)
        if not term_path:
            continue
        try:
            # Безопасно: term_path/apex — из фиксированного списка + shutil.which.
            subprocess.Popen(
                [term_path, flag, apex],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"launch_cli: открыт терминал {term} с apexcore"
        except OSError as exc:
            logger.warning("launch_cli: не удалось запустить %s: %s", term, exc)
            continue
    return "launch_cli: терминал-эмулятор не найден — запустите 'apexcore' вручную"


# ─── Probes (Linux/Astra) ──────────────────────────────────────────────────


def _probe_gpu_linux() -> dict[str, Any]:
    """Вернуть {nvidia, amd, intel, reason} по dictionary-протоколу bridge.js."""
    result: dict[str, Any] = {"nvidia": None, "amd": None, "intel": None, "reason": None}
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "-L"],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
            if out.returncode == 0:
                lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
                if lines:
                    result["nvidia"] = lines[0]
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("nvidia-smi failed: %s", exc)
    # AMD / Intel via lspci
    if shutil.which("lspci"):
        try:
            out = subprocess.run(
                ["lspci", "-nn"], capture_output=True, text=True, timeout=3.0, check=False,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    low = line.lower()
                    if not any(tag in low for tag in ("vga", "3d controller", "display")):
                        continue
                    if "1002" in line and not result["amd"]:
                        result["amd"] = line.split(":", 2)[-1].strip()
                    if "8086" in line and not result["intel"]:
                        result["intel"] = line.split(":", 2)[-1].strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("lspci failed: %s", exc)
    if not any([result["nvidia"], result["amd"], result["intel"]]):
        result["reason"] = "Дискретная GPU не найдена. GPU-метрики будут недоступны."
    return result


def _probe_gpu_windows() -> dict[str, Any]:
    """WMI Win32_VideoController → nvidia/amd/intel."""
    result: dict[str, Any] = {"nvidia": None, "amd": None, "intel": None, "reason": None}
    if not shutil.which("powershell"):
        result["reason"] = "PowerShell недоступен — GPU probe пропущен."
        return result
    try:
        cmd = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name, AdapterCompatibility | ConvertTo-Json -Compress",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8.0, check=False)
        if out.returncode != 0 or not out.stdout.strip():
            result["reason"] = "WMI Win32_VideoController вернул пусто."
            return result
        data = json.loads(out.stdout)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            name = (entry.get("Name") or "").strip()
            vendor = (entry.get("AdapterCompatibility") or "").lower()
            if "nvidia" in vendor and not result["nvidia"]:
                result["nvidia"] = name
            elif ("amd" in vendor or "advanced micro" in vendor) and not result["amd"]:
                result["amd"] = name
            elif "intel" in vendor and not result["intel"]:
                result["intel"] = name
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result["reason"] = f"GPU probe ошибка: {exc}"
    if not any([result["nvidia"], result["amd"], result["intel"]]):
        result["reason"] = result["reason"] or "GPU не обнаружена."
    return result


def _probe_environment() -> dict[str, Any]:
    """Сводный probe среды для шага Components: BLAS, sensors, smartctl, etc."""
    env: dict[str, Any] = {
        "platform": "linux" if platform.system() == "Linux" else "windows" if platform.system() == "Windows" else "other",
        "python": platform.python_version(),
    }
    # numpy / BLAS
    try:
        import numpy as np  # noqa: WPS433
        try:
            cfg_dump = json.dumps(np.show_config(mode="dicts"))
            env["blas"] = "OpenBLAS" if "openblas" in cfg_dump.lower() else (
                "MKL" if "mkl" in cfg_dump.lower() else "reference"
            )
        except (TypeError, AttributeError):
            env["blas"] = "unknown"
    except ImportError:
        env["blas"] = "missing"

    # Debian/Astra: sbin-утилиты не в PATH у обычного пользователя — используем
    # which_with_sbin как fallback (см. infrastructure/sbin_lookup.py).
    from apexcore.infrastructure.sbin_lookup import has_sbin
    env["sensors"] = has_sbin("sensors")
    env["smartctl"] = has_sbin("smartctl")
    env["stress_ng"] = has_sbin("stress-ng")
    env["dmidecode"] = has_sbin("dmidecode")
    env["pkexec"] = has_sbin("pkexec")
    env["lspci"] = has_sbin("lspci")
    return env


# ─── Post-install actions (Linux) ──────────────────────────────────────────


async def _run_postinstall_steps_linux(options: dict[str, Any]):
    """Async-генератор progress-эвентов для wizard'а.

    Шаги:
      1. probe environment (sanity-check)
      2. setcap для smartctl (через pkexec, опционально)
      3. setcap для dmidecode (через pkexec, опционально)
      4. sensors-detect --auto (через pkexec, опционально)
      5. финальная запись маркер-файла
    """
    total_steps = 5
    elapsed = 0.0

    yield {
        "event": "progress",
        "percent": 5,
        "step": "Проверка окружения",
        "log_line": f"Python {platform.python_version()} · platform {platform.system()}",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(0.3)
    elapsed += 0.3

    from apexcore.infrastructure.sbin_lookup import has_sbin

    # Шаг 2: smartctl — НАМЕРЕННО не эскалируем capability (решение по
    # безопасности). Температура диска идёт через kernel hwmon (smartctl для
    # неё не нужен), а SMART-атрибуты на NVMe требуют cap_sys_admin (near-root)
    # — не ставим ради nice-to-have. Полные SMART-данные — по root (sudo smartctl).
    pct = 25
    step_label = "Проверка smartctl"
    log = ("smartctl: capability не требуется — T° диска через kernel hwmon; "
           "SMART-атрибуты по root при необходимости")
    yield {"event": "progress", "percent": pct, "step": step_label, "log_line": log, "elapsed_sec": elapsed}
    await asyncio.sleep(0.3)
    elapsed += 0.3

    # Шаг 3: setcap dmidecode — cap_dac_read_search (читать root-only
    # /sys/firmware/dmi/tables/DMI без root → DRAM-инфо обычному юзеру).
    # cap_sys_rawio здесь недостаточен: нужен bypass DAC на чтение файла, а не
    # raw I/O. Проверено на Astra SE 1.8.5.46: даёт все модули памяти.
    pct = 45
    step_label = "setcap для dmidecode"
    log = "setcap cap_dac_read_search+ep /usr/sbin/dmidecode"
    if has_sbin("pkexec") and has_sbin("setcap") and has_sbin("dmidecode"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkexec", "setcap", "cap_dac_read_search+ep", "/usr/sbin/dmidecode",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0:
                log = f"setcap dmidecode: пропущено ({(err or b'').decode(errors='replace').strip()[:120]})"
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            log = f"setcap dmidecode: пропущено ({exc})"
    else:
        log = "setcap dmidecode: pkexec/setcap не найдены — пропускаем"
    yield {"event": "progress", "percent": pct, "step": step_label, "log_line": log, "elapsed_sec": elapsed}
    await asyncio.sleep(0.3)
    elapsed += 0.3

    # Шаг 4: sensors-detect (опционально, скипаем если уже есть /etc/sensors3.conf)
    pct = 70
    step_label = "Активация lm-sensors"
    if Path("/etc/sensors3.conf").exists() or Path("/etc/sensors.conf").exists():
        log = "sensors-detect: уже активирован, пропускаем"
    elif has_sbin("pkexec") and has_sbin("sensors-detect"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkexec", "sensors-detect", "--auto",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60.0)
            log = "sensors-detect --auto: завершено"
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            log = f"sensors-detect: {exc}"
    else:
        log = "sensors-detect: pkexec/sensors-detect не найдены — пропускаем"
    yield {"event": "progress", "percent": pct, "step": step_label, "log_line": log, "elapsed_sec": elapsed}
    await asyncio.sleep(0.3)
    elapsed += 0.3

    # Шаг 5: финал
    _mark_setup_completed()
    yield {
        "event": "progress",
        "percent": 100,
        "step": "Готово",
        "log_line": f"Setup marker: {SETUP_MARKER_PATH}",
        "elapsed_sec": elapsed,
        "state": "done",
    }


async def _run_postinstall_steps_other(_options: dict[str, Any]):
    """Demo-flow для не-Linux хостов (Windows dev preview через `apexcore setup`).

    Реальная установка под Windows идёт через bootstrapper.exe → silent
    Inno Setup engine (см. packaging/windows/bootstrapper/Installer.cs).
    Тут — только UI-preview wizard'а: имитируем 6 шагов с realistic
    задержками 2-3 сек чтобы человек успел увидеть прогресс, прочитать
    log-строки и понять что будет на реальной установке.

    Шаги совпадают с тем, что будет на production apexcore-engine.exe
    (см. Installer.cs::TailLog regex'ы): копирование файлов, распаковка
    .NET runtime, регистрация PawnIO MSI, регистрация sensord-сервиса.
    """
    env = _probe_environment()
    total_steps = 6
    elapsed = 0.0

    yield {
        "event": "progress",
        "percent": 5,
        "step": "Проверка окружения",
        "log_line": f"Python {env['python']} · platform {env['platform']} · BLAS {env.get('blas','?')}",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(2.5)
    elapsed += 2.5

    yield {
        "event": "progress",
        "percent": 25,
        "step": "Распаковка apexcore.exe (CLI + Web UI)",
        "log_line": "extract apexcore.exe + dependencies (~92 MB)",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(2.5)
    elapsed += 2.5

    yield {
        "event": "progress",
        "percent": 45,
        "step": "Распаковка .NET 9 runtime для LHM",
        "log_line": "extract dotnet/ bundled framework (~70 MB)",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(2.5)
    elapsed += 2.5

    yield {
        "event": "progress",
        "percent": 65,
        "step": "LibreHardwareMonitor DLL + sensor stack",
        "log_line": "copy LHM lib + 23 dependencies (~18 MB)",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(2.0)
    elapsed += 2.0

    yield {
        "event": "progress",
        "percent": 85,
        "step": "Регистрация PawnIO + apexcore_sensord",
        "log_line": "msiexec /i PawnIO.msi + sc.exe register apexcore_sensord",
        "elapsed_sec": elapsed,
    }
    await asyncio.sleep(2.5)
    elapsed += 2.5

    _mark_setup_completed()
    yield {
        "event": "progress",
        "percent": 100,
        "step": "Готово",
        "log_line": f"Setup marker: {SETUP_MARKER_PATH}",
        "elapsed_sec": elapsed,
        "state": "done",
    }


# ─── Router ────────────────────────────────────────────────────────────────


def make_setup_router(on_completed=None) -> APIRouter:
    """Возвращает APIRouter с маршрутами /setup, /setup/{path}, /ws/setup, /api/setup/*."""
    router = APIRouter()

    @router.get("/setup", response_class=HTMLResponse)
    @router.get("/setup/", response_class=HTMLResponse)
    async def setup_index():
        path = SETUP_STATIC_DIR / "index.html"
        if not path.exists():
            return HTMLResponse(
                "<h1>ApexCore Setup</h1><p>static/setup/index.html не найден. "
                "Запустите сборку: <code>python scripts/build_branding.py</code>.</p>",
                status_code=500,
            )
        html = path.read_text(encoding="utf-8")
        # Подставляем актуальную версию в meta-тег apexcore-version.
        # Regex по имени meta, а не str.replace по литералу версии: иначе при
        # bump'е версии в pyproject пришлось бы синхронно править захардкоженную
        # строку здесь (классический источник рассинхрона). Матчим любой текущий
        # content именно этого meta — версия в index.html может быть любой.
        html = re.sub(
            r'(<meta name="apexcore-version" content=")[^"]*(")',
            rf"\g<1>{_apexcore_version}\g<2>",
            html,
            count=1,
        )
        return HTMLResponse(html)

    # CSS/JS/assets отдаются через существующий /static mount на уровне app
    # (server.py делает app.mount("/static", StaticFiles(directory=STATIC_DIR))).
    # index.html ссылается на абсолютные пути /static/setup/css/... — это
    # снимает конфликт с @router.get("/setup") (которого нельзя пересекать с
    # router.mount("/setup")).

    @router.get("/api/setup/status")
    async def setup_status():
        return {"completed": is_setup_completed(), "version": _apexcore_version}

    @router.get("/api/setup/probe-gpu")
    async def http_probe_gpu():
        return _probe_gpu_linux() if platform.system() == "Linux" else _probe_gpu_windows()

    @router.get("/api/setup/probe-env")
    async def http_probe_env():
        return _probe_environment()

    @router.websocket("/ws/setup")
    async def ws_setup(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                reply_id = msg.get("id")
                action = msg.get("action")

                if action == "probeGpu":
                    data = _probe_gpu_linux() if platform.system() == "Linux" else _probe_gpu_windows()
                    await ws.send_json({"reply": reply_id, "data": data})
                elif action == "probeEnvironment":
                    await ws.send_json({"reply": reply_id, "data": _probe_environment()})
                elif action == "startInstall":
                    runner = (
                        _run_postinstall_steps_linux
                        if platform.system() == "Linux"
                        else _run_postinstall_steps_other
                    )
                    try:
                        async for ev in runner(msg.get("options") or {}):
                            await ws.send_json(ev)
                        await ws.send_json({"reply": reply_id, "data": {"ok": True}})
                    except Exception as exc:  # pragma: no cover
                        logger.exception("setup install failed")
                        await ws.send_json({"reply": reply_id, "error": str(exc)})
                elif action == "finish":
                    opts = msg.get("options") or {}
                    _mark_setup_completed()
                    # Linux: по чекбоксу «Запустить CLI» открываем терминал с
                    # интерактивным меню. Windows-путь (WebView2) обрабатывает
                    # launch_cli в C#-bridge и сюда не попадает — не трогаем.
                    if platform.system() == "Linux" and opts.get("launch_cli"):
                        try:
                            logger.info(_launch_cli_terminal_linux())
                        except Exception:  # pragma: no cover
                            logger.exception("launch_cli (Linux) failed")
                    if on_completed:
                        try:
                            on_completed(opts)
                        except Exception:  # pragma: no cover
                            logger.exception("on_completed callback failed")
                    await ws.send_json({"reply": reply_id, "data": {"ok": True}})
                else:
                    await ws.send_json({"reply": reply_id, "error": f"unknown action: {action}"})
        except WebSocketDisconnect:
            return
        except Exception:  # pragma: no cover
            logger.exception("ws_setup unexpected error")
            return

    return router
