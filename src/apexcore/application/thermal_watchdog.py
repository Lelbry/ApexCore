"""Активный термальный watchdog для стресс-тестов CPU+RAM.

Подписывается на ``MetricsBus`` и при каждом снимке проверяет, не
превысила ли температура любого CPU-сенсора порог ``Tj_max − margin``.
При первом превышении выставляет ``cancel_token`` (что мгновенно
останавливает все движки, использующие тот же токен), фиксирует
причину и продолжает молчать.

Источники Tj_max:
- Windows — расширение в ``infrastructure.sensors.lhm.read_lhm_tjmax()``;
- Linux/Astra — ``infrastructure.sensors.hwmon_thresholds.read_hwmon_tjmax()``;
- fallback — фиксированный 100°C (Intel Doc 655258 [25] — типичный Tj_max
  для потребительских Intel-процессоров).

Контракт:
- watchdog не запускает свой поток; он живёт в потоке шины (callback на
  publish). Это исключает «эффект наблюдателя» (см. отчёт §5.3) и не
  требует синхронизации с TelemetryService.
- watchdog игнорирует все ключи температур, не похожие на CPU/core/package —
  GPU и storage могут быть выше порога штатно.
- thread-safe: ``threading.Lock`` защищает мутацию ``_triggered``/``_reason``.
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from apexcore.domain.models import MetricSnapshot
from apexcore.domain.ports import MetricsBus
from apexcore.domain.sensor_models import DegradedReason

logger = logging.getLogger(__name__)

# Сенсоры считаются «CPU-температурой» если ключ содержит хотя бы одно из.
#
# ВНИМАНИЕ: ``thermal_zone`` / ``thermal zone`` сюда **не входят** намеренно.
# ACPI thermal zone в Windows (через perf-counter или ``MSAcpi``) — это
# температура корпуса/чипсета, не самого CPU. Её значение под нагрузкой
# почти не растёт (стабильно 25–30 °C) — пользователь сравнивал с AIDA64
# и убедился, что это не реальная CPU-температура. Если LHM/coretemp не
# доступны — лучше показать «нет данных», чем ложно-низкую температуру,
# при которой watchdog никогда не сработает.
_CPU_KEY_TOKENS = (
    "cpu",
    "core",
    "package",
    "tdie",
    "tctl",
    "k10temp",
    "coretemp",
    "ccd",
    "ccx",
)

DEFAULT_FALLBACK_TJMAX = 100.0
DEFAULT_MARGIN_C = 5.0
HARD_FLOOR_TJMAX = 70.0  # ниже которого fallback всё равно не уходит

# Grace-window: при T ≥ Tj_max − margin (зона warning) watchdog даёт прогону
# ещё ≤ GRACE_WINDOW_SEC секунд для измерения sustainable performance, и
# только потом отменяет. Если T достигла Tj_max (абсолют) — instant stop.
# Если T опустилась ниже threshold — grace-таймер сбрасывается.
#
# Цель: измерить r_thermal в зоне Critical (95+ °C) для метрики «оценка под
# нагрузкой», не жертвуя безопасностью (hard stop по абсолютному Tj_max).
# См. research `docs/research/stress_test_mark_method.md` §4.1 + ответ
# пользователя 2026-05-17 («win-win»).
DEFAULT_GRACE_WINDOW_SEC = 60.0


def _is_cpu_temp_key(key: str) -> bool:
    """Эвристика: ключ относится к CPU-температуре, а не GPU/SSD/чипсету."""
    k = key.lower()
    if any(blocked in k for blocked in ("gpu", "nvme", "ssd", "storage", "wifi")):
        return False
    return any(token in k for token in _CPU_KEY_TOKENS)


@dataclass(frozen=True)
class WatchdogTrigger:
    """Запись о срабатывании watchdog: что и где.

    ``reason``:
    - ``"tjmax_reached"`` — T достигла абсолютного Tj_max, instant stop.
    - ``"grace_window_expired"`` — T держалась в зоне warning
      (Tj_max−margin) ≥ GRACE_WINDOW_SEC, плановый stop по истечении окна.
    - ``"threshold_reached"`` — старое поведение (grace_window_sec=0),
      instant stop по факту превышения threshold.
    """

    sensor_key: str
    temperature_c: float
    threshold_c: float
    timestamp: object  # datetime, но без импорта — оставляем object для гибкости
    message: str
    reason: str = "threshold_reached"


def detect_tjmax(fallback: float = DEFAULT_FALLBACK_TJMAX) -> tuple[float, str]:
    """Определить эффективный Tj_max для активного watchdog.

    Возвращает кортеж (Tj_max в °C, источник). Источник полезен для отчётов
    и UI: «*определено через LHM/distance_to_tjmax*» / «*hwmon coretemp/temp1_crit*»
    / «*fallback (Intel Doc 655258)*».
    """
    system = platform.system().lower()
    try:
        if system == "windows":
            from apexcore.infrastructure.sensors.lhm import read_lhm_tjmax

            tjmax_map = read_lhm_tjmax()
            values = [
                v for v in tjmax_map.values()
                if HARD_FLOOR_TJMAX <= v <= 130.0
            ]
            if values:
                return float(min(values)), f"lhm:{min(tjmax_map, key=lambda k: tjmax_map[k])}"
        elif system == "linux":
            from apexcore.infrastructure.sensors.hwmon_thresholds import (
                best_tjmax,
                read_hwmon_tjmax,
            )

            tjmax_map = read_hwmon_tjmax()
            value = best_tjmax(tjmax_map, fallback=fallback)
            if tjmax_map:
                return value, f"hwmon:{min(tjmax_map, key=lambda k: tjmax_map[k])}"
    except Exception as exc:
        logger.debug("detect_tjmax: ошибка определения, fallback %.1f°C: %s", fallback, exc)
    return fallback, "fallback"


class ThermalWatchdog:
    """Активный страж: при достижении Tj_max−margin отменяет все движки.

    Использование:

        watchdog = ThermalWatchdog(bus=bus, cancel_token=token)
        watchdog.start()
        # ... запуск стресс-движков с тем же token ...
        watchdog.stop()
        if watchdog.triggered:
            print(watchdog.trigger.message)
    """

    def __init__(
        self,
        bus: MetricsBus,
        cancel_token: threading.Event,
        *,
        tjmax: float | None = None,
        margin_celsius: float = DEFAULT_MARGIN_C,
        grace_window_sec: float = DEFAULT_GRACE_WINDOW_SEC,
        on_trigger: Callable[[WatchdogTrigger], None] | None = None,
    ) -> None:
        if tjmax is None:
            self._tjmax, self._tjmax_source = detect_tjmax()
        else:
            self._tjmax = tjmax
            self._tjmax_source = "explicit"
        self._margin = max(1.0, margin_celsius)
        self._threshold = self._tjmax - self._margin
        # grace_window_sec=0 → старое поведение (instant stop на threshold).
        # Иначе — при T ≥ threshold даём ещё это окно для измерения
        # sustainable performance, потом forced stop.
        self._grace_window_sec = max(0.0, grace_window_sec)
        self._bus = bus
        self._cancel_token = cancel_token
        self._on_trigger = on_trigger
        self._lock = threading.Lock()
        self._trigger: WatchdogTrigger | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # P0.7: счётчик «сколько подряд snapshot'ов пришли без CPU-температуры».
        # Используется свойством ``no_data_reason`` для дифференциации
        # «trigger не сработал» vs «данных не было вообще».
        self._no_data_snapshots = 0
        self._snapshots_total = 0
        # Grace-window state: monotonic-время первого превышения threshold.
        # None — сейчас не в зоне warning. Сбрасывается, когда T опустилась
        # ниже threshold (но только если в snapshot были данные — иначе
        # пропуск сенсора не должен «спасать» от forced stop).
        self._grace_started_monotonic: float | None = None

    @property
    def tjmax(self) -> float:
        return self._tjmax

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def tjmax_source(self) -> str:
        return self._tjmax_source

    @property
    def grace_window_sec(self) -> float:
        return self._grace_window_sec

    @property
    def in_grace_window(self) -> bool:
        """True если watchdog сейчас ждёт истечения grace-окна.

        UI использует это для жёлтого сообщения «температура подходит к
        лимиту, прогон завершится через ≤ N сек».
        """
        with self._lock:
            return (
                self._trigger is None
                and self._grace_started_monotonic is not None
            )

    @property
    def grace_remaining_sec(self) -> float | None:
        """Сколько сек осталось до forced stop. None если grace неактивен."""
        with self._lock:
            if self._trigger is not None or self._grace_started_monotonic is None:
                return None
            elapsed = time.monotonic() - self._grace_started_monotonic
            return max(0.0, self._grace_window_sec - elapsed)

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._trigger is not None

    @property
    def trigger(self) -> WatchdogTrigger | None:
        with self._lock:
            return self._trigger

    @property
    def had_data(self) -> bool:
        """Был ли хоть один snapshot с CPU-температурой за время работы.

        Если ``False`` → watchdog никогда не имел шанса сработать (нет
        данных, а не «всё в порядке»). UX-слой должен сообщать об этом
        через ``no_data_reason`` вместо «watchdog не зафиксирован».
        """
        with self._lock:
            return self._snapshots_total > 0 and self._no_data_snapshots < self._snapshots_total

    @property
    def no_data_reason(self) -> DegradedReason | None:
        """``DegradedReason`` если все snapshot'ы пришли без CPU-температуры.

        Возвращает ``None`` если данные были — пусть и не превышали порог.
        Если данных не было → запрашивает причину через
        ``run_full_probe`` (HVCI/SAC/Defender/...).
        """
        with self._lock:
            if self._snapshots_total == 0:
                # Watchdog ни разу не получил snapshot — рано судить.
                return None
            if self._no_data_snapshots < self._snapshots_total:
                # Хотя бы один snapshot имел CPU-keys → есть данные.
                return None
        # Все snapshot'ы были без CPU-keys → классифицируем причину.
        try:
            from apexcore.application.diagnostics_sensors import (
                _classify_lhm_no_cpu_reason,
            )

            return _classify_lhm_no_cpu_reason()
        except Exception as exc:  # pragma: no cover
            logger.debug("no_data_reason: классификация упала: %s", exc)
            return DegradedReason.UNKNOWN

    def start(self) -> None:
        """Подписаться на шину. Идемпотентно: повторный вызов — no-op."""
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._bus.subscribe(self._on_snapshot)
        logger.debug(
            "ThermalWatchdog started: Tj_max=%.1f°C, threshold=%.1f°C, source=%s",
            self._tjmax,
            self._threshold,
            self._tjmax_source,
        )

    def stop(self) -> None:
        """Отписаться от шины. Можно вызывать многократно."""
        unsub = self._unsubscribe
        if unsub is not None:
            self._unsubscribe = None
            try:
                unsub()
            except Exception:
                logger.debug("ThermalWatchdog: ошибка при отписке", exc_info=True)

    def _on_snapshot(self, snap: MetricSnapshot) -> None:
        # Уже сработали — дальнейшие тики игнорируем (но не отписываемся, чтобы
        # не плодить race-condition с stop()).
        if self.triggered:
            return
        worst_key: str | None = None
        worst_value: float = -1.0
        had_any_cpu_value = False
        for key, value in snap.temperatures.items():
            if not _is_cpu_temp_key(key):
                continue
            if value is None:
                continue
            had_any_cpu_value = True
            if value > worst_value:
                worst_value = float(value)
                worst_key = key
        # P0.7: счётчик no-data для свойств ``had_data`` / ``no_data_reason``.
        with self._lock:
            self._snapshots_total += 1
            if not had_any_cpu_value:
                self._no_data_snapshots += 1

        # Нет CPU-данных в snapshot'е — не трогаем grace state (пропуск
        # сенсора не должен «спасать» от forced stop в зоне warning).
        if not had_any_cpu_value or worst_key is None:
            return

        # Зона safe: T ниже threshold — сбрасываем grace-таймер если был.
        if worst_value < self._threshold:
            with self._lock:
                self._grace_started_monotonic = None
            return

        now_mono = time.monotonic()
        # Зона hard-stop: T достигла абсолютного Tj_max → instant cancel
        # (безопасность остаётся приоритетом, см. research §4.1).
        if worst_value >= self._tjmax:
            self._fire_trigger(
                worst_key=worst_key,
                worst_value=worst_value,
                snap_timestamp=snap.timestamp,
                reason="tjmax_reached",
                message=(
                    f"Watchdog (hard stop): T={worst_value:.1f}°C на «{worst_key}» "
                    f"≥ Tj_max {self._tjmax:.0f}°C, источник лимита={self._tjmax_source}"
                ),
            )
            return

        # Зона warning: threshold ≤ T < Tj_max. Поведение зависит от grace_window_sec.
        if self._grace_window_sec <= 0:
            # Старое поведение: instant stop по threshold.
            self._fire_trigger(
                worst_key=worst_key,
                worst_value=worst_value,
                snap_timestamp=snap.timestamp,
                reason="threshold_reached",
                message=(
                    f"Watchdog: T={worst_value:.1f}°C на «{worst_key}» ≥ "
                    f"порога остановки "
                    f"(температурный лимит {self._tjmax:.0f}°C − {self._margin:.0f}°C), "
                    f"источник лимита={self._tjmax_source}"
                ),
            )
            return

        # Grace-window активен: запускаем таймер либо проверяем истёк ли.
        with self._lock:
            if self._grace_started_monotonic is None:
                self._grace_started_monotonic = now_mono
                logger.info(
                    "ThermalWatchdog: T=%.1f°C на «%s» в зоне warning "
                    "(threshold=%.1f°C, Tj_max=%.0f°C); grace-window %d сек запущен.",
                    worst_value, worst_key, self._threshold, self._tjmax,
                    int(self._grace_window_sec),
                )
                return
            elapsed = now_mono - self._grace_started_monotonic
            if elapsed < self._grace_window_sec:
                return
        # Grace истёк — forced stop.
        self._fire_trigger(
            worst_key=worst_key,
            worst_value=worst_value,
            snap_timestamp=snap.timestamp,
            reason="grace_window_expired",
            message=(
                f"Watchdog (grace истёк): T={worst_value:.1f}°C на «{worst_key}» "
                f"≥ {self._threshold:.1f}°C удерживалась ≥ {int(self._grace_window_sec)} сек, "
                f"плановый стоп для измерения sustainable performance; "
                f"источник лимита={self._tjmax_source}"
            ),
        )

    def _fire_trigger(
        self,
        *,
        worst_key: str,
        worst_value: float,
        snap_timestamp: object,
        reason: str,
        message: str,
    ) -> None:
        """Сформировать WatchdogTrigger, выставить cancel_token, вызвать callback."""
        trigger = WatchdogTrigger(
            sensor_key=worst_key,
            temperature_c=worst_value,
            threshold_c=self._threshold,
            timestamp=snap_timestamp,
            message=message,
            reason=reason,
        )
        with self._lock:
            if self._trigger is not None:
                return
            self._trigger = trigger
        # Cancel вне лока: cancel_token сам потокобезопасен.
        try:
            self._cancel_token.set()
        except Exception:
            logger.debug("ThermalWatchdog: cancel_token.set() упал", exc_info=True)
        logger.warning(trigger.message)
        if self._on_trigger is not None:
            try:
                self._on_trigger(trigger)
            except Exception:
                logger.debug("ThermalWatchdog.on_trigger callback упал", exc_info=True)


__all__ = [
    "DEFAULT_FALLBACK_TJMAX",
    "DEFAULT_GRACE_WINDOW_SEC",
    "DEFAULT_MARGIN_C",
    "ThermalWatchdog",
    "WatchdogTrigger",
    "detect_tjmax",
]
