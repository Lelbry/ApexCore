"""Балл «Оценок общей производительности» — детерминированный композитный score.

Спецификация: ``docs/general_benchmark.md``.

Идея — та же, что и у :mod:`stress_score`: безразмерные ratio
``measured / roofline_peak``, агрегированные через геометрическое среднее
(Fleming-Wallace 1986, Williams 2009), масштабированные в шкалу ×10 000.
Отличие — этот балл **не учитывает термальную стабильность**: мы измеряем
не «выживает ли система под нагрузкой», а «сколько она может выдать на
коротком прогоне». GPU-compute в первой итерации не входит.

Шкала и интерпретация:
- 10 000 = система достигает теоретических пиков (на практике недостижимо)
- 7000–8000 = очень мощная актуальная конфигурация
- 4000–6000 = типовой современный десктоп
- < 3000 = слабая / устаревшая / виртуальная среда

Конкретные числа в формулу заложены через clamp ``ratio ≤ 1.0`` — это не
штрафует топовое железо (NVMe Gen5, AVX-512 не различаемые heuristic'ом).
"""

from __future__ import annotations

from dataclasses import dataclass

from apexcore.application.scoring import geometric_mean

GENERAL_BENCHMARK_SCALE = 10_000.0
"""Множитель шкалы общего балла. Та же шкала, что у стресс-балла
(``STRESS_SCORE_SCALE``), чтобы пользователь мог сравнивать оба значения
«бок о бок» и видеть, как сильно cooling/throttling сажает мощность.
"""


@dataclass
class GeneralBenchmarkScoreContext:
    """Сводный контекст: измеренные значения, пики, ratio и итоговый балл.

    Все поля опциональны — если какой-то источник не вычислился (например,
    нет roofline для текущего CPU или disk-фаза пропущена из-за нехватки
    места), соответствующий ratio = ``None``, итоговый score = ``None``.
    """

    # Измерения за прогон.
    dgemm_gflops: float | None = None
    stream_gb_s: float | None = None
    disk_seq_read_mb_s: float | None = None
    disk_random_read_mb_s: float | None = None
    disk_seq_write_mb_s: float | None = None

    # Roofline-пики (детерминированы по конфигу системы).
    dgemm_peak_gflops: float | None = None
    stream_peak_gb_s: float | None = None
    disk_seq_read_peak_mb_s: float | None = None
    disk_random_read_peak_mb_s: float | None = None
    disk_seq_write_peak_mb_s: float | None = None

    # Безразмерные ratio после clamp (0..1).
    r_dgemm: float | None = None
    r_stream: float | None = None
    r_disk: float | None = None

    # Финальный балл (``GENERAL_BENCHMARK_SCALE × GM(r_dgemm, r_stream, r_disk)``).
    score: float | None = None

    # Метаданные для UI / БД.
    disk_media_label: str | None = None     # "NVMe" / "SATA SSD" / "HDD"
    boot_drive_path: str | None = None       # "C:\\" / "/"


def _clamp_ratio(r: float | None) -> float | None:
    """Ограничить ratio сверху единицей. None → None, нули/отрицательные → None.

    Clamp ≤ 1.0 не штрафует топовое железо, у которого реальный результат
    превышает наш консервативный peak (например, NVMe Gen5 vs peak 3500 MB/s).
    """
    if r is None:
        return None
    if r <= 0:
        return None
    return min(r, 1.0)


def _disk_ratio_from_components(
    seq_read_ratio: float | None,
    random_read_ratio: float | None,
    seq_write_ratio: float | None,
) -> float | None:
    """``r_disk = GM(seq_read, random_read, seq_write)`` после clamp каждого.

    Если хотя бы один из трёх компонентов отсутствует → ``None``. Это
    осознанное решение: «диск без write-замера» = неполное измерение,
    лучше показать «нет данных», чем приукрасить балл.
    """
    components = [
        _clamp_ratio(seq_read_ratio),
        _clamp_ratio(random_read_ratio),
        _clamp_ratio(seq_write_ratio),
    ]
    if any(c is None for c in components):
        return None
    return geometric_mean([c for c in components if c is not None])  # type: ignore[misc]


def compute_general_benchmark_score(
    r_dgemm: float | None,
    r_stream: float | None,
    r_disk: float | None,
) -> float | None:
    """``GENERAL_BENCHMARK_SCALE × GM(r_dgemm, r_stream, r_disk)`` либо ``None``.

    Все три ratio должны быть положительными и не-None. Иначе балл = ``None``
    (см. :mod:`stress_score` — та же семантика): пользователю «4500» при
    отсутствующем r_disk не даёт сравнимой с другими прогонами цифры.

    Перед агрегацией каждый ratio дополнительно clamp'ится к ``[0, 1]`` —
    это симметрия для всех подсистем, чтобы топовый CPU / NVMe / DRAM не
    задирали GM выше 10 000.
    """
    r_dgemm = _clamp_ratio(r_dgemm)
    r_stream = _clamp_ratio(r_stream)
    r_disk = _clamp_ratio(r_disk)
    if r_dgemm is None or r_stream is None or r_disk is None:
        return None
    r_overall = geometric_mean([r_dgemm, r_stream, r_disk])
    return GENERAL_BENCHMARK_SCALE * r_overall


__all__ = [
    "GENERAL_BENCHMARK_SCALE",
    "GeneralBenchmarkScoreContext",
    "_clamp_ratio",
    "_disk_ratio_from_components",
    "compute_general_benchmark_score",
]
