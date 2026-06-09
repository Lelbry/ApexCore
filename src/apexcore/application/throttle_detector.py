"""Детектор причины CPU-throttle (thermal / power / current / VR).

Расширяет существующий `cpu_throttled: bool` в `MetricSnapshot` структурой
`ThrottleState(cause, detail)`. Сам `MetricSnapshot` пока не меняется
(публичный контракт, нельзя ломать) — состояние храним в module-level
для будущей интеграции в `SensorSnapshot` в M4.

Источники по убыванию надёжности:

1. **Windows · LHM `SensorType.Factor`** — LibreHardwareMonitorLib публикует
   на новых Intel/AMD CPU sensor'ы вида ``CPU Clock Throttle / Thermal``,
   ``CPU Clock Throttle / Power`` и т.д. Если sensor с подстрокой
   ``throttle`` имеет значение > 0 — соответствующая причина активна.
   На некоторых CPU (особенно старых) этих sensor'ов нет — graceful degrade.

2. **Linux · `/sys/devices/system/cpu/cpu*/thermal_throttle/`** — kernel
   публикует кумулятивные counter'ы `core_throttle_count` и
   `package_throttle_count`. Сравниваем с предыдущим тиком — если вырос,
   throttle случился. Возвращаем cause=THERMAL (kernel этот источник
   публикует только для теплового; power/current нет).

3. **Fallback · heuristic** — если ни один источник не дал данных, и
   `cpu_avg / cpu_max < 0.85`, возвращаем cause=OTHER без детализации.
   Это поведение оставлено для совместимости с старым
   `base._detect_throttling`.

Никогда не бросает исключений: при любой ошибке возвращает
`ThrottleState(cause=NONE)`.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from apexcore.domain.sensor_models import ThrottleCause, ThrottleState

logger = logging.getLogger(__name__)

# Реэкспорт для обратной совместимости (до M4 типы жили в этом модуле).
# Существующий код, который импортировал `from apexcore.application.throttle_detector
# import ThrottleCause`, продолжит работать без правок. Канонический источник —
# `apexcore.domain.sensor_models`.
__all__ = [
    "ThrottleCause",
    "ThrottleState",
    "read_throttle_state",
]


# Состояние Linux thermal_throttle counter'ов между тиками.
_lock = threading.Lock()
_last_linux_counters: dict[str, int] = {}


def read_throttle_state(
    *,
    cpu_avg_mhz: float | None = None,
    cpu_max_mhz: float | None = None,
    cpu_model: str | None = None,
    cpu_percent: float | None = None,
) -> ThrottleState:
    """Определить причину throttle на текущем тике.

    Параметры ``cpu_avg_mhz`` / ``cpu_max_mhz`` опциональны — используются
    только в heuristic-fallback (когда ни LHM, ни sysfs не дали явного
    сигнала).

    Параметр ``cpu_model`` опционален — если задан и это гетерогенный
    Intel (Alder/Raptor Lake с P+E), heuristic отключается:
    ``cpu_avg / cpu_max < 0.85`` ложно срабатывает на гибридах
    (cpu_avg = mean(P+E) ≈ 0.7 × cpu_max = P-core boost), давая
    false-positive throttle даже при отсутствии реальной просадки.

    Параметр ``cpu_percent`` опционален — без значимой нагрузки
    (`cpu_percent < 80%`) heuristic отключается. На AMD Ryzen 6800H idle
    (4% load, powersave governor) частоты падают до ~2.4 ГГц при max
    boost 4.79 ГГц → ratio ≈ 0.5, что ложно классифицировалось как
    throttle. Реальный throttle всегда коррелирует с высокой нагрузкой.
    Legacy-вызовы (без ``cpu_percent``) сохраняют старое поведение для
    обратной совместимости.
    """
    if sys.platform == "win32":
        state = _read_windows_throttle()
        if state.cause is not ThrottleCause.NONE:
            return state
    else:
        state = _read_linux_throttle()
        if state.cause is not ThrottleCause.NONE:
            return state

    return _heuristic_throttle(cpu_avg_mhz, cpu_max_mhz, cpu_model, cpu_percent)


def _read_windows_throttle() -> ThrottleState:
    """Сканировать LHM CPU-sensors с "throttle" в имени.

    LHM на Intel Core / AMD Zen публикует sensor'ы вида:
    - ``CPU Clock Throttle / Thermal`` (SensorType.Factor, 0..1)
    - ``CPU Clock Throttle / Power``
    - ``CPU Clock Throttle / Current``
    Значение > 0 — соответствующая причина активна.
    """
    try:
        from apexcore.infrastructure.sensors import lhm

        computer = lhm._get_computer()
        if computer is None:
            return ThrottleState(cause=ThrottleCause.NONE)

        from LibreHardwareMonitor.Hardware import HardwareType  # type: ignore

        cpu_type = HardwareType.Cpu
        for hardware in computer.Hardware:
            if hardware.HardwareType != cpu_type:
                continue
            try:
                hardware.Update()
            except Exception as exc:
                logger.debug("LHM hardware.Update upal: %s", exc)
                continue
            for sensor in hardware.Sensors:
                name = str(sensor.Name).lower()
                if "throttle" not in name:
                    continue
                value = sensor.Value
                if value is None or float(value) <= 0.0:
                    continue
                cause = _classify_throttle_name(name)
                detail = f"{sensor.Name} = {float(value):.2f}"
                return ThrottleState(cause=cause, detail=detail)
    except Exception as exc:
        logger.debug("Windows throttle read failed: %s", exc)

    return ThrottleState(cause=ThrottleCause.NONE)


def _read_linux_throttle() -> ThrottleState:
    """Прочитать kernel thermal_throttle counter'ы.

    Для каждого ядра — ``core_throttle_count`` и ``package_throttle_count``.
    Сравниваем с предыдущим тиком: рост = throttle активен. Kernel
    публикует только thermal — возвращаем cause=THERMAL.
    """
    try:
        root = Path("/sys/devices/system/cpu")
        if not root.is_dir():
            return ThrottleState(cause=ThrottleCause.NONE)

        current: dict[str, int] = {}
        for cpu_dir in sorted(root.glob("cpu[0-9]*")):
            tt_dir = cpu_dir / "thermal_throttle"
            if not tt_dir.is_dir():
                continue
            for counter_name in ("core_throttle_count", "package_throttle_count"):
                f = tt_dir / counter_name
                if not f.exists():
                    continue
                try:
                    current[f"{cpu_dir.name}/{counter_name}"] = int(f.read_text().strip())
                except (OSError, ValueError):
                    continue

        if not current:
            return ThrottleState(cause=ThrottleCause.NONE)

        with _lock:
            previous = dict(_last_linux_counters)
            _last_linux_counters.clear()
            _last_linux_counters.update(current)

        if not previous:
            # Первый тик — есть baseline, но ещё нет роста; не сигналим.
            return ThrottleState(cause=ThrottleCause.NONE)

        for key, value in current.items():
            prev = previous.get(key, value)
            if value > prev:
                delta = value - prev
                detail = f"{key} +{delta}"
                return ThrottleState(cause=ThrottleCause.THERMAL, detail=detail)
    except Exception as exc:
        logger.debug("Linux throttle read failed: %s", exc)

    return ThrottleState(cause=ThrottleCause.NONE)


def _classify_throttle_name(name: str) -> ThrottleCause:
    """Имя LHM-sensor → ThrottleCause.

    Имена варьируются между поколениями: "Thermal", "Power Limit",
    "Current Limit", "VR Thermal", "EDP Other", и т.д.
    """
    if "thermal" in name and "vr" in name:
        return ThrottleCause.VR_THERMAL
    if "thermal" in name:
        return ThrottleCause.THERMAL
    if "power" in name:
        return ThrottleCause.POWER
    if "current" in name:
        return ThrottleCause.CURRENT
    return ThrottleCause.OTHER


def _heuristic_throttle(
    cpu_avg_mhz: float | None,
    cpu_max_mhz: float | None,
    cpu_model: str | None = None,
    cpu_percent: float | None = None,
) -> ThrottleState:
    """Старый heuristic из `base._detect_throttling`.

    Если средняя частота < 85% от максимума — throttle с причиной OTHER
    (точную причину не знаем). Сохранён для совместимости.

    Гарды от false-positive:
    - На гетерогенных Intel (Alder/Raptor Lake) heuristic отключается:
      ``cpu_avg = mean(P + E)`` всегда ниже ``cpu_max = P-core boost``,
      ratio ≈ 0.7 < 0.85 даёт false-positive throttle постоянно. Без
      LHM-сигнала (явный PROCHOT) на гибридах считаем «причина
      неизвестна» — лучше пропустить, чем врать.
    - При низкой нагрузке (``cpu_percent < 80``) heuristic тоже
      отключается: idle-частоты в powersave/P-state idle — это
      **штатное** поведение энергосбережения, не thermal throttle.
      На AMD APU (Ryzen U/HS/HX) idle частоты падают ~50% от boost,
      что без этой проверки давало красный баннер в WebUI на idle
      Astra. Реальный thermal throttle всегда коррелирует с высокой
      нагрузкой — оставшиеся ≥80% сценарии (CPU стресс, видео-кодинг,
      компиляция) корректно ловятся.
    """
    if cpu_avg_mhz is None or cpu_max_mhz is None or cpu_max_mhz <= 0:
        return ThrottleState(cause=ThrottleCause.NONE)
    if cpu_model and _is_hybrid_intel(cpu_model):
        return ThrottleState(cause=ThrottleCause.NONE)
    if cpu_percent is not None and cpu_percent < 80.0:
        return ThrottleState(cause=ThrottleCause.NONE)
    ratio = cpu_avg_mhz / cpu_max_mhz
    if ratio < 0.85:
        detail = f"freq ratio {ratio:.2f} < 0.85"
        return ThrottleState(cause=ThrottleCause.OTHER, detail=detail)
    return ThrottleState(cause=ThrottleCause.NONE)


def _is_hybrid_intel(cpu_model: str) -> bool:
    """Эвристика: cpu_model — гетерогенный Intel (Alder/Raptor Lake P+E)?

    Импорт `_detect_hybrid_topology` lazy чтобы избежать циклической
    зависимости (roofline.py не должен ничего знать про throttle_detector,
    а наоборот — допустимо как leaf-utility).
    """
    from apexcore.application.roofline import _INTEL_HYBRID_SKU_TABLE, _normalize_model

    model = _normalize_model(cpu_model)
    return any(marker in model for marker in _INTEL_HYBRID_SKU_TABLE)


def _reset_for_tests() -> None:
    """Сбросить module-state — только для unit-тестов."""
    with _lock:
        _last_linux_counters.clear()
