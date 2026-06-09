"""Чтение температур через нативные WMI/CIM-провайдеры Windows.

Модуль не зависит ни от каких сторонних процессов — используется как fallback,
когда внутрипроцессный LHM (``apexcore.infrastructure.sensors.lhm``) недоступен
(нет pythonnet, нет .NET, не загружен драйвер WinRing0).

Доступные источники:

- ``read_perf_counter_thermal_zone()`` — `\\Thermal Zone Information(*)\\Temperature`
  через PowerShell + Get-Counter. Без admin, без Python-пакета ``wmi``.
- ``read_msacpi_thermal_zone()`` — `MSAcpi_ThermalZoneTemperature` (root/wmi)
  через Python-пакет ``wmi`` или PowerShell Get-CimInstance. Часто требует
  прав администратора и работает не на всех чипсетах.

Все функции возвращают ``dict[str, float]`` (имя сенсора → °C). При любых
ошибках возвращают пустой словарь и логируют ``logger.debug`` — наружу
исключения не пробрасываются, это контракт graceful degradation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import subprocess
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Module-level кэш: «Python-пакет ``wmi`` недоступен в этом процессе».
# После первой неудачи больше не пытаемся — чтобы не платить ~0.5 с на каждый
# тик телеметрии за бесполезный повторный импорт.
#
# Зачем такой кэш. Сам ``import wmi`` на module-level вызывает
# ``GetObject("winmgmts:")``, что требует **инициализированного COM-апартмента
# в текущем потоке**. ``TelemetryService._run`` крутится в отдельном
# background-thread, в котором COM не инициализирован, и импорт падает с
# ``com_error: -2147221020 (MK_E_SYNTAX)`` — это плавающая ошибка: иногда
# COM уже инициализирован другими модулями (pythonnet/LHM в main-thread),
# и импорт проходит. Подробности — в ``ARCHITECTURE.md`` секция «CLI-меню».
#
# **Регрессионный инвариант** (см. ``ARCHITECTURE.md``): этот флаг + широкий
# ``except Exception`` должны сохраняться даже после введения dedicated
# worker'а (P1.3). Без них классы failure modes снова прорываются в
# TelemetryService как «Сбор метрик завершился ошибкой».
_WMI_PACKAGE_BROKEN = False


# ─── P1.3: Dedicated WMI worker thread с COM apartment ─────────────────────


class _WmiWorker:
    """Singleton thread с инициализированным COM apartment для пакета ``wmi``.

    Решает root cause плавающей COM-ошибки: на background-потоке без
    ``CoInitializeEx`` модуль ``wmi`` падает на module-level
    ``GetObject("winmgmts:")`` с ``com_error MK_E_SYNTAX``. Раньше этот
    класс failure mode обходился через ``_WMI_PACKAGE_BROKEN`` —
    «вообще не пытаться» (см. ARCHITECTURE.md). Worker даёт правильное
    решение: WMI-запрос идёт из своего потока с COM_APARTMENTTHREADED.

    Архитектура:

    - один daemon-thread на процесс (lazy start при первом запросе);
    - ``queue.Queue`` для request/response: вызывающий поток кладёт
      ``(kind, response_queue)``, worker отвечает ``(ok, data)``;
    - timeout 2 с на ответ — если worker завис на WMI вызове, не
      блокируем тик телеметрии;
    - если worker не стартовал (нет ``pythoncom``, нет ``wmi``,
      CoInit упал) — singleton помечается failed, дальнейшие запросы
      возвращают None мгновенно;
    - **safety net**: при failed worker'е ``read_msacpi_thermal_zone``
      идёт в legacy-путь (прямой ``import wmi`` + флаг
      ``_WMI_PACKAGE_BROKEN``) — это сохраняет регрессионный инвариант.

    Тесты подменяют ``_get_wmi_worker`` через ``_reset_wmi_worker_for_tests``.
    """

    _SENTINEL_SHUTDOWN: tuple[str, Any] = ("__shutdown__", None)

    def __init__(self) -> None:
        self._req_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._ready = threading.Event()
        self._failed = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="apexcore-wmi-worker",
        )

    def start(self, init_timeout: float = 2.0) -> bool:
        """Запустить thread; вернуть True если COM init + ``import wmi`` ОК."""
        self._thread.start()
        if not self._ready.wait(init_timeout):
            self._failed = True
            return False
        return not self._failed

    @property
    def failed(self) -> bool:
        return self._failed or not self._thread.is_alive()

    def query_msacpi(self, timeout: float = 2.0) -> dict[str, float] | None:
        """Прислать запрос MSAcpi. None — timeout / worker сломан / WMI вернул ошибку."""
        if self.failed:
            return None
        response_q: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        try:
            self._req_q.put_nowait(("msacpi", response_q))
        except queue.Full:
            return None
        try:
            ok, data = response_q.get(timeout=timeout)
        except queue.Empty:
            # Worker не ответил за timeout — считаем что он завис на WMI.
            # Не помечаем failed: следующий запрос может пройти.
            return None
        return data if ok else None

    def _run(self) -> None:
        """Thread target: CoInitializeEx + цикл по очереди + CoUninitialize."""
        pythoncom = None
        wmi_mod = None
        try:
            import pythoncom  # type: ignore

            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            import wmi as wmi_mod  # type: ignore
        except Exception as exc:
            # Любой сбой инициализации = worker не запустился.
            logger.debug("WMI worker init упал: %r", exc)
            self._failed = True
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                kind, response_q = self._req_q.get()
                if kind == self._SENTINEL_SHUTDOWN[0]:
                    break
                try:
                    if kind == "msacpi":
                        c = wmi_mod.WMI(namespace="root\\wmi")
                        zones = c.MSAcpi_ThermalZoneTemperature()
                        data: dict[str, float] = {
                            f"thermal_zone_{idx}": (float(z.CurrentTemperature) / 10.0)
                            - 273.15
                            for idx, z in enumerate(zones)
                            if getattr(z, "CurrentTemperature", None)
                        }
                        response_q.put((True, data))
                    else:
                        response_q.put((False, None))
                except Exception as exc:
                    logger.debug("WMI worker query упал (%s): %r", kind, exc)
                    response_q.put((False, None))
        finally:
            if pythoncom is not None:
                with contextlib.suppress(Exception):
                    pythoncom.CoUninitialize()


_WMI_WORKER_INSTANCE: _WmiWorker | None = None
_WMI_WORKER_LOCK = threading.Lock()


def _get_wmi_worker() -> _WmiWorker | None:
    """Singleton accessor для worker'а. ``None`` если worker не работает."""
    global _WMI_WORKER_INSTANCE
    if _WMI_WORKER_INSTANCE is not None:
        return None if _WMI_WORKER_INSTANCE.failed else _WMI_WORKER_INSTANCE
    with _WMI_WORKER_LOCK:
        if _WMI_WORKER_INSTANCE is None:
            worker = _WmiWorker()
            worker.start()
            _WMI_WORKER_INSTANCE = worker
    return None if _WMI_WORKER_INSTANCE.failed else _WMI_WORKER_INSTANCE


def _reset_wmi_worker_for_tests() -> None:
    """Тестовый hook: обнулить singleton (после shutdown-у нагрузки на CI нет).

    Не вызывать в production: оставшийся thread продолжит жить как daemon
    и тихо умрёт при завершении процесса. Для тестов это OK.
    """
    global _WMI_WORKER_INSTANCE
    with _WMI_WORKER_LOCK:
        _WMI_WORKER_INSTANCE = None


def read_perf_counter_thermal_zone() -> dict[str, float]:
    """Чтение температур через Performance Counter ``Thermal Zone Information``.

    Перфкаунтер исторически отдаёт значение в °F — приводим к °C.
    """
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-Counter '\\Thermal Zone Information(*)\\Temperature' "
            "| Select-Object -ExpandProperty CounterSamples "
            "| Select-Object Path,CookedValue "
            "| ConvertTo-Json -Compress"
        ),
    ]
    raw = _run_powershell_json(cmd)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        logger.debug("perf-counter thermal zone: bad json: %s", exc)
        return {}
    rows = parsed if isinstance(parsed, list) else [parsed]
    result: dict[str, float] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        path = str(row.get("Path") or f"counter_{idx}")
        value = row.get("CookedValue")
        if value is None:
            continue
        result[path] = (float(value) - 32.0) * (5.0 / 9.0)
    return result


def read_msacpi_thermal_zone() -> dict[str, float]:
    """Прочитать ``MSAcpi_ThermalZoneTemperature`` через Python-пакет wmi.

    Порядок попыток (см. P1.3):

    1. **Dedicated WMI worker** (``_WmiWorker``) — выделенный thread с
       инициализированным COM apartment. Покрывает основную причину
       COM-ошибки из background-потоков (P1.3).
    2. **Legacy путь**: прямой ``import wmi`` в текущем потоке. Работает
       если main-thread уже инициализировал COM (LHM/pythonnet делает
       это побочно). Сбой → выставляем ``_WMI_PACKAGE_BROKEN`` навсегда.
    3. **CIM-fallback**: ``Get-CimInstance`` через PowerShell.

    ``_WMI_PACKAGE_BROKEN`` сохранён как safety-net инвариант (см.
    ``ARCHITECTURE.md``) — он сужает попытки в (2) после первого фатального
    сбоя на любой класс failure mode. Worker'а это не касается:
    он самостоятельно помечается ``failed`` при init-сбое.
    """
    global _WMI_PACKAGE_BROKEN
    if _WMI_PACKAGE_BROKEN:
        return _read_msacpi_via_cim()

    # 1) Сначала пробуем worker. Он импортирует ``wmi`` внутри своего
    # потока с CoInitializeEx, что обходит плавающую COM-ошибку.
    worker = _get_wmi_worker()
    if worker is not None:
        data = worker.query_msacpi(timeout=2.0)
        if data is not None:
            return data
        # Worker timeout / runtime-сбой — не помечаем _WMI_PACKAGE_BROKEN,
        # это может быть транзиентная проблема (WMI занят). Идём в legacy.

    # 2) Legacy путь. Сохранён ради совместимости с main-thread сценарием
    # (LHM/pythonnet может инициализировать COM как побочный эффект).
    try:
        import wmi  # type: ignore
    # Намеренно широкий ``except``: помимо ``ImportError`` импорт ``wmi``
    # на module-level дёргает COM (``GetObject("winmgmts:")``), что в
    # фоновом потоке без инициализированного COM-апартмента бросает
    # ``com_error MK_E_SYNTAX (-2147221020)`` или ``CO_E_NOTINITIALIZED``.
    # Любой такой сбой = «пакет в этом процессе не работает» → CIM.
    except Exception as exc:
        logger.debug(
            "wmi-пакет недоступен (%s) — переключаемся на CIM-fallback навсегда",
            exc,
        )
        _WMI_PACKAGE_BROKEN = True
        return _read_msacpi_via_cim()
    try:
        c = wmi.WMI(namespace="root\\wmi")
        zones = c.MSAcpi_ThermalZoneTemperature()
        return {
            f"thermal_zone_{idx}": (float(z.CurrentTemperature) / 10.0) - 273.15
            for idx, z in enumerate(zones)
            if getattr(z, "CurrentTemperature", None)
        }
    except Exception as exc:
        # Runtime-сбой при работе с уже импортированным wmi (тоже COM-issue).
        logger.debug("WMI MSAcpi thermal zone read failed: %s", exc)
        return _read_msacpi_via_cim()


def _read_msacpi_via_cim() -> dict[str, float]:
    """Fallback: ``MSAcpi_ThermalZoneTemperature`` через PowerShell Get-CimInstance."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
            "| Select-Object CurrentTemperature "
            "| ConvertTo-Json -Compress"
        ),
    ]
    raw = _run_powershell_json(cmd)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        logger.debug("MSAcpi via CIM: bad json: %s", exc)
        return {}
    rows = parsed if isinstance(parsed, list) else [parsed]
    result: dict[str, float] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        raw_value = row.get("CurrentTemperature")
        if raw_value is None:
            continue
        result[f"thermal_zone_{idx}"] = (float(raw_value) / 10.0) - 273.15
    return result


def _run_powershell_json(cmd: list[str], timeout: float = 3.0) -> str | None:
    """Запустить PowerShell, вернуть stdout (stripped) или None при любой ошибке."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("powershell call failed: %s", exc)
        return None
    return out or None
