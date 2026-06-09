"""Сервис «Датчики» (M4): семплер + конвертер + Pub/Sub.

Параллельный к ``TelemetryService``: использует тот же ``OSAdapter`` и
тот же hot-path ``get_current_metrics()``, но публикует данные как
``SensorSnapshot`` — структурированные показания с группировкой и
явным kind/unit. Это основа для нового TUI «Датчики» (M5).

Сосуществование со старым `monitor`:

- Существующий ``apexcore monitor`` продолжает работать через
  ``TelemetryService`` + ``MetricSnapshot``. Никаких регрессий.
- Новый UI «Датчики» использует ``SensorService`` + ``SensorSnapshot``.
- Источники данных одни и те же (LHM/NVML/smartctl/...) — оба сервиса
  опрашивают железо через `adapter.get_current_metrics()`.

Подход к throttle: причина опрашивается через
``throttle_detector.read_throttle_state`` в момент конвертации каждого
снимка. Это уже работает в `_detect_throttling` для `MetricSnapshot.cpu_throttled`,
но `ThrottleState` (с причиной) сохраняется только в `SensorSnapshot.throttle`
— `MetricSnapshot` остаётся `bool`-only для обратной совместимости.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from apexcore.application.sensor_keys import parse_legacy_key
from apexcore.application.throttle_detector import read_throttle_state
from apexcore.domain.models import MetricSnapshot, SystemInfo
from apexcore.domain.ports import OSAdapter
from apexcore.domain.sensor_models import (
    SensorKind,
    SensorReading,
    SensorSnapshot,
    ThrottleState,
)

logger = logging.getLogger(__name__)

SensorSubscriber = Callable[[SensorSnapshot], None]


# ─── Pub/Sub ─────────────────────────────────────────────────────────────


class InMemorySensorBus:
    """Простая потокобезопасная Pub/Sub-шина для `SensorSnapshot`.

    Аналогична `InMemoryMetricsBus` — отдельная шина, чтобы не смешивать
    с `MetricSnapshot`-подписчиками (старый `monitor` и т.п.).
    """

    def __init__(self) -> None:
        self._subscribers: list[SensorSubscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, subscriber: SensorSubscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._subscribers.remove(subscriber)

        return _unsubscribe

    def publish(self, snapshot: SensorSnapshot) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(snapshot)
            except Exception:
                logger.exception("Подписчик SensorBus упал, продолжаем")


# ─── Конвертер ───────────────────────────────────────────────────────────


def metric_to_sensor_snapshot(
    metric: MetricSnapshot,
    *,
    system_info: SystemInfo | None = None,
    gpu_devices: dict[str, str] | None = None,
    tjmax_by_key: dict[str, float] | None = None,
    throttle: ThrottleState | None = None,
    storage_lhm_names: dict[str, str] | None = None,
    storage_smartctl_info: dict[str, dict[str, str]] | None = None,
) -> SensorSnapshot:
    """Преобразовать существующий ``MetricSnapshot`` в ``SensorSnapshot``.

    :param metric: исходный отсчёт из адаптера.
    :param system_info: модель CPU для подписи карточек (если не задан —
        будет «CPU»).
    :param gpu_devices: ``{prefix → device_name}``, например
        ``{"gpunvidia": "NVIDIA RTX 4070 Ti", "nvml": "NVIDIA RTX 4070 Ti"}``.
    :param tjmax_by_key: пороги из `lhm.read_lhm_tjmax()` — для CPU
        температурных threshold_crit.
    :param throttle: предвычисленный ThrottleState (если уже опрашивали —
        не дёргаем повторно). Если None — вызовем ``read_throttle_state``.
    :param storage_lhm_names: имена дисков из LHM
        (``read_lhm_storage_names()``).
    :param storage_smartctl_info: model+type дисков из smartctl
        (``read_smartctl_devices_info()``).
    """
    cpu_device = system_info.cpu_model if system_info else "CPU"
    gpu_devices = gpu_devices or {}
    tjmax_by_key = tjmax_by_key or {}
    storage_lhm_names = storage_lhm_names or {}
    storage_smartctl_info = storage_smartctl_info or {}

    readings: list[SensorReading] = []
    for key, value in metric.temperatures.items():
        r = parse_legacy_key(
            key, value,
            default_kind=SensorKind.TEMPERATURE,
            cpu_device=cpu_device,
            gpu_devices=gpu_devices,
            thresholds=tjmax_by_key,
            storage_lhm_names=storage_lhm_names,
            storage_smartctl_info=storage_smartctl_info,
        )
        if r is not None:
            readings.append(r)
    for key, value in metric.voltages.items():
        r = parse_legacy_key(
            key, value,
            default_kind=SensorKind.VOLTAGE,
            cpu_device=cpu_device,
            gpu_devices=gpu_devices,
        )
        if r is not None:
            readings.append(r)
    for key, value in metric.frequencies.items():
        r = parse_legacy_key(
            key, value,
            default_kind=SensorKind.FREQUENCY,
            cpu_device=cpu_device,
            gpu_devices=gpu_devices,
        )
        if r is not None:
            readings.append(r)

    if throttle is None:
        throttle = read_throttle_state(
            cpu_avg_mhz=metric.frequencies.get("cpu_avg"),
            cpu_max_mhz=metric.frequencies.get("cpu_max"),
            cpu_model=(system_info.cpu_model if system_info is not None else None),
            cpu_percent=metric.cpu_percent,
        )

    return SensorSnapshot(
        timestamp=metric.timestamp,
        readings=readings,
        throttle=throttle,
    )


# ─── Сервис ──────────────────────────────────────────────────────────────


class SensorService:
    """Фоновый семплер «Датчиков»: опрашивает адаптер, конвертит, публикует.

    Использование (mirror ``TelemetryService``):

    .. code-block:: python

        bus = InMemorySensorBus()
        bus.subscribe(render_sensor_snapshot)   # M5 UI
        service = SensorService(adapter=AdapterFactory.detect(), bus=bus)
        service.start()
        try:
            time.sleep(duration)
        finally:
            history = service.stop()
    """

    def __init__(
        self,
        adapter: OSAdapter,
        bus: InMemorySensorBus,
        sampling_rate_sec: float = 0.5,
        history_capacity: int = 100_000,
        gpu_devices: dict[str, str] | None = None,
        tjmax_by_key: dict[str, float] | None = None,
        storage_lhm_names: dict[str, str] | None = None,
        storage_smartctl_info: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._adapter = adapter
        self._bus = bus
        self._rate = max(0.05, sampling_rate_sec)
        self._gpu_devices = gpu_devices or {}
        self._tjmax_by_key = tjmax_by_key or {}
        self._storage_lhm_names = storage_lhm_names or {}
        self._storage_smartctl_info = storage_smartctl_info or {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._history: deque[SensorSnapshot] = deque(maxlen=history_capacity)
        self._lock = threading.Lock()
        # SystemInfo для подписи устройств собираем один раз при старте.
        self._system_info: SystemInfo | None = None

    def start(self) -> None:
        """Запустить фоновый семплер."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._history.clear()
        # Один раз снимаем sys-info для подписей. Дешёво, не на hot-path.
        try:
            self._system_info = self._adapter.get_system_info()
        except Exception:
            logger.exception("get_system_info() при старте SensorService упал")
            self._system_info = None
        self._thread = threading.Thread(
            target=self._run, name="apexcore-sensors", daemon=True
        )
        self._thread.start()
        logger.debug("SensorService запущен (rate=%.3fs)", self._rate)

    def stop(self, timeout: float = 2.0) -> list[SensorSnapshot]:
        """Остановить семплер и вернуть собранную историю."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        with self._lock:
            return list(self._history)

    def history(self) -> list[SensorSnapshot]:
        """Текущая накопленная история — потокобезопасный snapshot."""
        with self._lock:
            return list(self._history)

    def latest(self) -> SensorSnapshot | None:
        """Последний опубликованный snapshot (для UI «получи и нарисуй»)."""
        with self._lock:
            return self._history[-1] if self._history else None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                metric = self._adapter.get_current_metrics()
                snap = metric_to_sensor_snapshot(
                    metric,
                    system_info=self._system_info,
                    gpu_devices=self._gpu_devices,
                    tjmax_by_key=self._tjmax_by_key,
                    storage_lhm_names=self._storage_lhm_names,
                    storage_smartctl_info=self._storage_smartctl_info,
                )
            except Exception:
                logger.exception("Сбор/конвертация sensor-snapshot упали")
                if self._stop.wait(self._rate):
                    return
                continue
            with self._lock:
                self._history.append(snap)
            self._bus.publish(snap)
            if self._stop.wait(self._rate):
                return
        # Один финальный snapshot на выходе из цикла — гарантирует, что
        # последнее значение видно потребителю при немедленной остановке.

    def make_snapshot(self) -> SensorSnapshot | None:
        """Снять один отсчёт без запуска фонового потока.

        Полезно для подсказок в админ-CLI и для unit-тестов конвертера.
        """
        try:
            metric = self._adapter.get_current_metrics()
        except Exception:
            logger.exception("get_current_metrics() упал")
            return None
        sys_info = self._system_info
        if sys_info is None:
            with contextlib.suppress(Exception):
                sys_info = self._adapter.get_system_info()
        return metric_to_sensor_snapshot(
            metric,
            system_info=sys_info,
            gpu_devices=self._gpu_devices,
            tjmax_by_key=self._tjmax_by_key,
            storage_lhm_names=self._storage_lhm_names,
            storage_smartctl_info=self._storage_smartctl_info,
        )


def empty_sensor_snapshot() -> SensorSnapshot:
    """Пустой снимок — для отображения «загрузка…» в UI до первого тика."""
    return SensorSnapshot(timestamp=datetime.now(timezone.utc), readings=[])
