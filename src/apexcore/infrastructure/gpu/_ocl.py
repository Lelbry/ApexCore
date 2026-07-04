"""Низкоуровневая обёртка над OpenCL ICD-loader через ``ctypes``.

Модуль НЕ тянет `pyopencl` и вообще никаких новых зависимостей: он загружает
системный ICD-loader (`OpenCL.dll` на Windows, `libOpenCL.so.1`/`libOpenCL.so`
на Linux) и биндит ровно те функции OpenCL 1.2, что нужны бэкенду
:mod:`apexcore.infrastructure.gpu.opencl_backend`:

* перечисление платформ/устройств (``clGetPlatformIDs`` / ``clGetDeviceIDs`` /
  ``clGet*Info``) — паттерны заимствованы из рабочего пробника ``_ocl_probe.py``;
* создание контекста, очереди с профилированием, программы, буферов, кернелов;
* постановка кернела/трансфера в очередь и чтение времени через
  ``clGetEventProfilingInfo`` (наносекунды на стороне устройства).

Всё, что аллоцирует ресурс OpenCL, парно освобождается: сами вызовы
``clRelease*`` собраны здесь, а :class:`OclContext` в бэкенде выстраивает их в
``try/finally``. Каждый ``cl_int`` код проверяется — ненулевой превращается в
:class:`OpenClError`, чтобы утечка/двойное освобождение не роняли интерпретатор
молча.

Обёртка намеренно тонкая: никакой бизнес-логики (выбор размеров буферов, выбор
метрики) здесь нет — это уровень «сырых» вызовов.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
from typing import Any

# ─────────────────────────── Типы OpenCL ────────────────────────────────────────

cl_int = ctypes.c_int32
cl_uint = ctypes.c_uint32
cl_ulong = ctypes.c_uint64
cl_bitfield = ctypes.c_uint64
# Все opaque-хэндлы OpenCL (cl_platform_id, cl_device_id, cl_context, …) —
# указатели; в ctypes представляем их как c_void_p.
cl_handle = ctypes.c_void_p

# ─────────────────────────── Константы OpenCL ───────────────────────────────────

CL_SUCCESS = 0

# device type bitfield
CL_DEVICE_TYPE_ALL = 0xFFFFFFFF
CL_DEVICE_TYPE_GPU = 1 << 2
CL_DEVICE_TYPE_CPU = 1 << 1

# platform info
CL_PLATFORM_NAME = 0x0902
CL_PLATFORM_VERSION = 0x0901

# device info
CL_DEVICE_NAME = 0x102B
CL_DEVICE_VENDOR = 0x102C
CL_DRIVER_VERSION = 0x102D
CL_DEVICE_VERSION = 0x102F
CL_DEVICE_TYPE = 0x1000
CL_DEVICE_MAX_COMPUTE_UNITS = 0x1002
CL_DEVICE_MAX_WORK_GROUP_SIZE = 0x1004
CL_DEVICE_MAX_CLOCK_FREQUENCY = 0x100C
CL_DEVICE_GLOBAL_MEM_SIZE = 0x101F
CL_DEVICE_MAX_MEM_ALLOC_SIZE = 0x1010
CL_DEVICE_DOUBLE_FP_CONFIG = 0x1032

# context / queue
CL_QUEUE_PROFILING_ENABLE = 1 << 1
CL_CONTEXT_PLATFORM = 0x1084

# memory flags
CL_MEM_READ_WRITE = 1 << 0
CL_MEM_WRITE_ONLY = 1 << 1
CL_MEM_READ_ONLY = 1 << 2
CL_MEM_ALLOC_HOST_PTR = 1 << 4

# program build info
CL_PROGRAM_BUILD_LOG = 0x1183

# event profiling
CL_PROFILING_COMMAND_START = 0x1281
CL_PROFILING_COMMAND_END = 0x1282

# blocking flags
CL_TRUE = 1
CL_FALSE = 0


class OpenClError(RuntimeError):
    """Ошибка OpenCL: ненулевой ``cl_int`` из любого ``clXxx``-вызова."""

    def __init__(self, func: str, code: int) -> None:
        self.func = func
        self.code = code
        super().__init__(f"{func} failed: {code} ({_ERROR_NAMES.get(code, 'CL_UNKNOWN')})")


# Наиболее вероятные коды ошибок — для читаемых сообщений (не исчерпывающе).
_ERROR_NAMES: dict[int, str] = {
    0: "CL_SUCCESS",
    -1: "CL_DEVICE_NOT_FOUND",
    -2: "CL_DEVICE_NOT_AVAILABLE",
    -3: "CL_COMPILER_NOT_AVAILABLE",
    -4: "CL_MEM_OBJECT_ALLOCATION_FAILURE",
    -5: "CL_OUT_OF_RESOURCES",
    -6: "CL_OUT_OF_HOST_MEMORY",
    -11: "CL_BUILD_PROGRAM_FAILURE",
    -30: "CL_INVALID_VALUE",
    -33: "CL_INVALID_DEVICE",
    -34: "CL_INVALID_CONTEXT",
    -36: "CL_INVALID_COMMAND_QUEUE",
    -38: "CL_INVALID_MEM_OBJECT",
    -44: "CL_INVALID_PROGRAM",
    -45: "CL_INVALID_PROGRAM_EXECUTABLE",
    -46: "CL_INVALID_KERNEL_NAME",
    -48: "CL_INVALID_KERNEL",
    -49: "CL_INVALID_ARG_INDEX",
    -50: "CL_INVALID_ARG_VALUE",
    -51: "CL_INVALID_ARG_SIZE",
    -52: "CL_INVALID_KERNEL_ARGS",
    -54: "CL_INVALID_WORK_GROUP_SIZE",
    -55: "CL_INVALID_WORK_ITEM_SIZE",
    -61: "CL_INVALID_BUFFER_SIZE",
}


def _load_loader() -> ctypes.CDLL | None:
    """Загрузить ICD-loader. Вернуть ``CDLL`` или ``None``, если не найден.

    Никогда не бросает: единственная возможная ошибка (loader отсутствует)
    возвращается как ``None`` — вызывающий трактует это как «бэкенд недоступен».
    """

    candidates: list[str] = []
    if platform.system() == "Windows":
        candidates = ["OpenCL.dll"]
    else:
        candidates = ["libOpenCL.so.1", "libOpenCL.so"]
        found = ctypes.util.find_library("OpenCL")
        if found:
            candidates.insert(0, found)

    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _bind(lib: ctypes.CDLL) -> None:
    """Проставить argtypes/restype для используемых функций OpenCL.

    Явные сигнатуры критичны на 64-бит: без них ctypes считает указатели
    32-битными int и хэндлы/`size_t` рвутся (обрезка старших бит).
    """

    p = ctypes.c_void_p
    sz = ctypes.c_size_t
    psz = ctypes.POINTER(ctypes.c_size_t)
    pui = ctypes.POINTER(cl_uint)
    pint = ctypes.POINTER(cl_int)

    lib.clGetPlatformIDs.argtypes = [cl_uint, p, pui]
    lib.clGetPlatformIDs.restype = cl_int
    lib.clGetPlatformInfo.argtypes = [p, cl_uint, sz, p, psz]
    lib.clGetPlatformInfo.restype = cl_int

    lib.clGetDeviceIDs.argtypes = [p, cl_bitfield, cl_uint, p, pui]
    lib.clGetDeviceIDs.restype = cl_int
    lib.clGetDeviceInfo.argtypes = [p, cl_uint, sz, p, psz]
    lib.clGetDeviceInfo.restype = cl_int

    lib.clCreateContext.argtypes = [p, cl_uint, p, p, p, pint]
    lib.clCreateContext.restype = p
    lib.clReleaseContext.argtypes = [p]
    lib.clReleaseContext.restype = cl_int

    # clCreateCommandQueue устарел в OpenCL 2.0, но присутствует во всех ICD и
    # не требует передавать свойства структурой (в отличие от …WithProperties).
    lib.clCreateCommandQueue.argtypes = [p, p, cl_bitfield, pint]
    lib.clCreateCommandQueue.restype = p
    lib.clReleaseCommandQueue.argtypes = [p]
    lib.clReleaseCommandQueue.restype = cl_int
    lib.clFinish.argtypes = [p]
    lib.clFinish.restype = cl_int

    lib.clCreateProgramWithSource.argtypes = [p, cl_uint, ctypes.POINTER(ctypes.c_char_p), psz, pint]
    lib.clCreateProgramWithSource.restype = p
    lib.clBuildProgram.argtypes = [p, cl_uint, p, ctypes.c_char_p, p, p]
    lib.clBuildProgram.restype = cl_int
    lib.clGetProgramBuildInfo.argtypes = [p, p, cl_uint, sz, p, psz]
    lib.clGetProgramBuildInfo.restype = cl_int
    lib.clReleaseProgram.argtypes = [p]
    lib.clReleaseProgram.restype = cl_int

    lib.clCreateKernel.argtypes = [p, ctypes.c_char_p, pint]
    lib.clCreateKernel.restype = p
    lib.clSetKernelArg.argtypes = [p, cl_uint, sz, p]
    lib.clSetKernelArg.restype = cl_int
    lib.clReleaseKernel.argtypes = [p]
    lib.clReleaseKernel.restype = cl_int

    lib.clCreateBuffer.argtypes = [p, cl_bitfield, sz, p, pint]
    lib.clCreateBuffer.restype = p
    lib.clReleaseMemObject.argtypes = [p]
    lib.clReleaseMemObject.restype = cl_int

    lib.clEnqueueNDRangeKernel.argtypes = [p, p, cl_uint, psz, psz, psz, cl_uint, p, p]
    lib.clEnqueueNDRangeKernel.restype = cl_int
    lib.clEnqueueWriteBuffer.argtypes = [p, p, cl_uint, sz, sz, p, cl_uint, p, p]
    lib.clEnqueueWriteBuffer.restype = cl_int
    lib.clEnqueueReadBuffer.argtypes = [p, p, cl_uint, sz, sz, p, cl_uint, p, p]
    lib.clEnqueueReadBuffer.restype = cl_int

    lib.clWaitForEvents.argtypes = [cl_uint, p]
    lib.clWaitForEvents.restype = cl_int
    lib.clGetEventProfilingInfo.argtypes = [p, cl_uint, sz, p, psz]
    lib.clGetEventProfilingInfo.restype = cl_int
    lib.clReleaseEvent.argtypes = [p]
    lib.clReleaseEvent.restype = cl_int


class Ocl:
    """Держатель загруженного loader'а + типизированные врапперы вызовов.

    Экземпляр создаётся один раз (:func:`load`) и кэшируется. Методы —
    тонкие обёртки: проверяют ``cl_int`` и превращают ненулевой код в
    :class:`OpenClError`. Опрос строковых/скалярных полей устройства/платформы
    вынесен в helper'ы ``*_info_str`` / ``*_info_uint`` / ``*_info_ulong``.
    """

    def __init__(self, lib: ctypes.CDLL) -> None:
        self.lib = lib

    # ---- перечисление ----

    def get_platform_ids(self) -> list[int]:
        num = cl_uint()
        err = self.lib.clGetPlatformIDs(0, None, ctypes.byref(num))
        if err != CL_SUCCESS:
            raise OpenClError("clGetPlatformIDs", err)
        if num.value == 0:
            return []
        arr = (ctypes.c_void_p * num.value)()
        err = self.lib.clGetPlatformIDs(num.value, arr, None)
        if err != CL_SUCCESS:
            raise OpenClError("clGetPlatformIDs", err)
        return [int(arr[i] or 0) for i in range(num.value)]

    def get_device_ids(self, platform_id: int, dev_type: int = CL_DEVICE_TYPE_GPU) -> list[int]:
        num = cl_uint()
        err = self.lib.clGetDeviceIDs(platform_id, dev_type, 0, None, ctypes.byref(num))
        # CL_DEVICE_NOT_FOUND (-1) — легитимно: у платформы нет устройств этого типа.
        if err == -1 or num.value == 0:
            return []
        if err != CL_SUCCESS:
            raise OpenClError("clGetDeviceIDs", err)
        arr = (ctypes.c_void_p * num.value)()
        err = self.lib.clGetDeviceIDs(platform_id, dev_type, num.value, arr, None)
        if err != CL_SUCCESS:
            raise OpenClError("clGetDeviceIDs", err)
        return [int(arr[i] or 0) for i in range(num.value)]

    # ---- info helpers ----

    def platform_info_str(self, platform_id: int, param: int) -> str:
        size = ctypes.c_size_t()
        if self.lib.clGetPlatformInfo(platform_id, param, 0, None, ctypes.byref(size)) != CL_SUCCESS:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if self.lib.clGetPlatformInfo(platform_id, param, size.value, buf, None) != CL_SUCCESS:
            return ""
        return buf.value.decode(errors="replace")

    def device_info_str(self, device_id: int, param: int) -> str:
        size = ctypes.c_size_t()
        if self.lib.clGetDeviceInfo(device_id, param, 0, None, ctypes.byref(size)) != CL_SUCCESS:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if self.lib.clGetDeviceInfo(device_id, param, size.value, buf, None) != CL_SUCCESS:
            return ""
        return buf.value.decode(errors="replace")

    def device_info_uint(self, device_id: int, param: int) -> int:
        val = cl_uint()
        if self.lib.clGetDeviceInfo(device_id, param, 4, ctypes.byref(val), None) != CL_SUCCESS:
            return 0
        return int(val.value)

    def device_info_ulong(self, device_id: int, param: int) -> int:
        val = cl_ulong()
        if self.lib.clGetDeviceInfo(device_id, param, 8, ctypes.byref(val), None) != CL_SUCCESS:
            return 0
        return int(val.value)

    def device_info_size_t(self, device_id: int, param: int) -> int:
        val = ctypes.c_size_t()
        if self.lib.clGetDeviceInfo(device_id, param, ctypes.sizeof(val), ctypes.byref(val), None) != CL_SUCCESS:
            return 0
        return int(val.value)

    # ---- context / queue ----

    def create_context(self, platform_id: int, device_id: int) -> int:
        # Свойства контекста: [CL_CONTEXT_PLATFORM, <pid>, 0] — привязываем к
        # платформе устройства (обязательно, если в системе несколько ICD).
        props = (ctypes.c_void_p * 3)(
            ctypes.c_void_p(CL_CONTEXT_PLATFORM),
            ctypes.c_void_p(platform_id),
            ctypes.c_void_p(0),
        )
        dev = (ctypes.c_void_p * 1)(ctypes.c_void_p(device_id))
        err = cl_int()
        ctx = self.lib.clCreateContext(props, 1, dev, None, None, ctypes.byref(err))
        if err.value != CL_SUCCESS or not ctx:
            raise OpenClError("clCreateContext", err.value)
        return int(ctx)

    def create_command_queue(self, context: int, device_id: int, profiling: bool = True) -> int:
        props = CL_QUEUE_PROFILING_ENABLE if profiling else 0
        err = cl_int()
        q = self.lib.clCreateCommandQueue(context, device_id, props, ctypes.byref(err))
        if err.value != CL_SUCCESS or not q:
            raise OpenClError("clCreateCommandQueue", err.value)
        return int(q)

    def finish(self, queue: int) -> None:
        err = self.lib.clFinish(queue)
        if err != CL_SUCCESS:
            raise OpenClError("clFinish", err)

    # ---- program / kernel ----

    def build_program(self, context: int, device_id: int, source: str) -> int:
        src_bytes = source.encode("utf-8")
        src_ptr = ctypes.c_char_p(src_bytes)
        length = ctypes.c_size_t(len(src_bytes))
        err = cl_int()
        prog = self.lib.clCreateProgramWithSource(
            context, 1, ctypes.byref(src_ptr), ctypes.byref(length), ctypes.byref(err)
        )
        if err.value != CL_SUCCESS or not prog:
            raise OpenClError("clCreateProgramWithSource", err.value)

        dev = (ctypes.c_void_p * 1)(ctypes.c_void_p(device_id))
        berr = self.lib.clBuildProgram(prog, 1, dev, None, None, None)
        if berr != CL_SUCCESS:
            log = self._program_build_log(prog, device_id)
            # Освобождаем недособранную программу до подъёма исключения.
            self.lib.clReleaseProgram(prog)
            raise OpenClError(f"clBuildProgram (build log: {log.strip()[:400]})", berr)
        return int(prog)

    def _program_build_log(self, program: int, device_id: int) -> str:
        size = ctypes.c_size_t()
        if (
            self.lib.clGetProgramBuildInfo(program, device_id, CL_PROGRAM_BUILD_LOG, 0, None, ctypes.byref(size))
            != CL_SUCCESS
        ):
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if (
            self.lib.clGetProgramBuildInfo(program, device_id, CL_PROGRAM_BUILD_LOG, size.value, buf, None)
            != CL_SUCCESS
        ):
            return ""
        return buf.value.decode(errors="replace")

    def create_kernel(self, program: int, name: str) -> int:
        err = cl_int()
        k = self.lib.clCreateKernel(program, name.encode("ascii"), ctypes.byref(err))
        if err.value != CL_SUCCESS or not k:
            raise OpenClError("clCreateKernel", err.value)
        return int(k)

    def set_kernel_arg(self, kernel: int, index: int, size: int, value: Any) -> None:
        err = self.lib.clSetKernelArg(kernel, index, size, value)
        if err != CL_SUCCESS:
            raise OpenClError(f"clSetKernelArg(index={index})", err)

    def set_kernel_arg_mem(self, kernel: int, index: int, mem_obj: int) -> None:
        buf = ctypes.c_void_p(mem_obj)
        self.set_kernel_arg(kernel, index, ctypes.sizeof(buf), ctypes.byref(buf))

    # ---- buffers ----

    def create_buffer(self, context: int, flags: int, size: int, host_ptr: Any = None) -> int:
        err = cl_int()
        mem = self.lib.clCreateBuffer(context, flags, size, host_ptr, ctypes.byref(err))
        if err.value != CL_SUCCESS or not mem:
            raise OpenClError("clCreateBuffer", err.value)
        return int(mem)

    # ---- enqueue + profiling ----

    def enqueue_ndrange(self, queue: int, kernel: int, global_size: int, local_size: int | None) -> int:
        """Запустить кернел 1-D NDRange. Вернуть event-хэндл (для профилирования)."""

        gsz = (ctypes.c_size_t * 1)(global_size)
        lsz = (ctypes.c_size_t * 1)(local_size) if local_size else None
        event = ctypes.c_void_p()
        err = self.lib.clEnqueueNDRangeKernel(
            queue, kernel, 1, None, gsz, lsz, 0, None, ctypes.byref(event)
        )
        if err != CL_SUCCESS or not event:
            raise OpenClError("clEnqueueNDRangeKernel", err)
        return int(event.value)

    def enqueue_write_buffer(self, queue: int, mem: int, size: int, host_ptr: Any, blocking: bool = False) -> int:
        event = ctypes.c_void_p()
        err = self.lib.clEnqueueWriteBuffer(
            queue, mem, CL_TRUE if blocking else CL_FALSE, 0, size, host_ptr, 0, None, ctypes.byref(event)
        )
        if err != CL_SUCCESS or not event:
            raise OpenClError("clEnqueueWriteBuffer", err)
        return int(event.value)

    def enqueue_read_buffer(self, queue: int, mem: int, size: int, host_ptr: Any, blocking: bool = False) -> int:
        event = ctypes.c_void_p()
        err = self.lib.clEnqueueReadBuffer(
            queue, mem, CL_TRUE if blocking else CL_FALSE, 0, size, host_ptr, 0, None, ctypes.byref(event)
        )
        if err != CL_SUCCESS or not event:
            raise OpenClError("clEnqueueReadBuffer", err)
        return int(event.value)

    def wait_for_event(self, event: int) -> None:
        ev = (ctypes.c_void_p * 1)(ctypes.c_void_p(event))
        err = self.lib.clWaitForEvents(1, ev)
        if err != CL_SUCCESS:
            raise OpenClError("clWaitForEvents", err)

    def event_elapsed_ns(self, event: int) -> int:
        """Вернуть время выполнения команды (END−START) в наносекундах.

        Требует, чтобы очередь была создана с ``CL_QUEUE_PROFILING_ENABLE`` и
        событие уже завершилось (вызывающий делает :meth:`wait_for_event`).
        """

        start = cl_ulong()
        end = cl_ulong()
        e1 = self.lib.clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_START, 8, ctypes.byref(start), None)
        e2 = self.lib.clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_END, 8, ctypes.byref(end), None)
        if e1 != CL_SUCCESS or e2 != CL_SUCCESS:
            raise OpenClError("clGetEventProfilingInfo", e1 or e2)
        return int(end.value - start.value)

    # ---- release (никогда не бросают — вызываются в finally) ----

    def release_event(self, event: int | None) -> None:
        if event:
            self.lib.clReleaseEvent(event)

    def release_mem(self, mem: int | None) -> None:
        if mem:
            self.lib.clReleaseMemObject(mem)

    def release_kernel(self, kernel: int | None) -> None:
        if kernel:
            self.lib.clReleaseKernel(kernel)

    def release_program(self, program: int | None) -> None:
        if program:
            self.lib.clReleaseProgram(program)

    def release_queue(self, queue: int | None) -> None:
        if queue:
            self.lib.clReleaseCommandQueue(queue)

    def release_context(self, context: int | None) -> None:
        if context:
            self.lib.clReleaseContext(context)


_CACHED: Ocl | None = None
_LOAD_ATTEMPTED = False


def load() -> Ocl | None:
    """Загрузить и закэшировать обёртку OpenCL. ``None`` — loader недоступен.

    Идемпотентна: повторный вызов возвращает закэшированный экземпляр (или
    закэшированный ``None``, если loader не найден при первой попытке).
    Никогда не бросает исключение — это точка «is_available».
    """

    global _CACHED, _LOAD_ATTEMPTED
    if _LOAD_ATTEMPTED:
        return _CACHED
    _LOAD_ATTEMPTED = True
    try:
        lib = _load_loader()
        if lib is None:
            _CACHED = None
            return None
        _bind(lib)
        _CACHED = Ocl(lib)
    except (OSError, AttributeError, ValueError):
        # AttributeError — если loader есть, но не экспортирует ожидаемый символ
        # (крайне маловероятно для валидного ICD, но перестрахуемся).
        _CACHED = None
    return _CACHED
