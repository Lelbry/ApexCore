"""Раннеры пункта меню «Стресс-нагрузка» (timed / infinite).

Используют новый стресс-функционал (``StressOrchestrator``,
``ParallelStressRunner``, ``ThermalWatchdog``, ``SafetyGate``) и сохраняют
результат в БД через существующий ``SqliteResultRepository``.

UX-принципы:
- Rich Live с прогресс-баром и сгруппированной таблицей CPU/RAM/GPU +
  sparkline-тренды (по образцу команды ``sensors``); refresh 2 Гц;
- pre-flight + thermal watchdog активны всегда; пользователь видит
  сводный отчёт PASS/FAIL после завершения.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from rich.table import Table

from apexcore.application.diagnostics_sensors import diagnose_sensors
from apexcore.application.parallel_runner import (
    EngineSpec,
    ParallelStressResult,
    ParallelStressRunner,
)
from apexcore.application.stress_orchestrator import (
    StressFinalReport,
    StressOrchestrator,
    compute_stress_verdict,
)
from apexcore.application.stress_score import compute_stress_score_context
from apexcore.application.telemetry_service import (
    InMemoryMetricsBus,
    TelemetryService,
)
from apexcore.application.thermal import compute_thermal_stability
from apexcore.application.thermal_watchdog import (
    ThermalWatchdog,
    _is_cpu_temp_key,
)
from apexcore.domain.models import (
    BenchmarkConfig,
    BenchmarkResult,
    MetricSnapshot,
)
from apexcore.domain.ports import OSAdapter
from apexcore.infrastructure.adapters import AdapterFactory
from apexcore.infrastructure.stress.registry import (
    build_default_registry,
    category_user_hint,
    pick_cpu_stressor,
    pick_ram_stressor,
    pick_stability_engines_by_role,
)
from apexcore.interfaces.cli.menu.cancel import cancellable
from apexcore.interfaces.cli.menu.nav import _confirm
from apexcore.interfaces.cli.render import (
    console,
    render_engine_availability_table,
    render_safety_report,
    render_stress_final_report,
)
from apexcore.interfaces.cli.sparkline import sparkline

logger = logging.getLogger(__name__)

# Условный «потолок» для бесконечной нагрузки: 24 часа. Реально она
# завершится по Ctrl+C, по watchdog или по ошибке движка.
_INFINITE_DURATION_SEC = 24 * 3600.0

# Безопасный thermal-лимит для NVIDIA GPU потребительского класса (slowdown
# обычно начинается на 88–93°C). Используется только для отображения
# «лимит не превышен» в финальном отчёте.
_GPU_THERMAL_LIMIT_C = 88.0


# ─────────────── Одноразовое отключение всех защит ───────────────────────────
#
# Module-level флаг: пользователь явно через пункт меню «Термальная защита»
# отключил все защиты на следующий прогон. После одного прогона
# ``consume_safety_disabled()`` атомарно сбрасывает его в False.
#
# Не персистится в YAML — намеренно: «забыл что отключил» опасный паттерн.
# После перезапуска приложения защита всегда включена (по умолчанию).
_DISABLE_ALL_SAFETY_NEXT_RUN: bool = False


def is_safety_disabled_next_run() -> bool:
    """Проверить, отключены ли все защиты на следующий прогон."""
    return _DISABLE_ALL_SAFETY_NEXT_RUN


def toggle_safety_disabled_next_run() -> bool:
    """Переключить флаг и вернуть новое состояние."""
    global _DISABLE_ALL_SAFETY_NEXT_RUN
    _DISABLE_ALL_SAFETY_NEXT_RUN = not _DISABLE_ALL_SAFETY_NEXT_RUN
    return _DISABLE_ALL_SAFETY_NEXT_RUN


def consume_safety_disabled() -> bool:
    """Прочитать флаг и атомарно сбросить (одноразовость).

    GIL гарантирует атомарность чтения-записи bool. Все вызовы из
    main-thread меню — гонок не возникает.
    """
    global _DISABLE_ALL_SAFETY_NEXT_RUN
    cur = _DISABLE_ALL_SAFETY_NEXT_RUN
    _DISABLE_ALL_SAFETY_NEXT_RUN = False
    return cur


def _print_safety_disabled_banner() -> None:
    """Большой красный баннер перед прогоном с отключёнными защитами."""
    from rich.panel import Panel

    banner = (
        "[bold red]⚠ ВНИМАНИЕ: ВСЕ ЗАЩИТЫ ОТКЛЮЧЕНЫ ⚠[/]\n\n"
        "На этот прогон отключены:\n"
        "  • Thermal watchdog (авто-стоп при перегреве CPU)\n"
        "  • Cooling-sanity (детектор слабого охлаждения)\n"
        "  • Pre-flight: батарея / виртуализация / свободная RAM\n"
        "  • Лимит температуры GPU\n\n"
        "Следите за температурами вручную.\n"
        "После прогона защиты включатся автоматически."
    )
    console.print()
    console.print(Panel(banner, border_style="red", expand=False))
    console.print()


def _detect_cpu_temp_source(_: Any) -> tuple[bool, str, list[str]]:
    """Определить, доступна ли реальная CPU-температура и собрать совет.

    Делегируется в ``application.diagnostics_sensors.diagnose_sensors``,
    чтобы pre-flight-сообщение совпадало по смыслу с ``apexcore doctor``
    и сразу показывало пользователю actionable-совет (например, «запустите
    от админа один раз для регистрации WinRing0»). См. issue #20.

    Возвращает кортеж ``(has_cpu, message, advice)``:
    - ``has_cpu`` — есть ли реальный CPU-сенсор;
    - ``message`` — короткая подпись источника или причины отсутствия;
    - ``advice`` — список actionable-инструкций (пустой при наличии CPU).

    Параметр ``adapter`` не используется (раньше выгребали один снимок;
    теперь для целостности отчёта опираемся на полную диагностику), но
    сохранён в сигнатуре, чтобы не править все места вызова.
    """
    try:
        diag = diagnose_sensors()
    except Exception:
        # Graceful degrade: диагностика не должна ронять старт стресса.
        return False, "диагностика датчиков не запустилась", []
    if diag.has_cpu_temperature:
        return True, diag.cpu_temp_source or "источник определён", []
    return False, "реальная температура CPU недоступна", list(diag.advice)


# ─────────────────────────── GPU-поллер (NVIDIA) ────────────────────────────


def _poll_nvidia_smi() -> dict[str, Any] | None:
    """Опросить первую NVIDIA-GPU через ``nvidia-smi``.

    Возвращает словарь ``{name, temp_c, load_pct, mem_used_gb, mem_total_gb}``
    или None если ``nvidia-smi`` не найден / не отвечает / нет GPU.
    """
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "temp_c": float(parts[1]),
            "load_pct": float(parts[2]),
            "mem_used_gb": float(parts[3]) / 1024.0,
            "mem_total_gb": float(parts[4]) / 1024.0,
        }
    except ValueError:
        return None


class GpuPoller:
    """Фоновый поллер NVIDIA-GPU метрик через ``nvidia-smi`` раз в 2 секунды.

    Без GPU / nvidia-smi — все методы-аксессоры возвращают None, тред не
    запускается. Это позволяет писать клиентский код единообразно: всегда
    создаём поллер, проверяем ``available`` или ``latest()``.

    Хранит пиковые значения и накапливает суммы/счётчики для среднего.
    """

    POLL_INTERVAL_SEC = 2.0

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._peak_temp: float | None = None
        self._peak_load: float | None = None
        self._peak_mem: float | None = None
        self._mem_total: float | None = None
        self._name: str | None = None
        self._available = False
        self._temp_sum = 0.0
        self._temp_count = 0
        self._load_sum = 0.0
        self._load_count = 0

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        first = _poll_nvidia_smi()
        if first is None:
            return  # нет GPU / nvidia-smi — поллер не запускаем
        self._available = True
        with self._lock:
            self._absorb(first)
            self._name = first["name"]
            self._mem_total = first["mem_total_gb"]
        self._thread = threading.Thread(
            target=self._loop, name="apexcore-gpu-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def peaks(self) -> dict[str, float | str | None]:
        with self._lock:
            avg_temp = (
                self._temp_sum / self._temp_count if self._temp_count else None
            )
            avg_load = (
                self._load_sum / self._load_count if self._load_count else None
            )
            return {
                "name": self._name,
                "peak_temp_c": self._peak_temp,
                "peak_load_pct": self._peak_load,
                "peak_mem_gb": self._peak_mem,
                "mem_total_gb": self._mem_total,
                "avg_temp_c": avg_temp,
                "avg_load_pct": avg_load,
            }

    def _absorb(self, data: dict[str, Any]) -> None:
        """Обновить latest/peaks/sum по новой выборке. Под lock."""
        self._latest = data
        if self._peak_temp is None or data["temp_c"] > self._peak_temp:
            self._peak_temp = data["temp_c"]
        if self._peak_load is None or data["load_pct"] > self._peak_load:
            self._peak_load = data["load_pct"]
        if self._peak_mem is None or data["mem_used_gb"] > self._peak_mem:
            self._peak_mem = data["mem_used_gb"]
        self._temp_sum += float(data["temp_c"])
        self._temp_count += 1
        self._load_sum += float(data["load_pct"])
        self._load_count += 1

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL_INTERVAL_SEC):
            data = _poll_nvidia_smi()
            if data is None:
                continue
            with self._lock:
                self._absorb(data)


_VOLTAGE_NAME_TOKENS = ("core", "vcore", "vdd", "vid")

# CPU/GPU Vcore — узкий диапазон. Без этого фильтра в voltages могут
# попасть «непредусмотренные» рейлы: windows-адаптер пишет ключи
# ``cpu_power/*`` (ватты!) в тот же словарь как kludge до M4/SensorSnapshot
# (см. memory project_lhm_dll_flaky_test.md). 176 В от cpu_power/package
# тогда показывался как Vcore.
_CPU_GPU_VCORE_RANGE = (0.5, 2.5)

# Префиксы ключей в voltages-словаре, относящиеся к РЕАЛЬНЫМ напряжениям,
# а не к ваттам/токам. Сейчас единственное явное исключение —
# ``cpu_power/*`` (LHM CPU power-метрики, размерность Вт).
_POWER_NOT_VOLTAGE_HEADS = ("cpu_power",)

# DRAM-напряжение часто публикуется LHM не под HardwareType=Memory, а через
# Super I/O чип материнки (Nuvoton / ITE / Fintek) — там оно сидит на одном
# из voltage rails. Поэтому при prefix="memory" расширяем поиск на источники
# motherboard/superio и фильтруем по именным токенам DIMM/DRAM + диапазону
# (DDR4/DDR5: 0.8–2.0 В), чтобы не поймать рейлы 3.3 В / 5 В / 12 В.
_MEMORY_VOLTAGE_SOURCES = ("memory", "motherboard", "superio")
_MEMORY_VOLTAGE_TOKENS = ("dimm", "dram", "vddq", "vdimm", "memory", "vdd")
_MEMORY_VOLTAGE_RANGE = (0.8, 2.0)


def _extract_voltage(voltages: dict[str, float], prefix: str) -> float | None:
    """Найти представительное напряжение в ``snap.voltages`` по префиксу.

    LHM публикует сенсоры с ключами ``<hardware>/<normalized_name>``:
    ``cpu/cpu_core``, ``cpu/cpu_soc``, ``gpunvidia/gpu_core``,
    ``memory/dimm_vdd``, ``superio/dimm_vdd`` и т.д. Фильтруем по
    ``prefix`` (``cpu``, ``gpu``, ``memory``), затем по токенам имени,
    чтобы случайно не попасть в мейнборд-рейлы (+12 В, +5 В, +3.3 В).
    Берём максимум — это стабильно «настоящий» Vcore, а не VRM-телеметрия.

    Особый случай ``prefix="memory"``: DRAM-напряжение часто отсутствует
    в узле ``memory/...`` и сидит в ``superio/...`` / ``motherboard/...``.
    Поэтому источники расширены, плюс применяется диапазон 0.8–2.0 В.

    Возвращает ``None`` если ничего подходящего не нашлось — выше по стеку
    это рендерится как ``"—"``.
    """
    if not voltages:
        return None
    candidates: list[float] = []
    if prefix == "memory":
        v_min, v_max = _MEMORY_VOLTAGE_RANGE
        for key, value in voltages.items():
            head, sep, tail = key.partition("/")
            if not sep or head not in _MEMORY_VOLTAGE_SOURCES:
                continue
            tail_l = tail.lower()
            if not any(token in tail_l for token in _MEMORY_VOLTAGE_TOKENS):
                continue
            if not (v_min <= value <= v_max):
                continue
            candidates.append(value)
        return max(candidates) if candidates else None

    v_min, v_max = _CPU_GPU_VCORE_RANGE
    for key, value in voltages.items():
        head, sep, tail = key.partition("/")
        if not sep or not head.startswith(prefix):
            continue
        # Кludge: power-метрики LHM (cpu_power/cores, cpu_power/package, …)
        # пишутся в тот же словарь voltages — отфильтровываем по head, а не
        # по диапазону, чтобы случайные пограничные значения (например,
        # пакет на idle ~1.5 Вт) не проскальзывали как «вольтаж».
        if head in _POWER_NOT_VOLTAGE_HEADS:
            continue
        tail_l = tail.lower()
        if not any(token in tail_l for token in _VOLTAGE_NAME_TOKENS):
            continue
        # Реальный Vcore CPU/GPU всегда в 0.5–2.5 В — всё остальное отбрасываем.
        if not (v_min <= value <= v_max):
            continue
        candidates.append(value)
    return max(candidates) if candidates else None


def _extract_power_w(voltages: dict[str, float], component: str) -> float | None:
    """Извлечь power-метрику компонента из ``snap.voltages``.

    LHM CPU power-метрики (`cpu_power/cores`, `cpu_power/memory`,
    `cpu_power/package`, `cpu_power/platform`) пишутся в общий
    voltages-dict как kludge до M4/SensorSnapshot. Здесь мы вытягиваем
    их обратно по соглашению: префикс ``<component>_power`` (например,
    ``cpu_power`` для CPU). Берём максимум — это «package» как самый
    представительный показатель полного энергопотребления.

    Возвращает ``None`` если ни один ``cpu_power/*`` ключ не найден.
    """
    if not voltages:
        return None
    head_target = f"{component}_power"
    candidates: list[float] = [
        value
        for key, value in voltages.items()
        if key.partition("/")[0] == head_target and value > 0
    ]
    return max(candidates) if candidates else None


def _extract_gpu_power_w(voltages: dict[str, float]) -> float | None:
    """Извлечь GPU power из ``snap.voltages``.

    Источники, в порядке приоритета:
    - NVML: ключи вида ``nvml/<N>/power_w`` (см.
      ``infrastructure/sensors/nvidia_ml.read_nvml_power``). Аддативность по
      GPU не нужна — стресс не нагружает GPU намеренно, берём максимум для
      «больше — точнее» (типично один GPU).
    - LHM: ключи ``gpu_power/*`` если когда-нибудь появятся (сейчас LHM
      power публикует через cpu_power/* по kludge’у).

    Возвращает ``None`` если ни одного ключа не найдено.
    """
    if not voltages:
        return None
    candidates: list[float] = []
    for key, value in voltages.items():
        if value is None or value <= 0:
            continue
        kl = key.lower()
        is_nvml = kl.startswith("nvml/") and kl.endswith("/power_w")
        is_lhm_gpu = key.partition("/")[0] == "gpu_power"
        if is_nvml or is_lhm_gpu:
            candidates.append(float(value))
    return max(candidates) if candidates else None


def _summarize_metrics_history(
    history: list[MetricSnapshot],
) -> dict[str, float | None]:
    """Подсчитать ср./пик нагрузку CPU% / RAM% / Vcore по сенсорам прогона.

    Используется для финального отчёта: одно место расчёта, не размазываем
    по нескольким местам в коде. Helper :func:`_extract_voltage` тянет
    Vcore из ``snap.voltages`` (LHM, при доступности WinRing0).
    """
    import statistics

    if not history:
        return {
            "cpu_load_avg": None,
            "cpu_load_peak": None,
            "ram_load_avg": None,
            "ram_load_peak": None,
            "cpu_temp_avg": None,
            "cpu_temp_peak": None,
            "cpu_vcore_avg": None,
            "cpu_vcore_peak": None,
            "gpu_vcore_avg": None,
            "gpu_vcore_peak": None,
            "ram_vcore_avg": None,
            "ram_vcore_peak": None,
            "cpu_power_avg_w": None,
            "cpu_power_peak_w": None,
            "gpu_power_avg_w": None,
            "gpu_power_peak_w": None,
            "ram_temp_avg": None,
            "ram_temp_peak": None,
        }
    cpu_pcts = [s.cpu_percent for s in history if s.cpu_percent is not None]
    ram_pcts = [s.ram_percent for s in history if s.ram_percent is not None]
    cpu_temps_max: list[float] = []
    cpu_vcores: list[float] = []
    gpu_vcores: list[float] = []
    ram_vcores: list[float] = []
    cpu_powers: list[float] = []
    gpu_powers: list[float] = []
    ram_temps: list[float] = []
    for s in history:
        cpu_t = [v for k, v in s.temperatures.items() if _is_cpu_temp_key(k)]
        if cpu_t:
            cpu_temps_max.append(max(cpu_t))
        cv = _extract_voltage(s.voltages, "cpu")
        if cv is not None:
            cpu_vcores.append(cv)
        gv = _extract_voltage(s.voltages, "gpu")
        if gv is not None:
            gpu_vcores.append(gv)
        rv = _extract_voltage(s.voltages, "memory")
        if rv is not None:
            ram_vcores.append(rv)
        pw = _extract_power_w(s.voltages, "cpu")
        if pw is not None:
            cpu_powers.append(pw)
        gpw = _extract_gpu_power_w(s.voltages)
        if gpw is not None:
            gpu_powers.append(gpw)
        rt = _collect_ram_temp(s)
        if rt is not None:
            ram_temps.append(rt)
    return {
        "cpu_load_avg": statistics.fmean(cpu_pcts) if cpu_pcts else None,
        "cpu_load_peak": max(cpu_pcts) if cpu_pcts else None,
        "ram_load_avg": statistics.fmean(ram_pcts) if ram_pcts else None,
        "ram_load_peak": max(ram_pcts) if ram_pcts else None,
        "cpu_temp_avg": (
            statistics.fmean(cpu_temps_max) if cpu_temps_max else None
        ),
        "cpu_temp_peak": max(cpu_temps_max) if cpu_temps_max else None,
        "cpu_vcore_avg": statistics.fmean(cpu_vcores) if cpu_vcores else None,
        "cpu_vcore_peak": max(cpu_vcores) if cpu_vcores else None,
        "gpu_vcore_avg": statistics.fmean(gpu_vcores) if gpu_vcores else None,
        "gpu_vcore_peak": max(gpu_vcores) if gpu_vcores else None,
        "ram_vcore_avg": statistics.fmean(ram_vcores) if ram_vcores else None,
        "ram_vcore_peak": max(ram_vcores) if ram_vcores else None,
        "cpu_power_avg_w": (
            statistics.fmean(cpu_powers) if cpu_powers else None
        ),
        "cpu_power_peak_w": max(cpu_powers) if cpu_powers else None,
        "gpu_power_avg_w": (
            statistics.fmean(gpu_powers) if gpu_powers else None
        ),
        "gpu_power_peak_w": max(gpu_powers) if gpu_powers else None,
        "ram_temp_avg": (
            statistics.fmean(ram_temps) if ram_temps else None
        ),
        "ram_temp_peak": max(ram_temps) if ram_temps else None,
    }


# ─────────────────────────── Public entrypoints ─────────────────────────────


def run_timed_stress(minutes: float) -> None:
    """Стресс-нагрузка на N минут с pre-flight, watchdog и итоговым отчётом."""
    duration_sec = max(1.0, float(minutes) * 60.0)
    _execute_stress_session(
        duration_sec=duration_sec,
        infinite=False,
        profile_name="timed_stress",
        title=f"Стресс-тест на {minutes:.0f} мин",
    )


def run_infinite_stress() -> None:
    """Бесконечная стресс-нагрузка до Ctrl+C / watchdog."""
    _execute_stress_session(
        duration_sec=_INFINITE_DURATION_SEC,
        infinite=True,
        profile_name="infinite_stress",
        title="Бесконечная стресс-нагрузка (для оценки стабильности)",
    )


def show_engines_table() -> None:
    """Справочник: какие движки доступны на этой ОС."""
    console.clear()
    registry = build_default_registry()
    engines_by_role = pick_stability_engines_by_role(registry)
    console.rule("[bold]Доступные движки нагрузки[/]")
    render_engine_availability_table(engines_by_role)


# ─────────────────────────── Внутренняя логика ──────────────────────────────


def _execute_stress_session(
    *,
    duration_sec: float,
    infinite: bool,
    profile_name: str,
    title: str,
) -> None:
    """Общая логика для timed/infinite режима.

    1. Очистка экрана и шапка.
    2. Подбор CPU/RAM стрессоров по платформе.
    3. SafetyGate pre-flight + подтверждение.
    4. ParallelStressRunner в отдельном потоке + периодический текстовый
       статус в main-потоке (без Live).
    5. Сборка StressFinalReport, рендер вердикта, сохранение в БД.
    """
    console.clear()
    console.rule(f"[bold cyan]{title}[/]")

    # Одноразовое отключение всех защит — сразу читаем и сбрасываем флаг.
    # Если был установлен через пункт меню «Термальная защита: ✗ ВЫКЛ» —
    # печатаем красный баннер, watchdog/SafetyGate/cooling-sanity/GPU-limit
    # не активируются дальше по коду.
    safety_disabled = consume_safety_disabled()
    if safety_disabled:
        _print_safety_disabled_banner()

    adapter = AdapterFactory.detect()
    registry = build_default_registry()

    try:
        _cpu_alias, cpu_engine = pick_cpu_stressor(registry)
        _ram_alias, ram_engine = pick_ram_stressor(registry)
    except RuntimeError as exc:
        console.print(f"[red]Не удалось подобрать движки: {exc}[/]")
        return

    # DGEMM параллелится ВНУТРИ через BLAS (OpenBLAS использует все ядра),
    # поэтому python-поток ему нужен один: иначе N python-потоков × np.matmul
    # плодят N буферов C (~128 МБ) и оверсабскрайбят BLAS → OOM на машинах с
    # малым ОЗУ. STREAM — memory-bound, держим немного потоков (~logical/4).
    _logical = os.cpu_count() or 4
    plan = [
        EngineSpec(engine=cpu_engine, threads=1, label="CPU"),
        EngineSpec(engine=ram_engine, threads=max(2, _logical // 4), label="RAM"),
    ]

    # Пользователю не нужны технические имена движков («Стресс ЦП
    # (большой DGEMM)»), важно только что именно нагружается. Описание
    # категории из ``category_user_hint`` уже даёт человеко-понятную фразу.
    console.print(
        f"[bold]Нагрузка ЦП:[/] {category_user_hint(cpu_engine.category)}"
    )
    console.print(
        f"[bold]Нагрузка ОЗУ:[/] {category_user_hint(ram_engine.category)}"
    )
    if infinite:
        console.print(
            "\n[bold yellow]Прогон без ограничения по времени.[/] "
            "Останавливается по [bold]Ctrl+C[/] или термальным watchdog."
        )
    else:
        console.print(
            f"\n[bold]Длительность:[/] {duration_sec / 60:.1f} мин "
            f"(~{duration_sec:.0f} с). "
            "Можно остановить досрочно через [bold]Ctrl+C[/]."
        )

    # Диагностика источника CPU-температуры — пользователь должен сразу
    # видеть, будет ли в строке статуса реальная температура CPU. При
    # отсутствии источника печатаем actionable-совет от diagnose_sensors
    # (например, «запустите apexcore от администратора один раз»),
    # вместо прежнего безличного «не считывается». См. issue #20.
    cpu_temp_ok, cpu_temp_message, cpu_temp_advice = _detect_cpu_temp_source(adapter)
    # Если сенсоры доступны — ничего не выводим, нет смысла шуметь зелёной
    # галочкой. Привлекаем внимание только когда CPU-температура НЕ
    # считывается — это нештатная ситуация, которая снизит ценность отчёта.
    if not cpu_temp_ok:
        console.print(
            f"[bold]Температура CPU:[/]  [yellow]✗ {cpu_temp_message}[/]"
        )
        for advice_line in cpu_temp_advice:
            console.print(f"  [dim]→ {advice_line}[/]")
        console.print("  [dim]Подробнее: [bold]apexcore doctor[/][/]")
        console.print()

    # Pre-flight через SafetyGate. Пропускаем целиком если пользователь
    # отключил все защиты — это и есть ожидаемое поведение «голого» прогона.
    if safety_disabled:
        from apexcore.application.safety_gate import SafetyReport

        pre_flight = SafetyReport()  # пустой отчёт без проверок
    else:
        orchestrator = StressOrchestrator(adapter)
        pre_flight = orchestrator.check_pre_flight()
        render_safety_report(pre_flight)

        # Дополнительное подтверждение запрашиваем ТОЛЬКО если SafetyGate
        # заблокировал запуск (батарея, виртуализация, мало RAM). В обычном
        # сценарии пользователь уже сделал явный выбор пунктом меню —
        # повторно спрашивать «уверены?» не нужно.
        if pre_flight.blocked:
            console.print()
            console.print(
                "[bold red]Запуск заблокирован защитной проверкой.[/] "
                "См. причины выше."
            )
            if not _confirm("Запустить принудительно? (y/n)"):
                return

    # Прогон.
    bus = InMemoryMetricsBus()
    telemetry = TelemetryService(adapter, bus, sampling_rate_sec=0.5)
    telemetry.start(record_history=True)
    gpu_poller = GpuPoller()
    gpu_poller.start()

    started_dt = datetime.now(timezone.utc)
    parallel: ParallelStressResult | None = None
    watchdog: ThermalWatchdog | None = None

    console.print()
    console.print("[bold cyan]Старт нагрузки.[/]")
    if safety_disabled:
        console.print(
            "[bold red]Все защитные механизмы отключены.[/] "
            "Watchdog не остановит нагрузку при перегреве — следите вручную."
        )
    else:
        console.print(
            "[dim]При приближении к температурному лимиту CPU на 5 °C "
            "нагрузка будет остановлена автоматически.[/]"
        )
    if gpu_poller.available:
        gpu_peaks = gpu_poller.peaks()
        gpu_name = gpu_peaks.get("name") or "GPU"
        console.print(f"[dim]Видеокарта: {gpu_name} — телеметрия активна.[/]")
    console.print()

    try:
        with cancellable() as token:
            if safety_disabled:
                # Watchdog НЕ создаётся: пользователь явно отключил все защиты.
                watchdog = None
            else:
                watchdog = ThermalWatchdog(
                    bus=bus,
                    cancel_token=token,
                    on_trigger=lambda t: console.print(
                        f"\n[bold red]⚠ {t.message}[/]"
                    ),
                )
                watchdog.start()
                console.print(
                    f"[dim]Температурный лимит CPU: {watchdog.tjmax:.0f} °C "
                    f"(порог остановки: {watchdog.threshold:.0f} °C)[/]"
                )

            runner = ParallelStressRunner()
            holder: dict[str, Any] = {"result": None, "error": None}

            def _runner_thread() -> None:
                try:
                    holder["result"] = runner.run(
                        plan=plan,
                        duration_sec=duration_sec,
                        cancel_token=token,
                    )
                except Exception as e:  # pragma: no cover — defensive
                    holder["error"] = str(e)
                    logger.exception("Стресс-прогон упал")

            t = threading.Thread(
                target=_runner_thread, name="apexcore-stress-runner", daemon=True
            )
            t.start()

            # Live-цикл: прогресс-бар + сгруппированная таблица CPU/RAM/GPU
            # с sparkline-трендами. Обновляется 2 раза в секунду.
            _run_live_status(
                bus=bus,
                adapter=adapter,
                runner_thread=t,
                deadline_sec=duration_sec,
                cancel_token=token,
                infinite=infinite,
                gpu_poller=gpu_poller,
            )

            # Дожидаемся, пока runner не завершит чистку.
            t.join(timeout=duration_sec + 60.0)
            parallel = holder.get("result")
            if holder.get("error"):
                console.print(f"[red]Ошибка в прогоне: {holder['error']}[/]")
    finally:
        if watchdog is not None:
            watchdog.stop()
        gpu_poller.stop()
        history = telemetry.stop()

    finished_dt = datetime.now(timezone.utc)

    if parallel is None:
        console.print("[red]Прогон не дал результата.[/]")
        return

    thermal = compute_thermal_stability(history)
    wd_triggered = bool(watchdog and watchdog.triggered)
    wd_tjmax = watchdog.tjmax if watchdog else 100.0
    verdict = compute_stress_verdict(
        parallel=parallel,
        thermal=thermal,
        watchdog_triggered=wd_triggered,
        watchdog_tjmax_c=wd_tjmax,
    )

    gpu_peaks = gpu_poller.peaks() if gpu_poller.available else {}
    metrics_summary = _summarize_metrics_history(history)
    system_info = adapter.get_system_info()
    stress_ctx = compute_stress_score_context(
        system_info=system_info,
        parallel=parallel,
        thermal=thermal,
        duration_sec=parallel.duration_actual_sec,
    )
    report = StressFinalReport(
        profile_name=profile_name,
        started_at=started_dt,
        finished_at=finished_dt,
        duration_actual_sec=parallel.duration_actual_sec,
        requested_duration_sec=duration_sec,
        safety=pre_flight,
        parallel=parallel,
        thermal=thermal,
        watchdog_triggered=wd_triggered,
        watchdog_trigger=watchdog.trigger if watchdog else None,
        watchdog_tjmax_c=wd_tjmax,
        watchdog_tjmax_source=watchdog.tjmax_source if watchdog else "disabled",
        verdict=verdict,
        system_info=system_info,
        metrics_history=history,
        cpu_avg_load_pct=metrics_summary["cpu_load_avg"],
        cpu_peak_load_pct=metrics_summary["cpu_load_peak"],
        cpu_avg_temp_c=metrics_summary["cpu_temp_avg"],
        cpu_peak_temp_c=metrics_summary["cpu_temp_peak"],
        cpu_thermal_limit_c=wd_tjmax,
        ram_avg_load_pct=metrics_summary["ram_load_avg"],
        ram_peak_load_pct=metrics_summary["ram_load_peak"],
        gpu_avg_temp_c=gpu_peaks.get("avg_temp_c"),
        gpu_peak_temp_c=gpu_peaks.get("peak_temp_c"),
        gpu_avg_load_pct=gpu_peaks.get("avg_load_pct"),
        gpu_peak_load_pct=gpu_peaks.get("peak_load_pct"),
        gpu_peak_mem_gb=gpu_peaks.get("peak_mem_gb"),
        gpu_mem_total_gb=gpu_peaks.get("mem_total_gb"),
        gpu_thermal_limit_c=(
            _GPU_THERMAL_LIMIT_C
            if (gpu_poller.available and not safety_disabled)
            else None
        ),
        gpu_name=gpu_peaks.get("name"),
        cpu_avg_vcore_v=metrics_summary["cpu_vcore_avg"],
        cpu_peak_vcore_v=metrics_summary["cpu_vcore_peak"],
        gpu_avg_vcore_v=metrics_summary["gpu_vcore_avg"],
        gpu_peak_vcore_v=metrics_summary["gpu_vcore_peak"],
        ram_avg_vcore_v=metrics_summary["ram_vcore_avg"],
        ram_peak_vcore_v=metrics_summary["ram_vcore_peak"],
        ram_avg_temp_c=metrics_summary["ram_temp_avg"],
        ram_peak_temp_c=metrics_summary["ram_temp_peak"],
        cpu_avg_power_w=metrics_summary["cpu_power_avg_w"],
        cpu_peak_power_w=metrics_summary["cpu_power_peak_w"],
        gpu_avg_power_w=metrics_summary["gpu_power_avg_w"],
        gpu_peak_power_w=metrics_summary["gpu_power_peak_w"],
        cpu_temp_source_ok=cpu_temp_ok,
        cpu_temp_source_message=cpu_temp_message,
        cpu_temp_source_advice=list(cpu_temp_advice or []),
        gpu_was_stressed=False,
        stress_score=stress_ctx.stress_score,
        stress_r_dgemm=stress_ctx.r_dgemm,
        stress_r_stream=stress_ctx.r_stream,
        stress_r_stability=stress_ctx.r_stability,
        stress_r_thermal=stress_ctx.r_thermal,
        stress_t_max_c=stress_ctx.t_max_c,
        stress_tjmax_c=stress_ctx.tjmax_c,
        stress_duration_sec=stress_ctx.duration_sec,
        roofline_dgemm_peak_gflops=stress_ctx.dgemm_peak_gflops,
        roofline_stream_peak_gb_s=stress_ctx.stream_peak_gb_s,
        roofline_simd_level=stress_ctx.simd_level,
        roofline_clock_ghz=stress_ctx.clock_ghz,
        roofline_dram_mts=stress_ctx.dram_mts,
        roofline_dram_modules=stress_ctx.dram_modules,
    )

    console.print()
    render_stress_final_report(report)

    try:
        _persist_report(report)
    except Exception:
        logger.exception("Не удалось сохранить стресс-отчёт")
        console.print("[yellow]Предупреждение: запись в БД не удалась.[/]")


def _collect_cpu_temp(snap: MetricSnapshot | None) -> float | None:
    """Достать максимум по сенсорам, попадающим под фильтр CPU-температуры."""
    if snap is None:
        return None
    temps = [v for k, v in snap.temperatures.items() if _is_cpu_temp_key(k)]
    return max(temps) if temps else None


# DIMM-сенсоры LHM публикует как `memory/dimm_<N>` или
# `motherboard/temperature_dimm_<N>`. Берём максимум по всем валидным
# в диапазоне 20–110 °C (отбрасываем артефакты сенсоров и
# непропатченные нули). См. `application/sensor_keys.py:_DIMM_RE`.
_RAM_TEMP_TOKENS = ("dimm",)
_RAM_TEMP_RANGE = (20.0, 110.0)


def _collect_ram_temp(snap: MetricSnapshot | None) -> float | None:
    """Максимум температуры по сенсорам DIMM (1–4) из ``snap.temperatures``.

    LHM публикует температуру модулей памяти под ключами с подстрокой
    ``dimm`` (например, ``memory/dimm_1``, ``motherboard/temperature_dimm_2``).
    Возвращаем максимум — самый горячий модуль и есть «температура RAM»
    под нагрузкой; именно его watchdog должен был бы отслеживать.
    Фильтр 20–110 °C отбрасывает явные артефакты (0, отрицательные,
    >150 °C — это явно не температура DIMM).
    """
    if snap is None:
        return None
    vmin, vmax = _RAM_TEMP_RANGE
    candidates: list[float] = []
    for key, value in snap.temperatures.items():
        key_l = key.lower()
        if not any(token in key_l for token in _RAM_TEMP_TOKENS):
            continue
        if not (vmin <= value <= vmax):
            continue
        candidates.append(value)
    return max(candidates) if candidates else None


def _build_stress_live_table(
    *,
    snap: MetricSnapshot | None,
    gpu_data: dict[str, Any] | None,
    cpu_temp_history: list[float],
    cpu_load_history: list[float],
    ram_load_history: list[float],
    gpu_temp_history: list[float],
    gpu_load_history: list[float],
    gpu_available: bool = False,
) -> Table:
    """Сгруппированная таблица CPU/RAM/GPU с sparkline-трендами.

    Pure-функция: не печатает в консоль, не зависит от внешнего state.
    Облегчает тестирование без Rich Live и без запуска нагрузки.

    Если ``gpu_available=True``, GPU/VRAM-строки выводятся всегда (даже
    с «—», когда ``gpu_data=None`` — например, в первые секунды до
    первого опроса nvidia-smi). Без этого Rich Live получает renderable
    разной высоты на разных кадрах, и в conhost остаются строки-призраки
    предыдущих перерисовок («Стресс-нагрузка» дублируется в шапке).
    """
    tbl = Table.grid(padding=(0, 1))
    tbl.add_column("Компонент", style="bold cyan", no_wrap=True)
    tbl.add_column("Нагрузка", justify="right")
    tbl.add_column("Температура", justify="right")
    tbl.add_column("Vcore", justify="right")
    tbl.add_column("Тренд", justify="left")

    cpu_load_str = f"{snap.cpu_percent:5.1f} %" if snap else "—"
    cpu_temp = _collect_cpu_temp(snap)
    cpu_temp_str = f"{cpu_temp:5.1f} °C" if cpu_temp is not None else "—"
    cpu_vcore = _extract_voltage(snap.voltages, "cpu") if snap else None
    cpu_vcore_str = f"{cpu_vcore:.3f} В" if cpu_vcore is not None else "—"
    cpu_spark_src = cpu_temp_history if cpu_temp_history else cpu_load_history
    # Unicode block-elements (▁▂▃▄▅▆▇█) — пользователь явно подтвердил, что
    # «закрашенные прямоугольники лучше выглядят», даже если в его шрифте
    # они частично заменяются на ▯. См. memory feedback_powershell_font.md.
    cpu_spark = sparkline(cpu_spark_src, width=12) if cpu_spark_src else " " * 12
    tbl.add_row(
        "CPU", cpu_load_str, cpu_temp_str, cpu_vcore_str, f"[cyan]{cpu_spark}[/]"
    )

    ram_load_str = f"{snap.ram_percent:5.1f} %" if snap else "—"
    ram_temp = _collect_ram_temp(snap)
    ram_temp_str = f"{ram_temp:5.1f} °C" if ram_temp is not None else "—"
    ram_vcore = _extract_voltage(snap.voltages, "memory") if snap else None
    ram_vcore_str = f"{ram_vcore:.3f} В" if ram_vcore is not None else "—"
    ram_spark = sparkline(ram_load_history, width=12) if ram_load_history else " " * 12
    tbl.add_row(
        "RAM", ram_load_str, ram_temp_str, ram_vcore_str, f"[cyan]{ram_spark}[/]"
    )

    if gpu_available:
        # GPU/VRAM-строки выводим всегда (с «—» при отсутствии данных),
        # чтобы высота renderable оставалась константной между кадрами
        # Live — иначе старые строки оставались бы как «призраки» (баг
        # «Стресс-нагрузка дублируется» в шапке прогона в conhost).
        if gpu_data is not None:
            gpu_load_str = f"{gpu_data.get('load_pct', 0.0):5.1f} %"
            gpu_temp_str = f"{gpu_data.get('temp_c', 0.0):5.1f} °C"
            mem_used = gpu_data.get("mem_used_gb")
            mem_total = gpu_data.get("mem_total_gb")
            vram_str = (
                f"{mem_used:.1f}/{mem_total:.1f} ГБ"
                if mem_used is not None and mem_total
                else "—"
            )
        else:
            gpu_load_str = "—"
            gpu_temp_str = "—"
            vram_str = "—"
        gpu_vcore = (
            _extract_voltage(snap.voltages, "gpu") if snap is not None else None
        )
        gpu_vcore_str = f"{gpu_vcore:.3f} В" if gpu_vcore is not None else "—"
        gpu_spark_src = (
            gpu_temp_history if gpu_temp_history else gpu_load_history
        )
        gpu_spark = (
            sparkline(gpu_spark_src, width=12) if gpu_spark_src else " " * 12
        )
        tbl.add_row(
            "GPU [dim](фон)[/]",
            gpu_load_str,
            gpu_temp_str,
            gpu_vcore_str,
            f"[cyan]{gpu_spark}[/]",
        )
        # VRAM — это «занятость», нет смысла в колонках T° / Vcore /
        # тренд. Оставляем их пустыми, чтобы строка читалась как
        # «продолжение GPU», а не как полноценный сенсор без данных.
        tbl.add_row("VRAM", vram_str, "", "", "")
    return tbl


def _run_live_status(
    *,
    bus: InMemoryMetricsBus,
    adapter: OSAdapter,
    runner_thread: threading.Thread,
    deadline_sec: float,
    cancel_token: threading.Event,
    infinite: bool,
    gpu_poller: GpuPoller | None = None,
) -> None:
    """Live-цикл с прогресс-баром и сгруппированной таблицей CPU/RAM/GPU.

    Заменяет старый текстовый ``_status_loop`` (шапка-Panel + строка через
    ``·`` раз в 5 с). Теперь рисуем `rich.live.Live` с refresh 2 Гц.
    История метрик копится локально (deque-aware) и подаётся в sparkline —
    это и есть визуальный аналог «графика температуры» из AIDA64 в рамках
    CLI. Команда ``sensors`` использует тот же паттерн.
    """
    from rich.console import Group
    from rich.live import Live
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    last_snap_holder: dict[str, MetricSnapshot | None] = {"snap": None}

    def _on_snap(snap: MetricSnapshot) -> None:
        last_snap_holder["snap"] = snap

    unsubscribe = bus.subscribe(_on_snap)

    # Прогрев psutil ИМЕННО ЗДЕСЬ — в момент, когда стресс-engines уже
    # запущены (runner.run() стартовал выше) и CPU фактически нагружен.
    # ``psutil.cpu_percent(interval=None)`` усредняет CPU time между
    # последовательными вызовами. Этот холостой вызов фиксирует «начало
    # окна»; следующий snap от TelemetryService (через ~0.5 с) уже
    # сравнит CPU time с этим моментом → получит реальную нагрузку,
    # а не «idle с момента pre-flight».
    try:
        adapter.get_current_metrics()
    except Exception:
        logger.exception("Прогрев adapter перед Live упал")

    cpu_temp_history: list[float] = []
    cpu_load_history: list[float] = []
    ram_load_history: list[float] = []
    gpu_temp_history: list[float] = []
    gpu_load_history: list[float] = []
    _history_window = 30  # ~15 с при шаге 0.5 с — достаточно для тренда

    if infinite:
        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]Стресс-тест системы (без ограничения)"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        task_id = progress.add_task("running", total=None)
    else:
        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]Стресс-тест системы"),
            BarColumn(
                bar_width=None,
                complete_style="cyan",
                finished_style="bright_green",
            ),
            MofNCompleteColumn(),
            TextColumn("[dim]•[/]"),
            TimeElapsedColumn(),
            TextColumn("[dim]осталось[/]"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
        # Шкала в секундах — пользователь хочет видеть остаток в формате
        # MM:SS, а MofN покажет «X/Y с» для понимания «сколько прошло».
        task_id = progress.add_task("running", total=int(deadline_sec))

    started_mono = time.monotonic()

    gpu_available = bool(gpu_poller and gpu_poller.available)

    def _renderable() -> Group:
        snap = last_snap_holder["snap"]
        gpu_data = gpu_poller.latest() if gpu_poller else None
        return Group(
            progress,
            _build_stress_live_table(
                snap=snap,
                gpu_data=gpu_data,
                cpu_temp_history=cpu_temp_history,
                cpu_load_history=cpu_load_history,
                ram_load_history=ram_load_history,
                gpu_temp_history=gpu_temp_history,
                gpu_load_history=gpu_load_history,
                gpu_available=gpu_available,
            ),
        )

    try:
        with Live(_renderable(), console=console, refresh_per_second=2) as live:
            while runner_thread.is_alive():
                now = time.monotonic()
                elapsed = now - started_mono
                if cancel_token.is_set():
                    break
                if not infinite and elapsed >= deadline_sec:
                    break

                snap = last_snap_holder["snap"]
                if snap is not None:
                    cpu_load_history.append(snap.cpu_percent)
                    ram_load_history.append(snap.ram_percent)
                    temp = _collect_cpu_temp(snap)
                    if temp is not None:
                        cpu_temp_history.append(temp)
                gpu_latest = gpu_poller.latest() if gpu_poller else None
                if gpu_latest is not None:
                    if "load_pct" in gpu_latest:
                        gpu_load_history.append(float(gpu_latest["load_pct"]))
                    if "temp_c" in gpu_latest:
                        gpu_temp_history.append(float(gpu_latest["temp_c"]))

                # Урезаем историю до окна — sparkline всё равно возьмёт хвост.
                for hist in (
                    cpu_temp_history,
                    cpu_load_history,
                    ram_load_history,
                    gpu_temp_history,
                    gpu_load_history,
                ):
                    if len(hist) > _history_window:
                        del hist[:-_history_window]

                if not infinite:
                    progress.update(task_id, completed=min(int(elapsed), int(deadline_sec)))
                live.update(_renderable())
                # 0.5 с — шаг семплера телеметрии; чаще нет смысла.
                time.sleep(0.5)
    finally:
        with contextlib.suppress(Exception):
            unsubscribe()


def _fmt_time(sec: float) -> str:
    """``HH:MM:SS`` или ``MM:SS`` если меньше часа."""
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


def _persist_report(report: StressFinalReport) -> None:
    """Сохранить отчёт в существующую таблицу runs через payload_json."""
    from apexcore.infrastructure.persistence import SqliteResultRepository
    from apexcore.shared.config import load_settings

    settings = load_settings()
    repo = SqliteResultRepository(settings.db_path)

    stress_results = list(report.parallel.results)
    if stress_results:
        verdict_payload = {
            "passed": report.verdict.passed,
            "reason": report.verdict.reason,
            "sub_results": dict(report.verdict.sub_results),
            "watchdog_triggered": report.watchdog_triggered,
            "watchdog_tjmax_c": report.watchdog_tjmax_c,
            "watchdog_tjmax_source": report.watchdog_tjmax_source,
            "watchdog_trigger_message": (
                report.watchdog_trigger.message
                if report.watchdog_trigger is not None
                else None
            ),
            # `stress_score` намеренно НЕ кладётся в payload истории:
            # пользователь хочет в ленте «История прогонов» видеть «—» у
            # стресса до тех пор, пока валидность метода (1000·GM(DGEMM,
            # STREAM, stability)) не будет подтверждена отдельным анализом
            # (новое ТЗ в отдельном чате). Сама плашка стресс-балла в
            # финальном отчёте теста не трогается — она в render-слое
            # и относится к UX самого прогона, а не к истории.
        }
        first = stress_results[0]
        first_extra = dict(first.extra)
        first_extra["stress_verdict"] = verdict_payload
        stress_results[0] = first.model_copy(update={"extra": first_extra})

    bench = BenchmarkResult(
        system_info=report.system_info,
        config=BenchmarkConfig(
            profile_name=report.profile_name,
            duration_sec=report.requested_duration_sec,
        ),
        start_time=report.started_at,
        end_time=report.finished_at,
        metrics_history=report.metrics_history,
        stress_results=stress_results,
        final_score=0.0,
        status="completed" if not report.parallel.cancelled else "cancelled",
        thermal=report.thermal,
    )
    repo.save(bench)
    # UUID и путь к БД — техническая информация для аудита, не нужна
    # пользователю в TUI. Логируем на DEBUG-уровне: при дефолтной
    # конфигурации логгер не пишет это в консоль, но при отладке
    # (APEXCORE_LOG=DEBUG или похожем) можно посмотреть путь.
    logger.debug("stress run saved: db=%s uuid=%s", settings.db_path, bench.id)


__all__ = [
    "run_infinite_stress",
    "run_timed_stress",
    "show_engines_table",
]
