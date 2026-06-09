"""Read-only клиент Global shared-memory snapshot'а сенсоров.

Используется из apexcore-процессов (НЕ admin) для быстрого получения
LHM/PawnIO-сенсоров без открытия PawnIO драйвера. Если сервис
``apexcore_sensord`` не запущен — модуль молча возвращает ``None``
из всех функций, и caller'ы (см. ``infrastructure/sensors/lhm.py``)
fallback'ом идут на прямой LHM-путь.

Контракт деградации: любая ошибка (нет pywin32, mapping не существует,
snapshot протух, magic-mismatch) → возвращаем ``None`` и логируем
``logger.debug``. Никогда не пробрасываем исключение — это гарантия,
что отсутствие сервиса не ломает основной flow apexcore.

Mapping namespace: ``Global\\apexcore_sensors`` (фиксирован в
:data:`SHM_NAMESPACE`). Открывается с правами read-only — даже если
у клиента случайно полный доступ, попытка записи через mmap упадёт
сразу же.

Кэширование: открытый mmap-handle переиспользуется между вызовами в
пределах одного процесса (см. :func:`_get_mapping`). Закрывается через
:func:`close_shm_mapping` или ``atexit``. Это важно для горячего пути
(``WindowsAdapter.get_current_metrics`` дёргает сенсоры каждый poll).
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Any

from apexcore.services.shm_layout import (
    BUFFER_SIZE,
    Snapshot,
    unpack_snapshot,
)

logger = logging.getLogger(__name__)

# Namespace mapping'а. Префикс ``Global\\`` обязателен — без него
# клиент из user-session не увидит mapping, созданный сервисом
# (LocalSystem). Имя ``apexcore_sensors`` совпадает с тем, что
# использует ``services/sensord.py``.
SHM_NAMESPACE: str = r"Global\apexcore_sensors"

# Глобальный кеш открытого mapping'а — один на процесс. Отсутствие
# mapping'а кешируется через ``_open_failed``, чтобы не дёргать
# OpenFileMapping каждый poll, когда сервис заведомо не запущен.
_lock = threading.Lock()
_mapping: Any | None = None
_open_failed: bool = False


class _ReadOnlyMapping:
    """Read-only shared-memory view через ctypes (`OpenFileMapping` + `MapViewOfFile`).

    Python ``mmap.mmap`` с ``access=ACCESS_READ`` и tagname=Global\\... на
    самом деле запрашивает ``FILE_MAP_READ | FILE_MAP_WRITE`` — это
    подтверждается тем, что чистый ``OpenFileMappingW(FILE_MAP_READ, ...)``
    проходит, а ``mmap.mmap(...)`` валится с ``WinError 5``. Поэтому
    клиент использует Win32 API напрямую с явным ``FILE_MAP_READ``,
    что соответствует нашему SDDL ``D:P(A;;GR;;;WD)...``.

    Объект поддерживает slice-индексинг как ``bytes-like`` —
    ``view[:BUFFER_SIZE]`` возвращает обычные ``bytes`` (через
    ``ctypes.string_at``, это один memcpy).
    """

    def __init__(self, handle: int, address: int, size: int) -> None:
        self._handle = handle
        self._address = address
        self._size = size
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.closed = False

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, item: slice | int) -> bytes | int:
        if isinstance(item, slice):
            start, stop, step = item.indices(self._size)
            if step != 1:
                # Редкий путь — slice с шагом. Через цикл.
                arr = (ctypes.c_ubyte * self._size).from_address(self._address)
                return bytes(arr[start:stop:step])
            length = max(0, stop - start)
            return ctypes.string_at(self._address + start, length)
        # Один байт.
        return int.from_bytes(
            ctypes.string_at(self._address + int(item), 1),
            "little",
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._address:
                self._kernel32.UnmapViewOfFile(ctypes.c_void_p(self._address))
                self._address = 0
        except Exception as exc:
            logger.debug("UnmapViewOfFile failed: %s", exc)
        try:
            if self._handle:
                self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
                self._handle = 0
        except Exception as exc:
            logger.debug("CloseHandle failed: %s", exc)


def read_shm_snapshot() -> Snapshot | None:
    """Прочитать текущий snapshot из Global mapping; ``None`` если недоступен.

    Возможные причины ``None``:

    * сервис ``apexcore_sensord`` не установлен или не запущен;
    * Python без ``pywin32`` (mmap.mmap по tagname работает и без
      pywin32, но мы не падаем если что-то всё-таки сломается);
    * snapshot протух (timestamp старше ``FRESHNESS_LIMIT_NS``);
    * magic/version mismatch (старый клиент против нового сервиса
      или наоборот).

    Никогда не бросает исключения — все ошибки логируются на ``debug``.
    """
    buffer = _get_mapping()
    if buffer is None:
        return None
    try:
        # mmap → bytes только в нужных пределах. Без копии всего 64 КБ
        # каждый вызов — на горячем пути это было бы заметно. Но
        # snapshot обычно <10 КБ, копия дешёвая.
        snap = unpack_snapshot(buffer[:BUFFER_SIZE])
    except Exception as exc:
        logger.debug("shm: unpack_snapshot upal: %s", exc)
        return None
    if snap is None:
        return None
    if not snap.is_fresh():
        logger.debug(
            "shm: snapshot protuh (ts=%d, age=%d ms)",
            snap.timestamp_ns,
            (_now_ns() - snap.timestamp_ns) // 1_000_000,
        )
        return None
    return snap


def read_shm_by_prefix(prefix: str) -> dict[str, float] | None:
    """Снять срез snapshot'а по type-префиксу + вернуть «голые» ключи.

    Например ``read_shm_by_prefix("temp:")`` отдаст ``{"cpu/cpu_core_1":
    65.4, ...}`` — префикс ``temp:`` снят, и форма ключей совпадает
    с тем, что возвращает ``infrastructure/sensors/lhm.py:read_lhm_temperatures``.
    Это позволяет drop-in заменить прямой LHM-вызов на чтение snapshot'а.

    Возвращает ``None`` если snapshot недоступен/протух/пустой; пустой
    словарь — если snapshot валиден, но в нём нет ключей с таким префиксом
    (это валидное состояние, например на машине без CPU power-сенсоров).
    """
    snap = read_shm_snapshot()
    if snap is None:
        return None
    result: dict[str, float] = {}
    prefix_len = len(prefix)
    for key, value in snap.values.items():
        if key.startswith(prefix):
            result[key[prefix_len:]] = value
    return result


def is_shm_available() -> bool:
    """Признак: сервис снимает snapshot и mapping валиден прямо сейчас.

    Эквивалент ``read_shm_snapshot() is not None``, но без локального
    биндинга Snapshot для коротких диагностик (``apexcore doctor``).
    """
    return read_shm_snapshot() is not None


def close_shm_mapping() -> None:
    """Закрыть кешированный mapping. Идемпотентно.

    Полезно в тестах и при штатном выходе процесса (через ``atexit``).
    После закрытия следующий :func:`read_shm_snapshot` снова попробует
    открыть mapping.
    """
    global _mapping, _open_failed
    with _lock:
        if _mapping is not None:
            try:
                _mapping.close()
            except Exception as exc:
                logger.debug("shm close failed: %s", exc)
            _mapping = None
        _open_failed = False


def _get_mapping() -> _ReadOnlyMapping | None:
    """Открыть Global mapping для чтения; кешировать handle.

    Открытие выполняется ленивно при первом вызове. Если mapping
    не существует (`FileNotFoundError`/`OSError`), результат
    кешируется через ``_open_failed``: повторные попытки не делаем
    до явного :func:`close_shm_mapping`. Это защищает горячий путь
    от штрафа на каждый poll, когда сервис не установлен.

    Возвращает открытый mmap-объект (read-only) или ``None``.
    """
    global _mapping, _open_failed
    if _open_failed:
        return None
    if _mapping is not None:
        return _mapping
    with _lock:
        if _open_failed:
            return None
        if _mapping is not None:
            return _mapping
        try:
            mapping = _open_global_mapping()
        except Exception as exc:
            # FileNotFoundError если mapping не создан сервисом;
            # PermissionError если SDDL запрещает GENERIC_READ.
            logger.debug("shm: open(%s) upal: %s", SHM_NAMESPACE, exc)
            _open_failed = True
            return None
        _mapping = mapping
        atexit.register(close_shm_mapping)
        return _mapping


def _open_global_mapping() -> _ReadOnlyMapping:
    """Открыть Global mapping read-only через прямой Win32 API.

    Использует ``OpenFileMappingW(FILE_MAP_READ, ...)`` — это работает
    с SDDL, разрешающим только GENERIC_READ для Everyone (как у нас
    в `services/sensord.py:_MAPPING_SDDL`). Python `mmap.mmap` для
    того же сценария запрашивает write-доступ и падает с ACCESS_DENIED
    из non-admin процессов — поэтому мы его не используем.

    Бросает ``OSError`` если mapping не существует (сервис не запущен)
    или доступ запрещён (некорректный SDDL).
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    open_file_mapping = kernel32.OpenFileMappingW
    open_file_mapping.restype = wintypes.HANDLE
    open_file_mapping.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

    map_view = kernel32.MapViewOfFile
    map_view.restype = ctypes.c_void_p
    map_view.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]

    FILE_MAP_READ = 0x0004  # noqa: N806 — Win32 константа
    handle = open_file_mapping(FILE_MAP_READ, False, SHM_NAMESPACE)
    if not handle:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenFileMapping({SHM_NAMESPACE}): WinError {err}")

    address = map_view(wintypes.HANDLE(handle), FILE_MAP_READ, 0, 0, BUFFER_SIZE)
    if not address:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise OSError(err, f"MapViewOfFile упал: WinError {err}")

    return _ReadOnlyMapping(handle=handle, address=address, size=BUFFER_SIZE)


def _now_ns() -> int:
    """Тонкий wrapper над time.time_ns для подмены в тестах."""
    import time

    return time.time_ns()


# Реэкспортируем константы префиксов type из infrastructure/sensors/lhm.py —
# чтобы клиентский код имел один источник правды для разбора snapshot'а
# и не зависел напрямую от LHM-модуля при чтении из shared memory.
def _lhm_prefixes() -> dict[str, str]:
    """Ленивый импорт констант: shm_adapter не должен тянуть LHM/pythonnet."""
    from apexcore.infrastructure.sensors import lhm

    return {
        "temp": lhm.SHM_PREFIX_TEMP,
        "volt": lhm.SHM_PREFIX_VOLT,
        "power": lhm.SHM_PREFIX_POWER,
        "fan": lhm.SHM_PREFIX_FAN,
        "clock": lhm.SHM_PREFIX_CLOCK,
        "tjmax": lhm.SHM_PREFIX_TJMAX,
    }


def read_shm_temperatures() -> dict[str, float] | None:
    """Drop-in shm-аналог ``infrastructure/sensors/lhm.py:read_lhm_temperatures``."""
    return read_shm_by_prefix(_lhm_prefixes()["temp"])


def read_shm_voltages() -> dict[str, float] | None:
    """Drop-in shm-аналог ``read_lhm_voltages``."""
    return read_shm_by_prefix(_lhm_prefixes()["volt"])


def read_shm_cpu_power() -> dict[str, float] | None:
    """Drop-in shm-аналог ``read_lhm_cpu_power``."""
    return read_shm_by_prefix(_lhm_prefixes()["power"])


def read_shm_fans() -> dict[str, float] | None:
    """Drop-in shm-аналог ``read_lhm_fans``."""
    return read_shm_by_prefix(_lhm_prefixes()["fan"])


def read_shm_cpu_clocks() -> dict[str, float] | None:
    """Drop-in shm-аналог ``read_lhm_cpu_clocks``."""
    return read_shm_by_prefix(_lhm_prefixes()["clock"])


def read_shm_tjmax() -> dict[str, float] | None:
    """Drop-in shm-аналог ``read_lhm_tjmax``."""
    return read_shm_by_prefix(_lhm_prefixes()["tjmax"])


def read_shm_temperatures_and_voltages() -> tuple[dict[str, float], dict[str, float]] | None:
    """Совместный shm-аналог ``read_lhm_temperatures_and_voltages``.

    Делает ровно один проход по snapshot.values (а не два, как раздельные
    вызовы read_shm_temperatures + read_shm_voltages). Возвращает ``None``
    если snapshot недоступен/протух.
    """
    snap = read_shm_snapshot()
    if snap is None:
        return None
    prefixes = _lhm_prefixes()
    temp_p = prefixes["temp"]
    volt_p = prefixes["volt"]
    temps: dict[str, float] = {}
    voltages: dict[str, float] = {}
    temp_len = len(temp_p)
    volt_len = len(volt_p)
    for key, value in snap.values.items():
        if key.startswith(temp_p):
            temps[key[temp_len:]] = value
        elif key.startswith(volt_p):
            voltages[key[volt_len:]] = value
    return temps, voltages


