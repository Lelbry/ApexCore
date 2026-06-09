"""Обёртки над `stress-ng` — эталонная утилита нагрузки для Linux/Astra."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads
from apexcore.shared.timing import now


def _stress_ng_available() -> bool:
    return shutil.which("stress-ng") is not None


def _run_with_cancel(
    cmd: list[str],
    duration_sec: float,
    cancel_token: threading.Event | None,
) -> tuple[int, str, str]:
    """Запустить subprocess с поддержкой отмены через ``cancel_token``.

    Возвращает (returncode, stdout, stderr). При отмене: returncode = -1.
    Сам ``--timeout`` stress-ng завершит процесс по своему расписанию,
    но мы дополнительно проверяем cancel_token каждые 200 мс и при
    срабатывании посылаем SIGTERM.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = now() + duration_sec + 5.0  # +5 с запас на финализацию
    cancelled = False
    while True:
        if cancel_token is not None and cancel_token.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            cancelled = True
            break
        if proc.poll() is not None:
            break
        if now() >= deadline:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(0.2)
    stdout, stderr = proc.communicate()
    rc = -1 if cancelled else (proc.returncode or 0)
    return rc, stdout or "", stderr or ""


def _parse_bogo_ops(stderr: str) -> tuple[float, float]:
    """Извлечь (bogo ops total, bogo ops/s real time) из вывода stress-ng.

    Формат строки stress-ng (`--metrics`): пример вывода
    ``stress-ng: metrc: [PID] cpu  12345 100.00 12.34 13.45 1234.56  1000.0``
    Колонки: stressor bogo-ops time secs real-time bogo-ops/s-real-time bogo-ops/s-usr+sys
    Здесь нас интересуют 4-я колонка (real time secs) и 5-я (bogo ops/s).
    """
    total = 0.0
    rate = 0.0
    for line in stderr.splitlines():
        if "metrc:" not in line:
            continue
        parts = line.split()
        # Ищем последнее число с плавающей точкой как rate, второе с конца — usr+sys.
        floats = [p for p in parts if re.match(r"^-?\d+(\.\d+)?$", p)]
        if len(floats) >= 5:
            total += float(floats[-5])  # bogo ops total
            rate += float(floats[-2])  # bogo ops/s real-time
    return total, rate


# Регулярки для разбора verify-итогов stress-ng. Формат:
#   stress-ng: info:  [PID] passed: 8: cpu (8)
#   stress-ng: info:  [PID] failed: 0
#   stress-ng: info:  [PID] metrics untrustworthy: 0
# Источник формата: ColinIanKing/stress-ng README [14].
_RE_PASSED = re.compile(r"\bpassed:\s*(\d+)")
_RE_FAILED = re.compile(r"\bfailed:\s*(\d+)")


def _parse_verify(stderr: str) -> tuple[int, int]:
    """Извлечь (passed_count, failed_count) из info-строк stress-ng.

    Возвращает первые встреченные значения; если нет — (0, 0). Используется
    только когда команда запущена с ``--verify``.
    """
    passed = 0
    failed = 0
    for line in stderr.splitlines():
        if "passed:" in line and passed == 0:
            m = _RE_PASSED.search(line)
            if m:
                passed = int(m.group(1))
        if "failed:" in line and failed == 0:
            m = _RE_FAILED.search(line)
            if m:
                failed = int(m.group(1))
    return passed, failed


class StressNgCpuEngine(StressEngine):
    """``stress-ng --cpu N`` — эталонный CPU-стресс под Linux/Astra."""

    name = "external_stress_ng_cpu"
    category = "cpu_int"
    is_external = True

    def is_available(self) -> bool:
        return _stress_ng_available()

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        cmd = [
            "stress-ng",
            "--cpu",
            str(n_threads),
            "--cpu-method",
            "all",
            "--timeout",
            f"{int(duration_sec)}s",
            "--metrics-brief",
        ]
        started = now()
        rc, _stdout, stderr = _run_with_cancel(cmd, duration_sec, cancel_token)
        elapsed = now() - started
        total, rate = _parse_bogo_ops(stderr)
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=rate or (total / elapsed if elapsed > 0 else 0.0),
            throughput_unit="bogo-ops/s",
            threads=n_threads,
            error_count=0 if rc == 0 else 1,
            raw_output=stderr[-4000:] if stderr else None,
            extra={"cmd": " ".join(cmd), "cancelled": rc == -1},
        )


class StressNgVmEngine(StressEngine):
    """``stress-ng --vm`` — RAM-стресс с поддержкой `--verify` для pass/fail.

    Параметры:
        verify: добавить флаг ``--verify`` (sanity-check памяти, см. manpage [13]).
        vm_bytes: явный размер региона на поток. ``None`` ⇒ ``256M`` по умолчанию.
        vm_method: метод стресса. ``"all"`` (дефолт) перебирает все паттерны.
    """

    name = "external_stress_ng_vm"
    category = "ram_bw"
    is_external = True

    def __init__(
        self,
        verify: bool = False,
        vm_bytes: str | None = None,
        vm_method: str = "all",
    ) -> None:
        self._verify = verify
        self._vm_bytes = vm_bytes or "256M"
        self._vm_method = vm_method

    def is_available(self) -> bool:
        return _stress_ng_available()

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        cmd = [
            "stress-ng",
            "--vm",
            str(n_threads),
            "--vm-bytes",
            self._vm_bytes,
            "--vm-method",
            self._vm_method,
            "--timeout",
            f"{int(duration_sec)}s",
            "--metrics-brief",
        ]
        if self._verify:
            cmd.append("--verify")
        started = now()
        rc, _stdout, stderr = _run_with_cancel(cmd, duration_sec, cancel_token)
        elapsed = now() - started
        total, rate = _parse_bogo_ops(stderr)
        verify_passed, verify_failed = (0, 0)
        if self._verify:
            verify_passed, verify_failed = _parse_verify(stderr)
        # error_count = ненулевой rc + явные verify_failed.
        error_count = (0 if rc == 0 else 1) + verify_failed
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=rate or (total / elapsed if elapsed > 0 else 0.0),
            throughput_unit="bogo-ops/s",
            threads=n_threads,
            error_count=error_count,
            raw_output=stderr[-4000:] if stderr else None,
            extra={
                "cmd": " ".join(cmd),
                "cancelled": rc == -1,
                "verify": self._verify,
                "verify_passed": verify_passed,
                "verify_failed": verify_failed,
            },
        )


class StressNgMatrixEngine(StressEngine):
    """``stress-ng --cpu --cpu-method matrixprod`` — главный CPU-стресс на Linux.

    По Ubuntu Wiki Kernel/Reference/stress-ng [15]: «*The matrix stressor is
    a good way to exercise the CPU floating point operations as well as
    memory and processor data cache. Of all the tests, this one generally
    heats x86 CPUs the best.*»

    Параметр ``verify`` добавляет ``--verify`` для pass/fail-вердикта.
    """

    name = "external_stress_ng_matrix"
    category = "cpu_fp"
    is_external = True

    def __init__(self, verify: bool = False) -> None:
        self._verify = verify

    def is_available(self) -> bool:
        return _stress_ng_available()

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        cmd = [
            "stress-ng",
            "--cpu",
            str(n_threads),
            "--cpu-method",
            "matrixprod",
            "--timeout",
            f"{int(duration_sec)}s",
            "--metrics-brief",
        ]
        if self._verify:
            cmd.append("--verify")
        started = now()
        rc, _stdout, stderr = _run_with_cancel(cmd, duration_sec, cancel_token)
        elapsed = now() - started
        total, rate = _parse_bogo_ops(stderr)
        verify_passed, verify_failed = (0, 0)
        if self._verify:
            verify_passed, verify_failed = _parse_verify(stderr)
        error_count = (0 if rc == 0 else 1) + verify_failed
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=rate or (total / elapsed if elapsed > 0 else 0.0),
            throughput_unit="bogo-ops/s",
            threads=n_threads,
            error_count=error_count,
            raw_output=stderr[-4000:] if stderr else None,
            extra={
                "cmd": " ".join(cmd),
                "cancelled": rc == -1,
                "verify": self._verify,
                "verify_passed": verify_passed,
                "verify_failed": verify_failed,
            },
        )
