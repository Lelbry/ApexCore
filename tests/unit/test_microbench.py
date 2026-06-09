"""Smoke-тесты микробенчмарков.

Проверяем, что каждый тест:
- регистрируется в реестре,
- возвращает MicroBenchResult с положительным throughput,
- укладывается в короткий бюджет времени (т.к. это smoke).

Намеренно используется маленькая длительность (0.3 с), чтобы CI не
застрял. Это не валидация цифр, только проверка работоспособности.
"""

from __future__ import annotations

import pytest

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench import build_default_microbench_registry


@pytest.fixture
def registry():
    return build_default_microbench_registry()


def test_registry_size(registry):
    """В реестре должны быть все 12 канонических тестов AIDA64."""
    assert len(registry) == 12
    names = {t.name for t in registry}
    expected = {
        "memory_read", "memory_write", "memory_copy",
        "flops_sp", "flops_dp",
        "int_iops_24", "int_iops_32", "int_iops_64",
        "aes_256", "sha1",
        "julia_sp", "mandelbrot_dp",
    }
    assert names == expected


@pytest.mark.parametrize(
    "test_idx",
    range(12),
    ids=lambda i: build_default_microbench_registry()[i].name,
)
def test_microbench_runs(registry, test_idx):
    """Каждый микротест отрабатывает за короткое время без исключений."""
    test = registry[test_idx]
    if not test.is_available():
        pytest.skip(f"{test.name}: недоступен в окружении")

    result = test.run(duration_sec=0.3, threads=None)

    assert isinstance(result, MicroBenchResult)
    assert result.name == test.name
    assert result.category == test.category
    assert result.unit == test.unit
    assert result.error is None, f"{test.name} упал: {result.error}"
    assert result.value > 0, f"{test.name} вернул нулевой throughput"
    assert result.duration_actual_sec > 0
    assert result.iterations >= 1


def test_microbench_categories(registry):
    """Категории распределены ровно по AIDA-схеме."""
    categories = [t.category for t in registry]
    # 3 теста памяти, 2 FP, 3 целочисленных, 2 крипто, 2 фрактальных.
    assert categories.count("memory") == 3
    assert categories.count("flops") == 2
    assert categories.count("integer") == 3
    assert categories.count("crypto") == 2
    assert categories.count("fractal") == 2


def test_microbench_units(registry):
    """Единицы измерения соответствуют категории."""
    by_category: dict[str, set[str]] = {}
    for t in registry:
        by_category.setdefault(t.category, set()).add(t.unit)
    assert by_category["memory"] == {"MB/s"}
    assert by_category["flops"] == {"GFLOPS"}
    assert by_category["integer"] == {"GIOPS"}
    assert by_category["crypto"] == {"MB/s"}
    assert by_category["fractal"] == {"FPS"}
