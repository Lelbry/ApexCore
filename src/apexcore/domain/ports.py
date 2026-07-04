"""Порты — абстрактные интерфейсы между ядром и инфраструктурой.

Слой `domain` определяет контракты, а конкретные реализации живут в
`infrastructure/` (адаптеры ОС, стресс-движки, репозитории).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from apexcore.domain.cache import CacheTopology
from apexcore.domain.models import (
    BaselineProfile,
    BenchmarkResult,
    MetricSnapshot,
    MicroBenchSuiteResult,
    StressResult,
    SystemInfo,
)

if TYPE_CHECKING:
    from apexcore.domain.general_benchmark import GeneralBenchmarkReport
    from apexcore.domain.gpu import (
        GpuBenchmarkReport,
        GpuDeviceInfo,
        GpuMeasurement,
        GpuStressReport,
        GpuWorkloadKind,
    )
    from apexcore.domain.winsat import WinsatReport

# ─────────────────────────── Адаптер ОС ─────────────────────────────────────────


class OSAdapter(ABC):
    """Платформенно-зависимый шлюз: системные сведения и текущие метрики."""

    name: str = "abstract"

    @abstractmethod
    def get_system_info(self) -> SystemInfo:
        """Вернуть структурированное описание ОС и оборудования хоста."""

    @abstractmethod
    def get_current_metrics(self) -> MetricSnapshot:
        """Вернуть один отсчёт текущей утилизации и температур."""

    @abstractmethod
    def check_prerequisites(self) -> bool:
        """Проверить, что необходимые внешние стресс-утилиты доступны."""

    @abstractmethod
    def get_available_temps(self) -> list[str]:
        """Перечислить ключи/метки доступных температурных сенсоров."""

    @abstractmethod
    def get_frequencies_mhz(self) -> dict[str, float]:
        """Вернуть текущие частоты CPU (МГц): cpu_avg / cpu_min / cpu_max / core_<n>."""

    def get_cache_topology(self) -> CacheTopology:
        """Вернуть размеры L1/L2/L3 кеша и логический «уровень» DRAM.

        Реализация по умолчанию даёт fallback-значения (L1=32 КБ, L2=256 КБ,
        L3=8 МБ, DRAM=256 МБ). Конкретные адаптеры (Windows, Linux)
        переопределяют метод и подставляют реально определённые размеры.
        """
        from apexcore.infrastructure.adapters.cache import default_cache_topology

        return default_cache_topology()


# ─────────────────────────── Стресс-движок ──────────────────────────────────────


class StressEngine(ABC):
    """Стресс-движок: одна нагрузка одного типа (cpu_int, cpu_fp, ram_bw, ram_lat)."""

    name: str = "abstract"
    category: str = "abstract"
    is_external: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Можно ли запустить движок в текущей среде (есть зависимости/утилиты)?"""

    @abstractmethod
    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        """Запустить нагрузку на заданное время и вернуть результат.

        Если задан ``cancel_token`` и он становится set — движок должен
        корректно завершиться (не позднее, чем через одну единицу работы)
        и вернуть результат на том, что успел выполнить.
        """


# ─────────────────────────── GPU-compute бэкенд ─────────────────────────────────


class GpuComputeBackend(ABC):
    """Кроссвендорный бэкенд GPU-вычислений (реализация — OpenCL через ctypes).

    Абстрагирует перечисление устройств и запуск замеров пропускной
    способности, чтобы application-слой не зависел от конкретного API
    (OpenCL/CUDA/…). Реализация обязана деградировать корректно: если ни
    одного устройства/раннера нет — :meth:`is_available` возвращает False,
    а не бросает исключение.
    """

    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """Доступен ли бэкенд (загрузился ICD-loader, есть хотя бы одно устройство)?"""

    @abstractmethod
    def list_devices(self) -> list[GpuDeviceInfo]:
        """Перечислить GPU-устройства (пустой список — если ничего не найдено)."""

    @abstractmethod
    def supports(self, device_index: int, kind: GpuWorkloadKind) -> bool:
        """Поддерживает ли устройство данный тип нагрузки (напр. FP64 на iGPU)."""

    @abstractmethod
    def measure(
        self,
        device_index: int,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None = None,
    ) -> GpuMeasurement:
        """Прогнать нагрузку заданное время и вернуть измерение.

        Если задан ``cancel_token`` и он становится set — движок должен
        завершиться не позднее, чем через одну единицу работы, вернув
        измерение по факту уже выполненного.
        """


# ─────────────────────────── Шина метрик (Pub/Sub) ──────────────────────────────


MetricsSubscriber = Callable[[MetricSnapshot], None]


class MetricsBus(Protocol):
    """Простая Pub/Sub-шина для трансляции снимков телеметрии подписчикам."""

    def subscribe(self, subscriber: MetricsSubscriber) -> Callable[[], None]:
        """Подписаться. Возвращает функцию-отписку."""

    def publish(self, snapshot: MetricSnapshot) -> None:
        """Опубликовать снимок всем подписчикам."""


# ─────────────────────────── Репозитории ────────────────────────────────────────


class ResultRepository(ABC):
    """Хранилище прогонов бенчмарка (`BenchmarkResult`)."""

    @abstractmethod
    def save(self, result: BenchmarkResult) -> None:
        """Сохранить полный результат прогона."""

    @abstractmethod
    def get(self, run_id: UUID) -> BenchmarkResult | None:
        """Достать прогон по UUID."""

    @abstractmethod
    def list_runs(self, limit: int = 50, profile_name: str | None = None) -> list[BenchmarkResult]:
        """Вернуть последние прогоны (опционально фильтр по профилю)."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить прогон. Возвращает True, если удаление произошло."""


class BaselineRepository(ABC):
    """Хранилище базовых профилей нормализации."""

    @abstractmethod
    def save(self, baseline: BaselineProfile) -> None: ...

    @abstractmethod
    def get(self, baseline_id: UUID) -> BaselineProfile | None: ...

    @abstractmethod
    def find_by_name(self, name: str) -> BaselineProfile | None: ...

    @abstractmethod
    def list_baselines(self) -> Iterable[BaselineProfile]: ...


class MicroRunRepository(ABC):
    """Хранилище прогонов микробенчмарков (scoring v2 — общая оценка).

    Каждая запись = один ``MicroBenchSuiteResult`` (агрегированный по
    n_runs прогонам, с заполненным ``overall``). См. docs/scoring_v2.md.
    """

    @abstractmethod
    def save(self, suite: MicroBenchSuiteResult) -> None:
        """Сохранить агрегированный результат микро-прогона."""

    @abstractmethod
    def get(self, run_id: UUID) -> MicroBenchSuiteResult | None:
        """Достать по UUID."""

    @abstractmethod
    def list_runs(
        self, limit: int = 50, preset: str | None = None
    ) -> list[MicroBenchSuiteResult]:
        """Список последних прогонов; опционально фильтр по пресету."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить запись. True если удаление произошло."""


class WinsatRepository(ABC):
    """Хранилище прогонов Winsat-аналога.

    Один прогон = один :class:`WinsatReport` со шкалой 1.0–9.9.
    Не пересекается с ``MicroRunRepository`` (scoring v2, шкала 1000).
    """

    @abstractmethod
    def save(self, report: WinsatReport) -> None:
        """Сохранить полный winsat-отчёт."""

    @abstractmethod
    def get(self, run_id: UUID) -> WinsatReport | None:
        """Достать прогон по UUID."""

    @abstractmethod
    def list_runs(self, limit: int = 50) -> list[WinsatReport]:
        """Список последних прогонов (по убыванию started_at)."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить запись. True если удаление произошло."""


class GeneralBenchmarkRepository(ABC):
    """Хранилище прогонов «Оценок общей производительности».

    Один прогон = один :class:`GeneralBenchmarkReport` в шкале ×10 000.
    Отдельная сущность от Winsat (1.0–9.9) и от micro_runs (×1000).
    """

    @abstractmethod
    def save(self, report: GeneralBenchmarkReport) -> None:
        """Сохранить отчёт."""

    @abstractmethod
    def get(self, run_id: UUID) -> GeneralBenchmarkReport | None:
        """Достать прогон по UUID."""

    @abstractmethod
    def list_runs(self, limit: int = 50) -> list[GeneralBenchmarkReport]:
        """Список последних прогонов (по убыванию started_at)."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить запись."""


class GpuBenchmarkRepository(ABC):
    """Хранилище прогонов GPU-бенчмарка.

    Один прогон = один :class:`GpuBenchmarkReport` в шкале ×10 000.
    Отдельная сущность (таблица ``gpu_benchmark_runs``, схема v5), не
    пересекается с Winsat / micro_runs / general_benchmark_runs.
    """

    @abstractmethod
    def save(self, report: GpuBenchmarkReport) -> None:
        """Сохранить отчёт."""

    @abstractmethod
    def get(self, run_id: UUID) -> GpuBenchmarkReport | None:
        """Достать прогон по UUID."""

    @abstractmethod
    def list_runs(self, limit: int = 50) -> list[GpuBenchmarkReport]:
        """Список последних прогонов (по убыванию started_at)."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить запись. True если удаление произошло."""


class GpuStressRepository(ABC):
    """Хранилище прогонов GPU-стресс-теста (термостабильность).

    Один прогон = один :class:`GpuStressReport`. Здесь НЕТ балла — headline
    это вердикт PASS/WARN/FAIL/UNKNOWN. Отдельная сущность (таблица
    ``gpu_stress_runs``, схема v6), не пересекается с gpu_benchmark_runs
    (то Roofline-балл) / Winsat / micro_runs / general_benchmark_runs.
    """

    @abstractmethod
    def save(self, report: GpuStressReport) -> None:
        """Сохранить отчёт."""

    @abstractmethod
    def get(self, run_id: UUID) -> GpuStressReport | None:
        """Достать прогон по UUID."""

    @abstractmethod
    def list_runs(self, limit: int = 50) -> list[GpuStressReport]:
        """Список последних прогонов (по убыванию started_at)."""

    @abstractmethod
    def delete(self, run_id: UUID) -> bool:
        """Удалить запись. True если удаление произошло."""
