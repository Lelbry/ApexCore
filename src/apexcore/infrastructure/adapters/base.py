"""Базовый адаптер: общая логика на основе psutil, переиспользуемая Win/Linux."""

from __future__ import annotations

import platform
import socket
from datetime import datetime, timezone

import psutil

from apexcore.domain.models import CpuCores, MetricSnapshot, SystemInfo
from apexcore.domain.ports import OSAdapter
from apexcore.infrastructure.cpu_frequencies import (
    average_mhz,
    read_base_frequencies_by_cpu,
)
from apexcore.infrastructure.cpu_topology import detect_hybrid_topology


class PsutilBaseAdapter(OSAdapter):
    """Базовый адаптер с психтил-телеметрией. Платформенные подклассы дополняют детали.

    Использует stateful-замер дискового I/O (разница между вызовами get_current_metrics).
    """

    name = "psutil_base"

    def __init__(self) -> None:
        # Сохраняем «предыдущий» снимок дискового I/O для расчёта дельты МБ/отсчёт.
        self._prev_disk_read_bytes: int | None = None
        self._prev_disk_write_bytes: int | None = None
        # Кеш cpu_model для горячего пути _detect_throttling (нужен для отсечения
        # heuristic на гибридных CPU). Lazy init — заполняется при первом snapshot'е.
        self._cached_cpu_model: str | None = None
        # Прогрев per-cpu_percent (первый вызов psutil.cpu_percent с interval=None — мусор).
        psutil.cpu_percent(interval=None, percpu=False)
        psutil.cpu_percent(interval=None, percpu=True)

    # ────────── общие методы ──────────

    def get_system_info(self) -> SystemInfo:
        cpu_model = self._read_cpu_model() or platform.processor() or "unknown"
        ram = psutil.virtual_memory()
        hybrid = detect_hybrid_topology()
        freqs = read_base_frequencies_by_cpu()
        # Базовая частота: для hybrid — отдельно P и E (усреднение по группе);
        # для non-hybrid и как «общая» — среднее по всем найденным CPU.
        cpu_base_mhz = average_mhz(freqs, tuple(freqs.keys())) if freqs else None
        cpu_base_p_mhz = (
            average_mhz(freqs, hybrid.p_cpus) if hybrid and hybrid.p_cpus else None
        )
        cpu_base_e_mhz = (
            average_mhz(freqs, hybrid.e_cpus) if hybrid and hybrid.e_cpus else None
        )
        # Fallback на psutil если реестр/sysfs ничего не дали (старая Windows,
        # урезанная VM, нестандартное ядро Linux).
        if cpu_base_mhz is None:
            try:
                ps_freq = psutil.cpu_freq()
                if ps_freq and ps_freq.current:
                    cpu_base_mhz = float(ps_freq.current)
            except (NotImplementedError, OSError):
                pass
        return SystemInfo(
            os_name=platform.system(),
            os_version=platform.version(),
            cpu_model=cpu_model,
            cpu_cores=CpuCores(
                physical=psutil.cpu_count(logical=False) or 0,
                logical=psutil.cpu_count(logical=True) or 0,
                p_cores=hybrid.p_cores if hybrid else None,
                e_cores=hybrid.e_cores if hybrid else None,
                p_threads=hybrid.p_threads if hybrid else None,
                e_threads=hybrid.e_threads if hybrid else None,
            ),
            ram_total_gb=ram.total / (1024 ** 3),
            gpu_list=self._enumerate_gpus(),
            cpu_arch=platform.machine() or None,
            hostname=socket.gethostname(),
            cpu_base_mhz=cpu_base_mhz,
            cpu_base_p_mhz=cpu_base_p_mhz,
            cpu_base_e_mhz=cpu_base_e_mhz,
            timestamp=datetime.now(timezone.utc),
        )

    def get_current_metrics(self) -> MetricSnapshot:
        cpu_total = psutil.cpu_percent(interval=None, percpu=False)
        cpu_per = list(psutil.cpu_percent(interval=None, percpu=True))
        ram = psutil.virtual_memory()
        disk_read_mb, disk_write_mb = self._disk_io_delta_mb()
        temps, voltages = self._read_sensors()
        freqs = self.get_frequencies_mhz()
        throttled = self._detect_throttling(freqs, cpu_percent=cpu_total)
        return MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=cpu_total,
            cpu_per_core_percent=cpu_per,
            ram_percent=ram.percent,
            ram_used_gb=ram.used / (1024 ** 3),
            disk_read_mb=disk_read_mb,
            disk_write_mb=disk_write_mb,
            temperatures=temps,
            frequencies=freqs,
            voltages=voltages,
            cpu_throttled=throttled,
            power_w=None,
        )

    def _read_sensors(self) -> tuple[dict[str, float], dict[str, float]]:
        """Снять температуры и напряжения за один проход сенсорной шины.

        Хук для подклассов, у которых температуры и напряжения публикуются
        одним и тем же источником (например, LHM). Базовая реализация —
        температуры через ``_read_temperatures``, напряжения отсутствуют.
        """
        return self._read_temperatures(), {}

    def get_frequencies_mhz(self) -> dict[str, float]:
        result: dict[str, float] = {}
        try:
            avg = psutil.cpu_freq(percpu=False)
            if avg is not None and avg.current:
                result["cpu_avg"] = float(avg.current)
                if avg.min:
                    result["cpu_min"] = float(avg.min)
                if avg.max:
                    result["cpu_max"] = float(avg.max)
        except (NotImplementedError, OSError):
            pass
        try:
            per_core = psutil.cpu_freq(percpu=True) or []
            for idx, f in enumerate(per_core):
                if f and f.current:
                    result[f"core_{idx}"] = float(f.current)
        except (NotImplementedError, OSError):
            pass
        return result

    def get_available_temps(self) -> list[str]:
        return list(self._read_temperatures().keys())

    def check_prerequisites(self) -> bool:
        # Базовая реализация считает, что психтил всегда есть; уточняется в подклассах.
        return True

    # ────────── расширяемые в подклассах ──────────

    def _read_cpu_model(self) -> str | None:
        """Подклассы возвращают точную модель CPU."""
        return None

    def _enumerate_gpus(self) -> list[str]:
        """Подклассы возвращают список GPU. По умолчанию — пусто."""
        return []

    def _read_temperatures(self) -> dict[str, float]:
        """Подклассы возвращают словарь сенсор → °C."""
        return {}

    # ────────── вспомогательные ──────────

    def _disk_io_delta_mb(self) -> tuple[float, float]:
        try:
            io = psutil.disk_io_counters()
        except (NotImplementedError, OSError):
            return 0.0, 0.0
        if io is None:
            return 0.0, 0.0
        read_b = int(io.read_bytes)
        write_b = int(io.write_bytes)
        read_delta = 0 if self._prev_disk_read_bytes is None else read_b - self._prev_disk_read_bytes
        write_delta = (
            0 if self._prev_disk_write_bytes is None else write_b - self._prev_disk_write_bytes
        )
        self._prev_disk_read_bytes = read_b
        self._prev_disk_write_bytes = write_b
        return max(0.0, read_delta / (1024 ** 2)), max(0.0, write_delta / (1024 ** 2))

    def _detect_throttling(
        self,
        freqs: dict[str, float],
        cpu_percent: float | None = None,
    ) -> bool:
        """Определить активен ли throttle CPU.

        Делегирует в `throttle_detector.read_throttle_state`, который
        пробует точные источники (LHM на Windows, sysfs на Linux) и
        падает на старый heuristic «cpu_avg / cpu_max < 0.85» если их
        нет. Возвращает только bool — типизированная причина (thermal /
        power / current) сохраняется внутри throttle_detector и попадёт
        в новое поле `SensorSnapshot.throttle` в M4.

        ``cpu_model`` передаётся для отсечения false-positive heuristic
        на гетерогенных Intel (см. `throttle_detector._is_hybrid_intel`).
        ``cpu_percent`` — для отсечения idle false-positive (idle частоты
        в powersave/P-state idle это не throttle; на AMD APU без этой
        проверки ratio ≈ 0.5 на idle давал постоянный красный баннер).
        """
        from apexcore.application.throttle_detector import read_throttle_state

        if self._cached_cpu_model is None:
            self._cached_cpu_model = self._read_cpu_model() or platform.processor()

        state = read_throttle_state(
            cpu_avg_mhz=freqs.get("cpu_avg"),
            cpu_max_mhz=freqs.get("cpu_max"),
            cpu_model=self._cached_cpu_model,
            cpu_percent=cpu_percent,
        )
        return state.active
