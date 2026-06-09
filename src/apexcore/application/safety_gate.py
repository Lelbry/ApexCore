"""Pre-flight проверки безопасности перед длинным стресс-прогоном.

По §4.3 отчёта (`docs/research/aggregated_stress_testing.md`) запуск
полного параллельного стресса (CPU+RAM, ≥ 10 мин) требует обязательных
защитных проверок:

1. **AC + battery** — на ноутбуке заряд должен быть ≥ 50%, иначе запуск
   блокируется. Источник: pyperf docs «*if the power cable is unplugged
   ... the CPU speed can change when the battery level becomes too low*».
2. **VM-detection** — в виртуальной среде термальные/частотные метрики
   невалидны (см. отчёт §8 п.4). Запуск разрешён только с ``--force``.
3. **Free RAM** — для RAM-стрессора нужен заметный буфер; меньше 2 ГБ
   свободной памяти блокирует прогон, чтобы OOM-killer не убил процесс.
4. **Cooling-sanity** — диагностический проход в первые 30 с прогона:
   если ΔT < 5°C, возможно, кулер не работает. Не блокирует, выводит
   предупреждение (§4.3 п.4).

Контракт:
- Класс ``SafetyGate`` использует ``OSAdapter`` и ``psutil`` для проверок.
- Метод ``check_pre_flight()`` возвращает ``SafetyReport`` с разбиением
  на блокирующие и предупреждающие причины. UI/CLI решает, как реагировать.
- ``cooling_sanity_subscriber`` возвращает callback, который можно
  подписать на ``MetricsBus``: после ~30 с с момента старта сэмплирует
  начальную/текущую T, выставляет флаг при недостаточном росте.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field

from apexcore.domain.models import MetricSnapshot
from apexcore.domain.ports import OSAdapter

logger = logging.getLogger(__name__)


def _detect_virt_linux() -> tuple[bool, str | None]:
    """Linux/Astra: ``systemd-detect-virt`` → fallback ``/proc/cpuinfo``."""
    try:
        result = subprocess.run(
            ["systemd-detect-virt"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        kind = (result.stdout or "").strip()
        if kind and kind != "none":
            return True, kind
        if result.returncode == 0:
            return False, None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("systemd-detect-virt не доступен: %s", exc)
    # Fallback: флаг hypervisor в /proc/cpuinfo (CPUID feature bit 31 в ECX,
    # ядро публикует его строкой `flags`).
    try:
        from pathlib import Path

        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        if " hypervisor" in text or ("\nflags" in text and "hypervisor" in text):
            return True, "unknown"
    except OSError:
        pass
    return False, None


def _detect_virt_windows() -> tuple[bool, str | None]:
    """Windows: WMI Win32_ComputerSystem; fallback CPUID hypervisor-bit.

    Без новых зависимостей: WMI читается через PowerShell + Get-CimInstance.
    Если PowerShell недоступен — пробуем через ``ctypes`` CPUID
    (хotя стандартного способа в Python без привлечения C-кода нет, поэтому
    при отсутствии PowerShell просто возвращаем «unknown»).
    """
    signatures = {
        "vmware": "vmware",
        "innotek": "virtualbox",
        "qemu": "qemu",
        "kvm": "kvm",
        "xen": "xen",
        "microsoft corporation": "hyperv",  # Hyper-V VM
        "parallels": "parallels",
    }
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).Manufacturer + '|' "
                "+ (Get-CimInstance Win32_ComputerSystem).Model",
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        line = (result.stdout or "").lower().strip()
        for sig, kind in signatures.items():
            if sig in line:
                # Hyper-V: на физических машинах Microsoft Corporation встречается
                # только если установлен Hyper-V; этого нам недостаточно для
                # уверенной детекции — но для VM-гостя обычно уверенный сигнал.
                if kind == "hyperv" and "virtual machine" not in line:
                    continue
                return True, kind
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("powershell Win32_ComputerSystem не доступен: %s", exc)
    return False, None


def detect_virtualization() -> tuple[bool, str | None]:
    """Кросс-платформенная VM-детекция. Возвращает (is_vm, kind)."""
    system = platform.system().lower()
    if system == "linux":
        return _detect_virt_linux()
    if system == "windows":
        return _detect_virt_windows()
    return False, None


@dataclass
class SafetyReport:
    """Итог проверок перед запуском стресса."""

    on_battery: bool = False
    battery_percent: float | None = None
    is_virtualized: bool = False
    virtualization_kind: str | None = None
    free_ram_gb: float | None = None
    block_reasons: list[str] = field(default_factory=list)
    warn_reasons: list[str] = field(default_factory=list)
    # Cooling-sanity заполняется отдельным сабскрайбером во время прогона.
    cooling_sanity_ok: bool | None = None
    initial_temp_c: float | None = None
    after_30s_temp_c: float | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.block_reasons)


class SafetyGate:
    """Сервис pre-flight проверок и cooling-sanity callback."""

    MIN_BATTERY_PERCENT_DEFAULT = 50.0
    MIN_FREE_RAM_GB = 2.0
    COOLING_SANITY_DELTA_C = 5.0
    COOLING_SANITY_WINDOW_SEC = 30.0

    def __init__(
        self,
        adapter: OSAdapter,
        *,
        min_battery_percent: float = MIN_BATTERY_PERCENT_DEFAULT,
        min_free_ram_gb: float = MIN_FREE_RAM_GB,
    ) -> None:
        self._adapter = adapter
        self._min_battery = min_battery_percent
        self._min_free_ram_gb = min_free_ram_gb

    def check_pre_flight(self) -> SafetyReport:
        """Собрать отчёт. Не блокирующий — UI решает, что делать."""
        report = SafetyReport()
        self._check_battery(report)
        self._check_virtualization(report)
        self._check_free_ram(report)
        return report

    def cooling_sanity_subscriber(
        self, report: SafetyReport
    ) -> tuple[object, threading.Event]:
        """Вернуть (subscriber callback, event), пригодные для подписки на bus.

        После первого снимка фиксируется ``initial_temp_c``. По истечении
        ``COOLING_SANITY_WINDOW_SEC`` проверяется ΔT и заполняется
        ``cooling_sanity_ok``; одновременно выставляется ``event`` —
        подписчик после этого можно отписать.
        """
        first_temp: list[float | None] = [None]
        first_time: list[float | None] = [None]
        finished = threading.Event()

        def on_snapshot(snap: MetricSnapshot) -> None:
            if finished.is_set():
                return
            cpu_temp = _pick_cpu_temp(snap)
            if cpu_temp is None:
                return
            if first_temp[0] is None:
                first_temp[0] = cpu_temp
                first_time[0] = time.monotonic()
                report.initial_temp_c = cpu_temp
                return
            elapsed = time.monotonic() - (first_time[0] or 0.0)
            if elapsed < self.COOLING_SANITY_WINDOW_SEC:
                return
            report.after_30s_temp_c = cpu_temp
            delta = cpu_temp - (first_temp[0] or 0.0)
            ok = delta >= self.COOLING_SANITY_DELTA_C
            report.cooling_sanity_ok = ok
            if not ok:
                msg = (
                    f"cooling-sanity: ΔT за 30 с = {delta:+.1f}°C — "
                    f"возможно, датчик не работает или нагрузка не достигает CPU."
                )
                if msg not in report.warn_reasons:
                    report.warn_reasons.append(msg)
            finished.set()

        return on_snapshot, finished

    def _check_battery(self, report: SafetyReport) -> None:
        try:
            import psutil

            battery = psutil.sensors_battery()
        except Exception as exc:
            logger.debug("psutil.sensors_battery упал: %s", exc)
            return
        if battery is None:
            # Десктоп без батареи — проверка не нужна.
            return
        report.battery_percent = float(battery.percent)
        report.on_battery = not bool(battery.power_plugged)
        if report.on_battery and report.battery_percent < self._min_battery:
            report.block_reasons.append(
                f"на батарее, заряд {report.battery_percent:.0f}% < "
                f"{self._min_battery:.0f}% — подключите AC-питание"
            )
        elif report.on_battery:
            report.warn_reasons.append(
                f"работа на батарее (заряд {report.battery_percent:.0f}%): "
                f"частоты CPU могут меняться, метрики throughput не сравнимы"
            )
        elif report.battery_percent is not None and report.battery_percent < 80.0:
            report.warn_reasons.append(
                f"AC подключён, но заряд {report.battery_percent:.0f}% < 80% — "
                f"возможно, ноутбук в power-saving"
            )

    def _check_virtualization(self, report: SafetyReport) -> None:
        is_vm, kind = detect_virtualization()
        report.is_virtualized = is_vm
        report.virtualization_kind = kind
        if is_vm:
            report.block_reasons.append(
                f"обнаружена виртуализация ({kind or 'unknown'}): "
                f"термальные и частотные метрики гостя не отражают физическое железо"
            )

    def _check_free_ram(self, report: SafetyReport) -> None:
        try:
            import psutil

            free_gb = psutil.virtual_memory().available / (1024 ** 3)
        except Exception as exc:
            logger.debug("psutil.virtual_memory упал: %s", exc)
            return
        report.free_ram_gb = free_gb
        if free_gb < self._min_free_ram_gb:
            report.block_reasons.append(
                f"свободной RAM {free_gb:.1f} ГБ < "
                f"{self._min_free_ram_gb:.1f} ГБ — RAM-стрессор не запустится без OOM"
            )


def _pick_cpu_temp(snap: MetricSnapshot) -> float | None:
    """Лучший «CPU-температурный» сенсор из снимка для cooling-sanity."""
    from apexcore.application.thermal_watchdog import _is_cpu_temp_key

    cpu_values = [v for k, v in snap.temperatures.items() if _is_cpu_temp_key(k)]
    if not cpu_values:
        return None
    return float(max(cpu_values))


__all__ = [
    "SafetyGate",
    "SafetyReport",
    "detect_virtualization",
]
