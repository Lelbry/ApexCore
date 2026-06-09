"""Сервис телеметрии: фоновый поток-семплер + Pub/Sub.

`InMemoryMetricsBus` рассылает каждый снимок всем подписчикам, а
`TelemetryService` периодически опрашивает OS-адаптер с заданным шагом и
публикует `MetricSnapshot` в шину. Подписчиками могут выступать CLI live-view,
SQLite-писатель, websocket-канал.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable

from apexcore.domain.models import MetricSnapshot
from apexcore.domain.ports import MetricsSubscriber, OSAdapter

logger = logging.getLogger(__name__)


class InMemoryMetricsBus:
    """Простая потокобезопасная Pub/Sub-шина."""

    def __init__(self) -> None:
        self._subscribers: list[MetricsSubscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, subscriber: MetricsSubscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._subscribers.remove(subscriber)

        return _unsubscribe

    def publish(self, snapshot: MetricSnapshot) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(snapshot)
            except Exception:
                logger.exception("Подписчик MetricsBus упал, продолжаем")


class TelemetryService:
    """Фоновый семплер метрик с публикацией в шину."""

    def __init__(
        self,
        adapter: OSAdapter,
        bus: InMemoryMetricsBus,
        sampling_rate_sec: float = 0.5,
        history_capacity: int = 100_000,
    ) -> None:
        self._adapter = adapter
        self._bus = bus
        self._rate = max(0.05, sampling_rate_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._history: deque[MetricSnapshot] = deque(maxlen=history_capacity)
        self._lock = threading.Lock()
        self._record_history = True
        self._latest: MetricSnapshot | None = None

    def start(self, record_history: bool = True) -> None:
        """Запустить фоновый семплер."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._record_history = record_history
        with self._lock:
            self._history.clear()
            self._latest = None
        self._thread = threading.Thread(target=self._run, name="apexcore-telemetry", daemon=True)
        self._thread.start()
        logger.debug("TelemetryService запущен (rate=%.3fs)", self._rate)

    def stop(self, timeout: float = 2.0) -> list[MetricSnapshot]:
        """Остановить семплер и вернуть собранную историю."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        with self._lock:
            return list(self._history)

    def history(self) -> list[MetricSnapshot]:
        with self._lock:
            return list(self._history)

    def latest(self) -> MetricSnapshot | None:
        """Последний опубликованный снимок (или None до первого семпла).

        Нужен, чтобы новые подписчики получали данные мгновенно, не дожидаясь
        очередного тика семплера (``rate_sec``). В частности ``/ws/metrics``
        шлёт его сразу при connect — иначе топбар/дашборд держат «—» / «WS down»
        до первого тика. Симметрично ``SensorService.latest()``.
        """
        with self._lock:
            return self._latest

    def _run(self) -> None:
        # Прогрев выполнен синхронно в start() ДО запуска этого потока —
        # значит первый snap здесь уже валиден и публикуется сразу.
        while not self._stop.is_set():
            try:
                snap = self._adapter.get_current_metrics()
            except Exception:
                logger.exception("Сбор метрик завершился ошибкой")
                if self._stop.wait(self._rate):
                    return
                continue
            with self._lock:
                self._latest = snap
                if self._record_history:
                    self._history.append(snap)
            self._bus.publish(snap)
            if self._stop.wait(self._rate):
                return
