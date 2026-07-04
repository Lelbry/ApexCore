"""Тесты OpenCL GPU-compute бэкенда (`infrastructure/gpu/`).

Две группы:

* **Чистая логика** (без GPU): эвристики парсинга вендора/типа устройства,
  округление, сплющивание платформ×устройств в сквозной индекс, корректная
  деградация при отсутствующем ICD-loader'е, ``supports()`` для FP64. Эти тесты
  используют подставной (fake) :class:`Ocl` через ``monkeypatch`` и не трогают
  реальное железо.
* **Аппаратные** (гейт ``backend.is_available()``): если OpenCL-устройств нет —
  пропускаются через ``pytest.skip``; иначе меряют FP32/пропускную способность и
  проверяют разумные диапазоны и инварианты моделей.
"""

from __future__ import annotations

import threading
from typing import ClassVar

import pytest

from apexcore.domain.gpu import GpuDeviceInfo, GpuDeviceType, GpuMeasurement, GpuWorkloadKind
from apexcore.domain.ports import GpuComputeBackend
from apexcore.infrastructure.gpu import build_default_gpu_backend
from apexcore.infrastructure.gpu import opencl_backend as ob
from apexcore.infrastructure.gpu.opencl_backend import (
    OpenClGpuBackend,
    _classify_device_type,
    _parse_vendor,
    _round_up,
)

# ─────────────────────────── Фабрика / базовый контракт ─────────────────────────


def test_factory_returns_backend_implementing_port() -> None:
    backend = build_default_gpu_backend()
    assert isinstance(backend, OpenClGpuBackend)
    assert isinstance(backend, GpuComputeBackend)
    assert backend.name == "opencl"


def test_is_available_never_raises() -> None:
    # Контракт порта: is_available обязана вернуть bool, а не бросить.
    backend = build_default_gpu_backend()
    result = backend.is_available()
    assert isinstance(result, bool)


# ─────────────────────────── Эвристика вендора ──────────────────────────────────


@pytest.mark.parametrize(
    ("name", "vendor_raw", "platform", "expected"),
    [
        ("NVIDIA GeForce RTX 4070 Ti", "NVIDIA Corporation", "NVIDIA CUDA", "NVIDIA"),
        ("GeForce GTX 1080", "", "", "NVIDIA"),
        ("Quadro P2000", "", "", "NVIDIA"),
        ("Intel(R) UHD Graphics 770", "Intel(R) Corporation", "Intel(R) OpenCL Graphics", "Intel"),
        ("gfx1030", "Advanced Micro Devices, Inc.", "AMD Accelerated Parallel Processing", "AMD"),
        ("AMD Radeon RX 6800 XT", "", "", "AMD"),
        ("Radeon(TM) Graphics", "", "", "AMD"),
        ("Some Weird Accelerator", "Acme Compute", "Acme CL", "Acme Compute"),
        ("Nameless", "", "", "Unknown"),
    ],
)
def test_parse_vendor(name: str, vendor_raw: str, platform: str, expected: str) -> None:
    assert _parse_vendor(name, vendor_raw, platform) == expected


# ─────────────────────────── Эвристика типа устройства ──────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("NVIDIA GeForce RTX 4070 Ti", GpuDeviceType.DISCRETE),
        ("NVIDIA GeForce GTX 1660", GpuDeviceType.DISCRETE),
        ("AMD Radeon RX 6800 XT", GpuDeviceType.DISCRETE),
        ("AMD Radeon Pro W6800", GpuDeviceType.DISCRETE),
        ("Intel(R) Arc(TM) A770 Graphics", GpuDeviceType.DISCRETE),
        ("Tesla V100-SXM2-16GB", GpuDeviceType.DISCRETE),
        ("Intel(R) UHD Graphics 770", GpuDeviceType.INTEGRATED),
        ("Intel(R) HD Graphics 630", GpuDeviceType.INTEGRATED),
        ("Intel(R) Iris(R) Xe Graphics", GpuDeviceType.INTEGRATED),
        ("AMD Radeon(TM) Graphics", GpuDeviceType.INTEGRATED),
        ("AMD Radeon 680M", GpuDeviceType.INTEGRATED),
        ("Some Unknown GPU 9000", GpuDeviceType.UNKNOWN),
    ],
)
def test_classify_device_type(name: str, expected: GpuDeviceType) -> None:
    assert _classify_device_type(name, "") == expected


def test_discrete_token_wins_over_integrated_token() -> None:
    # 'Radeon RX' (discrete) содержит 'radeon', который иначе мог бы срабатывать
    # как integrated; дискретные токены имеют приоритет.
    assert _classify_device_type("AMD Radeon RX 7900 XTX Graphics", "") == GpuDeviceType.DISCRETE


# ─────────────────────────── round-up утилита ───────────────────────────────────


@pytest.mark.parametrize(
    ("value", "multiple", "expected"),
    [
        (1, 64, 64),
        (64, 64, 64),
        (65, 64, 128),
        (1000, 1024, 1024),
        (1025, 1024, 2048),
        (100, 0, 100),  # multiple<=0 → без изменений (защита от деления на 0)
        (100, 1, 100),
    ],
)
def test_round_up(value: int, multiple: int, expected: int) -> None:
    assert _round_up(value, multiple) == expected


# ─────────────────────────── Подставной Ocl (без железа) ────────────────────────


class _FakeOcl:
    """Минимальный fake :class:`Ocl` для проверки логики сплющивания индексов.

    Моделирует две платформы; у первой — 2 GPU (discrete + iGPU), у второй — 1.
    Возвращает ровно те поля устройства, что читает бэкенд.
    """

    _PLATFORMS: ClassVar[dict] = {
        0xA000: {
            "name": "NVIDIA CUDA",
            "version": "OpenCL 3.0 CUDA",
            "devices": {
                0xD001: {
                    "name": "NVIDIA GeForce RTX 4070 Ti",
                    "vendor": "NVIDIA Corporation",
                    "driver": "560.00",
                    "version": "OpenCL 3.0 CUDA",
                    "cu": 60,
                    "clock": 2655,
                    "gmem_mb": 12281,
                    "wgs": 1024,
                    "fp64": 63,
                },
                0xD002: {
                    "name": "NVIDIA Fake iGPU 610M",
                    "vendor": "NVIDIA Corporation",
                    "driver": "560.00",
                    "version": "OpenCL 1.2 CUDA",
                    "cu": 8,
                    "clock": 900,
                    "gmem_mb": 2048,
                    "wgs": 256,
                    "fp64": 0,
                },
            },
        },
        0xB000: {
            "name": "Intel(R) OpenCL Graphics",
            "version": "OpenCL 3.0",
            "devices": {
                0xD101: {
                    "name": "Intel(R) UHD Graphics 770",
                    "vendor": "Intel(R) Corporation",
                    "driver": "31.0",
                    "version": "OpenCL 3.0 NEO",
                    "cu": 32,
                    "clock": 1550,
                    "gmem_mb": 14810,
                    "wgs": 512,
                    "fp64": 0,
                },
            },
        },
    }

    def get_platform_ids(self) -> list[int]:
        return list(self._PLATFORMS.keys())

    def get_device_ids(self, platform_id: int, dev_type: int = 0) -> list[int]:
        return list(self._PLATFORMS[platform_id]["devices"].keys())

    def _dev(self, device_id: int) -> dict:
        for plat in self._PLATFORMS.values():
            if device_id in plat["devices"]:
                return plat["devices"][device_id]
        raise KeyError(device_id)

    def platform_info_str(self, platform_id: int, param: int) -> str:
        plat = self._PLATFORMS[platform_id]
        return plat["name"] if param == ob._ocl.CL_PLATFORM_NAME else plat["version"]

    def device_info_str(self, device_id: int, param: int) -> str:
        d = self._dev(device_id)
        mapping = {
            ob._ocl.CL_DEVICE_NAME: d["name"],
            ob._ocl.CL_DEVICE_VENDOR: d["vendor"],
            ob._ocl.CL_DRIVER_VERSION: d["driver"],
            ob._ocl.CL_DEVICE_VERSION: d["version"],
        }
        return mapping.get(param, "")

    def device_info_uint(self, device_id: int, param: int) -> int:
        d = self._dev(device_id)
        if param == ob._ocl.CL_DEVICE_MAX_COMPUTE_UNITS:
            return d["cu"]
        if param == ob._ocl.CL_DEVICE_MAX_CLOCK_FREQUENCY:
            return d["clock"]
        return 0

    def device_info_ulong(self, device_id: int, param: int) -> int:
        d = self._dev(device_id)
        if param == ob._ocl.CL_DEVICE_GLOBAL_MEM_SIZE:
            return d["gmem_mb"] * 1024 * 1024
        if param == ob._ocl.CL_DEVICE_DOUBLE_FP_CONFIG:
            return d["fp64"]
        return 0

    def device_info_size_t(self, device_id: int, param: int) -> int:
        d = self._dev(device_id)
        if param == ob._ocl.CL_DEVICE_MAX_WORK_GROUP_SIZE:
            return d["wgs"]
        return 0


@pytest.fixture()
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> OpenClGpuBackend:
    """Бэкенд с подставным Ocl — перечисление без реального железа."""

    backend = OpenClGpuBackend()
    fake = _FakeOcl()
    # _ensure_loaded кэширует результат _ocl.load(); подменяем и load, и уже
    # инициализированные поля, чтобы никакой реальный loader не подхватился.
    monkeypatch.setattr(ob._ocl, "load", lambda: fake)
    backend._ocl = fake
    backend._loaded = True
    return backend


def test_flatten_device_indexing(fake_backend: OpenClGpuBackend) -> None:
    devices = fake_backend.list_devices()
    # 2 платформы: 2 + 1 = 3 устройства, сквозной индекс 0..2.
    assert [d.index for d in devices] == [0, 1, 2]
    assert devices[0].name == "NVIDIA GeForce RTX 4070 Ti"
    assert devices[1].name == "NVIDIA Fake iGPU 610M"
    assert devices[2].name == "Intel(R) UHD Graphics 770"
    # Порядок стабилен между вызовами (кэш).
    assert [d.index for d in fake_backend.list_devices()] == [0, 1, 2]


def test_device_fields_populated(fake_backend: OpenClGpuBackend) -> None:
    rtx = fake_backend.list_devices()[0]
    assert rtx.vendor == "NVIDIA"
    assert rtx.platform_name == "NVIDIA CUDA"
    assert rtx.device_type == GpuDeviceType.DISCRETE
    assert rtx.opencl_version == "OpenCL 3.0 CUDA"
    assert rtx.driver_version == "560.00"
    assert rtx.compute_units == 60
    assert rtx.max_clock_mhz == 2655
    assert rtx.global_mem_mb == 12281
    assert rtx.max_work_group_size == 1024
    assert rtx.fp64_supported is True
    assert rtx.arch is None  # разрешается отдельным модулем

    igpu = fake_backend.list_devices()[2]
    assert igpu.vendor == "Intel"
    assert igpu.device_type == GpuDeviceType.INTEGRATED
    assert igpu.fp64_supported is False


def test_is_available_true_with_fake_devices(fake_backend: OpenClGpuBackend) -> None:
    assert fake_backend.is_available() is True


def test_supports_fp64_reflects_device(fake_backend: OpenClGpuBackend) -> None:
    # index 0 = RTX (fp64=True), index 2 = Intel (fp64=False)
    assert fake_backend.supports(0, GpuWorkloadKind.FP64) is True
    assert fake_backend.supports(2, GpuWorkloadKind.FP64) is False
    # Прочие нагрузки поддерживаются при существующем устройстве.
    for kind in (GpuWorkloadKind.FP32, GpuWorkloadKind.MEM_BANDWIDTH, GpuWorkloadKind.PCIE_H2D):
        assert fake_backend.supports(0, kind) is True


def test_supports_out_of_range_is_false(fake_backend: OpenClGpuBackend) -> None:
    assert fake_backend.supports(99, GpuWorkloadKind.FP32) is False
    assert fake_backend.supports(-1, GpuWorkloadKind.FP32) is False


def test_measure_out_of_range_raises(fake_backend: OpenClGpuBackend) -> None:
    with pytest.raises(IndexError):
        fake_backend.measure(99, GpuWorkloadKind.FP32, 0.1)


# ─────────────────────────── Деградация без loader'а ────────────────────────────


def test_loader_missing_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет ICD-loader'а → is_available False, list_devices пуст, никаких исключений."""

    monkeypatch.setattr(ob._ocl, "load", lambda: None)
    backend = OpenClGpuBackend()
    assert backend.is_available() is False
    assert backend.list_devices() == []
    assert backend.supports(0, GpuWorkloadKind.FP32) is False


def test_loader_missing_measure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """measure() при недоступном loader'е — понятный RuntimeError (а не сегфолт)."""

    monkeypatch.setattr(ob._ocl, "load", lambda: None)
    backend = OpenClGpuBackend()
    with pytest.raises(RuntimeError):
        backend.measure(0, GpuWorkloadKind.FP32, 0.1)


def test_load_wrapper_returns_none_when_no_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ocl.load() возвращает None (а не бросает), если loader не находится."""

    # Сбрасываем кэш модуля и заставляем _load_loader вернуть None.
    monkeypatch.setattr(ob._ocl, "_CACHED", None, raising=False)
    monkeypatch.setattr(ob._ocl, "_LOAD_ATTEMPTED", False, raising=False)
    monkeypatch.setattr(ob._ocl, "_load_loader", lambda: None)
    assert ob._ocl.load() is None


# ─────────────────────────── Аппаратные тесты (гейт) ────────────────────────────


@pytest.fixture(scope="module")
def real_backend() -> OpenClGpuBackend:
    backend = OpenClGpuBackend()
    if not backend.is_available():
        pytest.skip("OpenCL-устройства недоступны на этом хосте")
    return backend


def _first_index(backend: OpenClGpuBackend) -> int:
    return backend.list_devices()[0].index


def test_hw_enumerate_at_least_one_device(real_backend: OpenClGpuBackend) -> None:
    devices = real_backend.list_devices()
    assert len(devices) >= 1
    for d in devices:
        assert isinstance(d, GpuDeviceInfo)
        assert d.name
        assert d.compute_units > 0
        assert d.max_clock_mhz > 0
        assert d.global_mem_mb > 0


def test_hw_measure_fp32_sane(real_backend: OpenClGpuBackend) -> None:
    idx = _first_index(real_backend)
    m = real_backend.measure(idx, GpuWorkloadKind.FP32, 1.0)
    assert isinstance(m, GpuMeasurement)
    assert m.kind == GpuWorkloadKind.FP32
    assert m.unit == "GFLOPS"
    assert m.iterations >= 1
    assert m.error_count == 0
    # Любой реальный GPU выдаёт заметно больше нуля FP32 GFLOPS; верхняя граница
    # (широкая) ловит абсурд от битого FLOP-счёта/сломанного тайминга.
    assert 10.0 < m.throughput < 500_000.0
    assert m.extra["work_done"] > 0
    assert m.duration_sec > 0


def test_hw_measure_bandwidth_sane(real_backend: OpenClGpuBackend) -> None:
    idx = _first_index(real_backend)
    m = real_backend.measure(idx, GpuWorkloadKind.MEM_BANDWIDTH, 1.0)
    assert m.kind == GpuWorkloadKind.MEM_BANDWIDTH
    assert m.unit == "GB/s"
    assert m.iterations >= 1
    assert 1.0 < m.throughput < 10_000.0


def test_hw_fp64_respects_support(real_backend: OpenClGpuBackend) -> None:
    """FP64: где поддерживается — меряется; где нет — measure бросает, supports=False."""

    for d in real_backend.list_devices():
        supported = real_backend.supports(d.index, GpuWorkloadKind.FP64)
        assert supported == d.fp64_supported
        if not supported:
            with pytest.raises(RuntimeError):
                real_backend.measure(d.index, GpuWorkloadKind.FP64, 0.5)


def test_hw_cancel_token_stops_early(real_backend: OpenClGpuBackend) -> None:
    """Уже установленный cancel_token → measure возвращается почти сразу."""

    idx = _first_index(real_backend)
    token = threading.Event()
    token.set()
    m = real_backend.measure(idx, GpuWorkloadKind.FP32, 30.0, cancel_token=token)
    # Ничего не успели посчитать (или один заход) — но точно не крутились 30 с.
    assert m.duration_sec < 2.0
