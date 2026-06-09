"""Чтение NVIDIA GPU через NVML (pynvml).

Дополняет ``infrastructure/sensors/lhm.py`` метриками, которых LHM не отдаёт
(utilization GPU/Memory, абсолютные пороги slowdown/shutdown). На Windows
LHM остаётся primary-источником GPU-температуры (он видит
``memory_junction`` на потребительских RTX 30/40, который NVML не публикует).
На Linux NVML — единственный источник для NVIDIA GPU.

Контракт деградации: при отсутствии NVIDIA-драйвера, нет ``pynvml`` или
ошибке инициализации все ``read_*`` функции возвращают пустой словарь и
логируют ``logger.debug``. Наружу исключения не пробрасываются.

Префикс ключей — ``nvml/<device_n>/<metric>``. Использование префикса
``nvml/`` вместо ``gpunvidia/`` гарантирует, что значения, полученные через
LHM и через NVML, не перетирают друг друга в `MetricSnapshot`.
"""

from __future__ import annotations

import atexit
import logging
import threading

logger = logging.getLogger(__name__)

# Глобальный singleton: инициализирован ли NVML. NVML — process-wide,
# nvmlInit() безопасно вызывать многократно, но дешевле — один раз.
_nvml_initialized: bool = False
_init_failed: bool = False
_lock = threading.Lock()


def is_available() -> bool:
    """Проверка наличия рабочего NVML/NVIDIA-драйвера.

    Используется ``application/diagnostics_sensors.py`` для вывода в
    `apexcore doctor`. Не дёргает реальные сенсоры — только init.
    """
    return _ensure_init()


def read_nvml_temperatures() -> dict[str, float]:
    """Температуры GPU NVIDIA через NVML.

    Возвращает ``{"nvml/0/temperature": 52.0, ...}``. На consumer RTX 30/40
    `memory_junction` через NVML не доступен (NVIDIA отключила в драйвере) —
    публикуем только то, что возвращает не-None.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            try:
                value = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU
                )
                if value is not None:
                    result[f"nvml/{idx}/temperature"] = float(value)
            except pynvml.NVMLError as exc:
                logger.debug("NVML temp[%d] failed: %s", idx, exc)
        return result
    except Exception as exc:
        logger.debug("NVML temperatures read failed: %s", exc)
        return {}


def read_nvml_power() -> dict[str, float]:
    """Мгновенная мощность GPU (Вт).

    Возвращает ``{"nvml/0/power_w": 180.7, ...}``. ``nvmlDeviceGetPowerUsage``
    возвращает миллиВатты — делим на 1000. Если карта не поддерживает
    power-management (старые/embedded GPU), ключ просто отсутствует.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            try:
                mw = pynvml.nvmlDeviceGetPowerUsage(handle)
                result[f"nvml/{idx}/power_w"] = float(mw) / 1000.0
            except pynvml.NVMLError as exc:
                logger.debug("NVML power[%d] failed: %s", idx, exc)
        return result
    except Exception as exc:
        logger.debug("NVML power read failed: %s", exc)
        return {}


def read_nvml_frequencies() -> dict[str, float]:
    """Частоты GPU (МГц): graphics-clock и memory-clock.

    Возвращает ``{"nvml/0/clock_graphics": 2910.0, "nvml/0/clock_memory": 10701.0}``.
    Старые `Application Clocks` и `Clock Samples` API упразднены в новых
    драйверах — используем ``nvmlDeviceGetClockInfo``.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            for clock_id, name in (
                (pynvml.NVML_CLOCK_GRAPHICS, "clock_graphics"),
                (pynvml.NVML_CLOCK_MEM, "clock_memory"),
            ):
                try:
                    value = pynvml.nvmlDeviceGetClockInfo(handle, clock_id)
                    if value:
                        result[f"nvml/{idx}/{name}"] = float(value)
                except pynvml.NVMLError as exc:
                    logger.debug("NVML clock[%d,%s] failed: %s", idx, name, exc)
        return result
    except Exception as exc:
        logger.debug("NVML frequencies read failed: %s", exc)
        return {}


def read_nvml_utilization() -> dict[str, float]:
    """Загрузка GPU и шины памяти в процентах.

    Возвращает ``{"nvml/0/util_gpu": 70.0, "nvml/0/util_mem": 17.0}``.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                result[f"nvml/{idx}/util_gpu"] = float(util.gpu)
                result[f"nvml/{idx}/util_mem"] = float(util.memory)
            except pynvml.NVMLError as exc:
                logger.debug("NVML util[%d] failed: %s", idx, exc)
        return result
    except Exception as exc:
        logger.debug("NVML utilization read failed: %s", exc)
        return {}


def read_nvml_thresholds() -> dict[str, float]:
    """Абсолютные температурные пороги: slowdown / shutdown / max-operating.

    Возвращает ``{"nvml/0/threshold_slowdown": 95.0, ...}``. Используется
    в UI «Датчики» для colour-coding (warn = slowdown, crit = shutdown).
    Старые драйверы могут не публиковать всё — берём только не-None значения.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            for thr_id, name in (
                (pynvml.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN, "threshold_slowdown"),
                (pynvml.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN, "threshold_shutdown"),
                (pynvml.NVML_TEMPERATURE_THRESHOLD_GPU_MAX, "threshold_gpu_max"),
            ):
                try:
                    value = pynvml.nvmlDeviceGetTemperatureThreshold(handle, thr_id)
                    if value:
                        result[f"nvml/{idx}/{name}"] = float(value)
                except pynvml.NVMLError as exc:
                    logger.debug("NVML threshold[%d,%s] failed: %s", idx, name, exc)
        return result
    except Exception as exc:
        logger.debug("NVML thresholds read failed: %s", exc)
        return {}


def read_nvml_device_names() -> dict[int, str]:
    """Имена устройств для отображения в UI и diagnostics.

    Возвращает ``{0: "NVIDIA GeForce RTX 4070 Ti"}``. Кэшируется драйвером,
    дёшево вызывать. При ошибке — пустой dict.
    """
    if not _ensure_init():
        return {}
    try:
        import pynvml

        result: dict[int, str] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            try:
                raw = pynvml.nvmlDeviceGetName(handle)
                # В свежих pynvml возвращается str, в старых — bytes.
                name = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                result[idx] = name
            except pynvml.NVMLError as exc:
                logger.debug("NVML name[%d] failed: %s", idx, exc)
        return result
    except Exception as exc:
        logger.debug("NVML device names read failed: %s", exc)
        return {}


def read_nvml_all() -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Single-pass: возвращает (temperatures, power, frequencies).

    Используется в hot-path адаптера, чтобы не повторять `_ensure_init` и
    `nvmlDeviceGetCount` по 4 раза за тик. Утилизация и пороги собираются
    отдельно — пороги статичные (кэшируем в M4), utilization меняется
    но не используется в текущем рендере.

    Power возвращается как dict для использования рядом с `voltages` в
    `MetricSnapshot` — это временное решение до M4 (там будет отдельное
    поле `powers` в `SensorSnapshot`).
    """
    if not _ensure_init():
        return {}, {}, {}
    try:
        import pynvml

        temps: dict[str, float] = {}
        power: dict[str, float] = {}
        freqs: dict[str, float] = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            # Температура
            try:
                v = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                if v is not None:
                    temps[f"nvml/{idx}/temperature"] = float(v)
            except pynvml.NVMLError as exc:
                logger.debug("NVML temp[%d] failed: %s", idx, exc)
            # Мощность (мВт → Вт)
            try:
                mw = pynvml.nvmlDeviceGetPowerUsage(handle)
                power[f"nvml/{idx}/power_w"] = float(mw) / 1000.0
            except pynvml.NVMLError as exc:
                logger.debug("NVML power[%d] failed: %s", idx, exc)
            # Частоты
            for clock_id, name in (
                (pynvml.NVML_CLOCK_GRAPHICS, "clock_graphics"),
                (pynvml.NVML_CLOCK_MEM, "clock_memory"),
            ):
                try:
                    v = pynvml.nvmlDeviceGetClockInfo(handle, clock_id)
                    if v:
                        freqs[f"nvml/{idx}/{name}"] = float(v)
                except pynvml.NVMLError as exc:
                    logger.debug("NVML clock[%d,%s] failed: %s", idx, name, exc)
        return temps, power, freqs
    except Exception as exc:
        logger.debug("NVML all read failed: %s", exc)
        return {}, {}, {}


def _ensure_init() -> bool:
    """Ленивая инициализация NVML (потокобезопасная)."""
    global _nvml_initialized, _init_failed
    if _init_failed:
        return False
    if _nvml_initialized:
        return True
    with _lock:
        if _init_failed:
            return False
        if _nvml_initialized:
            return True
        try:
            import pynvml

            pynvml.nvmlInit()
            atexit.register(_shutdown)
            _nvml_initialized = True
            return True
        except Exception as exc:
            # ImportError если пакета нет, NVMLError_LibraryNotFound если
            # драйвер NVIDIA не установлен, NVMLError_DriverNotLoaded и т.д.
            logger.debug("NVML init failed: %s", exc)
            _init_failed = True
            return False


def _shutdown() -> None:
    """Освободить NVML при выходе процесса."""
    global _nvml_initialized
    if not _nvml_initialized:
        return
    try:
        import pynvml

        pynvml.nvmlShutdown()
    except Exception as exc:
        logger.debug("NVML shutdown failed: %s", exc)
    finally:
        _nvml_initialized = False


def _reset_for_tests() -> None:
    """Сбросить module-state singleton — только для unit-тестов."""
    global _nvml_initialized, _init_failed
    _nvml_initialized = False
    _init_failed = False
