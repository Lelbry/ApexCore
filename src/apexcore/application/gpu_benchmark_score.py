"""Балл GPU-бенчмарка — детерминированный композитный score (Roofline).

Спецификация: ``new-app/docs/gpu_benchmark.md``.

Идея — GPU-аналог :mod:`general_benchmark_score`: безразмерные ratio
``measured / roofline_peak`` по двум осям — вычисления (FP32) и пропускная
способность видеопамяти (VRAM) — агрегируются через геометрическое среднее
(Fleming-Wallace 1986, Williams 2009) и масштабируются в шкалу ×10 000.

Ключевое отличие от общей оценки: в headline-балл входят **ровно два**
множителя. FP64 и PCIe-копирование измеряются оркестратором и показываются
сырыми скоростями, но в GM **не участвуют** (см. ``gpu_benchmark.md`` §7):
на потребительских GeForce (FP64 = 1/64) и встроенных Intel/AMD iGPU
двойная точность намеренно урезана, её включение занизило бы игровую карту
несправедливо. PCIe — характеристика шины/платформы, а не самой карты.

Шкала и интерпретация (та же, что у общей оценки и стресс-балла — «бок о
бок»):
- 10 000 = карта достигает архитектурного пика по обеим осям (недостижимо)
- 6500–8000 = дискретная карта эффективно грузит ALU и VRAM
- 5000–6500 = хорошая дискретная / сильный iGPU
- 3000–5000 = встроенная графика / слабая дискретная
- < 3000 = сильно ограниченный / виртуальный GPU

Как и в общей оценке, каждый ratio clamp'ится сверху единицей: boost-частота
драйвера / vBIOS / заводской разгон могут дать измеренную производительность
выше нашего табличного пика; без clamp это дало бы ``score > 10 000`` и
сломало бы шкалу (``gpu_benchmark.md`` §3.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from apexcore.application.scoring import geometric_mean

GPU_BENCHMARK_SCALE = 10_000.0
"""Множитель шкалы GPU-балла. Та же шкала, что у общей оценки
(``GENERAL_BENCHMARK_SCALE``) и стресс-балла (``STRESS_SCORE_SCALE``), чтобы
пользователь мог сравнивать CPU-, стресс- и GPU-метрику в одних попугаях.
"""


@dataclass
class GpuBenchmarkScoreContext:
    """Сводный контекст: измеренные значения, пики, ratio и итоговый балл.

    Все поля опциональны — если фаза не выполнилась или архитектурный пик
    для устройства неизвестен, соответствующий ratio = ``None``. Если None
    оказался ``r_fp32`` или ``r_mem`` — итоговый ``score`` = ``None`` (FP64
    вне балла, поэтому его отсутствие score не обнуляет).
    """

    # Измерения за прогон.
    fp32_gflops: float | None = None
    fp64_gflops: float | None = None
    mem_bandwidth_gb_s: float | None = None
    pcie_h2d_gb_s: float | None = None
    pcie_d2h_gb_s: float | None = None

    # Roofline-пики (детерминированы по конфигу устройства).
    fp32_peak_gflops: float | None = None
    fp64_peak_gflops: float | None = None
    mem_bandwidth_peak_gb_s: float | None = None

    # Безразмерные ratio после clamp (0..1).
    r_fp32: float | None = None
    r_fp64: float | None = None  # информационный, в score не входит
    r_mem: float | None = None

    # Финальный балл (``GPU_BENCHMARK_SCALE × GM(r_fp32, r_mem)``).
    score: float | None = None

    # Метаданные для UI / БД.
    device_name: str | None = None
    arch: str | None = None
    peak_source: str | None = None


def _clamp_ratio(r: float | None) -> float | None:
    """Ограничить ratio сверху единицей. None → None, нули/отрицательные → None.

    Clamp ≤ 1.0 не штрафует топовое железо, у которого измеренный результат
    превышает наш консервативный табличный пик (boost-частота драйвера,
    заводской разгон). Симметрично :func:`general_benchmark_score._clamp_ratio`.
    """
    if r is None:
        return None
    if r <= 0:
        return None
    return min(r, 1.0)


def compute_gpu_benchmark_score(
    r_fp32: float | None,
    r_mem: float | None,
) -> float | None:
    """``GPU_BENCHMARK_SCALE × GM(r_fp32, r_mem)`` либо ``None``.

    Оба ratio должны быть положительными и не-None. Иначе балл = ``None`` —
    та же строгая семантика, что в общей оценке (``gpu_benchmark.md`` §2):
    неполный прогон (нет FP32-замера / нет пика VRAM для устройства) нельзя
    сравнивать с полным. FP64 и PCIe в формулу **не входят**, поэтому их
    отсутствие балл не обнуляет — здесь принимаются только два множителя.

    Перед агрегацией каждый ratio дополнительно clamp'ится к ``[0, 1]`` —
    симметрия по обеим осям, чтобы boost / разгон не задирали GM выше 10 000.
    """
    r_fp32 = _clamp_ratio(r_fp32)
    r_mem = _clamp_ratio(r_mem)
    if r_fp32 is None or r_mem is None:
        return None
    r_overall = geometric_mean([r_fp32, r_mem])
    return GPU_BENCHMARK_SCALE * r_overall


__all__ = [
    "GPU_BENCHMARK_SCALE",
    "GpuBenchmarkScoreContext",
    "_clamp_ratio",
    "compute_gpu_benchmark_score",
]
