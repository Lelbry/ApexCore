"""Roofline-калькулятор: теоретические пики архитектуры для микробенчмарков.

Спецификация: ``new-app/docs/scoring_v2.md`` §4.

Идея: вместо физической reference-машины (как у SPEC, BAPCo, Geekbench)
эталоном выступает **архитектурный предел железа конкретной системы**.
Балл = (measured / roofline) — доля от теоретического максимума.

Источник методики: Williams S., Waterman A., Patterson D. (2009).
*Roofline: An Insightful Visual Performance Model for Multicore Architectures.*
Communications of the ACM 52(4):65–76. DOI: 10.1145/1498765.1498785.

Формулы для конкретных подтестов вынесены в отдельные функции для тестируемости.
Все функции возвращают ``None``, если данные недостаточны для расчёта — тогда
вызывающий код должен использовать empirical proxy (см. references.py).

Кросс-платформенность
---------------------
- Clock: получаем через ``psutil.cpu_freq().max`` — работает и в Windows, и в
  Astra Linux. Если psutil не возвращает max — фолбэк на эвристический клок
  по строке cpu_model (например `i7-12700K @ 5.0GHz` → 5.0 GHz turbo).
- SIMD-уровень: определяется через парсинг `cpu_model` (Intel поколения,
  AMD Zen-серия). Это эвристика, не cpuid — но достаточная для типовых CPU.
  При желании пользователь может переопределить через env APEXCORE_SIMD.
- DRAM speed/channels: пытаемся прочитать через WMI (Windows) или
  ``/sys/devices/virtual/dmi/`` (Linux). Если не удалось — fallback к
  DDR4-3200 dual-channel (51.2 GB/s). См. ``compute_dram_peak``.

Никаких новых зависимостей сверх ``psutil`` (уже в проекте).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Literal

import psutil

from apexcore.domain.models import SystemInfo

# ─── SIMD detection ─────────────────────────────────────────────────────────

SIMDLevel = Literal["sse4", "avx", "avx2", "avx512"]

# ops/cycle на ядро (FMA = 2 ops). Источник: Intel Optimization Reference Manual,
# глава 17 (AVX-512) и таблицы 2.x для AVX/AVX2; AMD Zen Software Optimization Guide.
SIMD_OPS_PER_CYCLE: dict[SIMDLevel, dict[Literal["sp", "dp"], int]] = {
    "sse4": {"sp": 8, "dp": 4},      # 128-bit, 1× FMA: SP=4·2, DP=2·2
    "avx": {"sp": 16, "dp": 8},      # 256-bit, 1× FMA per cycle
    "avx2": {"sp": 32, "dp": 16},    # 256-bit, 2× FMA per cycle
    "avx512": {"sp": 64, "dp": 32},  # 512-bit, 2× FMA per cycle
}


def detect_simd_level(cpu_model: str, cpu_arch: str | None) -> SIMDLevel | None:
    """Эвристически определить SIMD-уровень по строке модели CPU.

    Возвращает наиболее «продвинутый» поддерживаемый уровень. Если не x86 —
    возвращает None (на ARM/aarch64 другая модель FLOPS, выходит за рамки v2.0).

    Override через env-var ``APEXCORE_SIMD`` (значения: sse4/avx/avx2/avx512).
    """
    override = os.environ.get("APEXCORE_SIMD", "").strip().lower()
    if override in ("sse4", "avx", "avx2", "avx512"):
        return override  # type: ignore[return-value]

    if cpu_arch and cpu_arch.lower() not in ("amd64", "x86_64", "x64"):
        return None  # ARM, RISC-V и т.д. — Roofline не применяется в v2.0

    model = _normalize_model(cpu_model)

    # AVX-512: Intel Skylake-X / Cascade Lake / Ice Lake-SP / Sapphire Rapids,
    # AMD Zen 4 (Ryzen 7000+), Xeon Phi.
    avx512_markers = (
        # Intel Xeon Scalable: 81xx / 82xx / 83xx / 84xx / 85xx / 86xx
        "xeon platinum", "xeon gold", "xeon silver", "xeon bronze",
        "xeon w-3", "xeon w-2",
        # Intel HEDT: i9-79xxX / i9-99xxX / i9-109xxX (Skylake-X / Cascade Lake-X)
        "i9-7900x", "i9-7920x", "i9-7940x", "i9-7960x", "i9-7980xe",
        "i9-9820x", "i9-9900x", "i9-9920x", "i9-9940x", "i9-9960x", "i9-9980xe", "i9-9990xe",
        "i9-10900x", "i9-10920x", "i9-10940x", "i9-10980xe",
        # Intel Ice Lake / Tiger Lake mobile (i3/i5/i7 11xx/12xx с avx-512 — спорно,
        # некоторые вариации поддерживают, некоторые нет). Возьмём только Ice Lake.
        "ice lake", "tiger lake",
        # AMD Zen 4
        "ryzen 7700", "ryzen 7800", "ryzen 7900", "ryzen 7950",
        "ryzen 9 7", "ryzen 7 7", "ryzen 5 7",
        "epyc 9", "epyc 8004",
    )
    for marker in avx512_markers:
        if marker in model:
            return "avx512"

    # AVX2: Intel Haswell (4xxx) и новее, AMD Excavator/Zen+.
    # Большинство x86 CPU 2013+ поддерживают AVX2.
    avx2_markers = (
        # Intel desktop/mobile с поколения Haswell (i_-4xxx) и далее
        "core i3-4", "core i5-4", "core i7-4",
        "core i3-5", "core i5-5", "core i7-5",
        "core i3-6", "core i5-6", "core i7-6",
        "core i3-7", "core i5-7", "core i7-7",
        "core i3-8", "core i5-8", "core i7-8", "core i9-8",
        "core i3-9", "core i5-9", "core i7-9", "core i9-9",
        "core i3-10", "core i5-10", "core i7-10", "core i9-10",
        "core i3-11", "core i5-11", "core i7-11", "core i9-11",
        "core i3-12", "core i5-12", "core i7-12", "core i9-12",
        "core i3-13", "core i5-13", "core i7-13", "core i9-13",
        "core i3-14", "core i5-14", "core i7-14", "core i9-14",
        # Без префикса "core "
        "i3-12", "i5-12", "i7-12", "i9-12",
        "i3-13", "i5-13", "i7-13", "i9-13",
        # Intel Xeon E3/E5 v3+, Xeon E (Skylake)
        "xeon e3-1", "xeon e5-2", "xeon e-2",
        "xeon e3-12", "xeon e5-26",
        # AMD Zen / Zen+ / Zen 2 / Zen 3
        "ryzen 3 1", "ryzen 5 1", "ryzen 7 1",
        "ryzen 3 2", "ryzen 5 2", "ryzen 7 2",
        "ryzen 3 3", "ryzen 5 3", "ryzen 7 3", "ryzen 9 3",
        "ryzen 3 4", "ryzen 5 4", "ryzen 7 4", "ryzen 9 4",
        "ryzen 5 5", "ryzen 7 5", "ryzen 9 5",
        "ryzen 5 6", "ryzen 7 6", "ryzen 9 6",
        "epyc 7", "threadripper",
        # Intel Atom Goldmont+ (Pentium Silver/Celeron N4xxx, N5xxx) — частично AVX2
        "alder lake-n",
    )
    for marker in avx2_markers:
        if marker in model:
            return "avx2"

    # AVX: Intel Sandy Bridge (2xxx) / Ivy Bridge (3xxx), AMD Bulldozer
    avx_markers = (
        "core i3-2", "core i5-2", "core i7-2",
        "core i3-3", "core i5-3", "core i7-3",
        "i3-2", "i5-2", "i7-2", "i3-3", "i5-3", "i7-3",
        "fx-",  # AMD FX (Bulldozer/Piledriver)
    )
    for marker in avx_markers:
        if marker in model:
            return "avx"

    # SSE4: всё, что старше или x86-64 без AVX
    return "sse4"


def _normalize_model(cpu_model: str) -> str:
    """Lowercase + убрать (R)/(TM) + схлопнуть пробелы.

    Тонкая обёртка над общей утилитой — оставлена ради единого места
    использования внутри ``roofline``. Логика живёт в ``_cpu_text``.
    """
    from apexcore.application._cpu_text import normalize_cpu_model

    return normalize_cpu_model(cpu_model)


def detect_aes_ni(cpu_model: str, cpu_arch: str | None) -> bool:
    """Эвристически определить наличие AES-NI.

    AES-NI присутствует на Intel Westmere (2010+) и AMD Bulldozer/Jaguar (2011+).
    Проще: если CPU x86_64 и не очень древний — почти всегда есть.
    """
    if cpu_arch and cpu_arch.lower() not in ("amd64", "x86_64", "x64"):
        return False
    model = _normalize_model(cpu_model)
    # Очень старые CPU без AES-NI: Core 2, Pentium 4, Atom до Goldmont.
    legacy_markers = ("core 2 ", "pentium 4", "atom n", "atom d", "atom z", "celeron n2", "celeron j1")
    return all(marker not in model for marker in legacy_markers)


def detect_sha_ni(cpu_model: str, cpu_arch: str | None) -> bool:
    """SHA-NI: Intel Goldmont (2016), Cannon Lake / Ice Lake desktop, AMD Zen (2017).

    Эвристика по поколению. На Intel mainstream desktop SHA-NI появился только
    с Ice Lake (10nm) в десктопе — то есть с 11-го поколения и Alder Lake (12+).
    Atom Goldmont имеет, но десктопные Skylake/Coffee Lake/Rocket Lake — нет.
    """
    if cpu_arch and cpu_arch.lower() not in ("amd64", "x86_64", "x64"):
        return False
    model = _normalize_model(cpu_model)
    # Intel Core 11-го поколения и новее — есть SHA-NI
    intel_with_sha = (
        "i3-11", "i5-11", "i7-11", "i9-11",
        "i3-12", "i5-12", "i7-12", "i9-12",
        "i3-13", "i5-13", "i7-13", "i9-13",
        "i3-14", "i5-14", "i7-14", "i9-14",
        "alder lake", "raptor lake", "ice lake", "rocket lake", "tiger lake",
        # Atom Goldmont и новее
        "celeron j4", "celeron n4", "celeron j5", "celeron n5", "pentium silver",
    )
    for marker in intel_with_sha:
        if marker in model:
            return True
    # AMD Zen 1+ (Ryzen 1xxx и далее) — есть SHA-NI
    return "ryzen" in model or "epyc" in model or "threadripper" in model


# ─── Гибридные Intel (P+E) — таблица SKU для compute_flops_peak ──────────────

# Таблица гибридных Intel CPU (Alder Lake / Raptor Lake): для каждого SKU —
# (p_cores, p_max_ghz, e_cores, e_max_ghz). Гомогенная формула cores × clock ×
# ops_per_cycle на P+E завышает peak: E-cores работают на меньшей частоте, чем
# P-core turbo (но с тем же AVX2 throughput per cycle). Источник: Intel ARK.
#
# Marker → ключ нормализованной модели (lowercase, без R/TM). Совпадение по
# substring; KS/KF/F-варианты покрыты одним префиксом. Если CPU не в таблице
# (например, не-K десктопы, mobile, server) — fallback в compute_flops_peak
# на гомогенную формулу. Расширять при появлении новых SKU.
_INTEL_HYBRID_SKU_TABLE: dict[str, tuple[int, float, int, float]] = {
    # Alder Lake-S (12th gen, 2021)
    "i9-12900": (8, 5.2, 8, 3.9),
    "i7-12700": (8, 5.0, 4, 3.8),
    "i5-12600k": (6, 4.9, 4, 3.6),
    # Raptor Lake-S (13th gen, 2022)
    "i9-13900": (8, 5.8, 16, 4.3),
    "i7-13700": (8, 5.4, 8, 4.2),
    "i5-13600k": (6, 5.1, 8, 3.9),
    # Raptor Lake Refresh (14th gen, 2023)
    "i9-14900": (8, 6.0, 16, 4.4),
    "i7-14700": (8, 5.6, 12, 4.3),
    "i5-14600k": (6, 5.3, 8, 4.0),
}


def _detect_hybrid_topology(
    cpu_model: str, total_physical_cores: int
) -> tuple[int, float, int, float] | None:
    """Распознать гибридный (P+E) Intel CPU и вернуть (P_n, P_GHz, E_n, E_GHz).

    Возвращает ``None`` если CPU не в таблице ``_INTEL_HYBRID_SKU_TABLE``
    либо если сумма P+E из таблицы не совпадает с измеренным числом физ. ядер
    (защита от неверной классификации не-K вариантов с другим топология).
    """
    model = _normalize_model(cpu_model)
    for marker, (p_n, p_ghz, e_n, e_ghz) in _INTEL_HYBRID_SKU_TABLE.items():
        if marker in model and p_n + e_n == total_physical_cores:
            return p_n, p_ghz, e_n, e_ghz
    return None


# ─── Roofline-формулы ────────────────────────────────────────────────────────


def _max_clock_ghz(system_info: SystemInfo) -> float | None:
    """Получить максимальную (turbo) частоту CPU в GHz.

    Источники в порядке приоритета:
    1. ``psutil.cpu_freq().max`` (МГц).
    2. Парсинг строки модели CPU (e.g. "i7-12700K @ 3.6GHz" → 3.6).
    3. Env-override ``APEXCORE_CPU_GHZ``.
    """
    override = os.environ.get("APEXCORE_CPU_GHZ", "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            pass

    try:
        freq = psutil.cpu_freq(percpu=False)
        if freq and freq.max and freq.max > 0:
            return float(freq.max) / 1000.0
    except (NotImplementedError, OSError):
        pass

    # Парсинг строки модели: "Intel(R) Core(TM) i7-12700K CPU @ 3.60GHz"
    model = system_info.cpu_model
    match = re.search(r"@\s*([\d.]+)\s*GHz", model, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return None


def compute_flops_peak(
    system_info: SystemInfo, dtype: Literal["sp", "dp"]
) -> float | None:
    """Теоретический peak FPU в GFLOPS.

    Базовая формула: ``cores × ops_per_cycle(SIMD, dtype) × clock_GHz``.

    Для гибридных Intel (Alder/Raptor Lake) — суммируется по P-cores и
    E-cores раздельно: гомогенная формула берёт total cores × P-core
    boost, что завышает peak (E-cores работают на меньшей частоте).
    Распознавание — через ``_detect_hybrid_topology``.

    Возвращает None, если архитектура не x86 или CPU не определён.
    """
    simd = detect_simd_level(system_info.cpu_model, system_info.cpu_arch)
    if simd is None:
        return None
    cores = system_info.cpu_cores.physical
    if cores <= 0:
        return None
    ops_per_cycle = SIMD_OPS_PER_CYCLE[simd][dtype]

    hybrid = _detect_hybrid_topology(system_info.cpu_model, cores)
    if hybrid is not None:
        # E-cores на Alder/Raptor Lake поддерживают AVX2 с тем же
        # throughput per cycle, что и P-cores; различие — частота.
        p_n, p_ghz, e_n, e_ghz = hybrid
        return p_n * ops_per_cycle * p_ghz + e_n * ops_per_cycle * e_ghz

    clock = _max_clock_ghz(system_info)
    if clock is None or clock <= 0:
        return None
    return cores * ops_per_cycle * clock


def compute_integer_peak(
    system_info: SystemInfo, bits: Literal[24, 32, 64]
) -> float | None:
    """Теоретический peak Integer ALU в GIOPS.

    Формула: ``cores × 4 ops/cycle × clock_GHz`` (general-purpose ALU,
    Hennessy-Patterson 6th ed., гл.3 — современные x86 ALU обычно 4 ops/cycle).

    Параметр bits зарезервирован для будущих уточнений (на 64-битных операциях
    некоторые CPU имеют меньший throughput, но для типовых x86 разница
    незначительна).
    """
    if system_info.cpu_arch and system_info.cpu_arch.lower() not in (
        "amd64", "x86_64", "x64",
    ):
        return None
    clock = _max_clock_ghz(system_info)
    if clock is None or clock <= 0:
        return None
    cores = system_info.cpu_cores.physical
    if cores <= 0:
        return None
    return cores * 4.0 * clock


def compute_aes_peak(system_info: SystemInfo) -> float | None:
    """Теоретический peak AES-256-CBC в MB/s.

    Если есть AES-NI — оценка по Intel datasheet: ~1.3 GB/s/GHz на ядро.
    В микробенчмарке AES однопоточный, поэтому домножение на cores не делаем.
    Возвращаем MB/s.

    Без AES-NI — None (используется empirical proxy).
    """
    if not detect_aes_ni(system_info.cpu_model, system_info.cpu_arch):
        return None
    clock = _max_clock_ghz(system_info)
    if clock is None or clock <= 0:
        return None
    # 1.3 GB/s/GHz на одно ядро = 1300 MB/s/GHz
    return 1300.0 * clock


def compute_sha1_peak(system_info: SystemInfo) -> float | None:
    """Теоретический peak SHA-1 в MB/s.

    С SHA-NI: ~3 cycles/byte → throughput = clock / 3 GB/s = clock * 1000 / 3 MB/s.
    Без SHA-NI: None (empirical proxy).
    """
    if not detect_sha_ni(system_info.cpu_model, system_info.cpu_arch):
        return None
    clock = _max_clock_ghz(system_info)
    if clock is None or clock <= 0:
        return None
    # 3 cycles per byte
    return clock * 1000.0 / 3.0


# ─── DRAM peak ───────────────────────────────────────────────────────────────


def _read_dram_speed_mts_windows() -> tuple[float, int] | None:
    """Прочитать скорость и количество модулей DRAM через PowerShell CIM (Windows).

    Используется ``Get-CimInstance Win32_PhysicalMemory`` — современная замена
    deprecated `wmic`. Возвращает (speed_MTs, num_modules) или None.

    Берётся ConfiguredClockSpeed (фактическая работающая частота); если она 0
    или отсутствует — Speed (заявленная JEDEC).
    """
    if sys.platform != "win32":
        return None
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "Get-CimInstance Win32_PhysicalMemory | "
            "ForEach-Object { Write-Output \"$($_.ConfiguredClockSpeed) $($_.Speed)\" }"
        ),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    speeds: list[float] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        try:
            configured = int(parts[0]) if parts[0].isdigit() else 0
            jedec = int(parts[1]) if parts[1].isdigit() else 0
        except (ValueError, IndexError):
            continue
        # Берём ConfiguredClockSpeed если он задан (фактический), иначе JEDEC.
        speed = configured if configured > 0 else jedec
        if speed > 0:
            speeds.append(float(speed))
    if not speeds:
        return None
    return max(speeds), len(speeds)


def _read_dram_speed_mts_linux() -> tuple[float, int] | None:
    """Прочитать скорость и количество модулей DRAM через dmidecode (Linux).

    Требует root для dmidecode; без root возвращает None.
    """
    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(
            ["dmidecode", "-t", "17"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    speeds: list[float] = []
    for line in out.stdout.splitlines():
        match = re.search(r"Configured Memory Speed:\s*(\d+)\s*MT/s", line, re.IGNORECASE)
        if not match:
            match = re.search(r"Speed:\s*(\d+)\s*MT/s", line, re.IGNORECASE)
        if match:
            try:
                s = float(match.group(1))
                if s > 0:
                    speeds.append(s)
            except ValueError:
                continue
    if not speeds:
        return None
    # dmidecode даёт по две строки на модуль (Speed + Configured Speed) — берём max.
    return max(speeds), len(speeds)


def _detect_dram(system_info: SystemInfo) -> tuple[float, int] | None:
    """Определить (speed_MTs, num_modules) для DRAM.

    Override через env APEXCORE_DRAM_MTS и APEXCORE_DRAM_MODULES.
    """
    speed_override = os.environ.get("APEXCORE_DRAM_MTS", "").strip()
    modules_override = os.environ.get("APEXCORE_DRAM_MODULES", "").strip()
    if speed_override and modules_override:
        try:
            return float(speed_override), int(modules_override)
        except ValueError:
            pass

    detected = _read_dram_speed_mts_windows() or _read_dram_speed_mts_linux()
    if detected is not None:
        return detected

    # Fallback: предположение DDR4-3200 dual-channel (типичный десктоп 2020+).
    # Это явный приближённый эталон; в notes scoring этот случай помечается.
    return None


def _max_dram_channels(cpu_model: str) -> int | None:
    """Максимум memory channels для платформы по строке модели CPU.

    Эвристика: desktop = 2 канала, HEDT = 4, server = 6/8/12. Нужна потому что
    ``modules × speed × 8`` физически некорректно для конфигураций с >1 DIMM
    на канал (типичный desktop с 4 DIMM в dual-channel mode даёт 2 канала,
    не 4). Без этой коррекции stream_peak завышается в 2× → r_stream занижен
    в 2× → балл «прыгает» 3600↔5100 на одной машине в зависимости от того,
    2× 32 GB или 4× 16 GB.

    Возвращает ``None`` если CPU не распознан → caller должен fallback на
    текущее (некорректное) поведение `modules × speed × 8`. Лучше консервативная
    некорректность чем None-балл из-за неизвестной платформы.
    """
    model = _normalize_model(cpu_model)

    # Server Intel Xeon Scalable / Sapphire Rapids / Emerald Rapids.
    # Точное число каналов варьируется (6 для Skylake-SP, 8 для Sapphire Rapids),
    # упрощаем до 6 — недооценка пика для новых server-CPU приемлемее, чем
    # таблица из 50 SKU.
    server_intel_markers = (
        "xeon platinum", "xeon gold", "xeon silver", "xeon bronze",
        "xeon e5-", "xeon e7-",
    )
    if any(m in model for m in server_intel_markers):
        return 6

    # Server AMD EPYC Zen 4+ (9xx4, 8004) — 12 каналов DDR5.
    if "epyc 9" in model or "epyc 8004" in model:
        return 12

    # Server AMD EPYC Zen 1-3 (7xx2/7xx3) — 8 каналов DDR4.
    if "epyc 7" in model:
        return 8

    # HEDT Intel: Xeon W, i9-X/XE (Skylake-X/Cascade Lake-X) — 4 канала.
    hedt_intel_markers = ("xeon w-", "xeon w3-",)
    if any(m in model for m in hedt_intel_markers):
        return 4
    # i9-XXXXX/XE/X (Skylake-X и новее)
    hedt_i9_markers = (
        "i9-7900x", "i9-7920x", "i9-7940x", "i9-7960x", "i9-7980xe",
        "i9-9820x", "i9-9900x", "i9-9920x", "i9-9940x", "i9-9960x", "i9-9980xe", "i9-9990xe",
        "i9-10900x", "i9-10920x", "i9-10940x", "i9-10980xe",
    )
    if any(m in model for m in hedt_i9_markers):
        return 4

    # HEDT/Workstation AMD: Threadripper PRO — 8 каналов.
    if "threadripper pro" in model:
        return 8
    # Threadripper non-PRO — 4 канала.
    if "threadripper" in model:
        return 4

    # Mainstream desktop: Intel Core i3/i5/i7/i9 (non-HEDT), AMD Ryzen 3/5/7/9
    # (non-Threadripper). Все DDR4/DDR5 desktop платформы — 2 канала.
    desktop_markers = (
        "core i3", "core i5", "core i7", "core i9",
        "ryzen 3 ", "ryzen 5 ", "ryzen 7 ", "ryzen 9 ",
        "pentium", "celeron",
        # Без префикса "core ":
        "i3-", "i5-", "i7-", "i9-",
    )
    if any(m in model for m in desktop_markers):
        return 2

    return None


def compute_dram_peak(system_info: SystemInfo) -> float | None:
    """Теоретический peak DRAM bandwidth в MB/s (для STREAM-style тестов).

    Формула: ``effective_channels × speed_MTs × 8 bytes/transfer``, где
    ``effective_channels = min(modules, max_channels(cpu_model))``.

    Контроллер не может опрашивать оба DIMM одного канала одновременно — bandwidth
    ограничен числом каналов, а не модулей. Раньше формула считала по числу
    модулей, что завышало peak в 2× на типичном desktop с 4 DIMM (4 модуля в
    dual-channel = 2 канала). См. ``_max_dram_channels``.

    None, если DRAM info недоступна. Если CPU не распознан (max_channels = None)
    — fallback на старое (некорректное, но не нулевое) поведение по числу модулей.
    """
    dram = _detect_dram(system_info)
    if dram is None:
        return None
    speed_mts, modules = dram

    max_channels = _max_dram_channels(system_info.cpu_model)
    effective_channels = (
        min(modules, max_channels) if max_channels is not None else modules
    )

    return effective_channels * speed_mts * 8.0


# ─── TJmax (thermal junction temperature) ────────────────────────────────────

# Документированный предел рабочей температуры кристалла CPU. Используется
# в `application/stress_score.compute_r_thermal` для нормировки headroom
# (`headroom = (TJmax - T_max) / 30°C`). Источники: Intel SDM Vol.3B §15
# (MSR_TEMPERATURE_TARGET), AMD CCD spec для Ryzen 5000+, AnandTech reviews.
#
# Только табличный fallback по семейству CPU; MSR-чтение не реализовано
# (см. `docs/research/stress_test_mark_method.md` §10 — отложено).
CPU_TJMAX_TABLE: dict[str, int] = {
    "intel_desktop": 100,
    "intel_hedt_xeon_w": 100,
    "intel_xeon_scalable": 100,
    "ryzen_5000": 90,
    "ryzen_7000": 95,
    "ryzen_9000": 95,
    "threadripper": 95,
    "epyc_genoa": 95,
    "epyc_bergamo": 105,
}


def _cpu_tjmax_family(cpu_model: str) -> str | None:
    """Классифицировать CPU в family-ключ для ``CPU_TJMAX_TABLE``.

    Возвращает ключ из таблицы либо None если CPU не распознан.
    """
    model = _normalize_model(cpu_model)

    # Intel Xeon Scalable (Platinum/Gold/Silver/Bronze).
    if any(m in model for m in (
        "xeon platinum", "xeon gold", "xeon silver", "xeon bronze",
    )):
        return "intel_xeon_scalable"

    # Intel HEDT / Workstation Xeon W.
    if "xeon w" in model or any(m in model for m in (
        "i9-7900x", "i9-7920x", "i9-7940x", "i9-7960x", "i9-7980xe",
        "i9-9820x", "i9-9900x", "i9-9920x", "i9-9940x", "i9-9960x", "i9-9980xe",
        "i9-9990xe",
        "i9-10900x", "i9-10920x", "i9-10940x", "i9-10980xe",
    )):
        return "intel_hedt_xeon_w"

    # Intel mainstream desktop (Core i3/i5/i7/i9, Pentium, Celeron, Atom).
    intel_desktop_markers = (
        "core i3", "core i5", "core i7", "core i9",
        "i3-", "i5-", "i7-", "i9-",
        "pentium", "celeron",
    )
    if any(m in model for m in intel_desktop_markers):
        return "intel_desktop"

    # AMD EPYC Zen 4 Bergamo (8004 series, density-optimized).
    if "epyc 8004" in model:
        return "epyc_bergamo"
    # AMD EPYC Zen 4 Genoa (9xx4 series).
    if "epyc 9" in model:
        return "epyc_genoa"

    # AMD Threadripper (incl. PRO).
    if "threadripper" in model:
        return "threadripper"

    # AMD Ryzen 9000 (Zen 5).
    if any(m in model for m in (
        "ryzen 3 9", "ryzen 5 9", "ryzen 7 9", "ryzen 9 9",
    )):
        return "ryzen_9000"

    # AMD Ryzen 7000 (Zen 4).
    if any(m in model for m in (
        "ryzen 3 7", "ryzen 5 7", "ryzen 7 7", "ryzen 9 7",
    )):
        return "ryzen_7000"

    # AMD Ryzen 5000 (Zen 3) — а также 6000 mobile (тот же TJmax).
    if any(m in model for m in (
        "ryzen 3 5", "ryzen 5 5", "ryzen 7 5", "ryzen 9 5",
        "ryzen 5 6", "ryzen 7 6", "ryzen 9 6",
    )):
        return "ryzen_5000"

    return None


def resolve_tjmax(system_info: SystemInfo) -> int | None:
    """Определить TJmax CPU в °C.

    Источники в порядке приоритета:
    1. Env-override ``APEXCORE_TJMAX`` (для тестов и edge cases).
    2. ``CPU_TJMAX_TABLE`` по family-ключу из ``_cpu_tjmax_family``.

    Возвращает None если CPU не распознан → r_thermal не строится →
    stress_score = None (см. ``application/stress_score.compute_stress_score``).
    """
    override = os.environ.get("APEXCORE_TJMAX", "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            pass

    family = _cpu_tjmax_family(system_info.cpu_model)
    if family is None:
        return None
    return CPU_TJMAX_TABLE.get(family)


# ─── Aggregator ──────────────────────────────────────────────────────────────


WORKLOAD_CATEGORIES: dict[str, str] = {
    "memory_read": "memory",
    "memory_write": "memory",
    "memory_copy": "memory",
    "flops_sp": "flops",
    "flops_dp": "flops",
    "int_iops_24": "integer",
    "int_iops_32": "integer",
    "int_iops_64": "integer",
    "aes_256": "crypto",
    "sha1": "crypto",
    "julia_sp": "fractal",
    "mandelbrot_dp": "fractal",
}


def get_roofline_reference(system_info: SystemInfo) -> dict[str, float | None]:
    """Вернуть Roofline-эталоны для всех 12 micro-тестов.

    Ключ — ``MicroBenchResult.name``. Значение в исходных единицах теста:
    MB/s для memory/crypto, GFLOPS для flops, GIOPS для integer, FPS для fractal.

    None в значении означает «теоретический предел недоступен» — вызывающий код
    должен использовать empirical proxy из ``data/empirical_reference.yaml``.

    Для memory_copy bandwidth удваивается (read+write по соглашению STREAM).
    """
    dram = compute_dram_peak(system_info)
    flops_sp = compute_flops_peak(system_info, "sp")
    flops_dp = compute_flops_peak(system_info, "dp")
    int_peak = compute_integer_peak(system_info, 32)  # одинаково для 24/32/64
    aes_peak = compute_aes_peak(system_info)
    sha_peak = compute_sha1_peak(system_info)

    return {
        # memory: MB/s
        "memory_read": dram,
        "memory_write": dram,
        # memory_copy в micro считается как 2× (read+write) — потолок тоже удвоенный
        "memory_copy": dram * 2.0 if dram is not None else None,
        # flops: GFLOPS
        "flops_sp": flops_sp,
        "flops_dp": flops_dp,
        # integer: GIOPS (формула одна для всех bit-width в первой версии)
        "int_iops_24": int_peak,
        "int_iops_32": int_peak,
        "int_iops_64": int_peak,
        # crypto: MB/s
        "aes_256": aes_peak,
        "sha1": sha_peak,
        # fractal: FPS — теоретический предел не вычисляется (нет аналитического Roofline)
        "julia_sp": None,
        "mandelbrot_dp": None,
    }


__all__ = [
    "CPU_TJMAX_TABLE",
    "SIMD_OPS_PER_CYCLE",
    "WORKLOAD_CATEGORIES",
    "SIMDLevel",
    "compute_aes_peak",
    "compute_dram_peak",
    "compute_flops_peak",
    "compute_integer_peak",
    "compute_sha1_peak",
    "detect_aes_ni",
    "detect_sha_ni",
    "detect_simd_level",
    "get_roofline_reference",
    "resolve_tjmax",
]
