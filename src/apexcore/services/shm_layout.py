"""Бинарный лэйаут shared-memory snapshot'а сенсоров.

Snapshot пишется сервисом ``apexcore_sensord`` (LocalSystem) в Global
mapping ``apexcore_sensors``; читается non-admin клиентами через
:mod:`apexcore.services.shm_adapter`. Чистый модуль без I/O — только
``struct``-операции, тестируется в unit-режиме.

Формат
------

::

    +------------------------- header (24 B) --------------------------+
    | magic   (4 B)  b"BSN1"        — Benchkit SeNsors v1              |
    | version (2 B)  u16            — SHM_VERSION                      |
    | flags   (2 B)  u16            — зарезервировано, сейчас 0        |
    | ts_ns   (8 B)  u64            — time.time_ns() в момент write    |
    | count   (4 B)  u32            — число записей                    |
    | rsvd    (4 B)  u32            — = 0                              |
    +------------------------ records[count] --------------------------+
    | key_len  (2 B)  u16           — длина имени в байтах (UTF-8)     |
    | key      (key_len B) bytes    — ASCII/UTF-8 sensor-ключ          |
    | value    (4 B)  f32           — значение сенсора                 |
    +-------------------------------------------------------------------+

Размер каждой записи переменный (зависит от длины ключа); общий размер
buffer'а фиксирован — :data:`BUFFER_SIZE` = 64 КБ. Типичный LHM-набор
из ~200 сенсоров со средней длиной ключа 20 символов укладывается
в ~5 КБ — большой запас.

Свежесть snapshot'а проверяется клиентом по полю ``timestamp_ns``
— :func:`Snapshot.is_fresh` сравнивает с ``time.time_ns()`` против
:data:`FRESHNESS_LIMIT_NS` (2 с). Если сервис умер — snapshot устаревает
и apexcore fallback'ом идёт на прямой LHM-путь (требует admin).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

# Маркер формата + версия — увеличиваются вместе при breaking-changes
# (новое поле в header, смена RECORD-формата). Клиенты со старой версией
# увидят несовпадение MAGIC/VERSION и проигнорируют snapshot, упав на
# fallback, — это явный graceful degrade, не падение.
MAGIC: bytes = b"BSN1"
SHM_VERSION: int = 1

# Little-endian: x86_64 native, не вводим лишнюю свободу для большой
# тонкости. Глобальный mmap на Windows читается только в рамках одного
# хоста — перенос между разными машинами/архитектурами не предусмотрен.
HEADER_FMT: str = "<4sHHQII"
HEADER_SIZE: int = struct.calcsize(HEADER_FMT)

# Заголовок записи (без переменного key и без значения): u16 key_len.
_REC_KEYLEN_FMT: str = "<H"
_REC_KEYLEN_SIZE: int = struct.calcsize(_REC_KEYLEN_FMT)
_REC_VALUE_FMT: str = "<f"
_REC_VALUE_SIZE: int = struct.calcsize(_REC_VALUE_FMT)

# 64 КБ — pages 16 × 4 КБ. На типичной системе живёт ~150–200 LHM-ключей
# средней длины ~20 символов → ~5 КБ; запас 12x под мультисокетные/
# multi-GPU сетапы и расширения (cpu_clock × 64 ядра уже = 64 записи).
BUFFER_SIZE: int = 65536

# Жёсткий предел длины sensor-ключа в байтах — защита от мусора.
# LHM-ключи имеют формат ``<hardware>/<sensor>`` и обычно ≤ 60 символов.
MAX_KEY_LEN: int = 128

# Snapshot старше 2 с считаем «протухшим». Сервис пишет каждые 250–500 мс,
# поэтому 2 с — 4-кратный запас на случай GC-задержек или временного
# stall'а LHM.Update().
FRESHNESS_LIMIT_NS: int = 2 * 1_000_000_000

_U64_MASK: int = (1 << 64) - 1


@dataclass(frozen=True)
class Snapshot:
    """Раскодированный snapshot — то, что отдаёт :func:`unpack_snapshot`.

    ``timestamp_ns`` — момент записи сервисом (``time.time_ns()``).
    ``values`` — словарь ``sensor_key → value``; порядок ключей сохраняется
    тем же, что в исходном dict сервиса (Python 3.7+ insertion order).
    """

    timestamp_ns: int
    values: dict[str, float] = field(default_factory=dict)

    def is_fresh(self, now_ns: int | None = None) -> bool:
        """Не протух ли snapshot — сервис обновлял его не позже 2 с назад.

        ``now_ns`` опционален; если не передан, берётся ``time.time_ns()``.
        Параметр нужен для тестируемости — фиксируем «текущее время».
        """
        if now_ns is None:
            now_ns = time.time_ns()
        return (now_ns - self.timestamp_ns) <= FRESHNESS_LIMIT_NS


def pack_snapshot(values: dict[str, float], timestamp_ns: int) -> bytes:
    """Сериализовать snapshot в bytes (без паддинга до :data:`BUFFER_SIZE`).

    На входе — словарь ``sensor_key → float``. Ключи кодируются UTF-8,
    длина каждого должна быть ≤ :data:`MAX_KEY_LEN` (более длинные молча
    отбрасываются). Сумма размеров header'а и всех записей не должна
    превышать :data:`BUFFER_SIZE` — лишние записи также молча
    отбрасываются по порядку итерации.

    NaN/inf-значения отфильтровываются заранее — они валидно пишутся
    в float32, но при чтении приведут к шуму в потребителях
    (диагностика, фронтенд). Лучше сразу выкинуть.

    Возвращает буфер фиксированной длины ``HEADER_SIZE + Σ record_size``.
    Паддинг до BUFFER_SIZE делает caller перед записью в mmap.
    """
    encoded: list[tuple[bytes, float]] = []
    total = HEADER_SIZE
    for raw_key, raw_value in values.items():
        if not isinstance(raw_value, (int, float)):
            continue
        value = float(raw_value)
        if not _is_finite(value):
            continue
        key_bytes = raw_key.encode("utf-8")
        if not key_bytes or len(key_bytes) > MAX_KEY_LEN:
            continue
        rec_size = _REC_KEYLEN_SIZE + len(key_bytes) + _REC_VALUE_SIZE
        if total + rec_size > BUFFER_SIZE:
            # Защитный лимит — на практике не должен срабатывать (LHM
            # отдаёт ~200 ключей × ~26 B = ~5 КБ). Дальше пишем то,
            # что успели; детерминированный обрез по порядку итерации.
            break
        encoded.append((key_bytes, value))
        total += rec_size

    out = bytearray(total)
    struct.pack_into(
        HEADER_FMT,
        out,
        0,
        MAGIC,
        SHM_VERSION,
        0,  # flags
        timestamp_ns & _U64_MASK,
        len(encoded),
        0,  # reserved
    )
    offset = HEADER_SIZE
    for key_bytes, value in encoded:
        struct.pack_into(_REC_KEYLEN_FMT, out, offset, len(key_bytes))
        offset += _REC_KEYLEN_SIZE
        out[offset:offset + len(key_bytes)] = key_bytes
        offset += len(key_bytes)
        struct.pack_into(_REC_VALUE_FMT, out, offset, value)
        offset += _REC_VALUE_SIZE
    return bytes(out)


def unpack_snapshot(buf: bytes | memoryview) -> Snapshot | None:
    """Декодировать snapshot из bytes/memoryview обратно в :class:`Snapshot`.

    Возвращает ``None`` если:

    * буфер короче header'а;
    * magic не совпадает (``b"BSN1"``);
    * version не совпадает с :data:`SHM_VERSION`;
    * хоть одна запись не помещается в оставшийся буфер;
    * key_len = 0 или больше :data:`MAX_KEY_LEN` (мусор);
    * decode UTF-8 ключа упал (ставит запись в skip, не валит snapshot).

    Это самая частая защита — на стороне клиента mmap может быть открыт
    раньше, чем сервис записал первый snapshot (зероинициализированный
    буфер не пройдёт magic-проверку).
    """
    if len(buf) < HEADER_SIZE:
        return None
    magic, version, _flags, timestamp_ns, count, _rsvd = struct.unpack_from(
        HEADER_FMT, buf, 0
    )
    if magic != MAGIC or version != SHM_VERSION:
        return None
    values: dict[str, float] = {}
    offset = HEADER_SIZE
    buf_len = len(buf)
    for _ in range(count):
        if offset + _REC_KEYLEN_SIZE > buf_len:
            return None
        (key_len,) = struct.unpack_from(_REC_KEYLEN_FMT, buf, offset)
        offset += _REC_KEYLEN_SIZE
        if key_len == 0 or key_len > MAX_KEY_LEN:
            return None
        if offset + key_len + _REC_VALUE_SIZE > buf_len:
            return None
        key_bytes = bytes(buf[offset:offset + key_len])
        offset += key_len
        (value,) = struct.unpack_from(_REC_VALUE_FMT, buf, offset)
        offset += _REC_VALUE_SIZE
        try:
            key = key_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Битая запись — пропускаем, но snapshot валидным считаем
            # (последующие записи могут быть нормальными).
            continue
        values[key] = float(value)
    return Snapshot(timestamp_ns=int(timestamp_ns), values=values)


def _is_finite(value: float) -> bool:
    """``math.isfinite`` без импорта math на горячем пути.

    NaN != NaN — единственный self-неравный float; для inf проверяем
    диапазон сравнением с границами f32 (1e38 чуть меньше FLT_MAX,
    но любой sensor-value «правда не inf»).
    """
    if value != value:
        return False
    return -1e38 < value < 1e38
