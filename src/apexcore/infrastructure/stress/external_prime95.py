"""Обёртка над `prime95` (mprime) — эталонный CPU-стресс под Windows.

Prime95 запускается в torture-test и не выводит throughput-метрики напрямую.
Мы запускаем его на заданное время и измеряем «попадание в нагрузку» —
факт того, что CPU был под 100% и без ошибок. Throughput фиксируем как 1.0
и используем как «pass/fail»-движок.

Режимы torture (TortureTest в prime.txt):
- Small  (1) — Lucas-Lehmer FFT, помещается в L2/L3, max FPU stress.
- Large  (2) — FFT, выходит за L3, нагружает контроллер памяти + FPU.
- Blend  (4) — чередование Small/Large, аналог AIDA64 System Stability.

prime95 не принимает режим через CLI — нужно записать ``prime.txt`` с
ключами ``TortureTest=N``, ``StressTester=1``, ``UsePrimenet=0`` в рабочую
директорию. Чтобы не загрязнять глобальный конфиг пользователя, мы создаём
временную директорию и запускаем prime95 оттуда.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from apexcore.domain.models import StressResult
from apexcore.domain.ports import StressEngine
from apexcore.infrastructure.stress.base import resolve_threads
from apexcore.shared.timing import now

logger = logging.getLogger(__name__)

Prime95Mode = Literal["small", "large", "blend"]
_MODE_TO_TORTURE: dict[str, int] = {"small": 1, "large": 2, "blend": 4}


def _prime95_available() -> bool:
    return shutil.which("prime95") is not None or shutil.which("mprime") is not None


class Prime95Engine(StressEngine):
    """Запуск prime95 в torture mode (small/large/blend) на заданное время.

    По умолчанию режим ``small`` — это Smallest FFT в OC-сообществе считается
    максимумом FPU-нагрузки (см. отчёт §6 Q3, GIMPS [16]). Для пары с
    RAM-стрессором лучше ``large`` или ``blend`` (Prime95 Blend = AIDA64-style).
    """

    name = "external_prime95"
    category = "cpu_fp"  # Prime95 — это FPU FFT, не int (исправлено vs прежней версии).
    is_external = True

    def __init__(self, mode: Prime95Mode = "small") -> None:
        self._mode: Prime95Mode = mode

    def is_available(self) -> bool:
        return _prime95_available()

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> StressResult:
        n_threads = resolve_threads(threads)
        exe = shutil.which("prime95") or shutil.which("mprime")
        if exe is None:
            return StressResult(
                engine=self.name,
                category=self.category,
                duration_actual_sec=0.0,
                throughput=0.0,
                throughput_unit="run",
                threads=n_threads,
                error_count=1,
                raw_output="prime95/mprime не найден в PATH",
            )
        torture_id = _MODE_TO_TORTURE.get(self._mode, 1)
        with tempfile.TemporaryDirectory(prefix="apexcore-prime95-") as workdir:
            self._write_prime_config(Path(workdir), torture_id, n_threads)
            # prime95 -t запускает torture, читая prime.txt из CWD.
            cmd = [exe, "-t"]
            started = now()
            err = 0
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as e:
                return StressResult(
                    engine=self.name,
                    category=self.category,
                    duration_actual_sec=0.0,
                    throughput=0.0,
                    throughput_unit="run",
                    threads=n_threads,
                    error_count=1,
                    raw_output=str(e),
                )
            deadline = started + duration_sec
            while True:
                if cancel_token is not None and cancel_token.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                if proc.poll() is not None:
                    err = 1  # процесс завершился сам — аномалия для torture-теста
                    break
                if now() >= deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(0.2)
            elapsed = now() - started
            results_path = Path(workdir) / "results.txt"
            results_text = ""
            if results_path.exists():
                try:
                    results_text = results_path.read_text(encoding="utf-8", errors="ignore")
                except OSError as exc:
                    logger.debug("prime95: не удалось прочитать results.txt: %s", exc)
            verify_failed = self._count_failures(results_text)
        error_count = err + verify_failed
        return StressResult(
            engine=self.name,
            category=self.category,
            duration_actual_sec=elapsed,
            throughput=1.0,
            throughput_unit="run",
            threads=n_threads,
            error_count=error_count,
            raw_output=results_text[-4000:] if results_text else None,
            extra={
                "cmd": " ".join(cmd),
                "mode": self._mode,
                "torture_test_id": torture_id,
                "verify_failed": verify_failed,
            },
        )

    def _write_prime_config(self, workdir: Path, torture_id: int, n_threads: int) -> None:
        """Записать prime.txt с настройками torture, отключив сетевую отчётность.

        Источники: ArchWiki «Stress testing» / GIMPS docs [16].
        """
        prime_txt = workdir / "prime.txt"
        prime_txt.write_text(
            "\n".join(
                [
                    "StressTester=1",
                    "UsePrimenet=0",
                    f"TortureTest={torture_id}",
                    f"TortureThreads={n_threads}",
                    # Минимизируем запись на диск во время прогона.
                    "OutputBothJournalAndScreen=0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        # local.txt — конфигурация воркера, нужен пустой, чтобы prime95 не
        # пытался читать пользовательский профиль из %APPDATA%.
        (workdir / "local.txt").write_text("", encoding="utf-8")

    def _count_failures(self, results_text: str) -> int:
        """Посчитать ошибки прогона по results.txt prime95.

        prime95 пишет «FATAL ERROR» / «Hardware failure detected» при сбое
        FFT-сверки. Считаем число таких строк.
        """
        if not results_text:
            return 0
        lower = results_text.lower()
        return lower.count("fatal error") + lower.count("hardware failure")
