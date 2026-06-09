"""Сравнение Single-Core vs Multi-Core: один бенч, два замера.

Запускает выбранный ``MicroBench`` дважды:

1. **Single-Core** — в текущем потоке, прибитом к одному P-ядру (если на
   гибридном CPU удалось определить P-cluster) или к CPU 0. Это даёт
   стабильное значение «производительности одного ядра».
2. **Multi-Core** — параллельно в N потоках (N = логические CPU),
   без affinity. Агрегированный throughput.

Возвращает ``SingleMultiResult``, в котором уже доступны производные
``speedup`` и ``efficiency``.

Дизайн-замечания
----------------
- Используем стандартный ``threading.Thread``. Микробенчи на ``numba``
  отпускают GIL внутри JIT-функций, поэтому реально параллелятся.
  NumPy-fallback тоже отпускает GIL на векторных операциях.
- ``cancel_token`` — общий для обоих замеров; пробрасывается в bench.
- Если affinity на платформе не поддерживается (macOS) — Single всё
  равно запустится, просто `pinned_cpu` будет ``None``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import psutil

from apexcore.domain.models import MicroBenchResult, SingleMultiResult
from apexcore.infrastructure.cpu_affinity import is_supported, pinned_to_cpus
from apexcore.infrastructure.cpu_topology import detect_hybrid_topology
from apexcore.infrastructure.microbench.base import MicroBench

logger = logging.getLogger(__name__)


def choose_pinned_cpu() -> tuple[int | None, str | None]:
    """Выбрать CPU для Single-замера. Возвращает (cpu_index, kind).

    - На hybrid Intel: первый CPU из `HybridTopology.p_cpus` — это P-core.
    - На AMD/обычном Intel: CPU 0 — обычно P-core (на всех современных
      AMD/Intel-non-hybrid все ядра одного класса).
    - Если affinity не поддерживается — (None, None) и кладёмся на
      решение scheduler'а.
    """
    if not is_supported():
        return None, None
    hybrid = detect_hybrid_topology()
    if hybrid and hybrid.p_cpus:
        return hybrid.p_cpus[0], "P-core"
    return 0, None  # non-hybrid — все ядра «одинаковые»


def run_single_multi_compare(
    bench: MicroBench,
    duration_sec: float,
    total_threads: int,
    cancel_token: threading.Event | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> SingleMultiResult:
    """Сделать два замера: Single (1 thread, pinned) и Multi (N threads).

    Args:
        bench: микробенч-движок (например, ``Int64IopsBench``).
        duration_sec: целевая длительность каждого замера, сек.
        total_threads: сколько воркеров в Multi-замере (обычно
            ``psutil.cpu_count(logical=True)``).
        cancel_token: общий флаг отмены.
        progress_cb: опциональный callback с короткой строкой статуса
            (для UI-spinner: «Single-Core…» / «Multi-Core…»).
    """
    pinned_cpu, pinned_kind = choose_pinned_cpu()

    # Параметры топологии для подписи в Multi-карточке: физические ядра
    # (16 на 12900K) и их разбивка P/E (8P+8E). Не путаем с числом потоков
    # (24), которые в `cores_used_multi`.
    hybrid = detect_hybrid_topology()
    physical_cores = psutil.cpu_count(logical=False) or total_threads
    physical_p = hybrid.p_cores if hybrid else None
    physical_e = hybrid.e_cores if hybrid else None

    if progress_cb:
        progress_cb("Single-Core")
    single = _run_single(bench, duration_sec, pinned_cpu, cancel_token)

    if progress_cb:
        progress_cb("Multi-Core")
    multi = _run_multi(bench, duration_sec, total_threads, cancel_token)

    return SingleMultiResult(
        bench_name=bench.name,
        duration_sec_per_test=duration_sec,
        single=single,
        multi=multi,
        cores_used_multi=total_threads,
        physical_cores=physical_cores,
        physical_p_cores=physical_p,
        physical_e_cores=physical_e,
        pinned_cpu=pinned_cpu,
        pinned_kind=pinned_kind,
    )


# ─────────────────────────── Single ────────────────────────────


def _run_single(
    bench: MicroBench,
    duration_sec: float,
    pinned_cpu: int | None,
    cancel_token: threading.Event | None,
) -> MicroBenchResult:
    """Запустить bench в текущем потоке, привязанном к одному CPU."""
    cpu_list = [pinned_cpu] if pinned_cpu is not None else []
    with pinned_to_cpus(cpu_list) as applied:
        result = bench.run(duration_sec, threads=1, cancel_token=cancel_token)
    # Дополним extra, чтобы render видел affinity-контекст.
    extra = dict(result.extra)
    extra["pinned_cpu"] = pinned_cpu
    extra["pinned_applied"] = applied
    return result.model_copy(update={"threads": 1, "extra": extra})


# ─────────────────────────── Multi ────────────────────────────


def _run_multi(
    bench: MicroBench,
    duration_sec: float,
    total_threads: int,
    cancel_token: threading.Event | None,
) -> MicroBenchResult:
    """Запустить bench параллельно в N потоках, агрегировать throughput.

    Воркеры стартуют максимально одновременно (после общего барьера),
    чтобы все они конкурировали за ядра одновременно — иначе один
    закончит раньше и multi-throughput окажется завышенным.
    """
    results: list[MicroBenchResult] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(total_threads)

    def worker() -> None:
        try:
            barrier.wait(timeout=10.0)
            r = bench.run(duration_sec, threads=1, cancel_token=cancel_token)
            with lock:
                results.append(r)
        except BaseException as exc:
            # Собираем ошибки чтобы не сорвать общий замер: если хоть один
            # воркер дал результат — Multi всё равно валиден.
            with lock:
                errors.append(exc)

    threads_list = [
        threading.Thread(target=worker, daemon=True, name=f"smc-worker-{i}")
        for i in range(total_threads)
    ]
    started = time.perf_counter()
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join()
    elapsed = time.perf_counter() - started

    if not results:
        first_err = errors[0] if errors else RuntimeError("нет результатов от воркеров")
        raise RuntimeError(f"Multi-замер не дал результатов: {first_err}") from first_err

    total_value = sum(r.value for r in results)
    total_iter = sum(r.iterations for r in results)
    backend_extra = dict(results[0].extra)
    backend_extra["parallel_workers"] = len(results)
    if errors:
        backend_extra["worker_errors"] = len(errors)

    return MicroBenchResult(
        name=bench.name,
        category=bench.category,
        value=total_value,
        unit=bench.unit,
        duration_actual_sec=elapsed,
        iterations=total_iter,
        threads=total_threads,
        backend=results[0].backend,
        extra=backend_extra,
    )
