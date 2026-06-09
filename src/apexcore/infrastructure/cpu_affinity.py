"""Кросс-платформенная привязка текущего потока к набору CPU.

Нужна для теста «Single-Core»: чтобы воркер действительно крутился на
одном конкретном P-ядре, а не прыгал между P и E ядрами по решению
scheduler'а (Windows на гибридах ровно так и делает по умолчанию).

API — единственный публичный контекст-менеджер ``pinned_to_cpus``.
Внутри:
- **Windows**: ``SetThreadAffinityMask`` через ``ctypes``. Возвращаемое
  значение `SetThreadAffinityMask` — это **предыдущая** маска (если 0,
  значит вызов провалился). При выходе восстанавливаем.
- **Linux**: ``os.sched_setaffinity(0, cpu_set)`` / ``sched_getaffinity``.
- **macOS / прочие**: no-op (на Darwin нет публичного API для thread
  affinity). Бенчмарк всё равно запустится, просто scheduler сам решит.

Любая ошибка деградирует к no-op: лучше получить менее точный замер,
чем сорвать тест из-за прав или особенностей системы.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def pinned_to_cpus(cpu_ids: Iterable[int]) -> Iterator[bool]:
    """Прибить текущий поток к указанным логическим CPU.

    Yields:
        ``True``, если affinity реально применилось; ``False``, если
        платформа не поддерживается или вызов провалился (бенч всё равно
        запустится, просто без гарантии «крутится на CPU X»).
    """
    cpu_list = list(cpu_ids)
    if not cpu_list:
        yield False
        return

    system = platform.system().lower()
    if system == "windows":
        applied, restore = _pin_windows(cpu_list)
    elif system == "linux":
        applied, restore = _pin_linux(cpu_list)
    else:
        applied, restore = False, _noop_restore

    try:
        yield applied
    finally:
        try:
            restore()
        except Exception:
            logger.debug("affinity restore failed", exc_info=True)


def _noop_restore() -> None:
    return None


# ─────────────────────────── Windows ────────────────────────────


def _pin_windows(cpu_ids: list[int]) -> tuple[bool, callable]:
    """Set the current thread's affinity. Return (applied, restore_callable)."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False, _noop_restore

    kernel32 = ctypes.windll.kernel32
    get_current_thread = kernel32.GetCurrentThread
    get_current_thread.restype = wintypes.HANDLE

    set_affinity = kernel32.SetThreadAffinityMask
    # SetThreadAffinityMask(HANDLE hThread, DWORD_PTR dwThreadAffinityMask)
    # Returns previous affinity mask (DWORD_PTR). 0 means error.
    set_affinity.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    set_affinity.restype = ctypes.c_size_t

    handle = get_current_thread()
    mask = 0
    for cpu in cpu_ids:
        mask |= 1 << cpu

    prev = set_affinity(handle, mask)
    if prev == 0:
        logger.debug(
            "SetThreadAffinityMask failed (cpus=%r, mask=%#x)", cpu_ids, mask
        )
        return False, _noop_restore

    def restore() -> None:
        # Восстанавливаем предыдущую маску, чтобы не «протекало» в
        # последующие операции этого потока.
        set_affinity(handle, prev)

    return True, restore


# ─────────────────────────── Linux ────────────────────────────


def _pin_linux(cpu_ids: list[int]) -> tuple[bool, callable]:
    set_aff = getattr(os, "sched_setaffinity", None)
    get_aff = getattr(os, "sched_getaffinity", None)
    if set_aff is None or get_aff is None:
        return False, _noop_restore

    try:
        prev = set(get_aff(0))
        set_aff(0, set(cpu_ids))
    except OSError:
        logger.debug("sched_setaffinity failed for cpus=%r", cpu_ids, exc_info=True)
        return False, _noop_restore

    def restore() -> None:
        try:
            set_aff(0, prev)
        except OSError:
            logger.debug("sched_setaffinity restore failed", exc_info=True)

    return True, restore


# ─────────────────────────── helpers ────────────────────────────


def is_supported() -> bool:
    """Поддерживается ли смена affinity на этой ОС.

    Полезно для UI — если ``False``, имеет смысл показать пользователю
    «affinity недоступна, single-thread замер может быть менее точным».
    """
    system = platform.system().lower()
    if system == "windows":
        return sys.platform == "win32"  # ctypes.windll доступен
    if system == "linux":
        return hasattr(os, "sched_setaffinity")
    return False
