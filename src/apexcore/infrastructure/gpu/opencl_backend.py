"""OpenCL-реализация :class:`~apexcore.domain.ports.GpuComputeBackend`.

Кроссвендорный GPU-compute бэкенд поверх системного ICD-loader (через
:mod:`apexcore.infrastructure.gpu._ocl` — только ``ctypes``, без новых
зависимостей). Умеет:

* перечислить все GPU (платформы × устройства сплющены в один индексируемый
  список) с полями для Roofline-пика;
* измерить реальную пропускную способность кернелами, которые компилируются на
  лету и таймятся device-side событиями (``clGetEventProfilingInfo``):
  - FP32/FP64 — плотный цикл FMA (peak GFLOPS),
  - MEM_BANDWIDTH — STREAM-triad по большим буферам (GB/s VRAM),
  - PCIE_H2D / PCIE_D2H — время ``clEnqueueWrite/ReadBuffer`` большого буфера.

Выбор «робастной» метрики пропускной способности: суммарная работа делится на
суммарное **device-время** таймленных итераций (``total_work / total_device_ns``),
а не медиана wall-clock. Причины:

* device-side профилирование меряет чистое время команды на GPU, исключая
  джиттер запуска с хоста и планировщик ОС — это самая стабильная величина;
* сумма по многим итерациям сглаживает разброс отдельных запусков не хуже
  медианы, но проще и не теряет длинные «хорошие» итерации;
* для peak-нагрузок (FP32/STREAM) именно устойчивое device-время выводит
  результат на архитектурный потолок, а не занижает его выбросами.

Управление ресурсами: каждый ресурс OpenCL освобождается в ``finally``; коды
ошибок проверяются в обёртке и превращаются в :class:`~._ocl.OpenClError`.
"""

from __future__ import annotations

import ctypes
import threading
import time

import numpy as np

from apexcore.domain.gpu import (
    GpuDeviceInfo,
    GpuDeviceType,
    GpuMeasurement,
    GpuWorkloadKind,
)
from apexcore.domain.ports import GpuComputeBackend
from apexcore.infrastructure.gpu import _ocl
from apexcore.infrastructure.gpu._ocl import Ocl, OpenClError

# ─────────────────────────── Параметры нагрузок ─────────────────────────────────

# Устройство FP-кернела (проверено на RTX 4070 Ti + UHD 770 по образцу clpeak).
#
# КОРЕНЬ ПРОБЛЕМЫ старой версии: `float acc[N]` с N=32 независимыми аккумуляторами
# создаёт высокое давление на регистровый файл. На Ada (и вообще на NVIDIA/AMD)
# это НЕ приводит к spill'у в local memory (CL_KERNEL_PRIVATE_MEM_SIZE == 0), но
# ЗАРЕЗАЕТ занятость (occupancy): чем больше живых регистров на work-item, тем
# меньше варпов резидентно на SM, и латентность конвейера FMA (~4 такта) уже
# нечем прятать. Пиковые микробенчмарки (clpeak compute_sp) используют РОВНО 2
# скалярных регистра и полагаются на occupancy: сотни резидентных варпов
# полностью скрывают латентность, а датапас FMA занят каждый такт.
#
# Паттерн: перемежающаяся цепочка `x = mad(y, x, y); y = mad(x, y, x)`.
# * Коэффициент умножения — ДРУГОЙ аккумулятор (не константа), поэтому
#   рекуррентность нелинейна и компилятор не может свернуть её в закрытую форму
#   (в отличие от `fma(acc, c_const, e_const)` — там ptxas сворачивает цикл).
# * `mad()` (а не `fma()`) — на всех вендорах (NVIDIA/AMD/Intel) отображается в
#   нативную операцию multiply-add; корректно-округлённый `fma()` того же пика,
#   но `mad()` даёт компилятору максимальную свободу и портируем на устройства
#   без аппаратного FMA.
#
# _FP_ACCUMULATORS — число скалярных регистров-аккумуляторов (чётное: пары x/y).
# Небольшое значение → низкое давление на регистры → высокая occupancy. Держим
# умеренный ILP (несколько пар), чтобы хорошо работать и на iGPU, где варпов
# меньше и немного ILP помогает. _FP_INNER_FMA — глубина разворота внутреннего
# цикла (mad-операций на аккумулятор за один оборот внешнего).
_FP_ACCUMULATORS = 8
_FP_INNER_FMA = 32  # mad на аккумулятор за один проход внешнего цикла

# Целевое число work-item'ов для FP-нагрузки: тысячи на CU, чтобы полностью
# занять все SM/CU дискретного GPU. Кратно 64 (варп/wavefront).
_FP_WORKITEMS_PER_CU = 4096
_FP_MIN_WORKITEMS = 1 << 16  # нижняя граница даже для скромных iGPU
_FP_OUTER_ITERS = 2048       # оборотов внешнего цикла за один запуск кернела

# Размер STREAM-буфера: min(256 МиБ, четверть видимой VRAM). Три таких буфера
# (a, b, c) должны поместиться в память устройства.
_STREAM_MAX_BYTES = 256 * 1024 * 1024
_PCIE_BYTES = 256 * 1024 * 1024

# STREAM: ширина вектора (float{N}) и число векторов на work-item (grid-stride).
# Векторные загрузки (clpeak global_bandwidth) дают больше in-flight запросов к
# памяти и лучший коалесинг, чем скалярный триад (1 элемент/поток).
#
# ВЫБОР ШИРИНЫ = float4 (кроссвендорный компромисс, НЕ подгон под NVIDIA):
# * на Ada RTX 4090 clpeak даёт float4 = 917 GB/s против float16 = 939 GB/s —
#   разница ~2%, т.е. float4 практически на пике дискретной карты;
# * на Intel UHD 770 замер показал float1/2/4 ≈ 68–71 GB/s (пик), а float8/16
#   ОБРУШИВАЮТ полосу до 47/32 GB/s (узкий порт памяти EU не любит широкие
#   векторы). float4 — единственная ширина у верхней границы на ОБЕИХ
#   архитектурах. 16 Б на элемент = 4 подряд идущих float, хорошо коалесится.
# _STREAM_VECS_PER_ITEM: каждый work-item обрабатывает столько векторов подряд с
# шагом на всю решётку (grid-stride) — больше запросов в полёте на дискретных GPU
# для скрытия латентности; на iGPU нейтрально.
_STREAM_VEC_WIDTH = 4
_STREAM_VECS_PER_ITEM = 8

# Прогрев перед таймленными итерациями (JIT-компиляция + раскрутка частот GPU).
_WARMUP_ITERS = 4

# Целевая минимальная длительность одного запуска кернела (сек). На слабых iGPU
# при большом _FP_OUTER_ITERS один запуск может длиться сотни мс — это нормально
# для корректности, но чтобы cancel_token реагировал вовремя и для медленных
# устройств не «переезжать» duration в разы, iters ужимается под эту цель.
_TARGET_LAUNCH_SEC = 0.10


# ─────────────────────────── Исходники кернелов ─────────────────────────────────


def _fp_chain_body(n_acc: int) -> str:
    """Тело перемежающейся mad-цепочки для ``n_acc`` аккумуляторов (пар x/y).

    Для каждой пары (2i, 2i+1) эмитит `x = mad(y, x, y); y = mad(x, y, x);` —
    это один «раунд» (2 mad-операции). Пары независимы между собой (ILP), внутри
    пары — 2-звенная цепочка зависимости, которую скрывает occupancy.
    """

    lines = []
    for i in range(0, n_acc, 2):
        lines.append(f"            a{i} = mad(a{i + 1}, a{i}, a{i + 1});")
        lines.append(f"            a{i + 1} = mad(a{i}, a{i + 1}, a{i});")
    return "\n".join(lines)


def _build_fp_kernel_src(kernel_name: str, scalar_t: str, enable_fp64: bool) -> str:
    """Собрать исходник FP-кернела (общий для fp32/fp64).

    ``scalar_t`` — ``float`` или ``double``. Кернел держит ``_FP_ACCUMULATORS``
    скалярных регистров, инициализированных из ``a``/``b``/``gid`` (чтобы значения
    были рантайм-зависимы и цикл нельзя было выкинуть), и крутит ``_FP_INNER_FMA``
    раундов mad-цепочки на оборот внешнего цикла. FLOP на work-item за один оборот
    внешнего цикла = _FP_ACCUMULATORS × _FP_INNER_FMA × 2 (mad = mul + add).
    """

    fp64_pragma = "#pragma OPENCL EXTENSION cl_khr_fp64 : enable\n" if enable_fp64 else ""
    suffix = "" if scalar_t == "double" else "f"
    # Инициализация: половина аккумуляторов от `a`, половина от `b`, со сдвигом по
    # gid/lid — рантайм-значения (компилятор не знает их на этапе сборки).
    inits = []
    for i in range(_FP_ACCUMULATORS):
        seed = "a" if i % 2 == 0 else "b"
        inits.append(f"    {scalar_t} a{i} = {seed} + ({scalar_t})(lid + {i}) * 1.0e-3{suffix};")
    init_block = "\n".join(inits)
    body = _fp_chain_body(_FP_ACCUMULATORS)
    sum_expr = " + ".join(f"a{i}" for i in range(_FP_ACCUMULATORS))
    return f"""
{fp64_pragma}__kernel void {kernel_name}(__global {scalar_t}* out, const {scalar_t} a,
                          const {scalar_t} b, const uint iters) {{
    const int gid = get_global_id(0);
    const int lid = get_local_id(0);
{init_block}
    for (uint it = 0; it < iters; ++it) {{
        #pragma unroll
        for (int j = 0; j < {_FP_INNER_FMA}; ++j) {{
{body}
        }}
    }}
    out[gid] = {sum_expr};
}}
"""


# FP32/FP64: перемежающаяся mad-цепочка (см. коммент к параметрам). `out[gid]`
# пишется в конце → результат живой, цикл не удаляется (DCE-guard).
_FP32_KERNEL_SRC = _build_fp_kernel_src("fma_fp32", "float", enable_fp64=False)
_FP64_KERNEL_SRC = _build_fp_kernel_src("fma_fp64", "double", enable_fp64=True)

# STREAM-triad: a[i] = b[i] + scalar*c[i], векторизованный на float{N} с grid-stride.
# 2 чтения (b, c) + 1 запись (a) на векторный элемент; векторные загрузки дают
# больше параллелизма на уровне памяти → полнее насыщают VRAM. Каждый work-item
# обрабатывает _STREAM_VECS_PER_ITEM векторов с шагом на всю решётку (коалесинг).
_STREAM_KERNEL_SRC = f"""
__kernel void stream_triad(__global float{_STREAM_VEC_WIDTH}* a,
                           __global const float{_STREAM_VEC_WIDTH}* b,
                           __global const float{_STREAM_VEC_WIDTH}* c,
                           const float scalar, const uint n_vec) {{
    const uint gid = get_global_id(0);
    const uint stride = get_global_size(0);
    #pragma unroll
    for (uint k = 0; k < {_STREAM_VECS_PER_ITEM}; ++k) {{
        const uint idx = gid + k * stride;
        if (idx < n_vec) a[idx] = b[idx] + scalar * c[idx];
    }}
}}
"""


def _empty_measurement(kind: GpuWorkloadKind, unit: str) -> GpuMeasurement:
    """Пустое измерение для мгновенного возврата при уже отменённом прогоне."""

    return GpuMeasurement(
        kind=kind,
        throughput=0.0,
        unit=unit,
        duration_sec=0.0,
        iterations=0,
        error_count=0,
        extra={},
    )


class OpenClGpuBackend(GpuComputeBackend):
    """GPU-compute бэкенд на OpenCL через ctypes (без внешних зависимостей).

    Устройства перечисляются один раз и кэшируются вместе со своими
    (platform_id, device_id) хэндлами. ``list_devices`` возвращает публичные
    :class:`GpuDeviceInfo` в стабильном сквозном порядке; ``measure`` берёт
    устройство по индексу из того же порядка.
    """

    name = "opencl"

    def __init__(self) -> None:
        self._ocl: Ocl | None = None
        self._loaded = False
        # Сквозной список: (GpuDeviceInfo, platform_id, device_id).
        self._devices: list[tuple[GpuDeviceInfo, int, int]] = []
        self._enumerated = False

    # ─────────────────────────── Загрузка / доступность ─────────────────────────

    def _ensure_loaded(self) -> Ocl | None:
        if not self._loaded:
            self._loaded = True
            self._ocl = _ocl.load()
        return self._ocl

    def is_available(self) -> bool:
        """True, если loader загрузился И найдено ≥1 устройство. Никогда не бросает."""

        try:
            ocl = self._ensure_loaded()
            if ocl is None:
                return False
            return len(self._enumerate()) > 0
        except Exception:
            # is_available обязана НИКОГДА не бросать (контракт порта): любую
            # ошибку загрузки/перечисления трактуем как «бэкенд недоступен».
            return False

    # ─────────────────────────── Перечисление устройств ─────────────────────────

    def _enumerate(self) -> list[tuple[GpuDeviceInfo, int, int]]:
        """Собрать сквозной список устройств (кэшируется после первого прохода)."""

        if self._enumerated:
            return self._devices
        ocl = self._ensure_loaded()
        if ocl is None:
            self._enumerated = True
            return self._devices

        flat: list[tuple[GpuDeviceInfo, int, int]] = []
        index = 0
        for pid in ocl.get_platform_ids():
            platform_name = ocl.platform_info_str(pid, _ocl.CL_PLATFORM_NAME)
            for did in ocl.get_device_ids(pid, _ocl.CL_DEVICE_TYPE_GPU):
                info = self._read_device_info(ocl, index, pid, platform_name, did)
                flat.append((info, pid, did))
                index += 1

        self._devices = flat
        self._enumerated = True
        return flat

    def _read_device_info(
        self, ocl: Ocl, index: int, platform_id: int, platform_name: str, device_id: int
    ) -> GpuDeviceInfo:
        name = ocl.device_info_str(device_id, _ocl.CL_DEVICE_NAME).strip()
        vendor_raw = ocl.device_info_str(device_id, _ocl.CL_DEVICE_VENDOR).strip()
        driver = ocl.device_info_str(device_id, _ocl.CL_DRIVER_VERSION).strip()
        version = ocl.device_info_str(device_id, _ocl.CL_DEVICE_VERSION).strip()
        cus = ocl.device_info_uint(device_id, _ocl.CL_DEVICE_MAX_COMPUTE_UNITS)
        clock = ocl.device_info_uint(device_id, _ocl.CL_DEVICE_MAX_CLOCK_FREQUENCY)
        gmem = ocl.device_info_ulong(device_id, _ocl.CL_DEVICE_GLOBAL_MEM_SIZE)
        wgs = ocl.device_info_size_t(device_id, _ocl.CL_DEVICE_MAX_WORK_GROUP_SIZE)
        fp64_cfg = ocl.device_info_ulong(device_id, _ocl.CL_DEVICE_DOUBLE_FP_CONFIG)

        return GpuDeviceInfo(
            index=index,
            name=name or "Unknown OpenCL device",
            vendor=_parse_vendor(name, vendor_raw, platform_name),
            platform_name=platform_name,
            device_type=_classify_device_type(name, platform_name),
            opencl_version=version,
            driver_version=driver,
            compute_units=int(cus),
            max_clock_mhz=int(clock),
            global_mem_mb=int(gmem // (1024 * 1024)),
            max_work_group_size=int(wgs),
            fp64_supported=fp64_cfg != 0,
            arch=None,  # разрешает отдельный модуль gpu_roofline
        )

    def list_devices(self) -> list[GpuDeviceInfo]:
        """Вернуть GPU-устройства в стабильном сквозном порядке (пусто — если нет)."""

        try:
            if self._ensure_loaded() is None:
                return []
            return [info for (info, _pid, _did) in self._enumerate()]
        except (OpenClError, OSError):
            return []

    def _device_at(self, device_index: int) -> tuple[GpuDeviceInfo, int, int]:
        devices = self._enumerate()
        if not 0 <= device_index < len(devices):
            raise IndexError(f"device_index {device_index} вне диапазона (устройств: {len(devices)})")
        return devices[device_index]

    # ─────────────────────────── supports ───────────────────────────────────────

    def supports(self, device_index: int, kind: GpuWorkloadKind) -> bool:
        """FP64 → зависит от устройства; остальное — True при существующем устройстве."""

        try:
            info, _pid, _did = self._device_at(device_index)
        except (IndexError, OpenClError, OSError):
            return False
        if kind == GpuWorkloadKind.FP64:
            return info.fp64_supported
        return True

    # ─────────────────────────── measure ────────────────────────────────────────

    def measure(
        self,
        device_index: int,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None = None,
    ) -> GpuMeasurement:
        """Прогнать нагрузку ``kind`` ~``duration_sec`` секунд и вернуть измерение.

        Диспетчеризует на нужный кернел/трансфер. Между итерациями проверяет
        ``cancel_token`` и при set-е возвращает результат по уже выполненному.
        Контекст/очередь/программа/буферы всегда освобождаются в ``finally``.
        """

        ocl = self._ensure_loaded()
        if ocl is None:
            raise RuntimeError("OpenCL loader недоступен — measure() вызван при is_available()==False")

        info, platform_id, device_id = self._device_at(device_index)

        # SUSTAINED_STRESS переиспользует FP32-кернел (максимальная ALU-нагрузка).
        effective_kind = kind
        if kind == GpuWorkloadKind.SUSTAINED_STRESS:
            effective_kind = GpuWorkloadKind.FP32

        if effective_kind == GpuWorkloadKind.FP64 and not info.fp64_supported:
            raise RuntimeError(f"устройство '{info.name}' не поддерживает FP64")

        # Ранняя проверка cancel ДО создания контекста/очереди и прогрева
        # (~0.5–0.7 c): уже отменённый прогон возвращается почти мгновенно.
        if cancel_token is not None and cancel_token.is_set():
            unit = "GFLOPS" if effective_kind in (GpuWorkloadKind.FP32, GpuWorkloadKind.FP64) else "GB/s"
            return _empty_measurement(kind, unit)

        context = 0
        queue = 0
        try:
            context = ocl.create_context(platform_id, device_id)
            queue = ocl.create_command_queue(context, device_id, profiling=True)

            if effective_kind in (GpuWorkloadKind.FP32, GpuWorkloadKind.FP64):
                result = self._measure_fp(
                    ocl, context, queue, info, effective_kind, duration_sec, cancel_token
                )
            elif effective_kind == GpuWorkloadKind.MEM_BANDWIDTH:
                result = self._measure_stream(ocl, context, queue, info, duration_sec, cancel_token)
            elif effective_kind in (GpuWorkloadKind.PCIE_H2D, GpuWorkloadKind.PCIE_D2H):
                result = self._measure_pcie(
                    ocl, context, queue, info, effective_kind, duration_sec, cancel_token
                )
            else:
                raise RuntimeError(f"неизвестный тип нагрузки: {kind}")
            # Сохраняем исходный kind в результате (в т.ч. SUSTAINED_STRESS).
            return result.model_copy(update={"kind": kind})
        finally:
            ocl.release_queue(queue)
            ocl.release_context(context)

    # ---- FP32 / FP64 ----

    def _measure_fp(
        self,
        ocl: Ocl,
        context: int,
        queue: int,
        info: GpuDeviceInfo,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> GpuMeasurement:
        # Ранняя проверка cancel: не тратить ~0.5–0.7 c на прогрев/калибровку/сборку,
        # если прогон отменён ещё до старта.
        if cancel_token is not None and cancel_token.is_set():
            return _empty_measurement(kind, "GFLOPS")

        is_fp64 = kind == GpuWorkloadKind.FP64
        src = _FP64_KERNEL_SRC if is_fp64 else _FP32_KERNEL_SRC
        kernel_name = "fma_fp64" if is_fp64 else "fma_fp32"
        elem_size = 8 if is_fp64 else 4

        # Глобальный размер: много work-item'ов, кратно 64 (варп/wavefront).
        global_size = max(_FP_MIN_WORKITEMS, info.compute_units * _FP_WORKITEMS_PER_CU)
        global_size = _round_up(global_size, 64)
        # local size None → рантайм подберёт сам (безопасно для любого WGS).

        program = 0
        kernel = 0
        out_mem = 0
        try:
            program = ocl.build_program(context, self._device_id_of(info), src)
            kernel = ocl.create_kernel(program, kernel_name)
            out_mem = ocl.create_buffer(context, _ocl.CL_MEM_WRITE_ONLY, global_size * elem_size)

            a_val = ctypes.c_double(1.0000001) if is_fp64 else ctypes.c_float(1.0000001)
            b_val = ctypes.c_double(0.9999999) if is_fp64 else ctypes.c_float(0.9999999)
            ocl.set_kernel_arg_mem(kernel, 0, out_mem)
            ocl.set_kernel_arg(kernel, 1, ctypes.sizeof(a_val), ctypes.byref(a_val))
            ocl.set_kernel_arg(kernel, 2, ctypes.sizeof(b_val), ctypes.byref(b_val))

            # Калибруем iters под ~_TARGET_LAUNCH_SEC на один запуск: замеряем
            # один прогрев с _FP_OUTER_ITERS и масштабируем. На быстром GPU это
            # оставит _FP_OUTER_ITERS, на медленном iGPU — ужмёт, чтобы
            # cancel_token срабатывал вовремя и длительность не «переезжала».
            iters = self._calibrate_fp_iters(ocl, queue, kernel, global_size)

            iters_c = ctypes.c_uint32(iters)
            ocl.set_kernel_arg(kernel, 3, ctypes.sizeof(iters_c), ctypes.byref(iters_c))

            # Прогрев на финальном iters (раскрутка частот, тёплые кэши).
            for _ in range(_WARMUP_ITERS):
                ocl.finish(queue)
                ev = ocl.enqueue_ndrange(queue, kernel, global_size, None)
                try:
                    ocl.wait_for_event(ev)
                finally:
                    ocl.release_event(ev)
            ocl.finish(queue)

            # FLOP за один оборот ВНЕШНЕГО цикла на весь NDRange:
            # work-items × ACCUMULATORS × INNER_FMA × 2 (mad = mul + add).
            flops_per_outer = float(global_size) * _FP_ACCUMULATORS * _FP_INNER_FMA * 2
            work_per_launch = flops_per_outer * iters

            iterations, wall_sec = self._run_timed_launches(
                ocl, queue, kernel, global_size, duration_sec, cancel_token
            )
            total_flops = work_per_launch * iterations
            gflops = (total_flops / wall_sec / 1e9) if wall_sec > 0 else 0.0
            return GpuMeasurement(
                kind=kind,
                throughput=gflops,
                unit="GFLOPS",
                duration_sec=wall_sec,
                iterations=iterations,
                error_count=0,
                extra={
                    "work_done": total_flops,
                    "device_ms": wall_sec * 1000.0,
                    "global_size": float(global_size),
                    "outer_iters": float(iters),
                },
            )
        finally:
            ocl.release_mem(out_mem)
            ocl.release_kernel(kernel)
            ocl.release_program(program)

    def _calibrate_fp_iters(self, ocl: Ocl, queue: int, kernel: int, global_size: int) -> int:
        """Подобрать outer-iters так, чтобы один запуск длился ~_TARGET_LAUNCH_SEC.

        Замеряем один запуск с базовым ``_FP_OUTER_ITERS`` (kernel arg 3 уже
        должен быть выставлен вызывающим на это значение НЕ обязательно — мы
        ставим его сами) и линейно масштабируем. Возвращаем ≥1.
        """

        base = _FP_OUTER_ITERS
        base_c = ctypes.c_uint32(base)
        ocl.set_kernel_arg(kernel, 3, ctypes.sizeof(base_c), ctypes.byref(base_c))
        # Один «горячий» прогон для оценки времени. Event освобождаем в finally,
        # чтобы исключение между enqueue и release не утекло хэндлом cl_event.
        ocl.finish(queue)
        ev = ocl.enqueue_ndrange(queue, kernel, global_size, None)
        try:
            ocl.wait_for_event(ev)
        finally:
            ocl.release_event(ev)
        t0 = time.perf_counter()
        ev = ocl.enqueue_ndrange(queue, kernel, global_size, None)
        try:
            ocl.finish(queue)
        finally:
            ocl.release_event(ev)
        dt = time.perf_counter() - t0
        if dt <= 0:
            return base
        scaled = int(base * (_TARGET_LAUNCH_SEC / dt))
        return max(1, min(scaled, base * 4))

    def _run_timed_launches(
        self,
        ocl: Ocl,
        queue: int,
        kernel: int,
        global_size: int,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> tuple[int, float]:
        """Гонять кернел до истечения ``duration_sec``; вернуть (число запусков, wall-сек).

        Тайминг — wall-clock (``perf_counter`` вокруг ``enqueue`` + ``clFinish``),
        НЕ device-события: на драйвере NVIDIA OpenCL ``clGetEventProfilingInfo``
        отдаёт квантованное значение накладных расходов запуска, не связанное с
        реальным временем выполнения кернела (проверено экспериментально: время
        события ~0.5 мс независимо от объёма работы, тогда как wall-clock растёт
        линейно с числом итераций и даёт корректный TFLOPS). ``clFinish`` служит
        барьером — засекаем стену вокруг реально завершённого GPU-выполнения.
        Каждую итерацию проверяем ``cancel_token`` → корректная ранняя остановка.
        """

        iterations = 0
        t_start = time.perf_counter()
        deadline = t_start + max(0.05, duration_sec)
        while time.perf_counter() < deadline:
            if cancel_token is not None and cancel_token.is_set():
                break
            ev = ocl.enqueue_ndrange(queue, kernel, global_size, None)
            try:
                ocl.finish(queue)  # барьер: ждём фактического завершения на GPU
            finally:
                ocl.release_event(ev)
            iterations += 1
        wall_sec = time.perf_counter() - t_start
        return iterations, wall_sec

    # ---- STREAM triad (VRAM bandwidth) ----

    def _measure_stream(
        self,
        ocl: Ocl,
        context: int,
        queue: int,
        info: GpuDeviceInfo,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> GpuMeasurement:
        # Ранняя проверка cancel: не тратить ~0.5 c на прогрев/аллокацию, если
        # прогон отменён ещё до старта.
        if cancel_token is not None and cancel_token.is_set():
            return _empty_measurement(GpuWorkloadKind.MEM_BANDWIDTH, "GB/s")

        # Размер каждого из 3 буферов: min(256 МиБ, четверть видимой VRAM). 256 МиБ
        # >> 48 МБ L2 (Ada) → нагрузка гарантированно упирается в VRAM, а не в кэш.
        vram_bytes = max(1, info.global_mem_mb) * 1024 * 1024
        buf_bytes = min(_STREAM_MAX_BYTES, max(vram_bytes // 4, 16 * 1024 * 1024))
        n = buf_bytes // 4  # число float-элементов
        # Кратно ширине вектора (кернел работает на float{VEC}) и достаточно, чтобы
        # покрыть grid-stride без хвоста.
        n = _round_up(n, _STREAM_VEC_WIDTH * 1024)
        buf_bytes = n * 4
        n_vec = n // _STREAM_VEC_WIDTH  # число векторных элементов в буфере
        # Каждый work-item обрабатывает _STREAM_VECS_PER_ITEM векторов (grid-stride).
        global_size = _round_up((n_vec + _STREAM_VECS_PER_ITEM - 1) // _STREAM_VECS_PER_ITEM, 64)

        device_id = self._device_id_of(info)
        program = 0
        kernel = 0
        a_mem = b_mem = c_mem = 0
        try:
            program = ocl.build_program(context, device_id, _STREAM_KERNEL_SRC)
            kernel = ocl.create_kernel(program, "stream_triad")

            a_mem = ocl.create_buffer(context, _ocl.CL_MEM_WRITE_ONLY, buf_bytes)
            b_mem = ocl.create_buffer(context, _ocl.CL_MEM_READ_ONLY, buf_bytes)
            c_mem = ocl.create_buffer(context, _ocl.CL_MEM_READ_ONLY, buf_bytes)

            # Инициализируем b и c один раз (host→device, вне таймингов).
            host = np.linspace(1.0, 2.0, n, dtype=np.float32)
            hptr = host.ctypes.data_as(ctypes.c_void_p)
            ev_w1 = ocl.enqueue_write_buffer(queue, b_mem, buf_bytes, hptr, blocking=True)
            ocl.release_event(ev_w1)
            ev_w2 = ocl.enqueue_write_buffer(queue, c_mem, buf_bytes, hptr, blocking=True)
            ocl.release_event(ev_w2)

            scalar = ctypes.c_float(3.0)
            n_vec_c = ctypes.c_uint32(n_vec)
            ocl.set_kernel_arg_mem(kernel, 0, a_mem)
            ocl.set_kernel_arg_mem(kernel, 1, b_mem)
            ocl.set_kernel_arg_mem(kernel, 2, c_mem)
            ocl.set_kernel_arg(kernel, 3, ctypes.sizeof(scalar), ctypes.byref(scalar))
            ocl.set_kernel_arg(kernel, 4, ctypes.sizeof(n_vec_c), ctypes.byref(n_vec_c))

            for _ in range(_WARMUP_ITERS):
                ev = ocl.enqueue_ndrange(queue, kernel, global_size, None)
                try:
                    ocl.wait_for_event(ev)
                finally:
                    ocl.release_event(ev)
            ocl.finish(queue)

            # 3 обращения к памяти на float-элемент: 2 чтения (b, c) + 1 запись (a).
            # Векторизация меняет паттерн доступа, но НЕ объём перемещённых байт.
            bytes_per_pass = 3.0 * n * 4
            iterations, wall_sec = self._run_timed_launches(
                ocl, queue, kernel, global_size, duration_sec, cancel_token
            )
            total_bytes = bytes_per_pass * iterations
            gb_s = (total_bytes / wall_sec / 1e9) if wall_sec > 0 else 0.0
            return GpuMeasurement(
                kind=GpuWorkloadKind.MEM_BANDWIDTH,
                throughput=gb_s,
                unit="GB/s",
                duration_sec=wall_sec,
                iterations=iterations,
                error_count=0,
                extra={
                    "work_done": total_bytes,
                    "device_ms": wall_sec * 1000.0,
                    "buffer_mb": float(buf_bytes / (1024 * 1024)),
                },
            )
        finally:
            ocl.release_mem(a_mem)
            ocl.release_mem(b_mem)
            ocl.release_mem(c_mem)
            ocl.release_kernel(kernel)
            ocl.release_program(program)

    # ---- PCIe H2D / D2H ----

    def _measure_pcie(
        self,
        ocl: Ocl,
        context: int,
        queue: int,
        info: GpuDeviceInfo,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None,
    ) -> GpuMeasurement:
        # Ранняя проверка cancel: не тратить ~0.5 c на прогрев/аллокацию pinned-буфера,
        # если прогон отменён ещё до старта.
        if cancel_token is not None and cancel_token.is_set():
            return _empty_measurement(kind, "GB/s")

        is_h2d = kind == GpuWorkloadKind.PCIE_H2D
        # Буфер ~256 МиБ, но не больше видимой VRAM (с запасом).
        vram_bytes = max(1, info.global_mem_mb) * 1024 * 1024
        buf_bytes = min(_PCIE_BYTES, max(vram_bytes // 4, 16 * 1024 * 1024))
        n = buf_bytes // 4
        buf_bytes = n * 4

        dev_mem = 0
        pinned_mem = 0
        try:
            dev_mem = ocl.create_buffer(context, _ocl.CL_MEM_READ_WRITE, buf_bytes)
            # Host-буфер через CL_MEM_ALLOC_HOST_PTR: рантайм выделяет
            # page-locked (pinned) память → DMA идёт без промежуточной копии,
            # что и меряет реальную пропускную способность шины. numpy-обёртка
            # над этим указателем даёт host-сторону для write/read.
            pinned_mem = ocl.create_buffer(
                context, _ocl.CL_MEM_READ_WRITE | _ocl.CL_MEM_ALLOC_HOST_PTR, buf_bytes
            )
            host = np.ones(n, dtype=np.float32)
            hptr = host.ctypes.data_as(ctypes.c_void_p)

            def one_transfer() -> int:
                if is_h2d:
                    return ocl.enqueue_write_buffer(queue, dev_mem, buf_bytes, hptr, blocking=False)
                return ocl.enqueue_read_buffer(queue, dev_mem, buf_bytes, hptr, blocking=False)

            # Прогрев трансфера в нужную сторону.
            for _ in range(_WARMUP_ITERS):
                ev = one_transfer()
                ocl.wait_for_event(ev)
                ocl.release_event(ev)
            ocl.finish(queue)

            # Тайминг — wall-clock (см. _run_timed_launches: device-события на
            # NVIDIA-драйвере ненадёжны). clFinish — барьер завершения DMA.
            iterations = 0
            t_start = time.perf_counter()
            deadline = t_start + max(0.05, duration_sec)
            while time.perf_counter() < deadline:
                if cancel_token is not None and cancel_token.is_set():
                    break
                ev = one_transfer()
                ocl.finish(queue)
                ocl.release_event(ev)
                iterations += 1
            wall_sec = time.perf_counter() - t_start

            total_bytes = float(buf_bytes) * iterations
            gb_s = (total_bytes / wall_sec / 1e9) if wall_sec > 0 else 0.0
            return GpuMeasurement(
                kind=kind,
                throughput=gb_s,
                unit="GB/s",
                duration_sec=wall_sec,
                iterations=iterations,
                error_count=0,
                extra={
                    "work_done": total_bytes,
                    "device_ms": wall_sec * 1000.0,
                    "buffer_mb": float(buf_bytes / (1024 * 1024)),
                },
            )
        finally:
            ocl.release_mem(dev_mem)
            ocl.release_mem(pinned_mem)

    # ---- вспомогательное ----

    def _device_id_of(self, info: GpuDeviceInfo) -> int:
        """Достать сырой device_id по публичному индексу из кэша."""

        for cached_info, _pid, did in self._enumerate():
            if cached_info.index == info.index:
                return did
        raise IndexError(f"device_id для индекса {info.index} не найден")


# ─────────────────────────── эвристики парсинга ─────────────────────────────────


def _parse_vendor(name: str, vendor_raw: str, platform_name: str) -> str:
    """Определить вендора по имени устройства/вендора/платформы."""

    blob = f"{name} {vendor_raw} {platform_name}".lower()
    if "nvidia" in blob or "geforce" in blob or "quadro" in blob or "tesla" in blob:
        return "NVIDIA"
    if "amd" in blob or "advanced micro devices" in blob or "radeon" in blob:
        return "AMD"
    if "intel" in blob:
        return "Intel"
    # Отдать «сырой» вендор как есть, если он непустой (пусть и незнакомый).
    return vendor_raw or "Unknown"


# Токены имён, характерные для встроенной графики (iGPU).
_INTEGRATED_TOKENS = (
    "uhd",
    "hd graphics",
    "iris",
    "radeon(tm) graphics",
    "radeon graphics",
    "vega",  # мобильные APU Vega (iGPU); дискретные Vega редки и всё равно попадут в discrete по имени карты
    "680m",
    "780m",
    "660m",
    "610m",
    "xe graphics",
    "graphics media",
)
# Токены дискретных карт (имеют приоритет над integrated, если оба совпали).
_DISCRETE_TOKENS = (
    "geforce",
    "rtx",
    "gtx",
    "quadro",
    "tesla",
    "radeon rx",
    "radeon pro",
    "instinct",
    "arc",  # Intel Arc — дискретная линейка
)


def _classify_device_type(name: str, platform_name: str) -> GpuDeviceType:
    """Эвристика discrete vs integrated по имени устройства/платформы."""

    n = name.lower()
    if any(tok in n for tok in _DISCRETE_TOKENS):
        return GpuDeviceType.DISCRETE
    if any(tok in n for tok in _INTEGRATED_TOKENS):
        return GpuDeviceType.INTEGRATED
    return GpuDeviceType.UNKNOWN


def _round_up(value: int, multiple: int) -> int:
    """Округлить ``value`` вверх до кратного ``multiple``."""

    if multiple <= 0:
        return value
    return ((value + multiple - 1) // multiple) * multiple
