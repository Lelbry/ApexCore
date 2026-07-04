"""Хранилище: SQLite-репозитории."""

from apexcore.infrastructure.persistence.general_benchmark_repo import (
    SqliteGeneralBenchmarkRepository,
)
from apexcore.infrastructure.persistence.gpu_benchmark_repo import (
    SqliteGpuBenchmarkRepository,
)
from apexcore.infrastructure.persistence.gpu_stress_repo import (
    SqliteGpuStressRepository,
)
from apexcore.infrastructure.persistence.sqlite_repo import (
    SqliteBaselineRepository,
    SqliteMicroRunRepository,
    SqliteResultRepository,
)
from apexcore.infrastructure.persistence.winsat_repo import SqliteWinsatRepository

__all__ = [
    "SqliteBaselineRepository",
    "SqliteGeneralBenchmarkRepository",
    "SqliteGpuBenchmarkRepository",
    "SqliteGpuStressRepository",
    "SqliteMicroRunRepository",
    "SqliteResultRepository",
    "SqliteWinsatRepository",
]
