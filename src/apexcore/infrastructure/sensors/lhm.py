"""Внутрипроцессное чтение температур через LibreHardwareMonitorLib.

Альтернатива внешнему процессу LibreHardwareMonitor (см. issue #17). DLL
библиотеки и драйвер WinRing0 поставляются вместе с apexcore; пользователь
не должен ничего устанавливать руками.

Архитектура:

- ленивый singleton ``_get_computer()`` инициализирует объект ``Computer`` из
  LibreHardwareMonitorLib один раз за процесс и регистрирует ``atexit``
  для корректного закрытия;
- ``read_lhm_temperatures()`` обновляет все аппаратные узлы и собирает
  только сенсоры ``SensorType.Temperature``;
- ключи имеют формат ``<hardware_kind>/<sensor_name>`` (нижний регистр,
  пробелы в подчёркивания) — стабильны между запусками.

Контракт деградации: при любой ошибке (нет pythonnet, нет .NET, DLL
не нашлась, WinRing0 не загрузился, антивирус заблокировал драйвер)
функция возвращает пустой словарь и логирует ``logger.debug``. Наружу
исключения не пробрасываются — это гарантия, что отсутствие LHM не
ломает основной flow apexcore.

Поставка .NET runtime:

- По умолчанию pythonnet использует **.NET Framework 4.8**, который идёт
  в составе Windows 10/11 из коробки — отдельно ставить не нужно. LHM-lib
  v0.9.6 собрана именно под net472, её зависимости (System.*, Microsoft.Bcl.*)
  тоже netstandard2.0 — всё совместимо с Framework 4.8.
- Опциональный escape-hatch: если выставлена переменная окружения
  ``APEXCORE_DOTNET_ROOT`` с runtime-config'ом, ``_configure_runtime``
  переключит pythonnet на coreclr (например, для self-contained .NET 8/9).
  В дефолтной поставке apexcore эта переменная не выставляется.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Параметры прогрева CPU-сенсоров после Computer.Open(). WinRing0
# регистрируется как kernel-driver синхронно, но первый MSR-read через него
# часто возвращает None — драйверу нужно пару опросов, чтобы наполнить
# внутренние кэши значений. Без прогрева самый первый внешний вызов
# read_lhm_temperatures() отдаёт температуры со всех сенсоров, КРОМЕ CPU,
# что особенно заметно при коротких стресс-прогонах: pre-flight-проверка
# в stress_menu успевает отработать на холодном LHM и пишет «температура
# CPU не считывается», хотя через 1–2 секунды она уже была бы доступна.
# См. issue #20.
_WARMUP_MAX_ATTEMPTS = 5
_WARMUP_DELAY_SEC = 0.2

# Путь к директории с LibreHardwareMonitorLib.dll и зависимостями. В dev-сборке
# это ``src/apexcore/infrastructure/sensors/lib/``; в PyInstaller-бандле
# package_data сохраняет ту же относительную раскладку.
_LIB_DIR = Path(__file__).resolve().parent / "lib"
_LIB_DLL = _LIB_DIR / "LibreHardwareMonitorLib.dll"

# Глобальный singleton: ``Computer`` инстанс LHM (или sentinel ``False``,
# если инициализация заведомо невозможна).
_lock = threading.Lock()
_computer: Any | None = None
_init_failed: bool = False
_runtime_configured: bool = False


def read_lhm_temperatures() -> dict[str, float]:
    """Собрать температуры со всех доступных аппаратных узлов LHM.

    Возвращает ``{"cpu/package": 65.4, "gpu/temperature": 51.0, ...}``.
    При недоступности LHM/драйвера/DLL — пустой словарь.
    """
    temps, _ = read_lhm_temperatures_and_voltages()
    return temps


def read_lhm_voltages() -> dict[str, float]:
    """Собрать напряжения со всех доступных аппаратных узлов LHM.

    Возвращает ``{"cpu/cpu_core": 1.25, "gpunvidia/gpu_core": 0.95, ...}``.
    Полезно при ручном разгоне: Vcore CPU/GPU, SoC, VRM. На бытовых NVIDIA
    GPU значение Vcore через LHM публикуется не всегда — это норма.
    При недоступности LHM/драйвера/DLL — пустой словарь.
    """
    _, voltages = read_lhm_temperatures_and_voltages()
    return voltages


def read_lhm_temperatures_and_voltages() -> tuple[dict[str, float], dict[str, float]]:
    """Однопроходное чтение Temperature + Voltage сенсоров.

    Сначала пробует получить значения из shared-memory snapshot'а
    (`apexcore.services.shm_adapter.read_shm_temperatures_and_voltages`)
    — это работает БЕЗ admin-прав, если установлен сервис ``apexcore_sensord``.
    Если snapshot недоступен/протух — fallback на прямой обход
    ``computer.Hardware`` (требует admin для CreateFile на PawnIO).

    Совмещает два сбора в одном обходе ``computer.Hardware`` — `hardware.Update()`
    вызывается ровно столько же раз, сколько и при сборе одних температур.
    Это исключает удвоение цены опроса при добавлении вольтажа в hot-path
    (``WindowsAdapter.get_current_metrics``).

    Возвращает кортеж ``(temperatures, voltages)``. При недоступности LHM
    — пара пустых словарей.
    """
    shm_result = _try_shm_temperatures_and_voltages()
    if shm_result is not None:
        return shm_result
    computer = _get_computer()
    if computer is None:
        return {}, {}
    try:
        return _collect_temperatures_and_voltages(computer)
    except Exception as exc:
        logger.debug("LHM read failed: %s", exc)
        return {}, {}


def _try_shm_temperatures_and_voltages() -> tuple[dict[str, float], dict[str, float]] | None:
    """Попытка прочесть temps+voltages из shared-memory snapshot'а.

    Вынесено отдельной функцией ради того, чтобы импорт `shm_adapter`
    не происходил при инициализации модуля (избегаем циклов и лишних
    зависимостей при unit-тестах LHM-фасада). При ошибке импорта — `None`.
    """
    try:
        from apexcore.services import shm_adapter
    except Exception:
        return None
    try:
        return shm_adapter.read_shm_temperatures_and_voltages()
    except Exception as exc:
        logger.debug("shm read failed: %s", exc)
        return None


def _try_shm_reader(reader_name: str) -> dict[str, float] | None:
    """Универсальная обёртка над shm_adapter.read_shm_<reader_name>.

    Возвращает результат вызова или ``None`` при любой ошибке (нет
    pywin32, mapping не существует, snapshot протух). Используется
    публичными ``read_lhm_*`` функциями для shm-first логики.
    """
    try:
        from apexcore.services import shm_adapter
        reader = getattr(shm_adapter, reader_name)
    except Exception:
        return None
    try:
        return reader()
    except Exception as exc:
        logger.debug("shm reader %s failed: %s", reader_name, exc)
        return None


# Кэш max-turbo CPU-частоты — это статичная характеристика чипа
# (max boost), её достаточно посчитать один раз за процесс. Опрос LHM
# через ``hardware.Update()`` для всех CPU-сенсоров может занимать ~1 с
# на машинах с большим числом ядер; для команды ``info`` это критично.
# Один раз попали — повторные вызовы уже мгновенные.
_cached_cpu_max_clock_mhz: float | None = None
_cpu_max_clock_lock = threading.Lock()


def read_lhm_cpu_max_clock_mhz(use_cache: bool = True) -> float | None:
    """Максимальный текущий clock CPU-ядра из LHM (МГц) — приближение turbo.

    LHM публикует ``SensorType.Clock`` для каждого ядра CPU и для bus.
    Мы фильтруем bus (типичные значения < 200 МГц) и берём максимум по
    остальным значениям. На современных Intel CPU с Turbo Boost при idle
    Speed Shift часто держит ядра на близкой к максимальной частоте,
    поэтому это значение — практическое приближение к max-turbo
    (4.9–5.2 ГГц для i9-12900K).

    Кэшируется в module-level переменной — повторные вызовы мгновенные.
    Передайте ``use_cache=False`` чтобы принудительно перечитать (например,
    в команде ``apexcore doctor``).

    Возвращает None если LHM недоступен / нет CPU-clock сенсоров. Никогда
    не бросает исключения наружу.
    """
    global _cached_cpu_max_clock_mhz
    if use_cache and _cached_cpu_max_clock_mhz is not None:
        return _cached_cpu_max_clock_mhz
    computer = _get_computer()
    if computer is None:
        return None
    try:
        with _cpu_max_clock_lock:
            if use_cache and _cached_cpu_max_clock_mhz is not None:
                return _cached_cpu_max_clock_mhz
            value = _collect_cpu_max_clock(computer)
            if value is not None:
                _cached_cpu_max_clock_mhz = value
            return value
    except Exception as exc:
        logger.debug("LHM CPU-clock read failed: %s", exc)
        return None


def read_lhm_fans() -> dict[str, float]:
    """Скорости вращения вентиляторов через LHM (об/мин).

    Возвращает ``{"fan/<sanitized_sensor_name>": rpm, ...}``. Пример:
    ``{"fan/cpu_fan": 1058.0, "fan/chassis_fan_1": 1503.0, "fan/water_pump": 2404.0,
       "fan/gpu_fan_1": 0.0, "fan/gpu_fan_2": 0.0}``.

    Собирает ``SensorType.Fan`` со ВСЕХ ``HardwareType``: материнка
    (Mainboard), CPU-кулер, GPU-кулеры. На NVIDIA GPU LHM публикует
    ``Fan 1``, ``Fan 2`` — у нас они идут с индексом.

    **0 RPM — валидное состояние** (idle GPU, zero-RPM mode современных
    кулеров), а не «не подключено». До v0.5.3 фильтр ``rpm <= 0`` отсекал
    такие fans, и при idle GPU карточка «Вентиляторы» рисовала misleading
    «нет данных от LHM», хотя физически fan'ы есть. Теперь stopped fans
    показываются как 0 RPM — пользователь видит реальное состояние, а
    дисклеймер ``_only_gpu_fans`` в render_sensors объясняет почему нет
    CPU/Chassis (LHM не парсит EC некоторых SuperIO-chip'ов).

    Отрицательные значения по-прежнему отсекаются — они означают ошибку
    sensor'а, не валидное чтение. ``sensor.Value == None`` тоже скипается.

    Пустой словарь при недоступности LHM или отсутствии вентиляторов.
    """
    shm_result = _try_shm_reader("read_shm_fans")
    if shm_result is not None:
        return shm_result
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        from LibreHardwareMonitor.Hardware import SensorType  # type: ignore

        fan_type = SensorType.Fan
        result: dict[str, float] = {}
        seen: dict[str, int] = {}
        for hw in computer.Hardware:
            try:
                hw.Update()
            except Exception as exc:
                logger.debug("LHM hw.Update упал на %s: %s", hw.Name, exc)
                continue
            hw_prefix = _hardware_prefix(hw)
            for sensor in hw.Sensors:
                if sensor.SensorType != fan_type or sensor.Value is None:
                    continue
                try:
                    rpm = float(sensor.Value)
                except (TypeError, ValueError):
                    continue
                # Отсекаем только отрицательные (sensor error). 0 RPM —
                # валидное состояние (idle GPU / zero-RPM mode), показываем.
                if rpm < 0:
                    continue
                name = _normalize_name(sensor.Name)
                # Уникализируем — если повторяется одинаковое имя (например
                # «Fan #1» на разных hardware), добавляем префикс hardware.
                key = f"fan/{name}"
                if key in result:
                    key = f"fan/{hw_prefix}_{name}"
                if key in result:
                    seen[key] = seen.get(key, 1) + 1
                    key = f"fan/{hw_prefix}_{name}_{seen[key]}"
                result[key] = rpm
        return result
    except Exception as exc:
        logger.debug("LHM fans read failed: %s", exc)
        return {}


def read_lhm_cpu_power() -> dict[str, float]:
    """Энергопотребление CPU через LHM (Вт).

    Возвращает ``{"cpu_power/package": 80.5, "cpu_power/cores": 65.2, ...}``.
    LHM публикует ``SensorType.Power`` на узлах ``HardwareType.Cpu``:
    обычно «CPU Package» (общая мощность чипа), «CPU Cores» (только ядра),
    «CPU Graphics» (iGPU если есть), «CPU Memory» (контроллер памяти).
    Префикс ``cpu_power/`` отличает power-датчики от voltage в hot-path.

    Пустой словарь при недоступности LHM или отсутствии power-датчиков
    (старые материнки/чипсеты могут не выводить).
    """
    shm_result = _try_shm_reader("read_shm_cpu_power")
    if shm_result is not None:
        return shm_result
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        from LibreHardwareMonitor.Hardware import (  # type: ignore
            HardwareType,
            SensorType,
        )

        cpu_type = HardwareType.Cpu
        power_type = SensorType.Power
        result: dict[str, float] = {}
        for hw in computer.Hardware:
            if hw.HardwareType != cpu_type:
                continue
            try:
                hw.Update()
            except Exception as exc:
                logger.debug("LHM cpu_power Update upal: %s", exc)
                continue
            for sensor in hw.Sensors:
                if sensor.SensorType != power_type or sensor.Value is None:
                    continue
                try:
                    watts = float(sensor.Value)
                except (TypeError, ValueError):
                    continue
                name = _normalize_name(sensor.Name)
                # Маппим типовые имена в ключи без префикса «cpu_»:
                # «CPU Package» → package, «CPU Cores» → cores, …
                short = name
                for prefix in ("cpu_", "cpu "):
                    if short.startswith(prefix):
                        short = short[len(prefix):]
                        break
                result[f"cpu_power/{short}"] = watts
        return result
    except Exception as exc:
        logger.debug("LHM cpu_power read failed: %s", exc)
        return {}


def read_lhm_storage_names() -> dict[str, str]:
    """Имена storage-устройств из LHM ``Computer.Hardware``.

    Возвращает ``{normalized_sensor_name: device_name}``. Например::

        {
          "composite_temperature": "Samsung SSD 980 PRO 1TB",
          "temperature": "Samsung SSD 980 PRO 1TB",
          "temperature_2": "WDC WD20EZBX",
        }

    Используется в `application/sensor_keys.py` чтобы карточка «Диски»
    показывала реальные модели вместо общего «Накопитель». Имя
    нормализуется как в ``_normalize_name`` — для устойчивого matching.

    Пустой словарь при недоступности LHM или отсутствии storage-узлов.
    Не дёргает `hardware.Update()` — имена статичные.
    """
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        from LibreHardwareMonitor.Hardware import HardwareType, SensorType  # type: ignore

        storage_type = HardwareType.Storage
        temp_type = SensorType.Temperature
        result: dict[str, str] = {}
        for hw in computer.Hardware:
            if hw.HardwareType != storage_type:
                continue
            device_name = str(hw.Name).strip() if hw.Name else ""
            if not device_name:
                continue
            for sensor in hw.Sensors:
                if sensor.SensorType != temp_type:
                    continue
                normalized = _normalize_name(sensor.Name)
                result[normalized] = device_name
        return result
    except Exception as exc:
        logger.debug("LHM storage names read failed: %s", exc)
        return {}


def read_lhm_cpu_clocks() -> dict[str, float]:
    """Собрать живые частоты всех CPU-ядер через LHM (МГц).

    LHM публикует ``SensorType.Clock`` для каждого ядра (`CPU Core #N`,
    `P-core #N`, `E-core #N`) и отдельно для bus (~100 МГц) — bus отсекаем
    по диапазону правдоподобия. Имена нормализуются как в
    ``read_lhm_temperatures`` (`cpu/p_core_1`, `cpu/e_core_1`, ...).

    Возвращает ``{"cpu/p_core_1": 4880.0, ...}``. Пустой словарь —
    LHM недоступен или CPU-clock сенсоров нет (нет админа → WinRing0 не
    зарегистрирован). Используется адаптером как primary-источник live freq
    вместо ``psutil.cpu_freq()`` (на Windows та отдаёт базовую из реестра).
    """
    shm_result = _try_shm_reader("read_shm_cpu_clocks")
    if shm_result is not None:
        return shm_result
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        return _collect_cpu_clocks(computer)
    except Exception as exc:
        logger.debug("LHM CPU-clocks read failed: %s", exc)
        return {}


def read_lhm_full_snapshot() -> dict[str, float]:
    """Однопроходный сбор ВСЕХ типов LHM-сенсоров для shared-memory сервиса.

    Используется ``apexcore_sensord`` (см. ``services/sensord.py``) — даёт
    готовый snapshot одним обходом ``Computer.Hardware``, что в 4–5 раз
    дешевле, чем последовательные ``read_lhm_temperatures_and_voltages``,
    ``read_lhm_cpu_power``, ``read_lhm_fans``, ``read_lhm_cpu_clocks``
    (каждая делает свой ``hardware.Update()`` по узлам).

    Ключи имеют **type-префикс** ``<тип>:<rest>``:

    * ``temp:<prefix>/<sensor>`` — Temperature (как в `read_lhm_temperatures`)
    * ``volt:<prefix>/<sensor>`` — Voltage (как в `read_lhm_voltages`)
    * ``power:cpu_power/<short>`` — Power по CPU (как в `read_lhm_cpu_power`)
    * ``fan:fan/<name>`` — Fan RPM (как в `read_lhm_fans`)
    * ``clock:<prefix>/<sensor>`` — Clock МГц (как в `read_lhm_cpu_clocks`)
    * ``tjmax:<prefix>/<sensor>`` — Tj_max ℃ (как в `read_lhm_tjmax`)

    Type-префиксы убираются адаптером на стороне apexcore-клиента —
    он восстанавливает старые контракты ``read_lhm_*`` функций. Зачем
    префикс: внутри одного hardware (особенно CPU) LHM выдаёт сенсоры
    Temperature и Clock с одинаковыми именами (``CPU Core #1``); без
    префикса они затёрли бы друг друга в общем dict snapshot'а.

    Возвращает плоский ``dict[key, value]`` (порядок сохраняется
    Python 3.7+ insertion order). При недоступности LHM — пустой словарь.
    """
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        return _collect_full_snapshot(computer)
    except Exception as exc:
        logger.debug("LHM full snapshot read failed: %s", exc)
        return {}


def read_lhm_tjmax() -> dict[str, float]:
    """Собрать пороги Tj_max (distance_to_tjmax → tjmax) с CPU-сенсоров LHM.

    LHM публикует параметр ``CPU Core #N Distance to TjMax`` под
    ``SensorType.Temperature``. Сам Tj_max получается как
    ``current_temp + distance_to_tjmax``. Берём текущую температуру и
    дистанцию по одному и тому же ядру и складываем — это даёт стабильный
    Tj_max для конкретного CPU.

    Возвращает ``{"cpu/cpu_core_1": 100.0, ...}``. Пустой словарь — данные
    недоступны (LHM не загружен, нет CPU-сенсоров).
    """
    shm_result = _try_shm_reader("read_shm_tjmax")
    if shm_result is not None:
        return shm_result
    computer = _get_computer()
    if computer is None:
        return {}
    try:
        return _collect_tjmax(computer)
    except Exception as exc:
        logger.debug("LHM Tj_max read failed: %s", exc)
        return {}


def is_available() -> bool:
    """Проверка, можно ли получить хотя бы один сенсор температуры через LHM.

    Используется командой ``apexcore info`` и диагностикой. Не вызывается в
    горячем пути (только в момент проверки); внутри всё равно дёргает
    ``_get_computer()``.
    """
    return bool(read_lhm_temperatures())


def _get_computer() -> Any | None:
    """Ленивая инициализация ``Computer`` (потокобезопасная)."""
    global _computer, _init_failed
    if _init_failed:
        return None
    if _computer is not None:
        return _computer
    with _lock:
        if _init_failed:
            return None
        if _computer is not None:
            return _computer
        try:
            _configure_runtime()
            computer = _open_computer()
        except Exception as exc:
            logger.debug("LHM init failed: %s", exc)
            _init_failed = True
            return None
        _computer = computer
        atexit.register(_close_computer)
        return _computer


def _configure_runtime() -> None:
    """Настроить .NET runtime для pythonnet до первого ``import clr``.

    Вызов идемпотентен. Приоритет источников (изменён в v0.9.0):

    1. **.NET Framework 4.8 через mscoree** (предустановлен в Win10/11) —
       ОСНОВНОЙ путь. LHM v0.9+ использует ``MutexAccessRule`` который есть
       в mscorlib.dll .NET Framework, но ОТСУТСТВУЕТ в .NET 9 standalone
       runtime (System.Threading.AccessControl там отдельным NuGet-пакетом,
       не входит в Microsoft.NETCore.App). На .NET 9 через clr_loader.coreclr
       LHM падает с TypeLoadException в Computer.Open() → frozen sensord
       не входит в Running.
    2. ``APEXCORE_DOTNET_ROOT`` env-var — fallback для случая когда netfx
       недоступен (Astra/Wine/Insider build без 4.8).
    3. Bundled .NET 9 — последний fallback (deprecated, оставлен для совместимости).
    """
    global _runtime_configured
    if _runtime_configured:
        return
    _runtime_configured = True

    # 1) Windows: пытаемся .NET Framework 4.8 first — это нативный путь
    # для LHM-DLL (она собрана для .NET Framework 4.7.2, все типы найдутся
    # в mscorlib/System.dll без отдельных NuGet-пакетов).
    if sys.platform == "win32":
        try:
            from pythonnet import load  # type: ignore
            load("netfx")
            logger.debug("LHM: настроен .NET Framework 4.8 (mscoree)")
            return
        except Exception as exc:
            logger.debug("pythonnet.load(netfx) upal: %s — fallback на coreclr", exc)

    # 2) Explicit env-var (dev / CI / Astra с custom .NET path).
    dotnet_root = os.environ.get("APEXCORE_DOTNET_ROOT")

    # 3) Bundled .NET 9 рядом с apexcore.exe (deprecated fallback,
    # для несовместимых OS где netfx недоступен).
    if not dotnet_root:
        bundled = _find_bundled_dotnet_root()
        if bundled is not None:
            dotnet_root = str(bundled)

    if not dotnet_root:
        return  # Pythonnet попытается auto-detect.

    runtime_config = Path(dotnet_root) / "apexcore.runtimeconfig.json"
    if not runtime_config.exists():
        logger.debug(
            "DOTNET_ROOT=%s но runtimeconfig не найден, fallback на default",
            dotnet_root,
        )
        return
    try:
        from clr_loader import get_coreclr  # type: ignore
        from pythonnet import set_runtime  # type: ignore
    except ImportError as exc:
        logger.debug("pythonnet/clr_loader недоступны: %s", exc)
        return
    try:
        os.environ.setdefault("DOTNET_ROOT", dotnet_root)
        set_runtime(get_coreclr(runtime_config=str(runtime_config)))
        logger.debug("LHM: fallback coreclr из %s", dotnet_root)
    except Exception as exc:
        logger.debug("set_runtime(coreclr) upal: %s", exc)


def _find_bundled_dotnet_root() -> Path | None:
    """Найти bundled .NET 9 в ``<install_root>/dotnet/`` (PyInstaller).

    После P0.8 ``build_windows.ps1`` кладёт runtime в подкаталог
    ``dotnet/`` рядом с ``apexcore.exe``. На dev-машине (editable
    install) этой папки нет — возвращает ``None``.
    """
    exe_path = Path(sys.executable).resolve()
    candidates: list[Path] = [exe_path.parent / "dotnet"]
    if len(exe_path.parents) > 1:
        candidates.append(exe_path.parents[1] / "dotnet")
    for c in candidates:
        if (c / "shared" / "Microsoft.NETCore.App").exists():
            return c
        if (c / "host" / "fxr").exists():
            return c
    return None


def _open_computer() -> Any:
    """Загрузить DLL и открыть ``Computer`` со всеми аппаратными источниками."""
    if not _LIB_DLL.exists():
        raise FileNotFoundError(
            f"LibreHardwareMonitorLib.dll не найден в {_LIB_DIR}; "
            "запустите scripts/fetch_lhm.ps1 для скачивания"
        )

    # Прокидываем lib/ в sys.path, чтобы pythonnet корректно находил DLL'и.
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))

    import clr  # type: ignore

    clr.AddReference(str(_LIB_DLL))

    from LibreHardwareMonitor.Hardware import Computer  # type: ignore

    computer = Computer()
    computer.IsCpuEnabled = True
    computer.IsGpuEnabled = True
    computer.IsMotherboardEnabled = True
    computer.IsMemoryEnabled = True
    computer.IsControllerEnabled = True
    computer.IsStorageEnabled = True
    computer.Open()
    logger.debug("LHM: Computer открыт, источников = %d", len(list(computer.Hardware)))
    _warmup_cpu_sensors(computer)
    return computer


def _warmup_cpu_sensors(computer: Any) -> None:
    """Дождаться появления CPU-температуры в LHM после Computer.Open().

    Прогревает WinRing0 короткими повторными опросами: тратит до
    ``_WARMUP_MAX_ATTEMPTS × _WARMUP_DELAY_SEC`` секунд (≈1 с в худшем
    случае). На уже прогретой машине возвращается мгновенно после первой
    итерации; если CPU-температура так и не появилась — молча выходит и не
    блокирует общий путь init (graceful degrade сохраняется).

    Без этого прогрева первый ``read_lhm_temperatures()`` (например, из
    pre-flight ``_detect_cpu_temp_source`` в stress_menu) часто видит
    непустой словарь без CPU-ключей и сообщает пользователю «температура
    CPU не считывается». См. issue #20.
    """
    for attempt in range(_WARMUP_MAX_ATTEMPTS):
        try:
            temps = _collect_temperatures(computer)
        except Exception as exc:
            logger.debug("LHM warmup: _collect_temperatures упал: %s", exc)
            return
        if any(k.startswith("cpu/") for k in temps):
            logger.debug(
                "LHM warmup: CPU-температура появилась на попытке %d/%d",
                attempt + 1,
                _WARMUP_MAX_ATTEMPTS,
            )
            return
        if attempt < _WARMUP_MAX_ATTEMPTS - 1:
            time.sleep(_WARMUP_DELAY_SEC)
    logger.debug(
        "LHM warmup: CPU-температура не появилась за %d попыток (~%.1f с)",
        _WARMUP_MAX_ATTEMPTS,
        _WARMUP_MAX_ATTEMPTS * _WARMUP_DELAY_SEC,
    )


def _close_computer() -> None:
    global _computer
    if _computer is None:
        return
    try:
        _computer.Close()
    except Exception as exc:
        logger.debug("LHM Close failed: %s", exc)
    _computer = None


def _collect_temperatures(computer: Any) -> dict[str, float]:
    """Обойти иерархию ``Hardware → SubHardware → Sensors`` и собрать температуры."""
    temps, _ = _collect_temperatures_and_voltages(computer)
    return temps


def _collect_temperatures_and_voltages(
    computer: Any,
) -> tuple[dict[str, float], dict[str, float]]:
    """Однопроходный сбор Temperature и Voltage по всем узлам LHM.

    Один проход = один ``hardware.Update()`` на узел; внутри проверяем
    ``sensor.SensorType`` и раскладываем в два словаря. Это позволяет
    добавить вольтаж в hot-path без удвоения цены LHM-опроса.
    """
    # Импорт SensorType отложен — модуль загружается динамически через CLR.
    from LibreHardwareMonitor.Hardware import SensorType  # type: ignore

    temp_type = SensorType.Temperature
    voltage_type = SensorType.Voltage
    temps: dict[str, float] = {}
    voltages: dict[str, float] = {}

    for hardware in computer.Hardware:
        try:
            hardware.Update()
        except Exception as exc:
            logger.debug("LHM hardware.Update upal: %s", exc)
            continue
        prefix = _hardware_prefix(hardware)
        _collect_temp_voltage_from_sensors(
            hardware, temp_type, voltage_type, prefix, temps, voltages
        )
        for sub in hardware.SubHardware:
            try:
                sub.Update()
            except Exception as exc:
                logger.debug("LHM sub.Update upal: %s", exc)
                continue
            _collect_temp_voltage_from_sensors(
                sub, temp_type, voltage_type, prefix, temps, voltages
            )
    return temps, voltages


def _collect_temp_voltage_from_sensors(
    node: Any,
    temp_type: Any,
    voltage_type: Any,
    prefix: str,
    temps: dict[str, float],
    voltages: dict[str, float],
) -> None:
    for sensor in node.Sensors:
        sensor_type = sensor.SensorType
        if sensor_type == temp_type:
            value = sensor.Value
            if value is None:
                continue
            normalized = _normalize_name(sensor.Name)
            if not _is_instantaneous_temp(normalized):
                continue
            temps[f"{prefix}/{normalized}"] = float(value)
        elif sensor_type == voltage_type:
            value = sensor.Value
            if value is None:
                continue
            normalized = _normalize_name(sensor.Name)
            voltages[f"{prefix}/{normalized}"] = float(value)


def _collect_from_sensors(
    node: Any,
    temp_type: Any,
    prefix: str,
    result: dict[str, float],
) -> None:
    """Сборщик температур (исторический хелпер, оставлен для обратной совместимости).

    Используется ``_collect_tjmax`` для пары current↔distance_to_tjmax.
    Новый hot-path (`get_current_metrics`) использует
    :func:`_collect_temperatures_and_voltages`.
    """
    for sensor in node.Sensors:
        if sensor.SensorType != temp_type:
            continue
        value = sensor.Value
        if value is None:
            continue
        normalized = _normalize_name(sensor.Name)
        if not _is_instantaneous_temp(normalized):
            continue
        result[f"{prefix}/{normalized}"] = float(value)


# LHM публикует под SensorType.Temperature не только мгновенные температуры,
# но и константы-пороги/параметры датчиков. Их подмешивать в metrics_history
# нельзя — `application/thermal.py` посчитает их за реальную температуру и
# сломает Frame-Rate-Stability метрику. Фильтруем по нормализованному имени.

_NON_TEMPERATURE_SUFFIXES = (
    # *_low_limit, *_high_limit, *_critical_low_limit, *_critical_high_limit —
    # пороги (memory thermal sensor).
    "_limit",
    # *_resolution — точность датчика, обычно 0.5 °C.
    "_resolution",
    # *_distance_to_tjmax — это дельта (запас до троттлинга), а не нагрев.
    "_distance_to_tjmax",
)

_NON_TEMPERATURE_PREFIXES = (
    # storage/warning_temperature — порог предупреждения.
    "warning_",
    # storage/critical_temperature — порог критической температуры.
    "critical_",
)


def _is_instantaneous_temp(name: str) -> bool:
    """Отфильтровать пороги/параметры, публикуемые LHM под SensorType.Temperature."""
    if name.endswith(_NON_TEMPERATURE_SUFFIXES):
        return False
    return not name.startswith(_NON_TEMPERATURE_PREFIXES)


def _hardware_prefix(hardware: Any) -> str:
    """Привести ``HardwareType`` к стабильному префиксу-ключу."""
    raw = str(hardware.HardwareType)
    # Обычные значения: "Cpu", "GpuNvidia", "GpuAmd", "GpuIntel", "Motherboard",
    # "SuperIO", "Memory", "Storage", "Network".
    return _normalize_name(raw)


_NAME_CLEAN = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: str) -> str:
    """``"CPU Core #1"`` → ``"cpu_core_1"``."""
    cleaned = _NAME_CLEAN.sub("_", name.strip().lower()).strip("_")
    return cleaned or "unknown"


# Минимально вменяемые границы Tj_max — отбраковка артефактов сенсоров.
_MIN_PLAUSIBLE_TJMAX = 60.0
_MAX_PLAUSIBLE_TJMAX = 130.0


def _collect_tjmax(computer: Any) -> dict[str, float]:
    """Собрать пары (текущая T, distance_to_tjmax) и сложить их в Tj_max.

    LHM публикует ``Distance to TjMax`` как ``SensorType.Temperature`` с
    суффиксом ``_distance_to_tjmax``. Парный «настоящий» сенсор имеет
    то же базовое имя без суффикса.
    """
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType  # type: ignore

    temp_type = SensorType.Temperature
    cpu_type = HardwareType.Cpu

    # Собираем сначала все температурные значения из CPU-узлов.
    pairs: dict[str, float] = {}
    distances: dict[str, float] = {}

    for hardware in computer.Hardware:
        if hardware.HardwareType != cpu_type:
            continue
        try:
            hardware.Update()
        except Exception:
            continue
        prefix = _hardware_prefix(hardware)
        for sensor in hardware.Sensors:
            if sensor.SensorType != temp_type or sensor.Value is None:
                continue
            normalized = _normalize_name(sensor.Name)
            value = float(sensor.Value)
            if normalized.endswith("_distance_to_tjmax"):
                base = normalized[: -len("_distance_to_tjmax")]
                distances[f"{prefix}/{base}"] = value
            elif _is_instantaneous_temp(normalized):
                pairs[f"{prefix}/{normalized}"] = value

    result: dict[str, float] = {}
    for key, distance in distances.items():
        current = pairs.get(key)
        if current is None:
            continue
        tjmax = current + distance
        if not (_MIN_PLAUSIBLE_TJMAX <= tjmax <= _MAX_PLAUSIBLE_TJMAX):
            continue
        result[key] = tjmax
    return result


# Вменяемый диапазон CPU clocks (МГц): отбрасываем bus speed (~100), и
# одновременно отлавливаем артефакты сенсоров (>10 ГГц = ошибка).
_MIN_PLAUSIBLE_CPU_CLOCK_MHZ = 200.0
_MAX_PLAUSIBLE_CPU_CLOCK_MHZ = 10000.0


def _collect_cpu_max_clock(computer: Any) -> float | None:
    """Найти максимальный clock среди CPU-сенсоров типа ``Clock``.

    LHM публикует ``SensorType.Clock`` для каждого ядра CPU и отдельно для
    bus (~100 МГц). Берём максимум по ядрам — на CPU с активным Turbo
    Boost это и есть max-turbo (или близко к нему даже в idle, благодаря
    Speed Shift).
    """
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType  # type: ignore

    clock_type = SensorType.Clock
    cpu_type = HardwareType.Cpu

    max_clock: float | None = None
    for hardware in computer.Hardware:
        if hardware.HardwareType != cpu_type:
            continue
        try:
            hardware.Update()
        except Exception as exc:
            logger.debug("LHM hardware.Update upal: %s", exc)
            continue
        for sensor in hardware.Sensors:
            if sensor.SensorType != clock_type or sensor.Value is None:
                continue
            value = float(sensor.Value)
            if not (
                _MIN_PLAUSIBLE_CPU_CLOCK_MHZ
                <= value
                <= _MAX_PLAUSIBLE_CPU_CLOCK_MHZ
            ):
                continue
            if max_clock is None or value > max_clock:
                max_clock = value
    return max_clock


def _collect_cpu_clocks(computer: Any) -> dict[str, float]:
    """Собрать per-core частоты с CPU-сенсоров типа ``Clock``.

    Возвращает словарь нормализованных имён → МГц. Bus-clock и значения
    вне диапазона правдоподобия (200–10000 МГц) отсекаются. Имена приводятся
    к виду ``cpu/p_core_1``, ``cpu/e_core_1`` — тот же стиль, что в
    ``read_lhm_temperatures``.
    """
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType  # type: ignore

    clock_type = SensorType.Clock
    cpu_type = HardwareType.Cpu

    result: dict[str, float] = {}
    for hardware in computer.Hardware:
        if hardware.HardwareType != cpu_type:
            continue
        try:
            hardware.Update()
        except Exception as exc:
            logger.debug("LHM hardware.Update upal: %s", exc)
            continue
        prefix = _hardware_prefix(hardware)
        for sensor in hardware.Sensors:
            if sensor.SensorType != clock_type or sensor.Value is None:
                continue
            value = float(sensor.Value)
            if not (
                _MIN_PLAUSIBLE_CPU_CLOCK_MHZ
                <= value
                <= _MAX_PLAUSIBLE_CPU_CLOCK_MHZ
            ):
                continue
            name = _normalize_name(sensor.Name)
            result[f"{prefix}/{name}"] = value
    return result


# Префиксы типов в shared-memory snapshot — см. документацию
# :func:`read_lhm_full_snapshot`. Объявлены модульно, чтобы клиент
# (`shm_adapter`) использовал ровно те же константы при разборе.
SHM_PREFIX_TEMP: str = "temp:"
SHM_PREFIX_VOLT: str = "volt:"
SHM_PREFIX_POWER: str = "power:"
SHM_PREFIX_FAN: str = "fan:"
SHM_PREFIX_CLOCK: str = "clock:"
SHM_PREFIX_TJMAX: str = "tjmax:"


def _collect_full_snapshot(computer: Any) -> dict[str, float]:
    """Один проход по `Computer.Hardware` со сбором всех типов сенсоров.

    Вызывает ``hardware.Update()`` ровно один раз на каждый узел верхнего
    уровня и один раз на каждый SubHardware. Извлекает Temperature, Voltage,
    Power, Fan, Clock — раскладывает по type-префиксированным ключам.
    Дополнительно по CPU-узлам считает Tj_max через пары current↔distance.

    Логика индивидуальных функций воспроизведена с теми же правилами
    фильтрации (например, ``_is_instantaneous_temp`` отсекает пороги
    и параметры), чтобы snapshot был drop-in заменой их вызовов.
    """
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType  # type: ignore

    temp_type = SensorType.Temperature
    voltage_type = SensorType.Voltage
    power_type = SensorType.Power
    fan_type = SensorType.Fan
    clock_type = SensorType.Clock
    cpu_type = HardwareType.Cpu

    snapshot: dict[str, float] = {}
    # Дубликаты имени fan на разных hardware — счётчик для уникализации
    # ровно как в `read_lhm_fans` (общий fan/cpu_fan / fan/mainboard_cpu_fan_2).
    fan_seen: dict[str, int] = {}
    # Для Tj_max нужны парные значения current↔distance по CPU-сенсорам.
    cpu_temps_pairs: dict[str, float] = {}
    cpu_temp_distances: dict[str, float] = {}

    for hardware in computer.Hardware:
        try:
            hardware.Update()
        except Exception as exc:
            logger.debug("LHM full-snapshot hw.Update upal: %s", exc)
            continue
        prefix = _hardware_prefix(hardware)
        is_cpu = hardware.HardwareType == cpu_type
        _emit_full_sensors(
            node=hardware,
            prefix=prefix,
            is_cpu=is_cpu,
            temp_type=temp_type,
            voltage_type=voltage_type,
            power_type=power_type,
            fan_type=fan_type,
            clock_type=clock_type,
            snapshot=snapshot,
            fan_seen=fan_seen,
            cpu_temps_pairs=cpu_temps_pairs,
            cpu_temp_distances=cpu_temp_distances,
        )
        for sub in hardware.SubHardware:
            try:
                sub.Update()
            except Exception as exc:
                logger.debug("LHM full-snapshot sub.Update upal: %s", exc)
                continue
            _emit_full_sensors(
                node=sub,
                prefix=prefix,
                is_cpu=is_cpu,
                temp_type=temp_type,
                voltage_type=voltage_type,
                power_type=power_type,
                fan_type=fan_type,
                clock_type=clock_type,
                snapshot=snapshot,
                fan_seen=fan_seen,
                cpu_temps_pairs=cpu_temps_pairs,
                cpu_temp_distances=cpu_temp_distances,
            )

    for key, distance in cpu_temp_distances.items():
        current = cpu_temps_pairs.get(key)
        if current is None:
            continue
        tjmax = current + distance
        if not (_MIN_PLAUSIBLE_TJMAX <= tjmax <= _MAX_PLAUSIBLE_TJMAX):
            continue
        snapshot[f"{SHM_PREFIX_TJMAX}{key}"] = tjmax

    return snapshot


def _emit_full_sensors(
    *,
    node: Any,
    prefix: str,
    is_cpu: bool,
    temp_type: Any,
    voltage_type: Any,
    power_type: Any,
    fan_type: Any,
    clock_type: Any,
    snapshot: dict[str, float],
    fan_seen: dict[str, int],
    cpu_temps_pairs: dict[str, float],
    cpu_temp_distances: dict[str, float],
) -> None:
    """Извлечение сенсоров одного узла; ветвление по ``SensorType``."""
    for sensor in node.Sensors:
        sensor_type = sensor.SensorType
        value = sensor.Value
        if value is None:
            continue
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            continue
        name = _normalize_name(sensor.Name)

        if sensor_type == temp_type:
            # Различаем мгновенные температуры и distance_to_tjmax по CPU.
            full_key = f"{prefix}/{name}"
            if name.endswith("_distance_to_tjmax"):
                if is_cpu:
                    base = name[: -len("_distance_to_tjmax")]
                    cpu_temp_distances[f"{prefix}/{base}"] = fvalue
                continue
            if not _is_instantaneous_temp(name):
                continue
            snapshot[f"{SHM_PREFIX_TEMP}{full_key}"] = fvalue
            if is_cpu:
                cpu_temps_pairs[full_key] = fvalue

        elif sensor_type == voltage_type:
            snapshot[f"{SHM_PREFIX_VOLT}{prefix}/{name}"] = fvalue

        elif sensor_type == power_type and is_cpu:
            # Имена «CPU Package»/«CPU Cores»/… ужимаем до package/cores
            # ровно как в `read_lhm_cpu_power`.
            short = name
            for raw_prefix in ("cpu_", "cpu "):
                if short.startswith(raw_prefix):
                    short = short[len(raw_prefix):]
                    break
            snapshot[f"{SHM_PREFIX_POWER}cpu_power/{short}"] = fvalue

        elif sensor_type == fan_type:
            # Логика как в `read_lhm_fans`: нулевые rpm не публикуем,
            # дубликаты имени — префиксуем hardware.
            if fvalue <= 0:
                continue
            key = f"fan/{name}"
            if f"{SHM_PREFIX_FAN}{key}" in snapshot:
                key = f"fan/{prefix}_{name}"
            if f"{SHM_PREFIX_FAN}{key}" in snapshot:
                fan_seen[key] = fan_seen.get(key, 1) + 1
                key = f"fan/{prefix}_{name}_{fan_seen[key]}"
            snapshot[f"{SHM_PREFIX_FAN}{key}"] = fvalue

        elif sensor_type == clock_type and is_cpu:
            if not (
                _MIN_PLAUSIBLE_CPU_CLOCK_MHZ
                <= fvalue
                <= _MAX_PLAUSIBLE_CPU_CLOCK_MHZ
            ):
                continue
            snapshot[f"{SHM_PREFIX_CLOCK}{prefix}/{name}"] = fvalue
