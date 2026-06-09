"""Реестр стресс-движков с автоопределением доступности и профилями.

После рефакторинга по плану ``stateful-enchanting-pretzel.md``:

- Существующие native-движки переклассифицированы как «бенчмарки», а не
  stability-stressors (LCG-LCG не нагревает SIMD/FPU; matmul 512×512
  помещается в L2/L3). Они остаются доступными для совместимости и
  отдельных пунктов меню, но в ``system_stress_full`` не входят.
- Добавлены новые native-движки ``builtin_large_dgemm``,
  ``builtin_fft_stress`` (small/large/blend), ``builtin_large_stream`` —
  все они используют рабочий набор > L3 и имеют verify-режим.
- Внешние обёртки (``stress-ng matrixprod`` / ``stress-ng vm`` /
  ``prime95``) используют ``--verify`` (или эквивалент) и работают как
  главные стрессоры на своих платформах.
"""

from __future__ import annotations

import shutil

from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.builtin_cpu import (
    BuiltinCpuFpEngine,
    BuiltinCpuIntEngine,
)
from apexcore.infrastructure.stress.builtin_fft_stress import BuiltinFftStressEngine
from apexcore.infrastructure.stress.builtin_large_dgemm import BuiltinLargeDgemmEngine
from apexcore.infrastructure.stress.builtin_large_stream import (
    BuiltinLargeStreamEngine,
)
from apexcore.infrastructure.stress.builtin_ram import (
    BuiltinRamBandwidthEngine,
    BuiltinRamLatencyEngine,
)
from apexcore.infrastructure.stress.external_prime95 import Prime95Engine
from apexcore.infrastructure.stress.external_stress_ng import (
    StressNgCpuEngine,
    StressNgMatrixEngine,
    StressNgVmEngine,
)

# ─────────────────────────── Метаданные движков ─────────────────────────────


# Категории по новой классификации:
#   stability_cpu  — нагрузка для проверки CPU-стабильности (FPU/SIMD/cache).
#   stability_ram  — нагрузка для проверки RAM-стабильности (bw + verify).
#   benchmark      — измерение peak/sustained без вердикта pass/fail.
ENGINE_ROLES: dict[str, str] = {
    # Главные stress-двигатели:
    "external_stress_ng_matrix_verify": "stability_cpu",
    "external_stress_ng_vm_verify": "stability_ram",
    "external_prime95_small": "stability_cpu",
    "external_prime95_large": "stability_cpu",
    "external_prime95_blend": "stability_cpu",
    "builtin_large_dgemm": "stability_cpu",
    "builtin_fft_stress_small": "stability_cpu",
    "builtin_fft_stress_large": "stability_cpu",
    "builtin_fft_stress_blend": "stability_cpu",
    "builtin_large_stream": "stability_ram",
    # Бенчмарки (для пунктов меню, но не для system_stress_full):
    "builtin_cpu_fp": "benchmark",
    "builtin_cpu_int": "benchmark",
    "builtin_ram_bw": "benchmark",
    "builtin_ram_lat": "benchmark",
    # Generic stress-ng обёртки (без verify) — оставляем для совместимости.
    "external_stress_ng_cpu": "benchmark",
    "external_stress_ng_matrix": "benchmark",
    "external_stress_ng_vm": "benchmark",
    "external_prime95": "benchmark",
}


# Короткие дружественные русские названия для UI. Используются вместо
# технических имён (``builtin_large_dgemm`` и т. п.) в шапках прогона
# и итоговых таблицах. Если имя не в словаре — берётся ``name`` как есть.
ENGINE_FRIENDLY_NAMES: dict[str, str] = {
    "external_stress_ng_matrix_verify": "Стресс ЦП (stress-ng matrixprod)",
    "external_stress_ng_vm_verify": "Стресс ОЗУ (stress-ng vm)",
    "external_prime95_small": "Стресс ЦП (Prime95 Small FFT)",
    "external_prime95_large": "Стресс ЦП (Prime95 Large FFT)",
    "external_prime95_blend": "Стресс ЦП (Prime95 Blend)",
    "external_prime95": "Стресс ЦП (Prime95)",
    "external_stress_ng_cpu": "Стресс ЦП (stress-ng cpu)",
    "external_stress_ng_matrix": "Стресс ЦП (stress-ng matrixprod)",
    "external_stress_ng_vm": "Стресс ОЗУ (stress-ng vm)",
    "builtin_large_dgemm": "Стресс ЦП (большой DGEMM)",
    "builtin_fft_stress_small": "Стресс ЦП (FFT, малый)",
    "builtin_fft_stress_large": "Стресс ЦП (FFT, большой)",
    "builtin_fft_stress_blend": "Стресс ЦП (FFT, чередующийся)",
    "builtin_large_stream": "Стресс ОЗУ (STREAM, большой буфер)",
    "builtin_cpu_fp": "Бенчмарк ЦП (matmul 512×512)",
    "builtin_cpu_int": "Бенчмарк ЦП (целочисленный микро)",
    "builtin_ram_bw": "Бенчмарк ОЗУ (полоса)",
    "builtin_ram_lat": "Бенчмарк ОЗУ (латентность)",
}


def friendly_engine_name(name: str) -> str:
    """Вернуть человекочитаемое имя нагрузки или само ``name`` если нет в словаре."""
    return ENGINE_FRIENDLY_NAMES.get(name, name)


# Короткие пояснения для пользователя, что именно нагружается, в терминах
# понятных не-специалисту. Привязаны к ``category`` движка (а не к имени) —
# меньше дубликатов, охватывают все варианты ЦП/ОЗУ-стрессоров.
CATEGORY_USER_HINTS: dict[str, str] = {
    "cpu_fp": "Высокая нагрузка на ядра процессора и его кэш-память",
    "cpu_int": "Целочисленная нагрузка на ядра процессора",
    "ram_bw": "Чтение и запись больших массивов в оперативную память",
    "ram_lat": "Случайные обращения к оперативной памяти (задержки)",
}


def category_user_hint(category: str) -> str:
    """Описание категории нагрузки для пользовательского интерфейса."""
    return CATEGORY_USER_HINTS.get(category, "")


ENGINE_DESCRIPTIONS: dict[str, str] = {
    "external_stress_ng_matrix_verify": (
        "stress-ng matrixprod + --verify — главный CPU-стресс на Linux/Astra "
        "(Ubuntu Wiki: «heats x86 CPUs the best»)."
    ),
    "external_stress_ng_vm_verify": (
        "stress-ng --vm --verify — главный RAM-стресс на Linux с pass/fail."
    ),
    "external_prime95_small": (
        "Prime95 Small FFT — кэш-резидентный FFT, max FPU stress (OC-стандарт)."
    ),
    "external_prime95_large": (
        "Prime95 Large FFT — выходит за L3, нагружает контроллер памяти."
    ),
    "external_prime95_blend": (
        "Prime95 Blend — чередование Small/Large FFT (AIDA64-style)."
    ),
    "builtin_large_dgemm": (
        "Native DGEMM на матрице > L3 (numpy/BLAS, аналог stress-ng matrixprod)."
    ),
    "builtin_fft_stress_small": "Native FFT (small, кэш-резидентный) с verify.",
    "builtin_fft_stress_large": "Native FFT (large, > L3) с verify.",
    "builtin_fft_stress_blend": "Native FFT (blend, чередование) с verify.",
    "builtin_large_stream": "Native STREAM Triad с динамическим размером и verify.",
    "builtin_cpu_fp": "Бенчмарк: matmul 512×512 (кэш-резидентный peak FLOPS).",
    "builtin_cpu_int": "Бенчмарк: ALU-микро (LCG, без SIMD).",
    "builtin_ram_bw": "Бенчмарк: STREAM Triad на 256 МБ массивах (sustained BW).",
    "builtin_ram_lat": "Бенчмарк: pointer-chasing на 64 МБ (lat_mem_rd).",
    "external_stress_ng_cpu": "stress-ng --cpu --cpu-method all (без verify).",
    "external_stress_ng_matrix": "stress-ng --cpu-method matrixprod (без verify).",
    "external_stress_ng_vm": "stress-ng --vm --vm-method all (без verify).",
    "external_prime95": "Prime95 в torture mode (legacy — без выбора режима).",
}


# ─────────────────────────── Реестр ─────────────────────────────────────────


class StressRegistry:
    """Каталог зарегистрированных стресс-движков."""

    def __init__(self) -> None:
        self._engines: dict[str, StressEngine] = {}

    def register(self, engine: StressEngine, *, alias: str | None = None) -> None:
        key = alias or engine.name
        # Дубликаты под разными alias допустимы: один Prime95Engine с разными
        # mode-параметрами регистрируется три раза. Чтобы рестрикции по name
        # не мешали, переопределяем engine.name на alias-уровне через атрибут.
        # ВАЖНО: каждый alias — отдельный экземпляр (не один общий).
        self._engines[key] = engine

    def get(self, name: str) -> StressEngine | None:
        return self._engines.get(name)

    def all(self) -> list[StressEngine]:
        return list(self._engines.values())

    def names(self) -> list[str]:
        return list(self._engines.keys())

    def available(self) -> list[StressEngine]:
        return [e for e in self._engines.values() if e.is_available()]

    def by_category(
        self, category: str, only_available: bool = True
    ) -> list[StressEngine]:
        out: list[StressEngine] = []
        for e in self._engines.values():
            if e.category != category:
                continue
            if only_available and not e.is_available():
                continue
            out.append(e)
        return out

    def by_role(self, role: str, only_available: bool = True) -> list[tuple[str, StressEngine]]:
        out: list[tuple[str, StressEngine]] = []
        for name, eng in self._engines.items():
            if ENGINE_ROLES.get(name) != role:
                continue
            if only_available and not eng.is_available():
                continue
            out.append((name, eng))
        return out


def _aliased(engine: StressEngine, alias: str) -> StressEngine:
    """Привязать alias-имя к экземпляру, чтобы реестр различал его варианты.

    Простейший способ — задать атрибут ``name`` на экземпляре. Тип уже
    позволяет это (атрибут класса просто перекрывается экземпляром).
    """
    engine.name = alias  # type: ignore[misc]
    return engine


def build_default_registry() -> StressRegistry:
    """Собрать реестр со всеми известными движками."""
    reg = StressRegistry()
    # Бенчмарки — оставлены для пунктов меню «5.2 / 5.3 — выбор движка»
    # и для совместимости со старыми профилями.
    reg.register(BuiltinCpuIntEngine())
    reg.register(BuiltinCpuFpEngine())
    reg.register(BuiltinRamBandwidthEngine())
    reg.register(BuiltinRamLatencyEngine())
    # Новые native stability-движки:
    reg.register(BuiltinLargeDgemmEngine())
    reg.register(_aliased(BuiltinFftStressEngine(size="small"), "builtin_fft_stress_small"))
    reg.register(_aliased(BuiltinFftStressEngine(size="large"), "builtin_fft_stress_large"))
    reg.register(_aliased(BuiltinFftStressEngine(size="blend"), "builtin_fft_stress_blend"))
    reg.register(BuiltinLargeStreamEngine())
    # Внешние generic — без verify (legacy):
    reg.register(StressNgCpuEngine())
    reg.register(StressNgMatrixEngine(verify=False))
    reg.register(StressNgVmEngine(verify=False))
    reg.register(Prime95Engine())  # legacy mode=small по умолчанию
    # Внешние stability — с verify и параметризацией:
    reg.register(_aliased(
        StressNgMatrixEngine(verify=True),
        "external_stress_ng_matrix_verify",
    ))
    reg.register(_aliased(
        StressNgVmEngine(verify=True, vm_method="all"),
        "external_stress_ng_vm_verify",
    ))
    reg.register(_aliased(Prime95Engine(mode="small"), "external_prime95_small"))
    reg.register(_aliased(Prime95Engine(mode="large"), "external_prime95_large"))
    reg.register(_aliased(Prime95Engine(mode="blend"), "external_prime95_blend"))
    return reg


# ─────────────────────────── Профили нагрузок ───────────────────────────────


# Старые профили оставлены как «benchmark profiles» для совместимости.
# Новые stability-профили построены через функцию ``build_stability_plan()``,
# которая выбирает движки согласно платформе и доступности внешних утилит.
PROFILES: dict[str, list[str]] = {
    "cpu_heavy": [
        "builtin_cpu_int",
        "builtin_cpu_fp",
    ],
    "ram_heavy": [
        "builtin_ram_bw",
        "builtin_ram_lat",
    ],
    "balanced": [
        "builtin_cpu_int",
        "builtin_cpu_fp",
        "builtin_ram_bw",
        "builtin_ram_lat",
    ],
    "compare_external": [
        "builtin_cpu_int",
        "external_stress_ng_cpu",
        "builtin_cpu_fp",
        "external_stress_ng_matrix",
        "builtin_ram_bw",
        "external_stress_ng_vm",
    ],
    "compare_external_windows": [
        "builtin_cpu_int",
        "external_prime95",
        "builtin_cpu_fp",
        "builtin_ram_bw",
    ],
}


def profile_engines(profile_name: str, registry: StressRegistry) -> list[StressEngine]:
    """Список движков для заданного профиля. Недоступные молча отбрасываются."""
    names = PROFILES.get(profile_name)
    if names is None:
        names = PROFILES["balanced"]
    out: list[StressEngine] = []
    for n in names:
        e = registry.get(n)
        if e is None:
            continue
        if not e.is_available():
            continue
        out.append(e)
    return out


# ─────────────────────────── Платформенные планы ────────────────────────────


def _has_stress_ng() -> bool:
    return shutil.which("stress-ng") is not None


def _has_prime95() -> bool:
    return shutil.which("prime95") is not None or shutil.which("mprime") is not None


def pick_cpu_stressor(registry: StressRegistry) -> tuple[str, StressEngine]:
    """Главный CPU-стрессор для scored-стресса («Оценка под нагрузкой»).

    Берём builtin_large_dgemm (BLAS DGEMM): он (а) сильно греет CPU —
    насыщает FMA/AVX(-512), один из максимальных по power-draw, и (б) выдаёт
    throughput в GFLOPS, которые stress_score сопоставляет с Roofline-пиком
    (r_dgemm).

    Раньше на Linux тут выбирался stress-ng matrixprod (а на Windows — prime95):
    греют не хуже, но отдают bogo-ops / без throughput → r_dgemm=None → стресс-
    балл НИКОГДА не считался (см. compute_stress_score_context — матч по unit
    "GFLOPS"). Поскольку apexcore жёстко зависит от stress-ng, на Linux балл
    был недоступен всегда. Для scored-стресса это критично — берём DGEMM.
    Fallback — builtin_fft_stress_large.
    """
    eng = registry.get("builtin_large_dgemm")
    if eng is not None:
        return "builtin_large_dgemm", eng
    fallback = registry.get("builtin_fft_stress_large")
    if fallback is not None:
        return "builtin_fft_stress_large", fallback
    raise RuntimeError("Нет ни одного CPU-стрессора — реестр повреждён")


def pick_ram_stressor(registry: StressRegistry) -> tuple[str, StressEngine]:
    """Главный RAM-стрессор для scored-стресса: builtin_large_stream (GB/s).

    Как и с CPU: STREAM насыщает контроллер памяти (греет IMC/DRAM) И выдаёт
    GB/s, которые stress_score сопоставляет с DRAM-пиком (r_stream). Раньше на
    Linux брался stress-ng vm — он отдаёт bogo-ops → r_stream=None → балл
    недоступен. Поэтому берём STREAM.
    """
    eng = registry.get("builtin_large_stream")
    if eng is not None:
        return "builtin_large_stream", eng
    raise RuntimeError("Нет RAM-стрессора в реестре")


def pick_stability_engines_by_role(registry: StressRegistry) -> dict[str, list[tuple[str, StressEngine]]]:
    """Все доступные stability-движки, сгруппированные по роли.

    Используется для UI «список движков и их доступность».
    """
    return {
        "stability_cpu": registry.by_role("stability_cpu", only_available=True),
        "stability_ram": registry.by_role("stability_ram", only_available=True),
        "benchmark": registry.by_role("benchmark", only_available=True),
    }
