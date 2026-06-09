"""Probe-фаза: пассивное обследование системы при старте apexcore.

Делает один проход по реестру / WMI / OpenFileMapping и строит
``ProbeResult`` (см. ``domain.sensor_models``). Результат кэшируется
module-level — повторные вызовы ``run_full_probe()`` мгновенны.

Зачем нужно. Без probe-фазы apexcore не может дифференцировать причины
отказа сенсорного слоя: «нет данных» это HVCI? Defender? Нет admin?
Нет .NET? Probe собирает эти сигналы один раз, и downstream-код
(``diagnostics_sensors``, ``adapters/windows``) использует их для
выбора стратегии чтения и для UX-сообщений.

Контракт graceful — любая ошибка чтения отдельного источника даёт
консервативное значение (например ``hvci_enabled=False``), но не
ломает probe целиком. На Linux/AstraLinux probe возвращает
``ProbeResult`` с пустыми флагами Windows-полей — это норма.

См. ``docs/research`` §4 шаг 0 для обоснования стратегии.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from apexcore.domain.sensor_models import ProbeResult

logger = logging.getLogger(__name__)

# Имена SHM-объектов для probe (см. docs/research §3).
SHM_NAMES = {
    "hwinfo": "Global\\HWiNFO_SENS_SM2",
    "coretemp": "CoreTempMappingObjectEx",
    "aida64": "AIDA64_SensorValues",
}

# Module-level cache: один probe за жизнь процесса.
# In-memory вместо disk-cache (см. план §4): пользователь может установить
# HWiNFO во время сессии, и перезапуск должен сразу подхватить. Стоимость
# повторного probe — единицы миллисекунд (winreg + OpenFileMapping быстры).
_cached_probe: ProbeResult | None = None
_cache_lock = threading.Lock()


def run_full_probe(force: bool = False) -> ProbeResult:
    """Прогнать все probe-узлы и вернуть кэшированный ``ProbeResult``.

    Параметр ``force=True`` сбрасывает кэш (используется в
    ``apexcore doctor`` для свежего среза).
    """
    global _cached_probe
    if not force and _cached_probe is not None:
        return _cached_probe
    with _cache_lock:
        if not force and _cached_probe is not None:
            return _cached_probe
        _cached_probe = _do_probe()
        return _cached_probe


def _do_probe() -> ProbeResult:
    """Один проход probe — собрать все сигналы."""
    is_windows = platform.system().lower() == "windows"
    arch = probe_architecture()
    if is_windows:
        return ProbeResult(
            timestamp=datetime.now(timezone.utc),
            architecture=arch,
            is_admin=probe_admin(),
            dotnet_versions=probe_dotnet_runtimes(),
            hvci_enabled=probe_hvci_status(),
            sac_enabled=probe_sac_status(),
            vbl_enabled=probe_vbl_status(),
            defender_quarantine_winring0=probe_defender_quarantine(),
            av_vendor=probe_av_vendor(),
            shm_available=probe_shm_available(),
        )
    # Не-Windows: возвращаем skeleton с консервативными значениями.
    # apexcore на Linux/Astra использует hwmon, эти поля не имеют смысла.
    return ProbeResult(
        timestamp=datetime.now(timezone.utc),
        architecture=arch,
        is_admin=False,
        dotnet_versions=[],
        hvci_enabled=False,
        sac_enabled=False,
        vbl_enabled=False,
        defender_quarantine_winring0=False,
        av_vendor=None,
        shm_available={k: False for k in SHM_NAMES},
    )


# ─── Архитектура ────────────────────────────────────────────────────────────


def probe_architecture() -> str:
    """``x64`` / ``ARM64`` / ``x86`` — определение платформы.

    На Windows используем ``platform.machine()`` (вернёт ``AMD64`` для x64,
    ``ARM64`` для Snapdragon). Нормализуем к одной из трёх строк.
    """
    machine = platform.machine().upper()
    if machine in ("AMD64", "X86_64"):
        return "x64"
    if machine in ("ARM64", "AARCH64"):
        return "ARM64"
    if machine in ("X86", "I386", "I686"):
        return "x86"
    return machine.lower() or "unknown"


# ─── Admin-права ────────────────────────────────────────────────────────────


def probe_admin() -> bool:
    """Запущен ли процесс под админом (нужно для регистрации WinRing0)."""
    if platform.system().lower() != "windows":
        # POSIX — проверяем uid 0; но на apexcore-Linux это не важно.
        try:
            import os

            return os.geteuid() == 0  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ImportError) as exc:
        logger.debug("probe_admin: %s", exc)
        return False


# ─── .NET runtime detection ─────────────────────────────────────────────────


def probe_dotnet_runtimes() -> list[str]:
    """Установленные версии .NET runtime (Framework 4.x + Core/.NET 6+).

    Источники:
    1. ``clr_loader.find_runtimes()`` — публичный API pythonnet (best);
    2. winreg ``HKLM\\SOFTWARE\\dotnet\\Setup\\InstalledVersions\\x64\\
       sharedfx\\Microsoft.NETCore.App`` (для coreclr);
    3. winreg ``HKLM\\SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v4``
       (для Framework 4.x).

    Возвращает список строк-версий (например ``["4.8", "9.0.1"]``).
    Пустой список = pythonnet/LHM скорее всего не запустятся.
    """
    versions: set[str] = set()

    # 1) clr_loader.find_runtimes() — самый надёжный источник.
    try:
        from clr_loader import find_runtimes  # type: ignore

        for spec in find_runtimes():
            version = getattr(spec, "version", None)
            if version:
                versions.add(str(version))
    except Exception as exc:
        # clr_loader может бросать разные ошибки на разных версиях — ловим
        # широко, потому что probe-функция не должна падать.
        logger.debug("clr_loader.find_runtimes: %s", exc)

    if platform.system().lower() != "windows":
        return sorted(versions)

    # 2) winreg .NET Core / .NET 5+ (sharedfx).
    versions.update(_winreg_dotnet_core_versions())

    # 3) winreg .NET Framework 4.x.
    fw_version = _winreg_dotnet_framework_version()
    if fw_version:
        versions.add(fw_version)

    return sorted(versions)


def _winreg_dotnet_core_versions() -> set[str]:
    """Прочитать установленные runtime'ы Microsoft.NETCore.App из реестра."""
    versions: set[str] = set()
    try:
        import winreg  # type: ignore

        path = (
            r"SOFTWARE\dotnet\Setup\InstalledVersions\x64\sharedfx"
            r"\Microsoft.NETCore.App"
        )
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            i = 0
            while True:
                try:
                    name, _value, _kind = winreg.EnumValue(key, i)
                    versions.add(name)
                    i += 1
                except OSError:
                    break
    except (ImportError, OSError) as exc:
        logger.debug("winreg .NETCore.App: %s", exc)
    return versions


def _winreg_dotnet_framework_version() -> str | None:
    """Прочитать установленный .NET Framework 4.x (Release → версия)."""
    try:
        import winreg  # type: ignore

        path = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            release_value, _ = winreg.QueryValueEx(key, "Release")
            # Release → версия по таблице Microsoft.
            # https://learn.microsoft.com/dotnet/framework/migration-guide/
            return _release_to_version(int(release_value))
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("winreg .NET Framework v4: %s", exc)
        return None


def _release_to_version(release: int) -> str:
    """Преобразовать Release-ID реестра в строку версии .NET Framework."""
    # Минимальная таблица — самые свежие версии (Win10/11 поставляются с 4.8).
    if release >= 533320:
        return "4.8.1"
    if release >= 528040:
        return "4.8"
    if release >= 461808:
        return "4.7.2"
    if release >= 461308:
        return "4.7.1"
    if release >= 460798:
        return "4.7"
    return "4.x"


# ─── HVCI / SAC / VBL ───────────────────────────────────────────────────────


def probe_hvci_status() -> bool:
    """HVCI / Memory Integrity активен? → WinRing0 не загрузится."""
    return _winreg_dword_truthy(
        r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
        r"\HypervisorEnforcedCodeIntegrity",
        "Enabled",
    )


def probe_sac_status() -> bool:
    """Smart App Control активен? → unsigned-driver блокируется."""
    return _winreg_dword_truthy(
        r"SYSTEM\CurrentControlSet\Control\CI\Policy",
        "VerifiedAndReputablePolicyState",
    )


def probe_vbl_status() -> bool:
    """Vulnerable Driver Blocklist активен? (по умолчанию с Win11 22H2+)."""
    return _winreg_dword_truthy(
        r"SYSTEM\CurrentControlSet\Control\CI\Config",
        "VulnerableDriverBlocklistEnable",
    )


def _winreg_dword_truthy(path: str, value_name: str) -> bool:
    """Прочитать DWORD из HKLM\\<path>\\<value_name> и проверить ``!= 0``."""
    if platform.system().lower() != "windows":
        return False
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return bool(int(value))
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("winreg %s\\%s: %s", path, value_name, exc)
        return False


# ─── SHM-источники ──────────────────────────────────────────────────────────


def probe_shm_available() -> dict[str, bool]:
    """Проверить наличие SHM-объектов HWiNFO/CoreTemp/AIDA64.

    Через ctypes ``OpenFileMappingW(FILE_MAP_READ, FALSE, name)``. Если
    handle != 0 — объект существует, сразу закрываем. Сама probe не
    читает содержимое — это делает ``infrastructure.sensors.shm``.
    """
    if platform.system().lower() != "windows":
        return {k: False for k in SHM_NAMES}
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenFileMappingW.restype = wintypes.HANDLE
        kernel32.OpenFileMappingW.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        # FILE_MAP_READ — стандартная Win32-константа из winnt.h; имя в
        # верхнем регистре сохранено для читаемости, ruff N806 игнорится.
        file_map_read = 0x0004
        result: dict[str, bool] = {}
        for short_name, full_name in SHM_NAMES.items():
            try:
                handle = kernel32.OpenFileMappingW(
                    file_map_read, False, full_name
                )
                if handle:
                    kernel32.CloseHandle(handle)
                    result[short_name] = True
                else:
                    result[short_name] = False
            except OSError as exc:
                logger.debug("OpenFileMappingW(%s): %s", full_name, exc)
                result[short_name] = False
        return result
    except (ImportError, OSError, AttributeError) as exc:
        logger.debug("probe_shm_available: %s", exc)
        return {k: False for k in SHM_NAMES}


# ─── Defender / AV ──────────────────────────────────────────────────────────


def probe_defender_quarantine() -> bool:
    """Проверить, карантинил ли Defender WinRing0 (через ``Get-MpThreatDetection``).

    На большинстве систем Defender помечает WinRing0x64.sys как
    ``VulnerableDriver:WinNT/Winring0.A`` через сигнатуры (волна
    обновлений Feb-Mar 2025, см. ресерч §2.2). Это критичный сигнал
    для UX: пользователь увидит «Defender карантинит WinRing0» с
    конкретной инструкцией.

    Возвращает ``True`` если найдена угроза по WinRing0 в истории
    Defender. Любая ошибка → консервативное False.
    """
    if platform.system().lower() != "windows":
        return False
    ps = shutil.which("powershell")
    if ps is None:
        return False
    try:
        result = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-Command",
                (
                    "Get-MpThreatDetection -ErrorAction SilentlyContinue "
                    "| Where-Object { $_.ThreatID -like '*Winring0*' "
                    "-or $_.Resources -like '*WinRing0*' } "
                    "| Select-Object -First 1 "
                    "| ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        out = (result.stdout or "").strip()
        return bool(out) and out not in ("null", "[]")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("probe_defender_quarantine: %s", exc)
        return False


def probe_av_vendor() -> str | None:
    """Определить установленный сторонний AV (Avast/Kaspersky/AVG/...).

    Через ``Get-CimInstance -Namespace root/SecurityCenter2 -ClassName
    AntiVirusProduct``. На обычной Windows 10/11 без 3rd-party AV
    единственный продукт — Microsoft Defender, его в результате не
    возвращаем (это не «блокирующий AV»).

    Полезно для UX: если у пользователя Avast и LHM/WinRing0 не работают —
    причина почти наверняка в нём.
    """
    if platform.system().lower() != "windows":
        return None
    ps = shutil.which("powershell")
    if ps is None:
        return None
    try:
        result = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance -Namespace root/SecurityCenter2 "
                    "-ClassName AntiVirusProduct -ErrorAction SilentlyContinue "
                    "| Select-Object -ExpandProperty displayName "
                    "| ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        out = (result.stdout or "").strip()
        if not out:
            return None
        # Может быть либо строка ("Avast Antivirus"), либо JSON-массив.
        import json

        try:
            parsed = json.loads(out)
        except ValueError:
            return None
        names = parsed if isinstance(parsed, list) else [parsed]
        for name in names:
            if not isinstance(name, str):
                continue
            lower = name.lower()
            if "defender" in lower or "windows security" in lower:
                continue
            return name
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("probe_av_vendor: %s", exc)
        return None


# ─── Сброс кэша (тесты) ─────────────────────────────────────────────────────


def reset_cache() -> None:
    """Сброс module-level кэша. Используется в тестах через monkeypatch."""
    global _cached_probe
    with _cache_lock:
        _cached_probe = None


__all__ = [
    "SHM_NAMES",
    "ProbeResult",
    "probe_admin",
    "probe_architecture",
    "probe_av_vendor",
    "probe_defender_quarantine",
    "probe_dotnet_runtimes",
    "probe_hvci_status",
    "probe_sac_status",
    "probe_shm_available",
    "probe_vbl_status",
    "reset_cache",
    "run_full_probe",
]
