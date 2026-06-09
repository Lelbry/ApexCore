"""Модели предметной области (Pydantic v2).

Здесь описано всё, чем оперирует ядро apexcore: характеристики системы, единичный
снимок телеметрии, конфигурация и результат прогона бенчмарка, а также
производные модели для статистики и диагностики (BaselineProfile, NormalizedScore,
Diagnostic). Зависимости — только pydantic, никаких внешних эффектов.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

# ─────────────────────────── Аппаратная конфигурация ────────────────────────────


class CpuCores(BaseModel):
    """Количество физических и логических ядер CPU.

    Опциональные поля ``p_cores``/``e_cores``/``p_threads``/``e_threads``
    заполняются только для гибридных архитектур (Intel 12th Gen+ с
    Performance- и Efficient-ядрами). Для AMD/обычных Intel остаются ``None``,
    рендер тогда использует только ``physical``/``logical``.
    """

    physical: int = Field(..., description="Количество физических ядер CPU.")
    logical: int = Field(..., description="Количество логических процессоров (потоков).")
    p_cores: int | None = Field(default=None, description="Число P-cores (Intel hybrid).")
    e_cores: int | None = Field(default=None, description="Число E-cores (Intel hybrid).")
    p_threads: int | None = Field(default=None, description="Число потоков на P-cores.")
    e_threads: int | None = Field(default=None, description="Число потоков на E-cores.")


class SystemInfo(BaseModel):
    """Снимок ключевых характеристик ОС и оборудования хоста."""

    os_name: str = Field(..., description="Название операционной системы.")
    os_version: str = Field(..., description="Версия операционной системы.")
    cpu_model: str = Field(..., description="Модель CPU.")
    cpu_cores: CpuCores = Field(..., description="Физические и логические ядра.")
    ram_total_gb: float = Field(..., description="Общий объём RAM, ГБ.")
    gpu_list: list[str] = Field(default_factory=list, description="Список обнаруженных GPU.")
    cpu_arch: str | None = Field(default=None, description="Архитектура CPU (x86_64, aarch64).")
    hostname: str | None = Field(default=None, description="Имя хоста.")
    cpu_base_mhz: float | None = Field(
        default=None, description="Базовая частота CPU (МГц), для non-hybrid или средняя."
    )
    cpu_base_p_mhz: float | None = Field(
        default=None, description="Базовая частота P-ядер (МГц), только для hybrid Intel."
    )
    cpu_base_e_mhz: float | None = Field(
        default=None, description="Базовая частота E-ядер (МГц), только для hybrid Intel."
    )
    timestamp: datetime = Field(..., description="Момент сбора сведений.")


# ─────────────────────────── Метрики и стресс ───────────────────────────────────


class MetricSnapshot(BaseModel):
    """Точечный снимок утилизации ресурсов и показаний сенсоров."""

    timestamp: datetime = Field(..., description="Момент снятия отсчёта.")
    cpu_percent: float = Field(..., description="Загрузка CPU (0–100).")
    cpu_per_core_percent: list[float] = Field(
        default_factory=list, description="Загрузка по логическим ядрам, %."
    )
    ram_percent: float = Field(..., description="Загрузка RAM (0–100).")
    ram_used_gb: float = Field(default=0.0, description="Используемая RAM, ГБ.")
    disk_read_mb: float = Field(default=0.0, description="Прочитано с дисков с предыдущего отсчёта, МБ.")
    disk_write_mb: float = Field(default=0.0, description="Записано на диски с предыдущего отсчёта, МБ.")
    temperatures: dict[str, float] = Field(
        default_factory=dict,
        description="Температуры по сенсорам (°C). Ключ — имя сенсора.",
    )
    frequencies: dict[str, float] = Field(
        default_factory=dict,
        description="Частоты CPU по ядрам, МГц. Ключи: 'cpu_avg', 'cpu_min', 'cpu_max', 'core_<n>'.",
    )
    voltages: dict[str, float] = Field(
        default_factory=dict,
        description="Напряжения по сенсорам (В). Ключ — имя сенсора (например, 'cpu/cpu_core').",
    )
    cpu_throttled: bool = Field(default=False, description="Признак тротлинга CPU на этом отсчёте.")
    power_w: float | None = Field(default=None, description="Потребляемая мощность, Вт (если доступно).")


class StressResult(BaseModel):
    """Результат запуска одного стресс-движка."""

    engine: str = Field(..., description="Имя движка (например builtin_cpu_int).")
    category: str = Field(..., description="Категория нагрузки: cpu_int, cpu_fp, ram_bw, ram_lat.")
    duration_actual_sec: float = Field(..., description="Фактическая длительность, с.")
    throughput: float = Field(..., description="Пропускная способность нагрузки.")
    throughput_unit: str = Field(..., description="Единицы измерения throughput (ops/s, GB/s, ns/access).")
    error_count: int = Field(default=0, description="Количество ошибок (ненулевой код, NaN и т.п.).")
    threads: int = Field(default=1, description="Количество параллельных потоков нагрузки.")
    raw_output: str | None = Field(default=None, description="Сырой вывод (для внешних утилит).")
    extra: dict[str, Any] = Field(default_factory=dict, description="Прочие параметры/метрики.")


# ─────────────────────────── Конфигурация бенчмарка ─────────────────────────────


class BenchmarkConfig(BaseModel):
    """Параметры запуска бенчмарка."""

    profile_name: str = Field(..., description="Имя профиля нагрузки.")
    duration_sec: float = Field(..., description="Длительность каждой стресс-фазы, с.")
    sampling_rate_sec: float = Field(default=0.5, description="Интервал между отсчётами телеметрии, с.")
    engines: list[str] = Field(
        default_factory=list,
        description="Список имён стресс-движков. Пусто = выбрать по профилю автоматически.",
    )
    threads: int | None = Field(default=None, description="Количество потоков; None — все логические ядра.")
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Веса категорий для композитного балла (cpu_int, cpu_fp, ram_bw, ram_lat).",
    )
    baseline_id: UUID | None = Field(default=None, description="UUID baseline для сравнения; опционально.")


class BenchmarkResult(BaseModel):
    """Полный результат прогона бенчмарка."""

    id: UUID = Field(default_factory=uuid4, description="Уникальный идентификатор прогона.")
    system_info: SystemInfo = Field(..., description="Информация о системе на момент прогона.")
    config: BenchmarkConfig = Field(..., description="Использованная конфигурация.")
    start_time: datetime = Field(..., description="Время старта.")
    end_time: datetime = Field(..., description="Время окончания.")
    metrics_history: list[MetricSnapshot] = Field(
        default_factory=list, description="Хронологические снимки телеметрии."
    )
    stress_results: list[StressResult] = Field(
        default_factory=list, description="Результаты стресс-фаз."
    )
    final_score: float = Field(
        default=0.0,
        description=(
            "Устаревшее поле scoring v1 (composite_score). "
            "В scoring v2 не заполняется (всегда 0.0); реальный балл живёт в "
            "MicroBenchSuiteResult.overall. Поле сохранено для обратной совместимости JSON."
        ),
    )
    status: str = Field(default="completed", description="Статус прогона: completed, failed, cancelled.")
    thermal: ThermalStabilityResult | None = Field(
        default=None,
        description=(
            "Метрика стабильности под нагрузкой (UL 3DMark стиль, см. docs/scoring_v2.md §7). "
            "Заполняется StabilityService для длительных прогонов; для коротких — None."
        ),
    )


# ─────────────────────────── Базовые профили и нормализация ─────────────────────


class BaselineProfile(BaseModel):
    """Эталонный профиль для нормализации и сравнения.

    Хранит статистики (mean, std, n) по подметрикам, накопленные по нескольким
    «здоровым» прогонам той же конфигурации.
    """

    id: UUID = Field(default_factory=uuid4, description="UUID профиля.")
    name: str = Field(..., description="Человекочитаемое имя.")
    profile_name: str = Field(..., description="Имя профиля бенчмарка, к которому привязан baseline.")
    system_fingerprint: str = Field(
        ..., description="Хеш ключевых полей SystemInfo для сопоставимости."
    )
    means: dict[str, float] = Field(default_factory=dict, description="Среднее по подметрикам.")
    stds: dict[str, float] = Field(default_factory=dict, description="Стандартное отклонение.")
    sample_size: int = Field(..., description="Количество прогонов в выборке.")
    raw_samples: dict[str, list[float]] = Field(
        default_factory=dict,
        description="Сырые наблюдения по подметрикам (для пересчёта и стат. тестов).",
    )
    created_at: datetime = Field(..., description="Когда сформирован baseline.")


class NormalizedScore(BaseModel):
    """Нормализованный итоговый балл с раскладкой по подсистемам."""

    composite: float = Field(..., description="Итоговый композитный балл.")
    subscores: dict[str, float] = Field(
        default_factory=dict,
        description="Балл по подсистемам (cpu_int, cpu_fp, ram_bw, ram_lat, thermal_stability...).",
    )
    method: str = Field(..., description="Метод нормализации: 'min_max' или 'z_score'.")
    weights: dict[str, float] = Field(default_factory=dict, description="Применённые веса.")


# ─────────────────────────── Диагностика ────────────────────────────────────────


class DiagnosticSeverity(str, Enum):
    """Уровень критичности диагностического сообщения."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Diagnostic(BaseModel):
    """Сообщение стат-движка диагностики."""

    model_config = ConfigDict(use_enum_values=True)

    code: str = Field(..., description="Машинный код причины (например 'cpu_thermal_throttle').")
    severity: DiagnosticSeverity = Field(..., description="Уровень критичности.")
    message: str = Field(..., description="Человекочитаемое сообщение для пользователя.")
    metric: str | None = Field(default=None, description="Связанная подметрика, если применимо.")
    p_value: float | None = Field(default=None, description="p-value стат-теста, если применимо.")
    effect_size: float | None = Field(default=None, description="Cohen's d, если применимо.")
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Подтверждающие данные: значения метрик, пороги, статистики.",
    )
    recommendation: str | None = Field(default=None, description="Рекомендуемое действие.")


# ─────────────────────────── Микробенчмарки (AIDA64-style) ──────────────────────


class MicroBenchResult(BaseModel):
    """Результат одного микробенчмарка (AIDA64-style тест: Memory, FLOPS, IOPS,
    AES, SHA-1, фрактальные нагрузки и т.п.).

    В отличие от ``StressResult`` (длительная нагрузка, оценка стабильности),
    микробенчмарк меряет короткими прогонами пиковую пропускную способность
    одной фундаментальной операции.
    """

    name: str = Field(..., description="Имя теста (например memory_read, flops_sp).")
    category: str = Field(
        ...,
        description="Категория: memory, flops, integer, crypto, fractal.",
    )
    value: float = Field(..., description="Значение метрики (см. unit).")
    unit: str = Field(..., description="Единица: MB/s, GFLOPS, GIOPS, FPS.")
    duration_actual_sec: float = Field(..., description="Фактическая длительность теста, с.")
    iterations: int = Field(default=1, description="Сколько раз был выполнен внутренний цикл.")
    threads: int = Field(default=1, description="Количество параллельных потоков.")
    backend: str = Field(default="cpu", description="Бэкенд: cpu, gpu (на будущее).")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Дополнительные параметры (размер буфера, dtype, JIT и т.п.).",
    )
    error: str | None = Field(default=None, description="Сообщение об ошибке, если тест упал.")


class SingleMultiResult(BaseModel):
    """Результат сравнения «один поток на P-ядре» vs «все потоки CPU».

    Используется отдельным пунктом меню «Тест Single-Core / Multi-Core» в
    «Расширенное тестирование процессора». Дает наглядную оценку
    масштабирования (speedup и efficiency) одного и того же бенчмарка
    при разной параллельности.
    """

    bench_name: str = Field(..., description="Имя движка бенча (например, int64_iops).")
    duration_sec_per_test: float = Field(
        ..., description="Целевая длительность одного замера (Single или Multi), сек."
    )
    single: MicroBenchResult = Field(
        ..., description="Результат с threads=1, прибитый к одному CPU."
    )
    multi: MicroBenchResult = Field(
        ..., description="Результат с threads=N (все логические CPU), без affinity."
    )
    cores_used_multi: int = Field(
        ..., description="Сколько логических CPU использовалось в multi-замере."
    )
    physical_cores: int | None = Field(
        default=None,
        description="Общее число физических ядер CPU (для подписи 'все ядра').",
    )
    physical_p_cores: int | None = Field(
        default=None, description="Физические P-ядра (Intel hybrid)."
    )
    physical_e_cores: int | None = Field(
        default=None, description="Физические E-ядра (Intel hybrid)."
    )
    pinned_cpu: int | None = Field(
        default=None,
        description="Логический CPU, к которому прибит Single. None если affinity недоступна.",
    )
    pinned_kind: str | None = Field(
        default=None,
        description="Тип ядра для Single: 'P-core', 'E-core' или None (не hybrid).",
    )

    @property
    def speedup(self) -> float | None:
        """Multi / Single. None если одно из значений 0 или ошибка."""
        if self.single.value <= 0 or self.multi.value <= 0:
            return None
        return self.multi.value / self.single.value

    @property
    def efficiency(self) -> float | None:
        """Speedup / cores_used. 1.0 = идеальное масштабирование."""
        sp = self.speedup
        if sp is None or self.cores_used_multi <= 0:
            return None
        return sp / self.cores_used_multi


class MicroBenchSuiteResult(BaseModel):
    """Результат прогона всего набора микробенчмарков (одна команда `micro run`)."""

    id: UUID = Field(default_factory=uuid4, description="Уникальный идентификатор прогона.")
    system_info: SystemInfo = Field(..., description="Сведения о системе на момент прогона.")
    results: list[MicroBenchResult] = Field(
        default_factory=list, description="Результаты всех тестов набора."
    )
    start_time: datetime = Field(..., description="Время старта набора.")
    end_time: datetime = Field(..., description="Время окончания набора.")
    duration_sec_per_test: float = Field(
        ..., description="Целевая длительность одного теста (запрошенная пользователем)."
    )
    threads: int = Field(default=0, description="Запрошенное число потоков (0 = авто).")
    # Поля v2 scoring (см. docs/scoring_v2.md). Заполняются ScoringService после
    # агрегации одного или нескольких прогонов; для simple `micro run` без
    # пресета остаются None.
    overall: OverallScore | None = Field(
        default=None,
        description="Итоговый балл по scoring v2 (Roofline + HM/GM). None для legacy/standalone прогонов.",
    )
    preset: str | None = Field(
        default=None,
        description="Пресет точности: fast/standard/accurate. None если запуск без пресета.",
    )
    n_runs: int = Field(
        default=1,
        description="Сколько прогонов всех тестов было агрегировано в этот результат (>=1).",
    )


# ─────────────────────────── Scoring v2 ─────────────────────────────────────────


class OverallScore(BaseModel):
    """Итоговый балл общей оценки производительности по scoring v2.

    Спецификация: ``docs/scoring_v2.md``. Принципы:
    - Шкала ×1000: ``overall_score = 1000 · overall_ratio``.
    - ``overall_ratio = 1.0`` означает 100% теоретического архитектурного пика
      (Roofline-модель, Williams 2009).
    - Иерархия: HM подтестов внутри категории → GM категорий внутри подсистемы
      → взвешенное GM подсистем.
    - Subscores раскладывают балл по 5 категориям + 2 подсистемам.

    Сравнение баллов разных ``scoring_version`` запрещено (см. §8 спецификации).
    """

    model_config = ConfigDict(extra="forbid")

    overall_ratio: float = Field(
        ...,
        description="Безразмерное отношение к Roofline-эталону (1.0 = 100% пика).",
    )
    overall_score: float | None = Field(
        default=None,
        description=(
            "DEPRECATED (удалён из CLI/Web в 0.9.x): единый «итоговый балл» "
            "micro-прогона. Раздел «Расш. тест процессора» — это детальный "
            "per-category анализ (см. subscores), а не системный балл; единую "
            "оценку дают только Стресс-тест / Общая оценка / Winsat. Поле "
            "сохранено как Optional ради чтения старых записей БД (extra=forbid); "
            "новые прогоны его не заполняют."
        ),
    )
    subscores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Подскоры по категориям и подсистемам: r_memory, r_flops, r_integer, "
            "r_crypto, r_fractal, R_MEM, R_CPU_compute."
        ),
    )
    ci_lower: float | None = Field(
        default=None,
        description="Нижняя граница 95% CI для overall_ratio (ratio-шкала). None если n<2.",
    )
    ci_upper: float | None = Field(
        default=None,
        description="Верхняя граница 95% CI для overall_ratio (ratio-шкала). None если n<2.",
    )
    ci_method: str | None = Field(
        default=None,
        description="Метод CI: 't_logscale' / 'bootstrap' / 'median_of_3' / None.",
    )
    n_runs: int = Field(
        default=1,
        description="Количество прогонов, по которым посчитан балл.",
    )
    reference_id: str = Field(
        default="roofline-v1",
        description="Идентификатор reference set (см. references.py).",
    )
    weights_profile: str = Field(
        default="default",
        description="Имя профиля весов из data/weights/.",
    )
    scoring_version: str = Field(
        default="2.0.0",
        description="Версия формулы scoring (см. docs/scoring_v2.md §8).",
    )
    provisional: bool = Field(
        default=False,
        description="True если reference не финализирован (empirical proxy с n<10).",
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Машинно-читаемые предупреждения: 'no_ci_n1', 'roofline_partial', "
            "'roofline_unavailable', 'workload_skipped:<id>', и т.п."
        ),
    )


class ThermalStabilityResult(BaseModel):
    """Результат отдельной метрики стабильности под нагрузкой.

    Считается во время 10-минутного теста стабильности (не в общей оценке).
    Образец — UL 3DMark Stress Test: Frame-Rate-Stability ≥ 97% = pass.
    Источник методики: docs/scoring_v2.md §7.
    """

    model_config = ConfigDict(extra="forbid")

    frame_rate_stability_pct: float | None = Field(
        default=None,
        description="100·min(cpu_avg)/max(cpu_avg) по telemetry. None если нет частот.",
    )
    pass_threshold_97: bool | None = Field(
        default=None,
        description="True если frame_rate_stability_pct >= 97. None если нет данных.",
    )
    tsc: float | None = Field(
        default=None,
        description="Thermal Sensitivity Coefficient = (S_cold - S_steady) / S_cold.",
    )
    clock_min_mhz: float | None = Field(default=None, description="Минимум cpu_avg за прогон.")
    clock_max_mhz: float | None = Field(default=None, description="Максимум cpu_avg за прогон.")
    temp_max_c: float | None = Field(default=None, description="Максимум температуры за прогон.")
    temp_avg_c: float | None = Field(default=None, description="Среднее по температурам.")
    throttle_observed: bool = Field(
        default=False,
        description="Зафиксирован ли cpu_throttled в любом снапшоте.",
    )
    samples: int = Field(default=0, description="Сколько MetricSnapshot обработано.")
