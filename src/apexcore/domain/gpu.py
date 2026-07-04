"""Доменные модели GPU-бенчмарка (кроссвендорный OpenCL-путь).

Отдельный файл — по тем же причинам, что и :mod:`domain.general_benchmark`:
шкала (×10 000), ratio и компоненты специфичны для GPU и не пересекаются с
публичным контрактом :mod:`domain.models`.

Методика — Roofline: измеренная производительность делится на
*архитектурный* пик (число вычислительных блоков × частота × операций на
блок за такт), результат — доля от потолка в шкале ×10 000. Headline-балл
считается по вычислениям (FP32) и пропускной способности VRAM; FP64 —
информационный тест (на потребительских/встроенных GPU он намеренно урезан
и не должен занижать общий балл). Спецификация: ``docs/gpu_benchmark.md``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from apexcore.domain.models import SystemInfo


class GpuWorkloadKind(str, Enum):
    """Тип измеряемой GPU-нагрузки.

    Значения используются и бэкендом (что запускать), и оркестратором
    (какие фазы прогонять), и стресс-движком (:data:`SUSTAINED_STRESS`).
    """

    FP32 = "fp32"                      # пиковые FP32 FMA (GFLOPS)
    FP64 = "fp64"                      # пиковые FP64 FMA (GFLOPS), может отсутствовать
    MEM_BANDWIDTH = "mem_bandwidth"    # пропускная способность VRAM (STREAM-triad, GB/s)
    PCIE_H2D = "pcie_h2d"              # host→device копирование (GB/s)
    PCIE_D2H = "pcie_d2h"              # device→host копирование (GB/s)
    SUSTAINED_STRESS = "sustained_stress"  # длительная максимальная нагрузка (для термотеста)


class GpuDeviceType(str, Enum):
    """Класс GPU-устройства (для приоритизации и интерпретации балла)."""

    DISCRETE = "discrete"
    INTEGRATED = "integrated"
    VIRTUAL = "virtual"
    UNKNOWN = "unknown"


class GpuDeviceInfo(BaseModel):
    """Описание одного GPU-устройства, обнаруженного через OpenCL.

    Заполняется бэкендом (:class:`domain.ports.GpuComputeBackend`) из
    ``clGetDeviceInfo``. Поля ``compute_units`` / ``max_clock_mhz`` — входы
    для Roofline-пика; ``fp64_supported`` управляет тем, запускать ли FP64.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., description="Сквозной индекс устройства для выбора (0..N-1).")
    name: str = Field(..., description="Имя устройства, напр. 'NVIDIA GeForce RTX 4070 Ti'.")
    vendor: str = Field(default="", description="Вендор: NVIDIA / AMD / Intel / прочее.")
    platform_name: str = Field(default="", description="Имя OpenCL-платформы ('NVIDIA CUDA').")
    device_type: GpuDeviceType = Field(default=GpuDeviceType.UNKNOWN)
    opencl_version: str = Field(default="", description="Версия OpenCL устройства.")
    driver_version: str = Field(default="", description="Версия драйвера (если доступна).")

    compute_units: int = Field(default=0, description="Число вычислительных блоков (CU/SM/Xe-core).")
    max_clock_mhz: int = Field(default=0, description="Максимальная тактовая частота ядра, МГц.")
    global_mem_mb: int = Field(default=0, description="Объём глобальной памяти (VRAM), МиБ.")
    max_work_group_size: int = Field(default=0, description="Максимальный размер рабочей группы.")
    fp64_supported: bool = Field(default=False, description="Есть ли аппаратная поддержка FP64.")

    # Разрешённая архитектура (заполняет gpu_roofline при резолве пика).
    arch: str | None = Field(default=None, description="Ключ архитектуры: nvidia_ada / amd_rdna2 / ...")


class GpuMeasurement(BaseModel):
    """Результат одного измерения нагрузки на GPU.

    ``throughput`` в единицах ``unit`` (GFLOPS для FP32/FP64, GB/s для
    памяти/PCIe). ``error_count`` > 0 означает провал верификации
    (несовпадение контрольной суммы) — используется стресс-движком.
    """

    model_config = ConfigDict(extra="forbid")

    kind: GpuWorkloadKind = Field(..., description="Какая нагрузка измерялась.")
    throughput: float = Field(..., description="Пропускная способность в единицах unit.")
    unit: str = Field(..., description="'GFLOPS' или 'GB/s'.")
    duration_sec: float = Field(default=0.0, description="Фактическая длительность измерения.")
    iterations: int = Field(default=0, description="Число прогонов кернела за замер.")
    error_count: int = Field(default=0, description="Число несовпадений верификации (0 = ок).")
    extra: dict[str, float] = Field(default_factory=dict, description="Доп. метрики (work_done и т.п.).")


class GpuPeak(BaseModel):
    """Архитектурные (Roofline) пики устройства.

    Считает :func:`application.gpu_roofline.compute_gpu_peak` по
    ``GpuDeviceInfo`` + таблице ``data/gpu_arch.yaml``. ``None`` там, где
    архитектура/модель неизвестна и пик оценить нельзя (тогда
    соответствующий ratio и вклад в балл — тоже ``None``).
    """

    model_config = ConfigDict(extra="forbid")

    fp32_peak_gflops: float | None = Field(default=None)
    fp64_peak_gflops: float | None = Field(default=None)
    mem_bandwidth_peak_gb_s: float | None = Field(default=None)

    arch: str | None = Field(default=None, description="Разрешённый ключ архитектуры.")
    source: str = Field(default="unknown", description="Как получен пик: roofline / model_table / fallback.")
    notes: list[str] = Field(default_factory=list)


class GpuStressVerdict(str, Enum):
    """Вердикт стабильности GPU под длительной нагрузкой («термотест»).

    Аналог PASS/FAIL полного CPU-стресса, но с явным ``WARN`` (мягкая
    просадка/буст-settle — не провал) и ``UNKNOWN`` (телеметрия недоступна:
    нагрузку прогнали, но судить о стабильности не по чему).
    """

    PASS = "pass"        # частоты держатся, температура в норме, троттлинга нет
    WARN = "warn"        # заметный settle/умеренный нагрев — не провал, но обратить внимание
    FAIL = "fail"        # тепловой троттлинг: T достигла лимита или частота обвалилась
    UNKNOWN = "unknown"  # нет телеметрии (или прогон не состоялся) — судить не по чему


class GpuStressSample(BaseModel):
    """Один отсчёт GPU-телеметрии за секунду прогона (для UI-спарклайна).

    Компактный — только то, что рисуется на графике. Список таких отсчётов
    в отчёте ограничен сверху (см. ``GpuStressReport.samples``), чтобы JSON в
    БД не распухал на длинных прогонах. Любое поле может быть ``None``, если
    конкретный сенсор недоступен (например, мощность на iGPU).
    """

    model_config = ConfigDict(extra="forbid")

    t_sec: float = Field(..., description="Секунда прогона от старта нагрузки.")
    temp_c: float | None = Field(default=None, description="Температура ядра GPU, °C.")
    power_w: float | None = Field(default=None, description="Потребляемая мощность, Вт.")
    clock_mhz: float | None = Field(default=None, description="Частота ядра (graphics/SM), МГц.")
    util_pct: float | None = Field(default=None, description="Загрузка GPU, %.")


class GpuStressReport(BaseModel):
    """Отчёт GPU-стресс-теста (термостабильность, «power virus»).

    Аналог :class:`StressFinalReport` для CPU, но кроссвендорный и headless:
    длительная максимальная FP32-нагрузка (``SUSTAINED_STRESS``) + посекундная
    телеметрия (температура / мощность / частота / загрузка) + вердикт
    PASS/WARN/FAIL/UNKNOWN. Сохраняется как JSON вызывающим кодом (Screen /
    CLI / WebUI); оркестратор от persistence не зависит.

    Все серии свёрнуты в сводки (max/avg/min); ``samples`` — опциональный
    ограниченный список отсчётов для спарклайна. Поля телеметрии ``None``,
    если сенсор недоступен (тогда ``verdict=UNKNOWN`` + note про телеметрию).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="UUID прогона.")
    system_info: SystemInfo = Field(..., description="Снимок системы.")
    device: GpuDeviceInfo = Field(..., description="Тестируемое GPU-устройство.")
    started_at: datetime = Field(..., description="Момент начала прогона (UTC).")
    ended_at: datetime = Field(..., description="Момент окончания прогона (UTC).")

    duration_sec: float = Field(default=0.0, description="Фактическая длительность нагрузки, с.")
    requested_duration_sec: float = Field(default=0.0, description="Запрошенная длительность, с.")

    # ── Сводки по сериям телеметрии (None, если сенсор не отдал ни одного отсчёта). ──
    max_temp_c: float | None = Field(default=None, description="Пиковая температура за прогон, °C.")
    avg_temp_c: float | None = Field(default=None, description="Средняя температура за прогон, °C.")
    max_power_w: float | None = Field(default=None, description="Пиковая мощность, Вт.")
    avg_power_w: float | None = Field(default=None, description="Средняя мощность, Вт.")
    min_clock_mhz: float | None = Field(default=None, description="Минимальная наблюдённая частота ядра, МГц.")
    avg_clock_mhz: float | None = Field(default=None, description="Средняя частота ядра, МГц.")
    max_clock_mhz_observed: float | None = Field(
        default=None, description="Максимальная наблюдённая частота ядра (пик буста), МГц."
    )
    avg_util_pct: float | None = Field(default=None, description="Средняя загрузка GPU за прогон, %.")

    # ── Троттлинг / тепловой лимит. ──
    throttle_detected: bool = Field(default=False, description="Обнаружен ли тепловой троттлинг/просадка.")
    throttle_reasons: list[str] = Field(
        default_factory=list, description="Человекочитаемые причины (обвал частоты, T у лимита, обвал загрузки)."
    )
    thermal_limit_c: float | None = Field(
        default=None, description="Порог теплового замедления (NVML slowdown), °C — если известен."
    )

    verdict: GpuStressVerdict = Field(
        default=GpuStressVerdict.UNKNOWN, description="Итоговый вердикт стабильности."
    )
    notes: list[str] = Field(default_factory=list, description="Короткие человекочитаемые заметки.")
    cancelled: bool = Field(default=False, description="Был ли прогон отменён пользователем/таймаутом.")

    # Ограниченный набор отсчётов для спарклайна (может быть пустым).
    samples: list[GpuStressSample] = Field(
        default_factory=list, description="Прореженные отсчёты телеметрии для графика (bounded)."
    )
    samples_taken: int = Field(default=0, description="Сколько всего отсчётов телеметрии снято за прогон.")


class GpuBenchmarkReport(BaseModel):
    """Полный отчёт GPU-бенчмарка (Roofline, шкала ×10 000).

    Сохраняется как JSON в таблице ``gpu_benchmark_runs`` (схема v5).
    Все измерения/пики/ratio опциональны: если фаза не выполнилась или пик
    для устройства неизвестен — соответствующее поле остаётся ``None``.
    Итоговый ``score`` = ``GM(r_fp32, r_mem) × 10 000`` (FP64 — вне балла).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="UUID прогона.")
    system_info: SystemInfo = Field(..., description="Снимок системы.")
    device: GpuDeviceInfo = Field(..., description="Тестируемое GPU-устройство.")
    started_at: datetime = Field(..., description="Момент начала прогона (UTC).")
    ended_at: datetime = Field(..., description="Момент окончания прогона (UTC).")

    # Длительности фаз (фактические).
    fp32_duration_sec: float = Field(default=0.0)
    fp64_duration_sec: float = Field(default=0.0)
    mem_bandwidth_duration_sec: float = Field(default=0.0)
    pcie_duration_sec: float = Field(default=0.0)

    # Измерения.
    fp32_gflops: float | None = Field(default=None)
    fp64_gflops: float | None = Field(default=None)
    mem_bandwidth_gb_s: float | None = Field(default=None)
    pcie_h2d_gb_s: float | None = Field(default=None)
    pcie_d2h_gb_s: float | None = Field(default=None)

    # Roofline-пики.
    fp32_peak_gflops: float | None = Field(default=None)
    fp64_peak_gflops: float | None = Field(default=None)
    mem_bandwidth_peak_gb_s: float | None = Field(default=None)

    # Ratio (после clamp ≤1.0).
    r_fp32: float | None = Field(default=None)
    r_fp64: float | None = Field(default=None)
    r_mem: float | None = Field(default=None)

    # Финальный балл в шкале ×10 000 (GM(r_fp32, r_mem)).
    score: float | None = Field(default=None)

    # Метаданные пика / арх.
    arch: str | None = Field(default=None)
    peak_source: str = Field(default="unknown")

    # Notes / warnings — короткие человекочитаемые сообщения.
    notes: list[str] = Field(default_factory=list)
    cancelled: bool = Field(default=False)


__all__ = [
    "GpuBenchmarkReport",
    "GpuDeviceInfo",
    "GpuDeviceType",
    "GpuMeasurement",
    "GpuPeak",
    "GpuStressReport",
    "GpuStressSample",
    "GpuStressVerdict",
    "GpuWorkloadKind",
]
