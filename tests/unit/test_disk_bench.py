"""Тесты для дисковых микробенчмарков (Sequential / Random Read).

Тесты используют маленький файл (8 МБ) и короткую длительность (0.5 с),
чтобы не нагружать CI и SSD пользователя.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apexcore.infrastructure.microbench.disk import (
    DiskRandomReadBench,
    DiskSequentialReadBench,
    DiskSequentialWriteBench,
    _make_test_file,
)


def test_make_test_file_returns_existing_path_with_correct_size(tmp_path: Path) -> None:
    """_make_test_file создаёт файл нужного размера со случайным содержимым."""
    path = _make_test_file(2)
    try:
        assert path.exists()
        assert path.stat().st_size == 2 * 1024 * 1024
    finally:
        path.unlink(missing_ok=True)


def test_sequential_read_bench_produces_positive_throughput() -> None:
    bench = DiskSequentialReadBench()
    if not bench.is_available():
        pytest.skip("свободное место < 1 ГБ — пропускаем")
    # Уменьшаем файл для быстрого теста.
    bench.FILE_SIZE_MB = 8
    result = bench.run(duration_sec=0.5)
    assert result.value > 0, "MB/s должно быть положительным"
    assert result.unit == "MB/s"
    assert result.iterations >= 1
    assert result.category == "disk"


def test_sequential_read_bench_cleans_up_temp_file() -> None:
    bench = DiskSequentialReadBench()
    if not bench.is_available():
        pytest.skip("свободное место < 1 ГБ — пропускаем")
    bench.FILE_SIZE_MB = 4
    # Подсмотрим временный файл через monkey-patched tempfile? Слишком сложно.
    # Проще — проверим, что в tempfile.gettempdir() не остаются файлы
    # с нашим префиксом после прогона.
    import os
    import tempfile

    tempdir = Path(tempfile.gettempdir())
    before = {p for p in tempdir.iterdir() if p.name.startswith("apexcore-winsat-")}
    bench.run(duration_sec=0.3)
    after = {p for p in tempdir.iterdir() if p.name.startswith("apexcore-winsat-")}
    # Никаких новых артефактов не остаётся.
    new = after - before
    # Очистим если какие-то артефакты остались от прошлых тестов.
    for p in new:
        try:
            os.unlink(p)
        except OSError:
            pass
    assert not new, f"остались артефакты: {new}"


def test_random_read_bench_produces_positive_throughput() -> None:
    bench = DiskRandomReadBench()
    if not bench.is_available():
        pytest.skip("свободное место < 1 ГБ — пропускаем")
    bench.FILE_SIZE_MB = 8
    result = bench.run(duration_sec=0.5)
    assert result.value > 0
    assert result.unit == "MB/s"
    assert result.category == "disk"


def test_bench_metadata_includes_block_size_and_queue_depth() -> None:
    bench = DiskSequentialReadBench()
    if not bench.is_available():
        pytest.skip("свободное место < 1 ГБ — пропускаем")
    bench.FILE_SIZE_MB = 4
    result = bench.run(duration_sec=0.3)
    assert result.extra["block_size_kb"] == 64
    assert result.extra["file_size_mb"] == 4
    assert "queue_depth" in result.extra


# ─── target_dir и DiskSequentialWriteBench ─────────────────────────────────


def test_seq_read_bench_uses_target_dir(tmp_path: Path) -> None:
    """target_dir создаёт временный файл именно там, не в системном tempdir.

    С v0.9.0 файл создаётся в поддиректории ``apexcore-bench/`` чтобы не
    загрязнять корень диска (anti-virus / NTFS overhead в C:\\).
    """
    bench = DiskSequentialReadBench(target_dir=tmp_path)
    bench.FILE_SIZE_MB = 4
    if not bench.is_available():
        pytest.skip("на target_dir < 1 ГБ — пропускаем")
    result = bench.run(duration_sec=0.3)
    assert result.value > 0
    assert result.extra["target_dir"] == str(tmp_path / "apexcore-bench")
    # После прогона файл удалён (но поддиректория apexcore-bench может остаться).
    leftovers = list((tmp_path / "apexcore-bench").glob("apexcore-winsat-*.bin"))
    assert leftovers == []


def test_random_read_bench_uses_target_dir(tmp_path: Path) -> None:
    bench = DiskRandomReadBench(target_dir=tmp_path)
    bench.FILE_SIZE_MB = 4
    if not bench.is_available():
        pytest.skip("на target_dir < 1 ГБ — пропускаем")
    result = bench.run(duration_sec=0.3)
    assert result.value > 0
    assert result.extra["target_dir"] == str(tmp_path / "apexcore-bench")


def test_seq_write_bench_produces_positive_throughput(tmp_path: Path) -> None:
    bench = DiskSequentialWriteBench(target_dir=tmp_path)
    bench.FILE_SIZE_MB = 4  # маленький файл для CI
    if not bench.is_available():
        pytest.skip("на target_dir < 1 ГБ — пропускаем")
    result = bench.run(duration_sec=0.0)  # игнорируется
    assert result.value > 0
    assert result.unit == "MB/s"
    assert result.category == "disk"
    assert result.extra["block_size_kb"] == 64
    assert result.extra["fsync_called"] is True
    # Файл удалён после прогона.
    leftovers = list(tmp_path.glob("apexcore-write-*.bin"))
    assert leftovers == []


def test_seq_write_bench_is_single_pass(tmp_path: Path) -> None:
    """Write-бенч пишет ровно FILE_SIZE_MB МБ, не циклится."""
    bench = DiskSequentialWriteBench(target_dir=tmp_path)
    bench.FILE_SIZE_MB = 2
    result = bench.run(duration_sec=0.0)
    # iterations = FILE_SIZE_MB * 1024 * 1024 / BLOCK_SIZE = 2*1024*1024/65536 = 32
    expected_iters = (bench.FILE_SIZE_MB * 1024 * 1024) // bench.BLOCK_SIZE
    assert result.iterations == expected_iters
