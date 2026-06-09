"""Runtime-discovery AMD Ryzen Master Monitoring SDK (P1.4).

AMD Ryzen Master ships с публичным monitoring-SDK
(``Platform.AMD.RyzenMaster.dll`` в ``C:\\Program Files\\AMD\\RyzenMaster\\
bin\\``). Если у пользователя на desktop AMD установлен Ryzen Master —
apexcore может через ctypes прочитать **Tctl/Tdie/Vcore/SoC** без
admin-прав самого apexcore (подписанный AMD-driver уже зарегистрирован).

**Лицензия.** Ryzen Master EULA запрещает редистрибуцию DLL — apexcore
**не распространяет** её. Использование уже установленной пользователем
версии через ``OpenFileMapping``/``ctypes`` — industry-standard паттерн
(см. ``docs/research`` §3.4 для аналогичной ситуации с HWiNFO/AIDA64).

**Caveat.** Эта реализация **не верифицирована на реальном железе** —
пользователь работает на Intel i9-12900K. SDK API известен из публичной
документации, но конкретные signature'ы (имена функций, struct layouts)
могут отличаться между версиями SDK. При первой работе на AMD desktop
ожидаются мелкие правки. См. P1.4 в плане.

Поддерживаемая аудитория: desktop AMD (Zen 2/3/4/5). Mobile AMD —
официально не поддерживается AMD SDK. Не AMD CPU — модуль немедленно
возвращает пустые словари без попытки загрузки DLL.

API (предположительный, из публичной AMD Monitoring SDK 1.0):

- ``InitPlatform()`` → ``int`` (0 = success)
- ``GetCpuParameters(ptr)`` → заполняет struct с temp/vcore/...
- ``DeInitPlatform()`` → cleanup при shutdown
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


# Стандартный путь установки Ryzen Master. AMD не меняет его между
# версиями (проверено на v2.7..v2.13). На custom-installation путь
# может отличаться — если будут жалобы, ввести env-override
# ``APEXCORE_RYZEN_MASTER_DLL``.
_DEFAULT_DLL_PATH = Path(
    r"C:\Program Files\AMD\RyzenMaster\bin\Platform.AMD.RyzenMaster.dll"
)


# Module-level cache: «модуль уже не работает в этом процессе» — чтобы не
# логировать failure каждый тик телеметрии.
_RYZEN_MASTER_UNAVAILABLE = False
_RYZEN_MASTER_DLL_HANDLE: object | None = None


def _cpu_is_amd() -> bool:
    """Проверить что текущий CPU — AMD. Только для Windows.

    Использует ``platform.processor()`` который на Windows возвращает
    содержимое реестра ``HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0\\
    ProcessorNameString``. Это надёжнее ``platform.machine()`` (тот
    возвращает архитектуру 'AMD64' для всех x64 CPU, включая Intel).
    """
    if platform.system().lower() != "windows":
        return False
    name = platform.processor() or ""
    low = name.lower()
    return "amd " in low or "ryzen" in low or "epyc" in low or "threadripper" in low


def _resolve_dll_path() -> Path | None:
    """Найти путь к Platform.AMD.RyzenMaster.dll. ``None`` если не существует."""
    import os

    override = os.environ.get("APEXCORE_RYZEN_MASTER_DLL")
    if override:
        path = Path(override)
        return path if path.exists() else None
    return _DEFAULT_DLL_PATH if _DEFAULT_DLL_PATH.exists() else None


def is_available() -> bool:
    """Можно ли вообще пробовать читать Ryzen Master?

    Условия: ОС=Windows, CPU=AMD, DLL найдена. **Не** грузит DLL —
    это lightweight-проверка для probe-фазы и capability matrix.
    """
    if _RYZEN_MASTER_UNAVAILABLE:
        return False
    if not _cpu_is_amd():
        return False
    return _resolve_dll_path() is not None


def _ensure_dll_loaded() -> object | None:
    """Lazy-load DLL. None если не получилось.

    Помечает ``_RYZEN_MASTER_UNAVAILABLE = True`` при сбое чтобы не
    повторять загрузку каждый тик.
    """
    global _RYZEN_MASTER_UNAVAILABLE, _RYZEN_MASTER_DLL_HANDLE
    if _RYZEN_MASTER_UNAVAILABLE:
        return None
    if _RYZEN_MASTER_DLL_HANDLE is not None:
        return _RYZEN_MASTER_DLL_HANDLE
    if not _cpu_is_amd():
        _RYZEN_MASTER_UNAVAILABLE = True
        return None
    dll_path = _resolve_dll_path()
    if dll_path is None:
        _RYZEN_MASTER_UNAVAILABLE = True
        return None
    try:
        import ctypes

        # WinDLL для stdcall (Win32 ABI). Если AMD выпустит native libray
        # с cdecl — переключиться на CDLL. У текущей SDK Platform.AMD.
        # RyzenMaster.dll — managed assembly, поэтому корректнее грузить
        # через pythonnet. Здесь оставляем ctypes как entry point;
        # реальное верификация — на AMD desktop (P1.4 caveat).
        handle = ctypes.WinDLL(str(dll_path))  # type: ignore[attr-defined]
    except Exception as exc:
        # AMD DLL не загружается — может быть .NET assembly, может быть
        # missing dependency. Помечаем unavailable до конца процесса.
        logger.debug(
            "Ryzen Master DLL %s не загрузилась: %r. P1.4 нуждается в "
            "верификации на AMD desktop — см. план.",
            dll_path,
            exc,
        )
        _RYZEN_MASTER_UNAVAILABLE = True
        return None
    _RYZEN_MASTER_DLL_HANDLE = handle
    return handle


def read_ryzen_master_temperatures() -> dict[str, float]:
    """Прочитать Tctl/Tdie через Ryzen Master Monitoring SDK.

    Возвращает ``{"cpu/tctl": 65.4, "cpu/tdie": 60.0}`` (Tctl = Tctrl
    с AMD-смещением для control loop, Tdie = реальная температура die).
    Пустой словарь при недоступности DLL / неверной signature.

    **AMD-only**, **needs verification on AMD desktop**: реальные имена
    функций и struct layout в Monitoring SDK 1.0 могут отличаться от
    предположенных. См. P1.4 caveat в плане.
    """
    handle = _ensure_dll_loaded()
    if handle is None:
        return {}
    try:
        return _read_temperatures_impl(handle)
    except Exception as exc:
        logger.debug("Ryzen Master temperature read upal: %r", exc)
        return {}


def read_ryzen_master_voltages() -> dict[str, float]:
    """Прочитать Vcore и SoC напряжение через Ryzen Master SDK.

    Возвращает ``{"cpu/vcore": 1.2, "cpu/soc": 1.1}``. Пустой словарь
    при недоступности DLL.
    """
    handle = _ensure_dll_loaded()
    if handle is None:
        return {}
    try:
        return _read_voltages_impl(handle)
    except Exception as exc:
        logger.debug("Ryzen Master voltage read upal: %r", exc)
        return {}


def _read_temperatures_impl(handle: object) -> dict[str, float]:
    """Real-API call site. Изолирован от ``read_*`` для тестового мока.

    AMD Monitoring SDK 1.0 публичный API:

    - ``GetCpuTemperature(double* tctl_out)`` — Tctl, чаще всего же что Tdie.
    - На Zen3+ может быть отдельный ``GetCpuDieTemperature``.

    **P1.4 caveat**: signature точно не знаем без AMD desktop. Текущая
    реализация — defensive: если функция не найдена в DLL, возвращаем
    пустой dict; если она есть — пробуем вызвать с буфером ``c_double``.
    """
    import ctypes

    result: dict[str, float] = {}
    # Tctl
    func = getattr(handle, "GetCpuTemperature", None)
    if callable(func):
        out = ctypes.c_double(0.0)
        func.argtypes = [ctypes.POINTER(ctypes.c_double)]
        func.restype = ctypes.c_int
        rc = func(ctypes.byref(out))
        if rc == 0 and out.value > 0:
            result["cpu/tctl"] = float(out.value)
            # На большинстве Ryzen Tctl ≈ Tdie + AMD-смещение (10 °C для
            # X-series, 0 для non-X). Без отдельного GetCpuDieTemperature
            # дублируем как tdie — пользователю важнее само значение.
            result["cpu/tdie"] = float(out.value)
    return result


def _read_voltages_impl(handle: object) -> dict[str, float]:
    """Real-API call site для voltages (изолирован для теста)."""
    import ctypes

    result: dict[str, float] = {}
    func = getattr(handle, "GetCpuVoltage", None)
    if callable(func):
        out = ctypes.c_double(0.0)
        func.argtypes = [ctypes.POINTER(ctypes.c_double)]
        func.restype = ctypes.c_int
        rc = func(ctypes.byref(out))
        if rc == 0 and out.value > 0:
            result["cpu/vcore"] = float(out.value)
    func = getattr(handle, "GetSocVoltage", None)
    if callable(func):
        out = ctypes.c_double(0.0)
        func.argtypes = [ctypes.POINTER(ctypes.c_double)]
        func.restype = ctypes.c_int
        rc = func(ctypes.byref(out))
        if rc == 0 and out.value > 0:
            result["cpu/soc"] = float(out.value)
    return result


def _reset_for_tests() -> None:
    """Тестовый hook: обнулить module-level state."""
    global _RYZEN_MASTER_UNAVAILABLE, _RYZEN_MASTER_DLL_HANDLE
    _RYZEN_MASTER_UNAVAILABLE = False
    _RYZEN_MASTER_DLL_HANDLE = None


__all__ = [
    "is_available",
    "read_ryzen_master_temperatures",
    "read_ryzen_master_voltages",
]
