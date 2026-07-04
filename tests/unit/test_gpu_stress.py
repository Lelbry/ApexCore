"""Тесты `application/gpu_stress` — GPU стресс-тест / термостабильность.

Всё гоняется на фейках (fake backend + fake telemetry) — реальный GPU/OpenCL
не нужен. Главные инварианты:

1. Стабильные частоты + прохлада → PASS.
2. Обвал частоты (last-third << first-third) → FAIL.
3. Пик температуры у порога NVML slowdown → WARN; на самом пороге → FAIL.
4. Нет телеметрии (пустые отсчёты) → UNKNOWN, но нагрузка выполнена.
5. Отмена посреди прогона → cancelled=True, вердикт по частичным данным (WARN).
6. Бэкенд недоступен / нет устройства / плохой индекс → graceful UNKNOWN, без
   исключения, нагрузка не гонялась.

Плюс отдельные тесты чистых хелперов ``summarize_gpu_stress`` /
``compute_gpu_stress_verdict`` (без оркестратора и потоков).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from apexcore.application.gpu_stress import (
    FALLBACK_TEMP_FAIL_C,
    GpuStressOrchestrator,
    GpuTelemetryReading,
    _Series,
    compute_gpu_stress_verdict,
    summarize_gpu_stress,
)
from apexcore.domain.gpu import (
    GpuDeviceInfo,
    GpuDeviceType,
    GpuMeasurement,
    GpuStressVerdict,
    GpuWorkloadKind,
)
from apexcore.domain.models import CpuCores, SystemInfo

# ─── Fakes ───────────────────────────────────────────────────────────────────


class _FakeAdapter:
    name = "fake"

    def __init__(self, sys_info: SystemInfo) -> None:
        self._sys_info = sys_info

    def get_system_info(self) -> SystemInfo:
        return self._sys_info

    def get_current_metrics(self):  # pragma: no cover — hwmon-путь не в этих тестах
        raise NotImplementedError


def _make_sys_info() -> SystemInfo:
    return SystemInfo(
        os_name="Windows",
        os_version="10.0",
        cpu_model="Intel(R) Core(TM) i9-12900K",
        cpu_cores=CpuCores(physical=16, logical=24),
        ram_total_gb=32.0,
        gpu_list=["NVIDIA GeForce RTX 4070 Ti"],
        cpu_arch="x86_64",
        hostname="test-host",
        cpu_base_mhz=3200.0,
        timestamp=datetime.now(timezone.utc),
    )


def _discrete_device(name: str = "NVIDIA GeForce RTX 4070 Ti") -> GpuDeviceInfo:
    return GpuDeviceInfo(
        index=0,
        name=name,
        vendor="NVIDIA",
        platform_name="NVIDIA CUDA",
        device_type=GpuDeviceType.DISCRETE,
        compute_units=60,
        max_clock_mhz=2655,
        global_mem_mb=12288,
        fp64_supported=True,
    )


class _StepClock:
    """Общий счётчик «шагов нагрузки»: связывает fake-бэкенд и fake-телеметрию.

    Бэкенд инкрементирует его на каждой итерации нагрузки; телеметрия читает
    текущий шаг, чтобы вернуть reading этой позиции серии. Так форма серии
    **детерминирована** и не зависит от того, насколько часто оркестратор
    опрашивает ``sample()`` (устраняет схлопывание тренда при оверсемплинге).
    """

    def __init__(self) -> None:
        self.step = 0


class _FakeBackend:
    """In-memory бэкенд: ``measure(SUSTAINED_STRESS)`` «крутит» нагрузку.

    Чтобы цикл семплирования оркестратора успел снять несколько отсчётов на
    коротком тесте, ``measure`` не спит по-настоящему, а делает ``iterations``
    коротких итераций (по ``iter_sec``), проверяя ``cancel_token``, и на каждой
    двигает общий ``clock.step``. Так тест остаётся быстрым (< 0.2 с), поток
    нагрузки реально живёт параллельно семплеру, а телеметрия привязана к шагу
    нагрузки. ``cancel_after_iters`` — эмулировать внешнюю отмену: движок сам
    ставит ``cancel_token`` после N итераций (как «Стоп» в UI).
    """

    name = "fake_backend"

    def __init__(
        self,
        devices: list[GpuDeviceInfo],
        *,
        available: bool = True,
        iterations: int = 6,
        iter_sec: float = 0.01,
        cancel_after_iters: int | None = None,
        raise_on_measure: bool = False,
        clock: _StepClock | None = None,
    ) -> None:
        self._devices = devices
        self._available = available
        self._iterations = iterations
        self._iter_sec = iter_sec
        self._cancel_after = cancel_after_iters
        self._raise = raise_on_measure
        self._clock = clock or _StepClock()
        self.measured_kinds: list[GpuWorkloadKind] = []

    @property
    def clock(self) -> _StepClock:
        return self._clock

    def is_available(self) -> bool:
        return self._available

    def list_devices(self) -> list[GpuDeviceInfo]:
        return list(self._devices)

    def supports(self, device_index: int, kind: GpuWorkloadKind) -> bool:
        return True

    def measure(
        self,
        device_index: int,
        kind: GpuWorkloadKind,
        duration_sec: float,
        cancel_token: threading.Event | None = None,
    ) -> GpuMeasurement:
        self.measured_kinds.append(kind)
        if self._raise:
            raise RuntimeError("kernel launch failed")
        done = 0
        for i in range(self._iterations):
            if cancel_token is not None and cancel_token.is_set():
                break
            threading.Event().wait(self._iter_sec)  # короткая «работа»
            done = i + 1
            self._clock.step = done
            if self._cancel_after is not None and done >= self._cancel_after and cancel_token is not None:
                cancel_token.set()
                break
        return GpuMeasurement(
            kind=kind,
            throughput=30000.0,
            unit="GFLOPS",
            duration_sec=done * self._iter_sec,
            iterations=done,
        )


class _FakeTelemetry:
    """Заскриптованный семплер, привязанный к шагу нагрузки через ``_StepClock``.

    Индекс reading'а = текущий ``clock.step`` нагрузки (1-based) → позиция в
    серии. Поэтому **форма серии не зависит от частоты семплирования**: сколько
    бы раз оркестратор ни дёрнул ``sample()`` внутри одной итерации нагрузки,
    он получит reading именно этого шага. За пределами серии повторяется
    последний reading. Если ``clock`` не передан — деградирует до счётчика
    вызовов (для тестов чистых хелперов клок не нужен).

    ``readings`` — список :class:`GpuTelemetryReading` (пустой → всегда пустой
    отсчёт). ``limit_c`` — порог теплового замедления (или None).
    """

    def __init__(
        self,
        readings: list[GpuTelemetryReading],
        *,
        limit_c: float | None = 95.0,
        clock: _StepClock | None = None,
    ) -> None:
        self._readings = readings
        self._limit = limit_c
        self._clock = clock
        self._fallback_i = 0
        self.sample_calls = 0

    def thermal_limit_c(self) -> float | None:
        return self._limit

    def sample(self) -> GpuTelemetryReading:
        self.sample_calls += 1
        if not self._readings:
            return GpuTelemetryReading()
        if self._clock is not None:
            idx = max(0, self._clock.step - 1)
        else:
            idx = self._fallback_i
            self._fallback_i += 1
        idx = min(idx, len(self._readings) - 1)
        return self._readings[idx]


def _reading(temp=None, power=None, clock=None, util=None) -> GpuTelemetryReading:
    return GpuTelemetryReading(temp_c=temp, power_w=power, clock_mhz=clock, util_pct=util)


def _orch(backend: _FakeBackend) -> GpuStressOrchestrator:
    # Крошечный интервал семплирования: на fake-прогоне (итерации по iter_sec)
    # цикл успевает снять по отсчёту почти на каждую итерацию — так серия
    # набирает достаточно точек для тренда без реальных секундных пауз.
    return GpuStressOrchestrator(
        _FakeAdapter(_make_sys_info()), backend, sample_interval_sec=0.001
    )


def _paired(
    readings: list[GpuTelemetryReading],
    *,
    limit_c: float | None = 95.0,
    iterations: int | None = None,
    iter_sec: float = 0.02,
    cancel_after_iters: int | None = None,
) -> tuple[_FakeBackend, _FakeTelemetry]:
    """Собрать бэкенд + телеметрию, разделяющих один ``_StepClock``.

    Телеметрия отдаёт reading текущего шага нагрузки → серия детерминирована.
    По умолчанию ``iterations`` = длине серии (по шагу на reading).
    """
    clock = _StepClock()
    backend = _FakeBackend(
        [_discrete_device()],
        iterations=iterations if iterations is not None else len(readings),
        iter_sec=iter_sec,
        cancel_after_iters=cancel_after_iters,
        clock=clock,
    )
    telem = _FakeTelemetry(readings, limit_c=limit_c, clock=clock)
    return backend, telem


# ─── Тесты оркестратора (fake backend + fake telemetry) ──────────────────────


def test_stable_run_yields_pass():
    """Стабильные частоты, прохлада, полная загрузка → PASS."""
    readings = [_reading(temp=62 + i * 0.2, power=280.0, clock=2600.0, util=99.0) for i in range(12)]
    telem = _FakeTelemetry(readings, limit_c=95.0)
    backend = _FakeBackend([_discrete_device()], iterations=10)

    progress: list[tuple[float, float]] = []
    report = _orch(backend).run(
        duration_sec=10.0,
        telemetry=telem,
        on_progress=lambda e, d: progress.append((e, d)),
    )

    assert GpuWorkloadKind.SUSTAINED_STRESS in backend.measured_kinds
    assert report.verdict is GpuStressVerdict.PASS
    assert report.throttle_detected is False
    assert report.cancelled is False
    assert report.max_temp_c is not None and report.avg_clock_mhz == pytest.approx(2600.0)
    assert report.thermal_limit_c == pytest.approx(95.0)
    assert report.samples_taken >= 1
    assert report.samples  # спарклайн заполнен
    assert progress  # прогресс дёргался


def test_clock_collapse_yields_fail():
    """Частота обваливается к концу прогона (>15%) → FAIL, троттлинг."""
    # first-third ~2600, last-third ~2000 → просадка ~23%. Шаг нагрузки на
    # reading (общий _StepClock) делает форму серии детерминированной.
    clocks = [2600, 2600, 2600, 2400, 2200, 2050, 2000, 2000, 2000]
    readings = [_reading(temp=70.0, power=250.0, clock=float(c), util=99.0) for c in clocks]
    backend, telem = _paired(readings, limit_c=95.0)

    report = _orch(backend).run(duration_sec=9.0, telemetry=telem)

    assert report.verdict is GpuStressVerdict.FAIL
    assert report.throttle_detected is True
    assert any("обвал частоты" in r for r in report.throttle_reasons)
    assert report.min_clock_mhz == pytest.approx(2000.0)
    assert report.max_clock_mhz_observed == pytest.approx(2600.0)


def test_temp_at_slowdown_limit_yields_fail():
    """Пик температуры достиг порога NVML slowdown → FAIL."""
    readings = [_reading(temp=95.0, power=290.0, clock=2500.0, util=99.0) for _ in range(8)]
    telem = _FakeTelemetry(readings, limit_c=95.0)
    backend = _FakeBackend([_discrete_device()], iterations=8)

    report = _orch(backend).run(duration_sec=8.0, telemetry=telem)

    assert report.verdict is GpuStressVerdict.FAIL
    assert any("достигла порога" in r for r in report.throttle_reasons)


def test_temp_near_slowdown_limit_yields_warn():
    """Пик температуры у порога (в пределах margin), но частота держится → WARN."""
    # порог 95, margin 3 → 93 попадает в WARN-зону; частота стабильна.
    readings = [_reading(temp=93.0, power=285.0, clock=2600.0, util=99.0) for _ in range(8)]
    telem = _FakeTelemetry(readings, limit_c=95.0)
    backend = _FakeBackend([_discrete_device()], iterations=8)

    report = _orch(backend).run(duration_sec=8.0, telemetry=telem)

    assert report.verdict is GpuStressVerdict.WARN
    assert report.throttle_detected is True
    assert any("у порога замедления" in r for r in report.throttle_reasons)


def test_no_telemetry_yields_unknown_but_load_ran():
    """Пустая телеметрия → UNKNOWN, но нагрузка реально гонялась."""
    telem = _FakeTelemetry([], limit_c=None)  # каждый sample() пуст
    backend = _FakeBackend([_discrete_device()], iterations=6)

    report = _orch(backend).run(duration_sec=6.0, telemetry=telem)

    assert GpuWorkloadKind.SUSTAINED_STRESS in backend.measured_kinds  # нагрузка была
    assert report.verdict is GpuStressVerdict.UNKNOWN
    assert report.max_temp_c is None
    assert report.avg_clock_mhz is None
    assert any("телеметрия" in n.lower() for n in report.notes)
    assert telem.sample_calls >= 1


def test_cancel_mid_run_marks_cancelled_with_partial_verdict():
    """Отмена посреди прогона → cancelled=True, вердикт по частичным данным (WARN)."""
    readings = [_reading(temp=68.0, power=270.0, clock=2600.0, util=99.0) for _ in range(20)]
    telem = _FakeTelemetry(readings, limit_c=95.0)
    # Движок сам ставит cancel после 3 итераций (эмуляция «Стоп»).
    backend = _FakeBackend([_discrete_device()], iterations=20, cancel_after_iters=3)

    token = threading.Event()
    report = _orch(backend).run(duration_sec=20.0, telemetry=telem, cancel_token=token)

    assert report.cancelled is True
    assert token.is_set()
    # Есть данные, FAIL-сигналов нет → best-effort WARN, не PASS.
    assert report.verdict is GpuStressVerdict.WARN
    assert any("отмен" in n.lower() or "прерван" in n.lower() for n in report.notes)


def test_backend_unavailable_graceful_unknown():
    """Бэкенд недоступен → UNKNOWN-отчёт, без исключения, нагрузка не гонялась."""
    backend = _FakeBackend([], available=False)
    report = _orch(backend).run(duration_sec=5.0)

    assert report.verdict is GpuStressVerdict.UNKNOWN
    assert report.cancelled is False
    assert report.device.index == -1
    assert backend.measured_kinds == []
    assert any("недоступен" in n.lower() for n in report.notes)


def test_no_devices_graceful_unknown():
    """Бэкенд доступен, но устройств нет → тот же graceful UNKNOWN."""
    backend = _FakeBackend([], available=True)
    report = _orch(backend).run(duration_sec=5.0)

    assert report.verdict is GpuStressVerdict.UNKNOWN
    assert backend.measured_kinds == []


def test_device_index_out_of_range_graceful():
    """Несуществующий device_index → graceful UNKNOWN, без исключения."""
    backend = _FakeBackend([_discrete_device()])
    report = _orch(backend).run(device_index=5, duration_sec=5.0)

    assert report.verdict is GpuStressVerdict.UNKNOWN
    assert backend.measured_kinds == []
    assert any("device_index" in n for n in report.notes)


def test_backend_measure_raises_is_graceful():
    """Движок нагрузки бросил исключение → прогон не падает, вердикт по телеметрии."""
    readings = [_reading(temp=60.0, clock=2600.0, util=99.0) for _ in range(4)]
    telem = _FakeTelemetry(readings, limit_c=95.0)
    backend = _FakeBackend([_discrete_device()], raise_on_measure=True)

    report = _orch(backend).run(duration_sec=3.0, telemetry=telem)

    # Не бросили; отчёт собран. Ошибка движка попала в notes.
    assert isinstance(report.verdict, GpuStressVerdict)
    assert any("движка нагрузки" in n.lower() for n in report.notes)


# ─── Тесты чистых хелперов (без оркестратора/потоков) ────────────────────────


def _series_from(readings: list[GpuTelemetryReading]) -> _Series:
    s = _Series()
    for r in readings:
        s.add(r)
    return s


def test_summarize_computes_series_stats():
    """Сводки max/avg/min считаются пофичево, дырки не ломают."""
    readings = [
        _reading(temp=60.0, power=200.0, clock=2600.0, util=98.0),
        _reading(temp=70.0, clock=2500.0, util=99.0),  # power молчит
        _reading(temp=80.0, power=300.0, clock=2400.0),  # util молчит
    ]
    summary = summarize_gpu_stress(
        _series_from(readings), thermal_limit_c=95.0, load_ran=True
    )
    assert summary.max_temp_c == pytest.approx(80.0)
    assert summary.avg_temp_c == pytest.approx(70.0)
    assert summary.max_power_w == pytest.approx(300.0)
    assert summary.avg_power_w == pytest.approx(250.0)
    assert summary.min_clock_mhz == pytest.approx(2400.0)
    assert summary.max_clock_mhz_observed == pytest.approx(2600.0)
    assert summary.avg_util_pct == pytest.approx(98.5)


def test_summarize_small_series_no_false_throttle():
    """Мало отсчётов (< порога тренда) → частотный тренд не считается, троттлинга нет."""
    readings = [_reading(temp=60.0, clock=2600.0, util=99.0) for _ in range(3)]
    summary = summarize_gpu_stress(
        _series_from(readings), thermal_limit_c=95.0, load_ran=True
    )
    assert summary.throttle_detected is False


def test_summarize_util_collapse_flag():
    """Низкая средняя загрузка под нагрузкой → сигнал просадки."""
    readings = [_reading(temp=55.0, clock=2600.0, util=20.0) for _ in range(8)]
    summary = summarize_gpu_stress(
        _series_from(readings), thermal_limit_c=95.0, load_ran=True
    )
    assert summary.throttle_detected is True
    assert any("загрузка GPU" in r for r in summary.throttle_reasons)


def test_summarize_fallback_temp_thresholds_without_limit():
    """Порог неизвестен (None) → используются абсолютные фолбэк-пороги."""
    hot = FALLBACK_TEMP_FAIL_C + 1.0
    readings = [_reading(temp=hot, clock=2600.0, util=99.0) for _ in range(8)]
    summary = summarize_gpu_stress(
        _series_from(readings), thermal_limit_c=None, load_ran=True
    )
    assert summary.throttle_detected is True
    assert any("порог устройства неизвестен" in r for r in summary.throttle_reasons)


def test_verdict_no_telemetry_is_unknown():
    """has_telemetry=False → UNKNOWN независимо от summary."""
    summary = summarize_gpu_stress(_Series(), thermal_limit_c=95.0, load_ran=True)
    verdict, notes = compute_gpu_stress_verdict(
        summary, has_telemetry=False, thermal_limit_c=95.0, cancelled=False
    )
    assert verdict is GpuStressVerdict.UNKNOWN
    assert any("телеметрия" in n.lower() for n in notes)


def test_verdict_clean_run_is_pass():
    """Есть телеметрия, троттлинга нет, не отменён → PASS."""
    readings = [_reading(temp=65.0, clock=2600.0, util=99.0) for _ in range(8)]
    summary = summarize_gpu_stress(
        _series_from(readings), thermal_limit_c=95.0, load_ran=True
    )
    verdict, _ = compute_gpu_stress_verdict(
        summary, has_telemetry=True, thermal_limit_c=95.0, cancelled=False
    )
    assert verdict is GpuStressVerdict.PASS
