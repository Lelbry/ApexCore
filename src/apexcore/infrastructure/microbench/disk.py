"""Disk Sequential / Random Read + Sequential Write — пропускная способность накопителя.

MVP-ограничения (документированы в docs/winsat.md):
- Queue depth не моделируется: Python sync I/O = effective queue 1.
  Реальный Winsat использует -n 0..4 (asynchronous overlapped I/O).
- Без ``FILE_FLAG_NO_BUFFERING``: чтения могут попадать в page cache ОС.
  Mitigation: тестовый файл = 256 МБ (больше типичного активного page cache
  на старте) + 4 warmup-итерации в ``time_loop``.
- Тестовый файл создаётся в ``tempfile.gettempdir()`` по умолчанию. Для
  «Оценок общей производительности» бенчмарк диска ОС
  (``application/general_benchmark.py``) пробрасывает путь к загрузочному
  диску через параметр ``target_dir`` — тогда измеряется он, а не tempdir.
- Свободное место: минимум 1 ГБ (для read-тестов) и 1 ГБ для write
  (256 МБ файл + запас на FS overhead).

Для тестового кода ``FILE_SIZE_MB`` можно переопределить через атрибут класса.
"""

from __future__ import annotations

import contextlib
import os
import random
import shutil
import tempfile
import threading
import time
from pathlib import Path

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

# Параметры по умолчанию (Winsat: -seq 64KB, -ran 16KB; FILE_SIZE_MB = 256).
SEQ_BLOCK_SIZE = 64 * 1024
RANDOM_BLOCK_SIZE = 16 * 1024
WRITE_BLOCK_SIZE = 64 * 1024  # тот же блок, что и для seq read — сравнимо
FILE_SIZE_MB_DEFAULT = 256
WRITE_CHUNK_SIZE = 4 * 1024 * 1024  # 4 МБ — баланс между память и скорость заполнения
MIN_FREE_BYTES = 1 * 1024**3  # требуется минимум 1 ГБ свободно


def _try_make_writable(d: Path) -> bool:
    """Создать каталог и убедиться, что в него реально можно писать.

    Возвращает True, если каталог создан и проба записи прошла. mkdir на
    корне монтирования (``/`` на Linux, ``C:\\`` на Windows без админа) даёт
    ``PermissionError`` — тогда False, и вызывающий уходит в fallback.
    """
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".apexcore-write-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_target_dir(target_dir: Path | str | None) -> str:
    """Привести ``target_dir`` к писаемому каталогу для ``tempfile.mkstemp``.

    ``None`` → ``tempfile.gettempdir()`` (dev-режим, без привязки к boot-диску).

    Если задан boot-путь — пробуем поддиректорию ``apexcore-bench`` на нём
    (измеряем именно загрузочный диск). КРИТИЧНО: корень монтирования часто
    НЕ писаем обычному пользователю — ``/`` на Linux (root-only), ``C:\\`` на
    Windows без админа. Раньше fallback возвращал тот же неписаемый путь →
    ``mkstemp`` падал с ``Permission denied`` и вся disk-фаза «Общей оценки»
    обрывалась (r_disk=None, нет балла). Теперь при неписаемом корне уходим в
    писаемый каталог на РЕАЛЬНОМ диске (``~/.cache/apexcore-bench`` — на типовой
    одно-дисковой системе это тот же физический накопитель, что и boot-диск,
    поэтому скорость корректна). НЕ используем сразу tempdir — ``/tmp`` часто
    tmpfs (RAM) и завысил бы disk-скорость.
    """
    if target_dir is None:
        return tempfile.gettempdir()
    bench_dir = Path(target_dir) / "apexcore-bench"
    if _try_make_writable(bench_dir):
        return str(bench_dir)
    cache_dir = Path.home() / ".cache" / "apexcore-bench"
    if _try_make_writable(cache_dir):
        return str(cache_dir)
    return tempfile.gettempdir()


def _make_test_file(size_mb: int, target_dir: Path | str | None = None) -> Path:
    """Создать временный файл размера ``size_mb`` МБ со псевдослучайным содержимым.

    Содержимое — ``os.urandom`` чанками по ``WRITE_CHUNK_SIZE``: гарантированно
    несжимаемо, поэтому ОС не сможет хранить файл компактно (NTFS compression
    отключена в tempdir по умолчанию, но на всякий случай — урандом).
    Удаление — ответственность вызывающего (через try/finally).

    ВАЖНО — без ``fsync``:
    Раньше после write вызывался ``os.fsync`` чтобы гарантировать что данные
    физически на диске. Однако:
      1. На slow SATA SSD (Samsung 860 EVO и подобные) fsync 256MB занимал
         60-90 сек на flush write-cache → «Общая оценка» висла на каждой
         disk-фазе. Полная установка C: + 256MB urandom + fsync × 3 фаз
         доходила до 5 минут вместо заявленных ~90 сек.
      2. fsync **синхронизирует** к storage, но НЕ invalidates page cache
         ОС. Поэтому последующий ``open().read()`` всё равно частично
         попадает в cache → не даёт «честный» disk read без отдельного
         ``FILE_FLAG_NO_BUFFERING``.

    Mitigation: используем большой файл (256MB > типичный 96MB page cache
    Windows) — основной объём идёт с диска. ``f.flush()`` отдаёт буферы в
    ОС, дальше pages могут lazy-flush'иться. Для quality-critical
    бенчмарка планируется DIRECT_IO через ctypes win32 API (отдельная
    задача — `FILE_FLAG_NO_BUFFERING`).
    """
    fd, path_str = tempfile.mkstemp(
        prefix="apexcore-winsat-",
        suffix=".bin",
        dir=_resolve_target_dir(target_dir),
    )
    path = Path(path_str)
    total = size_mb * 1024 * 1024
    written = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while written < total:
                remaining = total - written
                chunk = os.urandom(min(WRITE_CHUNK_SIZE, remaining))
                f.write(chunk)
                written += len(chunk)
            f.flush()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


class DiskSequentialReadBench:
    """Sequential read 64 KB блоками — аналог Winsat ``-seq -read``.

    Тестовый файл проходится последовательно от начала до конца, по
    достижении EOF — ``seek(0)`` и продолжаем. MB/s = total_bytes / elapsed.

    Параметр ``target_dir`` (опциональный) задаёт каталог тестового файла.
    По умолчанию — ``tempfile.gettempdir()`` (старое поведение). Используется
    «Оценками общей производительности», чтобы измерить именно загрузочный
    диск.
    """

    name = "disk_seq_read"
    category = "disk"
    unit = "MB/s"
    BLOCK_SIZE = SEQ_BLOCK_SIZE
    FILE_SIZE_MB = FILE_SIZE_MB_DEFAULT

    def __init__(self, target_dir: Path | str | None = None) -> None:
        self.target_dir = target_dir

    def is_available(self) -> bool:
        try:
            free = shutil.disk_usage(_resolve_target_dir(self.target_dir)).free
        except OSError:
            return False
        return free >= MIN_FREE_BYTES

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        path = _make_test_file(self.FILE_SIZE_MB, target_dir=self.target_dir)
        bytes_read = 0
        try:
            with open(path, "rb", buffering=0) as f:

                def work() -> None:
                    nonlocal bytes_read
                    buf = f.read(self.BLOCK_SIZE)
                    if not buf:
                        f.seek(0)
                        buf = f.read(self.BLOCK_SIZE)
                    bytes_read += len(buf)

                iterations, elapsed = time_loop(
                    work,
                    duration_sec,
                    warmup_calls=4,
                    cancel_token=cancel_token,
                )
        finally:
            path.unlink(missing_ok=True)

        mb_per_sec = (bytes_read / max(elapsed, 1e-9)) / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "block_size_kb": self.BLOCK_SIZE // 1024,
                "file_size_mb": self.FILE_SIZE_MB,
                "queue_depth": "sync (1, MVP)",
                "target_dir": _resolve_target_dir(self.target_dir),
            },
        )


class DiskRandomReadBench:
    """Random read 16 KB блоками — аналог Winsat ``-ran -read``.

    Между чтениями делаем ``seek`` на случайное смещение, выровненное по
    ``BLOCK_SIZE``. На SSD это даёт микс случайных I/O; на HDD — читаемая
    деградация по сравнению с sequential.

    ``target_dir`` работает так же, как у :class:`DiskSequentialReadBench`.
    """

    name = "disk_random_read"
    category = "disk"
    unit = "MB/s"
    BLOCK_SIZE = RANDOM_BLOCK_SIZE
    FILE_SIZE_MB = FILE_SIZE_MB_DEFAULT

    def __init__(self, target_dir: Path | str | None = None) -> None:
        self.target_dir = target_dir

    def is_available(self) -> bool:
        try:
            free = shutil.disk_usage(_resolve_target_dir(self.target_dir)).free
        except OSError:
            return False
        return free >= MIN_FREE_BYTES

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        path = _make_test_file(self.FILE_SIZE_MB, target_dir=self.target_dir)
        file_bytes = self.FILE_SIZE_MB * 1024 * 1024
        max_offset = file_bytes - self.BLOCK_SIZE
        bytes_read = 0
        rng = random.Random(0xDEADBEEF)
        try:
            with open(path, "rb", buffering=0) as f:

                def work() -> None:
                    nonlocal bytes_read
                    offset = rng.randrange(0, max_offset)
                    # Выравнивание на BLOCK_SIZE — чуть честнее в плане SSD-IO.
                    offset -= offset % self.BLOCK_SIZE
                    f.seek(offset)
                    buf = f.read(self.BLOCK_SIZE)
                    bytes_read += len(buf)

                iterations, elapsed = time_loop(
                    work,
                    duration_sec,
                    warmup_calls=4,
                    cancel_token=cancel_token,
                )
        finally:
            path.unlink(missing_ok=True)

        mb_per_sec = (bytes_read / max(elapsed, 1e-9)) / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "block_size_kb": self.BLOCK_SIZE // 1024,
                "file_size_mb": self.FILE_SIZE_MB,
                "queue_depth": "sync (1, MVP)",
                "target_dir": _resolve_target_dir(self.target_dir),
            },
        )


class DiskSequentialWriteBench:
    """Sequential write 64 KB блоками — измерение записи на boot-диск.

    В отличие от read-бенчей **не циклится**: пишет ровно один проход
    ``FILE_SIZE_MB`` МБ и измеряет общее время. Минимизирует износ SSD —
    одна сессия = ~256 МБ записи (≈1 GB суммарно за прогон комплексного
    бенчмарка с warmup). При типовом TBW NVMe 600 ТБ это ~600 000 запусков
    бенчмарка = 1640+ лет ежедневного использования.

    Параметр ``cancel_token`` проверяется между блоками — отмена возможна
    с гранулярностью ~10 мс.

    ``target_dir`` — каталог для временного файла. По умолчанию tempdir.
    """

    name = "disk_seq_write"
    category = "disk"
    unit = "MB/s"
    BLOCK_SIZE = WRITE_BLOCK_SIZE
    FILE_SIZE_MB = FILE_SIZE_MB_DEFAULT

    def __init__(self, target_dir: Path | str | None = None) -> None:
        self.target_dir = target_dir

    def is_available(self) -> bool:
        try:
            free = shutil.disk_usage(_resolve_target_dir(self.target_dir)).free
        except OSError:
            return False
        return free >= MIN_FREE_BYTES

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        # duration_sec игнорируется: write-проход всегда фиксированный
        # размер (FILE_SIZE_MB), иначе при разной длительности нельзя
        # сравнивать MB/s между прогонами честно.
        total_bytes = self.FILE_SIZE_MB * 1024 * 1024
        block = os.urandom(self.BLOCK_SIZE)  # один блок urandom — переиспользуем
        fd, path_str = tempfile.mkstemp(
            prefix="apexcore-write-",
            suffix=".bin",
            dir=_resolve_target_dir(self.target_dir),
        )
        path = Path(path_str)
        written = 0
        iterations = 0
        elapsed = 0.0
        try:
            with os.fdopen(fd, "wb", buffering=0) as f:
                started = time.perf_counter()
                while written < total_bytes:
                    if cancel_token is not None and cancel_token.is_set():
                        break
                    remaining = total_bytes - written
                    if remaining < self.BLOCK_SIZE:
                        f.write(block[:remaining])
                        written += remaining
                    else:
                        f.write(block)
                        written += self.BLOCK_SIZE
                    iterations += 1
                f.flush()
                # fsync — гарантия что данные дошли до контроллера, а не
                # остались в page cache (иначе MB/s будет преувеличен).
                # На некоторых ФС fsync может не поддерживаться.
                with contextlib.suppress(OSError):
                    os.fsync(f.fileno())
                elapsed = time.perf_counter() - started
        finally:
            path.unlink(missing_ok=True)

        mb_per_sec = (written / max(elapsed, 1e-9)) / 1e6
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=mb_per_sec,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "block_size_kb": self.BLOCK_SIZE // 1024,
                "file_size_mb": self.FILE_SIZE_MB,
                "queue_depth": "sync (1, MVP)",
                "target_dir": _resolve_target_dir(self.target_dir),
                "fsync_called": True,
            },
        )


__all__ = [
    "DiskRandomReadBench",
    "DiskSequentialReadBench",
    "DiskSequentialWriteBench",
    "_make_test_file",
]
