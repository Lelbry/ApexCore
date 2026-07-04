"""Windows-сервис ``apexcore_sensord`` — long-running LHM/PawnIO host.

Цель: убрать UAC из горячего пути apexcore. Сервис запускается под
``LocalSystem``, разово вызывает ``Computer.Open()`` (которая через
``PawnIOLib.dll`` грузит AMX-blob'ы в драйвер PawnIO), и поддерживает
этот executor живым на всё время своей работы. Каждые
:data:`POLL_INTERVAL_SEC` он делает ``Computer.Update()`` → собирает
все сенсоры одним проходом → пишет snapshot в Global shared memory
``Global\\apexcore_sensors``.

apexcore-клиенты (без admin) читают snapshot через
:mod:`apexcore.services.shm_adapter`. Если сервис не установлен или
упал — клиент fallback'ом идёт на прямой LHM, который требует admin.

Установка / удаление сервиса
----------------------------

Через :mod:`win32serviceutil` (входит в ``pywin32``)::

    python -m apexcore.services.sensord install   # под admin
    python -m apexcore.services.sensord start
    python -m apexcore.services.sensord stop
    python -m apexcore.services.sensord remove

Удобнее — через сопровождающий скрипт
:file:`scripts/install_sensord.ps1`, который сам триггерит UAC.

Зависимости и graceful degrade
------------------------------

- Этот модуль импортируется ленивно — top-level импорт ``win32serviceutil``
  только в условной ветке :func:`_load_win32serviceutil`. Это позволяет
  держать модуль в зеленом списке импортов даже на не-Windows и на
  Windows без ``pywin32`` (тесты shm_layout/shm_adapter не падают).
- При запуске ``python -m apexcore.services.sensord`` без ``pywin32``
  CLI печатает дружелюбное сообщение и завершается с кодом 1.
- Все ошибки runtime-цикла логируются в файл
  ``%PROGRAMDATA%\\apexcore\\sensord.log`` через стандартный logging.

Безопасность
------------

- Сервис запускается под ``LocalSystem`` (`-stayrunning`, `-autostart`
  настраивает PowerShell-скрипт).
- Shared memory mapping создаётся с явным SDDL
  ``D:P(A;;GR;;;WD)(A;;GA;;;SY)(A;;GA;;;BA)`` — ``Everyone:GENERIC_READ``,
  ``LocalSystem:GenericAll``, ``Built-in Administrators:GenericAll``.
  Не-admin user'ы могут только читать.
- Запись делает только сам сервис; apexcore-клиент открывает mapping
  read-only через ``shm_adapter._open_global_mapping``.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Имя сервиса в Windows SCM. Используется в `install_sensord.ps1`
# и в командах `python -m apexcore.services.sensord <action>`.
SERVICE_NAME: str = "apexcore_sensord"
SERVICE_DISPLAY_NAME: str = "ApexCore Sensors Daemon"
SERVICE_DESCRIPTION: str = (
    "Long-running LHM/PawnIO host; publishes a sensor snapshot to "
    "Global\\apexcore_sensors so non-admin apexcore processes can read "
    "CPU temperature/voltage/power/fans without re-acquiring PawnIO."
)

# Период обновления snapshot'а. 250 мс — баланс между свежестью данных
# (apexcore stress UI рисует график раз в ~500 мс) и нагрузкой на LHM.
# В наших измерениях один Computer.Update() на современной системе
# занимает 20–80 мс CPU-time; 250 мс даёт ≤30 % CPU нагрузки на одном
# ядре в worst-case и не съедает заметной мощности на idle (см. план
# inherited-enchanting-crown.md, открытый вопрос 2).
POLL_INTERVAL_SEC: float = 0.25

# Лог сервиса. ProgramData доступен на запись для LocalSystem (мы — он),
# и читается для админов — удобный путь для диагностики.
_LOG_FILE_DEFAULT: Path = Path(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData")
) / "apexcore" / "sensord.log"

# SDDL для shared memory mapping'а. Дисциплина:
#   D:P             — Protected DACL (не наследуется)
#   (A;;GR;;;WD)    — Allow GENERIC_READ для Everyone (SID WD = World)
#   (A;;GA;;;SY)    — Allow GENERIC_ALL для LocalSystem
#   (A;;GA;;;BA)    — Allow GENERIC_ALL для Built-in Administrators
_MAPPING_SDDL: str = "D:P(A;;GR;;;WD)(A;;GA;;;SY)(A;;GA;;;BA)"


def _boot_log(msg: str) -> None:
    """Append-only лог САМОГО старта main(), до любых рисковых импортов.

    Когда apexcore-sensord.exe крашится на servicemanager.Initialize() или
    подобном, sensord.log сервиса не успевает создаться → невозможно
    понять что произошло. Этот примитивный лог пишется через builtin
    `open`, без logging-фреймворка, синхронно. На I/O-ошибки молча
    игнорируем — это лог, не критика.
    """
    try:
        log_dir = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "apexcore"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "sensord-boot.log", "a", encoding="utf-8") as fp:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            fp.write(f"[{ts}] pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — обёртка над ``win32serviceutil.HandleCommandLine``.

    Три режима:

    1. **Командная строка** (``python -m apexcore.services.sensord install``,
       ``apexcore-sensord.exe install|start|stop|remove|debug``) — обычный
       вызов ``HandleCommandLine``: install прописывает сервис в SCM,
       remove снимает и т.д.

    2. **SCM-старт frozen-сборки** (``apexcore-sensord.exe`` без аргументов,
       запущенный Service Control Manager'ом) — нужно явно поднять
       ServiceCtrlDispatcher, иначе SCM не получит handler и ServiceMain
       не вызовется. Pywin32 узнаёт frozen-режим через ``sys.frozen``;
       мы делаем то же определение и переключаем ветку.

    3. **selftest** (``apexcore-sensord.exe selftest``) — пробег полной
       init-цепочки в console-mode с подробным выводом. Не регистрирует
       сервис, не открывает Global mapping (только обычный). Нужен для
       offline-диагностики «почему сервис падает на SvcDoRun».

    На не-Windows / без pywin32 — печатает сообщение и возвращает 1.
    """
    # Pre-log самого факта вызова main(). Делаем ПЕРВЫМ — до любых
    # модулей которые могут уронить процесс на импорте.
    try:
        _boot_log(f"main() called argv={argv if argv is not None else sys.argv} frozen={getattr(sys, 'frozen', False)}")
    except Exception:
        pass

    # Selftest — не требует pywin32 service infrastructure, поэтому
    # обрабатываем ДО _load_win32serviceutil (которое может оставить
    # пользователя без диагностики если pywin32 missing).
    if argv is None:
        argv = sys.argv
    if len(argv) >= 2 and argv[1].lower() == "selftest":
        return _run_selftest()

    sf = _load_win32serviceutil()
    if sf is None:
        # Печатаем в stderr, чтобы msg не смешался с другим выводом
        # (например, в pipe внутри installer'а).
        print(
            "apexcore_sensord: требуется Windows + pywin32 "
            "(pip install 'apexcore[windows]')",
            file=sys.stderr,
        )
        _boot_log("ERROR: pywin32 не найден")
        return 1

    # SCM запускает frozen-EXE без аргументов (argv = [exe_path]). В этом
    # случае нам нужен service-dispatcher loop, а не CLI-парсер. Без этой
    # ветки SCM выдаст «service did not respond in a timely fashion»
    # ровно через 30 с, потому что HandleCommandLine при пустом argv
    # печатает usage и завершается.
    is_frozen = getattr(sys, "frozen", False)
    if is_frozen and len(argv) == 1:
        _boot_log("SCM-start branch (frozen + argv=1)")
        try:
            import servicemanager  # type: ignore
        except ImportError as exc:
            print(f"servicemanager импорт упал: {exc}", file=sys.stderr)
            _boot_log(f"ERROR: import servicemanager: {exc}")
            return 1
        try:
            _boot_log("calling servicemanager.Initialize()")
            servicemanager.Initialize()
            _boot_log("calling PrepareToHostSingle(ApexcoreSensord)")
            servicemanager.PrepareToHostSingle(ApexcoreSensord)
            _boot_log("calling StartServiceCtrlDispatcher()")
            servicemanager.StartServiceCtrlDispatcher()
            _boot_log("StartServiceCtrlDispatcher returned cleanly")
        except Exception as exc:
            # SCM dispatcher умер до того как logger в SvcDoRun успел
            # настроиться. Boot-log — единственный источник правды.
            import traceback
            _boot_log(f"ERROR in SCM dispatcher: {type(exc).__name__}: {exc}")
            _boot_log("traceback:\n" + traceback.format_exc())
            return 1
        return 0

    _boot_log(f"CLI branch HandleCommandLine argv={argv}")
    sf.HandleCommandLine(ApexcoreSensord, argv=argv)
    return 0


def _run_selftest() -> int:
    """Selftest: исполняет init-цепочку сервиса в console-mode.

    Не регистрирует сервис, не создаёт Global mapping (только обычный
    Local\\ mapping для тестового write/read). Печатает все шаги в stdout
    + дублирует в sensord-boot.log. Нужен для offline-диагностики
    «почему сервис падает при SCM-старте» — пользователь запускает
    `apexcore-sensord.exe selftest` из любой PowerShell и видит точку
    отказа без необходимости работать с SCM.
    """
    print("=== apexcore-sensord selftest ===")
    _boot_log("=== SELFTEST started ===")
    steps: list[tuple[str, Any]] = []
    rc = 0

    def step(name: str, fn: Any) -> Any:
        nonlocal rc
        print(f"\n--- {name} ---")
        _boot_log(f"selftest: {name}")
        try:
            result = fn()
            print(f"  OK  {result if result is not None else ''}")
            steps.append((name, "OK"))
            return result
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            print(tb)
            _boot_log(f"selftest FAIL {name}: {type(exc).__name__}: {exc}")
            _boot_log("traceback:\n" + tb)
            steps.append((name, f"FAIL: {exc}"))
            rc = 1
            return None

    # 1. pywin32 modules import
    def _check_pywin32() -> str:
        import servicemanager  # noqa: F401
        import win32api  # noqa: F401
        import win32event  # noqa: F401
        import win32file  # noqa: F401
        import win32service  # noqa: F401
        import win32serviceutil  # noqa: F401
        import win32timezone  # noqa: F401  — lazy в HandleCommandLine
        return "все pywin32 модули загружены"
    step("1. pywin32 imports", _check_pywin32)

    # 2. pythonnet / clr_loader / .NET runtime
    def _check_dotnet() -> str:
        from apexcore.infrastructure.sensors import lhm as _lhm
        _lhm._configure_runtime()
        return f"runtime configured, APEXCORE_DOTNET_ROOT={os.environ.get('DOTNET_ROOT', '<not set>')}"
    step("2. .NET runtime", _check_dotnet)

    # 3. LHM Computer.Open()
    def _check_lhm() -> str:
        from apexcore.infrastructure.sensors import lhm as _lhm
        computer = _lhm._open_computer()
        hw_count = len(list(computer.Hardware))
        try:
            computer.Close()
        except Exception:
            pass
        return f"Computer.Open() = OK, Hardware-источников: {hw_count}"
    step("3. LHM Computer.Open()", _check_lhm)

    # 4. Local mapping create (Global требует admin)
    def _check_local_mapping() -> str:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileMappingW.restype = wintypes.HANDLE
        k32.CreateFileMappingW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
        ]
        handle = k32.CreateFileMappingW(-1, None, 0x04, 0, 4096, "apexcore_selftest")
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(f"CreateFileMapping упал: WinError {err}")
        k32.CloseHandle(handle)
        return "Local\\ mapping создан и закрыт"
    step("4. shared-memory mapping", _check_local_mapping)

    # 5. install_log directory writable
    def _check_logdir() -> str:
        log_dir = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "apexcore"
        log_dir.mkdir(parents=True, exist_ok=True)
        test_file = log_dir / "_selftest_write.txt"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return f"{log_dir} писабельна"
    step("5. PROGRAMDATA writability", _check_logdir)

    print("\n=== Summary ===")
    for name, result in steps:
        marker = "OK  " if result == "OK" else "FAIL"
        print(f"  [{marker}] {name}: {result}")
    print(f"\nExit code: {rc}")
    _boot_log(f"=== SELFTEST done, rc={rc} ===")
    return rc


def _load_win32serviceutil() -> Any | None:
    """Импорт ``win32serviceutil`` с graceful degrade.

    Если pywin32 не установлен (например, на Linux dev-машине) —
    вернёт ``None``. Caller'у это сигнал «сервис не доступен».
    """
    try:
        import win32serviceutil  # type: ignore
    except ImportError:
        return None
    return win32serviceutil


# Объявляем класс сервиса условно — наследовать от ServiceFramework
# можно только если pywin32 импортируется. На системах без pywin32
# `ApexcoreSensord` остаётся `None`, и `main()` корректно отказывает.
#
# КРИТИЧНО: класс определяется БЕЗ underscore-префикса (был `_ApexcoreSensord`).
# pywin32 в frozen-режиме `PrepareToHostSingle(klass)` использует `klass.__name__`
# для регистрации в SCM и lookup при start; underscore-имя могло вызывать тихое
# завершение dispatcher'а (`StartServiceCtrlDispatcher returned cleanly` без
# вызова SvcDoRun, наблюдаемо в v0.9.0 при upgrade-тесте).

ApexcoreSensord: type | None = None

try:
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore

    class ApexcoreSensord(win32serviceutil.ServiceFramework):  # type: ignore[no-redef]
        """Windows-сервис: главный цикл + reaper по stop-event."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = threading.Event()

        def SvcStop(self) -> None:  # noqa: N802 (win32serviceutil API)
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop_event.set()

        def SvcDoRun(self) -> None:  # noqa: N802 (win32serviceutil API)
            # ВАЖНО: SCM по умолчанию ждёт SERVICE_RUNNING 90 с, иначе
            # сообщает «error 1053» (service did not respond in a timely
            # fashion). Сообщаем готовность РАНЬШЕ инициализации LHM —
            # это лёгкая операция, а LHM.Open() может занимать секунды.
            #
            # Логируем сам факт входа в SvcDoRun через boot-log ДО любых
            # рисковых операций — если _configure_service_logging упадёт
            # на permission/file-error, мы хотя бы увидим что control
            # дошёл сюда (boot-log пишется через builtin open, минимум
            # зависимостей).
            _boot_log(f"SvcDoRun entered (PID {os.getpid()})")
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            try:
                _configure_service_logging()
                _boot_log("_configure_service_logging OK")
            except Exception as exc:
                _boot_log(f"ERROR _configure_service_logging: {type(exc).__name__}: {exc}")
                import traceback
                _boot_log("traceback:\n" + traceback.format_exc())
                raise
            logger = logging.getLogger("apexcore.sensord")
            logger.info("apexcore_sensord starting (poll=%.3fs)", POLL_INTERVAL_SEC)
            try:
                _run_main_loop(self._stop_event, logger)
            except Exception:
                logger.exception("apexcore_sensord aborted on exception")
                _boot_log("_run_main_loop raised — see sensord.log")
                raise
            finally:
                logger.info("apexcore_sensord stopped")
                _boot_log(f"SvcDoRun exited (PID {os.getpid()})")

except ImportError:
    # pywin32 нет — модуль остаётся импортируемым, ApexcoreSensord = None.
    pass


def _configure_service_logging(log_file: Path | None = None) -> None:
    """Настроить логгер сервиса: rotated file в %PROGRAMDATA%\\apexcore\\.

    Идемпотентно — повторные вызовы не плодят хендлеров. Параметр
    ``log_file`` нужен для тестируемости (можно подменить путь).
    """
    target = log_file or _LOG_FILE_DEFAULT
    # Если не смогли создать директорию — пишем только в EventLog
    # через servicemanager (LocalSystem умеет туда писать). В цикле
    # это всё равно отработает — просто без файла.
    with contextlib.suppress(Exception):
        target.parent.mkdir(parents=True, exist_ok=True)

    # Логгер сервиса + логгер модуля lhm — оба пишут в один файл. Без этого
    # debug-сообщения LHM init failure (вызовы logger.debug в lhm.py) теряются
    # и мы не видим точную причину «Computer не открылся».
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        handler = logging.handlers.RotatingFileHandler(
            str(target), maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
    except Exception:
        # Если файл не открылся — оставляем без файлового хендлера;
        # SvcDoRun продолжит работу, но логи будут только в EventLog.
        return

    for logger_name in ("apexcore.sensord", "apexcore.infrastructure.sensors.lhm"):
        lg = logging.getLogger(logger_name)
        if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers):
            continue
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)


def _run_main_loop(stop_event: threading.Event, logger: logging.Logger) -> None:
    """Цикл: init LHM → создать mapping → каждые POLL_INTERVAL_SEC писать snapshot.

    Любая ошибка внутри цикла логируется (`exception`) и обрабатывается:
    LHM.Update() exception не валит сервис — следующая итерация снова
    попробует. Полный фейл init (LHM не открылся, mapping не создался)
    — сервис уходит в shutdown с явной диагностикой.
    """
    from apexcore.infrastructure.sensors import lhm

    # Дублируем _open_computer с полным logger.exception. Внутри lhm.py
    # ошибки init глотаются на debug-уровне, и в EventLog летит только
    # «Computer не открылся» без traceback. Без полного контекста
    # диагностировать LHM/PawnIO/pythonnet-фейл под LocalSystem нереально.
    try:
        lhm._configure_runtime()
        computer = lhm._open_computer()
        lhm._computer = computer
    except Exception:
        logger.exception("LHM init failed — полный traceback ниже")
        return
    if computer is None:
        logger.error(
            "LHM init failed — Computer вернул None без exception. "
            "Сервис завершается."
        )
        return

    mapping = _create_global_mapping(logger)
    if mapping is None:
        logger.error("Не удалось создать Global mapping — сервис завершается")
        return

    try:
        cycle = 0
        while not stop_event.is_set():
            iter_start = time.monotonic()
            try:
                snapshot_dict = lhm.read_lhm_full_snapshot()
            except Exception:
                logger.exception("read_lhm_full_snapshot exception")
                snapshot_dict = {}

            if snapshot_dict:
                try:
                    _write_snapshot_to_mapping(mapping, snapshot_dict)
                except Exception:
                    logger.exception("mmap write exception")
            else:
                # Пустой snapshot — не пишем (старый snapshot устареет
                # через FRESHNESS_LIMIT_NS и клиент перейдёт на fallback).
                # Логируем редко, чтобы не забить файл.
                if cycle % 40 == 0:  # ~раз в 10 с при poll=250ms
                    logger.warning("LHM выдал пустой snapshot (cycle=%d)", cycle)

            cycle += 1
            elapsed = time.monotonic() - iter_start
            remaining = POLL_INTERVAL_SEC - elapsed
            if remaining > 0:
                stop_event.wait(remaining)
    finally:
        _close_mapping(mapping)


def _create_global_mapping(logger: logging.Logger) -> Any | None:
    """Создать ``Global\\apexcore_sensors`` mapping с явным SDDL.

    Реализация через ``ctypes`` + raw Win32 API (kernel32 + advapi32),
    чтобы не зависеть от различий в pywin32 (где ``CreateFileMapping``
    исторически лежит в разных модулях между версиями).

    Возвращает кортеж (mmap_obj, handle). На любой ошибке — ``None``.
    """
    import ctypes
    from ctypes import wintypes

    from apexcore.services.shm_layout import BUFFER_SIZE

    # Имена констант и struct'а в UPPER_SNAKE — так они называются в Windows SDK;
    # переводить в snake_case ради ruff-конвенции означает скрывать связь
    # с Win32 API, что хуже для читаемости.
    SDDL_REVISION_1 = 1  # noqa: N806
    PAGE_READWRITE = 0x04  # noqa: N806
    INVALID_HANDLE_VALUE = -1  # noqa: N806

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.restype = wintypes.BOOL
    convert_sddl.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]

    sd_ptr = ctypes.c_void_p()
    if not convert_sddl(_MAPPING_SDDL, SDDL_REVISION_1, ctypes.byref(sd_ptr), None):
        err = ctypes.get_last_error()
        logger.error("ConvertStringSecurityDescriptor упал: WinError %d", err)
        return None

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = sd_ptr
    sa.bInheritHandle = False

    create_file_mapping = kernel32.CreateFileMappingW
    create_file_mapping.restype = wintypes.HANDLE
    create_file_mapping.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]

    handle = create_file_mapping(
        wintypes.HANDLE(INVALID_HANDLE_VALUE),
        ctypes.byref(sa),
        PAGE_READWRITE,
        0,
        BUFFER_SIZE,
        r"Global\apexcore_sensors",
    )

    # SECURITY_DESCRIPTOR можно освобождать — kernel скопировал его
    # к mapping-объекту во время CreateFileMapping.
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    local_free(sd_ptr)

    if not handle:
        err = ctypes.get_last_error()
        logger.error("CreateFileMapping упал: WinError %d", err)
        return None

    import mmap

    try:
        buffer = mmap.mmap(
            -1,
            BUFFER_SIZE,
            tagname=r"Global\apexcore_sensors",
            access=mmap.ACCESS_WRITE,
        )
    except Exception as exc:
        logger.error("mmap.mmap(...) после CreateFileMapping упал: %s", exc)
        with contextlib.suppress(Exception):
            kernel32.CloseHandle(handle)
        return None

    logger.info("Global\\apexcore_sensors mapping создан, размер=%d", BUFFER_SIZE)
    return (buffer, handle)


def _write_snapshot_to_mapping(
    mapping: tuple[Any, Any], snapshot_dict: dict[str, float]
) -> None:
    """Сериализовать snapshot и записать в начало mapping'а."""
    from apexcore.services.shm_layout import (
        BUFFER_SIZE,
        HEADER_SIZE,
        pack_snapshot,
    )

    buffer, _handle = mapping
    payload = pack_snapshot(snapshot_dict, time.time_ns())
    # Защита от переполнения — pack_snapshot уже обрезает, но на всякий.
    if len(payload) > BUFFER_SIZE:
        payload = payload[:BUFFER_SIZE]

    # Сначала пишем header, потом записи — это даёт клиенту шанс прочесть
    # старый count'/timestamp, если он скрэпит mapping ровно в момент
    # записи (что маловероятно — клиент копирует mapping в bytes за раз).
    # Чтобы избежать torn read, обнуляем count в header перед записью
    # тела, потом пишем тело, потом окончательный header.
    mapping_view = memoryview(buffer)

    # Очистить count в header (поле count = offset 16, длина 4) — это
    # самый простой барьер для клиента: count=0 значит snapshot пуст,
    # клиент возьмёт fallback вместо «полу-snapshot'а».
    # Поля header (HEADER_FMT = "<4sHHQII"):
    #   magic(4) + version(2) + flags(2) + ts_ns(8) = 16
    #   count = offset 16, len 4
    import struct

    struct.pack_into("<I", mapping_view, 16, 0)
    # Запись новых данных
    mapping_view[: len(payload)] = payload
    # Обнуляем хвост, чтобы там не оставались байты от предыдущего snapshot'а
    # с большим count. Без этого старая запись может «торчать» за пределами
    # нового count'а, и клиент с ошибкой парсинга её пропустит — что ок,
    # но шумит в debug-логах.
    if len(payload) < HEADER_SIZE + 64:
        # очищаем дальше — но не весь буфер, чтобы не тратить I/O на 64 КБ
        zero_len = min(BUFFER_SIZE - len(payload), 256)
        mapping_view[len(payload):len(payload) + zero_len] = b"\x00" * zero_len


def _close_mapping(mapping: tuple[Any, Any] | None) -> None:
    """Закрыть mmap и CreateFileMapping handle (если живы)."""
    if mapping is None:
        return
    buffer, handle = mapping
    with contextlib.suppress(Exception):
        buffer.close()
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
