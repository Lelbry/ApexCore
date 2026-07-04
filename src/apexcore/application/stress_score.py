"""Stress test mark: 4-компонентный балл sustainable performance под нагрузкой.

Спецификации: ``new-app/docs/stress_score.md``,
``docs/research/stress_test_mark_method.md``.

Балл агрегирует четыре безразмерных ratio через геометрическое среднее:

- ``r_dgemm``      — measured DGEMM / Roofline FLOPS peak
- ``r_stream``     — measured STREAM / DRAM peak
- ``r_stability``  — min/max частоты CPU за прогон (лагающий thermal-индикатор)
- ``r_thermal``    — функция (TJmax − T_max) — leading thermal headroom

GM — Williams 2009 (Roofline), Fleming & Wallace 1986 (GM нормализованных ratio),
sustainable performance argument (research §3). Для построения балла нужны все
четыре ratio; CPU temp требует LHM/PawnIO/sensord (см. CLAUDE.md).

Длительность прогона **не блокирует** расчёт балла: даже короткий прогон
(< 90 сек) даёт оценку, но рендер показывает warning «приближённая, для
точности используйте 10-60 мин». Тепловой стационар воздушного кулера
60–120 секунд (research §3.2 / §8.3) — корректная sustainable-метрика
требует ≥ 10 мин, но дать пользователю ориентировочное число лучше, чем
скрывать (запрос пользователя 2026-05-17).

Лейбл в UI — «Оценка под нагрузкой (CPU+RAM+охлаждение)». Внутренние имена
(модуль stress_score, функции compute_stress_score*) оставлены ради
обратной совместимости.
"""

from __future__ import annotations

from dataclasses import dataclass

from apexcore.application.parallel_runner import ParallelStressResult
from apexcore.application.roofline import (
    _detect_dram,
    _max_clock_ghz,
    compute_dram_peak,
    compute_flops_peak,
    detect_simd_level,
    resolve_tjmax,
)
from apexcore.application.scoring import geometric_mean
from apexcore.domain.models import SystemInfo, ThermalStabilityResult

# Порог «короткий прогон» — балл считается, но UI помечает результат как
# приближённый и рекомендует 10–60 мин для полного выхода кулера на
# тепловой стационар (research §3.2 / §8.3 — 60–120 сек воздушный кулер).
# Используется в `render.py` для отображения warning; на сам расчёт не влияет.
RELIABLE_DURATION_SEC = 90.0


@dataclass
class StressScoreContext:
    """Сводный контекст: измеренные значения, Roofline-пики и стресс-балл.

    Все поля опциональны: если архитектура неизвестна или нет данных
    телеметрии, отдельные ratio = None, а ``stress_score`` = None (для
    балла нужны все четыре компонента).
    """

    # Измеренные значения за прогон.
    dgemm_gflops: float | None = None
    stream_gb_s: float | None = None
    stability_pct: float | None = None
    t_max_c: float | None = None

    # Roofline-пики и thermal-лимит (детерминированы по конфигу системы).
    dgemm_peak_gflops: float | None = None
    stream_peak_gb_s: float | None = None
    tjmax_c: int | None = None

    # Безразмерные ratio.
    r_dgemm: float | None = None
    r_stream: float | None = None
    r_stability: float | None = None
    r_thermal: float | None = None

    # Стресс-балл (STRESS_SCORE_SCALE × GM(r_dgemm, r_stream, r_stability, r_thermal)).
    stress_score: float | None = None

    # Длительность прогона (для гейта 90 сек и пояснений в UI).
    duration_sec: float | None = None

    # Метаданные для пояснительных строк рендера.
    simd_level: str | None = None
    clock_ghz: float | None = None
    physical_cores: int | None = None
    dram_mts: float | None = None
    dram_modules: int | None = None


def _find_throughput(parallel: ParallelStressResult, unit: str) -> float | None:
    """Достать throughput первого результата с указанным unit (например, "GFLOPS").

    Стресс-план CPU+RAM содержит один CPU-движок и один RAM-движок. Поиск по
    unit устойчив к переименованию движков и алиасам — пока единицы измерения
    остаются "GFLOPS" и "GB/s".
    """
    for r in parallel.results:
        if r.throughput_unit == unit and r.throughput is not None and r.throughput > 0:
            return float(r.throughput)
    return None


def compute_r_thermal(
    t_max: float,
    tjmax: float,
    headroom_reference: float = 30.0,
    alpha: float = 0.5,
    floor: float = 0.50,
    cap: float = 1.15,
) -> float:
    """Множитель теплового headroom (research `stress_test_mark_method.md` §5.1).

    Формула::

        headroom = (TJmax - T_max) / headroom_reference
        raw = 1.0 + alpha * (headroom - 1.0)
        r_thermal = clamp(raw, floor, cap)

    Параметры по умолчанию из research:
    - ``headroom_reference = 30 °C`` — эталонный запас (ambient летом +10,
      деградация TIM +5, пыль +5, тепловая инерция +5–10).
    - ``alpha = 0.5`` — крутизна реакции (±15% бонус, до 50% штраф).
    - ``floor = 0.50``, ``cap = 1.15`` — асимметричный диапазон: троттлинг
      реальный риск, а ультра-холод лишь комфорт без пропорциональной отдачи.

    Pure-функция: одинаковые входы → один и тот же выход.
    """
    headroom = (tjmax - t_max) / headroom_reference
    raw = 1.0 + alpha * (headroom - 1.0)
    return max(floor, min(cap, raw))


def compute_stress_score_context(
    *,
    system_info: SystemInfo,
    parallel: ParallelStressResult,
    thermal: ThermalStabilityResult,
    duration_sec: float,
) -> StressScoreContext:
    """Собрать измеренные, Roofline-пики, r_thermal и итоговый балл.

    Pure-функция, без сайд-эффектов: одинаковые входы → одинаковый выход.

    Возвращает заполненный ``StressScoreContext``. Если хотя бы один из четырёх
    ratio (DGEMM, STREAM, стабильность, thermal headroom) не вычислился — или
    ``duration_sec < MIN_DURATION_FOR_SCORE_SEC`` — ``stress_score=None``.
    Рендер в этом случае не показывает плашку, а под Сводкой остаётся блок
    «недоступно» с конкретной причиной.
    """
    dgemm_gflops = _find_throughput(parallel, "GFLOPS")
    stream_gb_s = _find_throughput(parallel, "GB/s")
    stability_pct = thermal.frame_rate_stability_pct
    t_max_c = thermal.temp_max_c

    # Roofline DGEMM пик (GFLOPS, double precision — тот же dtype, что в движке).
    dgemm_peak = compute_flops_peak(system_info, "dp")

    # Roofline STREAM пик. compute_dram_peak возвращает MB/s — нормируем в GB/s.
    dram_peak_mb_s = compute_dram_peak(system_info)
    stream_peak_gb_s = dram_peak_mb_s / 1000.0 if dram_peak_mb_s else None

    # TJmax по семейству CPU (для r_thermal).
    tjmax_c = resolve_tjmax(system_info)

    r_dgemm = (
        dgemm_gflops / dgemm_peak
        if dgemm_gflops is not None and dgemm_peak and dgemm_peak > 0
        else None
    )
    r_stream = (
        stream_gb_s / stream_peak_gb_s
        if stream_gb_s is not None and stream_peak_gb_s and stream_peak_gb_s > 0
        else None
    )
    r_stability = stability_pct / 100.0 if stability_pct is not None else None

    # r_thermal требует обоих: T_max и TJmax. Гейта по длительности
    # больше нет — короткие прогоны дают приближённую оценку, UI
    # помечает это warning (см. RELIABLE_DURATION_SEC и render.py).
    r_thermal = (
        compute_r_thermal(t_max_c, float(tjmax_c))
        if t_max_c is not None and tjmax_c is not None
        else None
    )

    stress_score = compute_stress_score(r_dgemm, r_stream, r_stability, r_thermal)

    # Метаданные для пояснительной строки в рендере. Не влияют на балл.
    simd = detect_simd_level(system_info.cpu_model, system_info.cpu_arch)
    clock = _max_clock_ghz(system_info)
    dram_info = _detect_dram(system_info)
    dram_mts = dram_info[0] if dram_info else None
    dram_modules = dram_info[1] if dram_info else None

    return StressScoreContext(
        dgemm_gflops=dgemm_gflops,
        stream_gb_s=stream_gb_s,
        stability_pct=stability_pct,
        t_max_c=t_max_c,
        dgemm_peak_gflops=dgemm_peak,
        stream_peak_gb_s=stream_peak_gb_s,
        tjmax_c=tjmax_c,
        r_dgemm=r_dgemm,
        r_stream=r_stream,
        r_stability=r_stability,
        r_thermal=r_thermal,
        stress_score=stress_score,
        duration_sec=duration_sec,
        simd_level=simd,
        clock_ghz=clock,
        physical_cores=system_info.cpu_cores.physical,
        dram_mts=dram_mts,
        dram_modules=dram_modules,
    )


STRESS_SCORE_SCALE = 10_000.0
"""Множитель шкалы стресс-балла. С 4-компонентной формулой и cap=1.15
для r_thermal максимально возможный балл ≈ 11 500 (когда все r_* = 1.0
и r_thermal на потолке). Типичный десктоп — 1500–6000.
См. `docs/stress_score.md`.
"""


def compute_stress_score(
    r_dgemm: float | None,
    r_stream: float | None,
    r_stability: float | None,
    r_thermal: float | None,
) -> float | None:
    """STRESS_SCORE_SCALE × GM(r_dgemm, r_stream, r_stability, r_thermal).

    GM выбран (а не HM как внутри scoring v2 категорий) потому что здесь мы
    агрегируем разные по природе подсистемы (compute / memory / cooling /
    thermal headroom) — тот же подход, что и `R_overall = GM(R_MEM,
    R_CPU_compute)` в scoring.py.

    Если хоть один ratio = None → балл не строится. Без CPU temp r_thermal=None
    → балл недоступен (строгий режим, см. research §1: оценка sustainable
    performance невозможна без thermal headroom).
    """
    components = (r_dgemm, r_stream, r_stability, r_thermal)
    if any(r is None or r <= 0 for r in components):
        return None
    r_overall = geometric_mean([r_dgemm, r_stream, r_stability, r_thermal])  # type: ignore[list-item]
    return STRESS_SCORE_SCALE * r_overall


__all__ = [
    "RELIABLE_DURATION_SEC",
    "STRESS_SCORE_SCALE",
    "StressScoreContext",
    "compute_r_thermal",
    "compute_stress_score",
    "compute_stress_score_context",
]
