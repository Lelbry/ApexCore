"""Установка SIGINT-обработчика на время активного теста.

Идея проста: пока пользователь сидит в меню, Ctrl+C приводит к стандартному
``KeyboardInterrupt`` (его ловит каркас меню — просто возврат на текущий
экран). Но как только запущен тест, Ctrl+C должен НЕ ронять процесс и НЕ
выводить traceback, а только выставить ``threading.Event`` — а сам цикл
теста, увидев флаг, корректно прервётся между итерациями.

Контекстный менеджер ``cancellable()`` этим и занимается:

    with cancellable() as token:
        result = test.run(duration_sec=5, cancel_token=token)
        if token.is_set():
            console.print("[yellow]Прервано[/]")

После выхода из контекста SIGINT-обработчик восстанавливается на исходный.

Кросс-платформенность
---------------------
- Linux/Astra: стандартный ``signal.signal(SIGINT, handler)``.
- Windows: то же самое работает в основном потоке консольного приложения;
  Python внутри субпроцесс-thread не имеет доступа к консольному event-у,
  но обработчик в основном потоке Ctrl+C ловит штатно (CTRL_C_EVENT
  доставляется как SIGINT).
- ``signal.signal`` нужно вызывать ИЗ ОСНОВНОГО ПОТОКА. В меню так и есть:
  меню крутится в main-thread, тесты запускаются в нём же синхронно.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Iterator


@contextlib.contextmanager
def cancellable() -> Iterator[threading.Event]:
    """На время блока подменить SIGINT-обработчик на «выставить Event».

    После выхода из блока — восстановить прежний обработчик. Если функция
    вызвана не из main-thread (нельзя установить signal handler) —
    тихо отдать Event без хука; пользователь сможет отменить только
    через KeyboardInterrupt (его поймает каркас меню).
    """
    token = threading.Event()
    is_main = threading.current_thread() is threading.main_thread()
    previous = None
    if is_main:
        try:
            previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda signum, frame: token.set())
        except (ValueError, OSError):
            # На некоторых ограниченных средах (embedded Python, IDE-консоли)
            # signal.signal недоступен — работаем без перехвата.
            previous = None
    try:
        yield token
    finally:
        if is_main and previous is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGINT, previous)


__all__ = ["cancellable"]
