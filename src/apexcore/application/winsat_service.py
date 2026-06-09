"""Application-сервис Winsat-аналога: оркестратор полного прогона.

Запускает четыре измерения (AES-256, SHA-1, Memory Read, Disk seq + random),
сводит их в подскоры через :mod:`winsat_scoring` и возвращает
:class:`WinsatReport`. Поддержка отмены через ``cancel_token``: пройденные
тесты сохраняются как PASS, оставшиеся помечаются ERROR с пометкой «отменено».

Сервис доступен только на Windows (``WinsatService.is_supported()``). На
Linux вызов ``run_formal()`` бросает ``RuntimeError``.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from apexcore.application.winsat_scoring import (
    compute_cpu_score,
    compute_disk_score,
    compute_memory_score,
    compute_winspr_level,
    error_subscore,
)
from apexcore.domain.ports import OSAdapter, WinsatRepository
from apexcore.domain.winsat import WinsatReport, WinsatSubscore
from apexcore.infrastructure.microbench.base import CancelledError
from apexcore.infrastructure.microbench.crypto import Aes256Bench, Sha1Bench
from apexcore.infrastructure.microbench.disk import (
    DiskRandomReadBench,
    DiskSequentialReadBench,
)
from apexcore.infrastructure.microbench.memory import MemoryReadBench

StageName = Literal[
    "cpu_aes", "cpu_sha1", "memory", "disk_seq", "disk_random", "dwm"
]
ProgressCallback = Callable[[StageName, int, int], None]

# Шесть стадий полного прогона. Стадия "dwm" выдаёт сразу обе графические
# метрики (GraphicsScore + GamingScore) через одну нативную команду
# `winsat dwm -xml` — отдельный `winsat d3d` не нужен.
STAGES: tuple[StageName, ...] = (
    "cpu_aes",
    "cpu_sha1",
    "memory",
    "disk_seq",
    "disk_random",
    "dwm",
)


class WinsatService:
    """Оркестратор Winsat-аналога.

    Пример использования::

        adapter = AdapterFactory.detect()
        repo = SqliteWinsatRepository(settings.db_path)
        service = WinsatService(adapter, repo)
        report = service.run_formal(duration_sec_per_test=5.0)
    """

    def __init__(
        self,
        adapter: OSAdapter,
        repo: WinsatRepository | None = None,
    ) -> None:
        self._adapter = adapter
        self._repo = repo

    @staticmethod
    def is_supported() -> bool:
        """Доступен ли модуль на текущей ОС (только Windows в MVP)."""
        return sys.platform == "win32"

    def run_formal(
        self,
        duration_sec_per_test: float = 5.0,
        cancel_token: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        save: bool = True,
    ) -> WinsatReport:
        """Запустить полный прогон Winsat-аналога.

        Параметры
        ---------
        duration_sec_per_test:
            Длительность каждого подтеста (по умолчанию 5 с — стандарт Winsat).
        cancel_token:
            Опциональный флаг отмены. Прерванные подтесты помечаются ERROR.
        on_progress:
            Callback ``(stage, idx, total)`` для UI-анимации (Rich Progress).
        save:
            Если True и передан репозиторий в конструкторе — сохранить
            ``WinsatReport`` в БД.
        """
        if not self.is_supported():
            raise RuntimeError(
                "Winsat-аналог недоступен на этой ОС (только Windows)"
            )

        sys_info = self._adapter.get_system_info()
        started = datetime.now(timezone.utc)
        cancelled = False
        total = len(STAGES)

        # ─── Стадия 1: AES-256 ─────────────────────────────────────────────
        aes_value: float | None = None
        sha_value: float | None = None
        memory_read_value: float | None = None
        disk_seq_value: float | None = None
        disk_random_value: float | None = None
        graphics_score_raw: float | None = None
        gaming_score_raw: float | None = None
        dwm_fps_value: float | None = None
        vmem_bw_value: float | None = None
        dwm_error: str | None = None

        if on_progress:
            on_progress("cpu_aes", 1, total)
        try:
            aes_result = Aes256Bench().run(
                duration_sec_per_test, cancel_token=cancel_token
            )
            aes_value = aes_result.value
        except CancelledError:
            cancelled = True

        # ─── Стадия 2: SHA-1 ───────────────────────────────────────────────
        if not cancelled:
            if on_progress:
                on_progress("cpu_sha1", 2, total)
            try:
                sha_result = Sha1Bench().run(
                    duration_sec_per_test, cancel_token=cancel_token
                )
                sha_value = sha_result.value
            except CancelledError:
                cancelled = True

        # ─── Стадия 3: Memory Read ─────────────────────────────────────────
        if not cancelled:
            if on_progress:
                on_progress("memory", 3, total)
            try:
                mem_result = MemoryReadBench().run(
                    duration_sec_per_test, cancel_token=cancel_token
                )
                memory_read_value = mem_result.value
            except CancelledError:
                cancelled = True

        # ─── Стадия 4: Disk Sequential Read ────────────────────────────────
        if not cancelled:
            if on_progress:
                on_progress("disk_seq", 4, total)
            try:
                seq_bench = DiskSequentialReadBench()
                if seq_bench.is_available():
                    seq_result = seq_bench.run(
                        duration_sec_per_test, cancel_token=cancel_token
                    )
                    disk_seq_value = seq_result.value
            except CancelledError:
                cancelled = True

        # ─── Стадия 5: Disk Random Read ────────────────────────────────────
        if not cancelled:
            if on_progress:
                on_progress("disk_random", 5, total)
            try:
                rnd_bench = DiskRandomReadBench()
                if rnd_bench.is_available():
                    rnd_result = rnd_bench.run(
                        duration_sec_per_test, cancel_token=cancel_token
                    )
                    disk_random_value = rnd_result.value
            except CancelledError:
                cancelled = True

        # ─── Стадия 6: DWM (Graphics + Gaming в одном вызове) ──────────────
        # `winsat dwm -xml` запускает Desktop Window Manager assessment и
        # параллельно выдаёт обе GPU-метрики: GraphicsScore (desktop) и
        # GamingScore (D3D). Поэтому отдельный `winsat d3d` не нужен.
        # Требует admin (UAC) для записи в C:\Windows\Performance\WinSAT.
        if not cancelled:
            if on_progress:
                on_progress("dwm", 6, total)
            (
                graphics_score_raw,
                gaming_score_raw,
                dwm_fps_value,
                vmem_bw_value,
                dwm_error,
            ) = _run_winsat_dwm()

        # ─── Сборка подскоров ──────────────────────────────────────────────
        cpu_sub = self._build_cpu_subscore(aes_value, sha_value)
        memory_sub = self._build_memory_subscore(memory_read_value)
        disk_sub = self._build_disk_subscore(disk_seq_value, disk_random_value)
        graphics_sub = self._build_graphics_subscore(
            graphics_score_raw, dwm_fps_value, dwm_error,
        )
        d3d_sub = self._build_d3d_subscore(
            gaming_score_raw, vmem_bw_value, dwm_error,
        )

        all_subs = (cpu_sub, memory_sub, disk_sub, graphics_sub, d3d_sub)
        winspr = compute_winspr_level(all_subs)

        ended = datetime.now(timezone.utc)
        report = WinsatReport(
            system_info=sys_info,
            started_at=started,
            ended_at=ended,
            cpu_score=cpu_sub,
            memory_score=memory_sub,
            disk_score=disk_sub,
            graphics_score=graphics_sub,
            d3d_score=d3d_sub,
            winspr_level=winspr,
            cancelled=cancelled,
        )

        if save and self._repo is not None and not cancelled:
            self._repo.save(report)

        return report

    # ─── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_cpu_subscore(
        aes_value: float | None, sha_value: float | None
    ) -> WinsatSubscore:
        if aes_value is None or sha_value is None:
            return error_subscore(
                "cpu", note="отменено пользователем" if aes_value is None else "ошибка"
            )
        return compute_cpu_score(aes_value, sha_value)

    @staticmethod
    def _build_memory_subscore(value: float | None) -> WinsatSubscore:
        if value is None:
            return error_subscore("memory", note="отменено пользователем")
        return compute_memory_score(value)

    @staticmethod
    def _build_disk_subscore(
        seq_value: float | None, rnd_value: float | None
    ) -> WinsatSubscore:
        if seq_value is None or rnd_value is None:
            note = (
                "отменено пользователем"
                if seq_value is None and rnd_value is None
                else f"seq={seq_value} rnd={rnd_value} (частично)"
            )
            return error_subscore("disk", note=note)
        return compute_disk_score(seq_value, rnd_value)

    @staticmethod
    def _build_graphics_subscore(
        score: float | None, dwm_fps: float | None, error: str | None,
    ) -> WinsatSubscore:
        if score is None:
            from apexcore.application.winsat_scoring import error_subscore as _err
            return _err("graphics", note=error or "winsat dwm недоступен")
        from apexcore.application.winsat_scoring import compute_graphics_score
        return compute_graphics_score(score, dwm_fps)

    @staticmethod
    def _build_d3d_subscore(
        score: float | None, vmem_bw: float | None, error: str | None,
    ) -> WinsatSubscore:
        if score is None:
            from apexcore.application.winsat_scoring import error_subscore as _err
            return _err("d3d", note=error or "winsat dwm недоступен")
        from apexcore.application.winsat_scoring import compute_d3d_score
        return compute_d3d_score(score, vmem_bw)


# ─── Native winsat helper ──────────────────────────────────────────────────


def _is_admin() -> bool:
    """True если текущий процесс запущен с правами администратора (Windows)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_winsat_dwm_elevated(tmp_path) -> str | None:
    """Поднять ТОЛЬКО стадию `winsat dwm` от администратора (UAC) и дать ей
    записать XML в ``tmp_path``.

    Нужно для Web UI: он работает без admin (sensord даёт сенсоры UAC-free),
    но `winsat dwm` требует elevation для графики/D3D. Паттерн как в
    repair-drivers — powershell `Start-Process -Verb RunAs -Wait`. Один UAC
    на стадию графики; CLI (уже elevated) сюда не заходит.

    Возвращает ``None`` при успехе или строку-ошибку (UAC отклонён и т.п.).
    """
    import subprocess

    p = str(tmp_path).replace("'", "''")  # экранируем для PS single-quoted
    ps = (
        "$ErrorActionPreference='Stop'; "
        "try { Start-Process -FilePath 'winsat' "
        f"-ArgumentList 'dwm','-xml','{p}' -Verb RunAs -WindowStyle Hidden -Wait; "
        "exit 0 } catch { exit 3 }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=180.0, check=False,
        )
    except FileNotFoundError:
        return "powershell не найден — не могу поднять winsat dwm от админа"
    except subprocess.TimeoutExpired:
        return "winsat dwm (от админа): таймаут"
    if res.returncode == 3:
        return "графика/D3D пропущены — UAC отклонён (нужны права администратора)"
    if res.returncode != 0:
        return f"не удалось запустить winsat dwm от админа (код {res.returncode})"
    return None


def _run_winsat_dwm() -> tuple[
    float | None, float | None, float | None, float | None, str | None,
]:
    """Запустить `winsat dwm -xml` и вернуть (graphics, gaming, dwm_fps, vmem_bw, error).

    Одна команда даёт сразу оба GPU-скора — отдельный `winsat d3d` не нужен.
    Стадия требует admin. Если процесс УЖЕ elevated (CLI) — прямой вызов; если
    нет (Web UI без admin) — поднимаем только эту стадию от администратора
    через UAC (`_run_winsat_dwm_elevated`), чтобы графика/D3D считались и в
    web, как в CLI.
    """
    import contextlib
    import subprocess
    import tempfile
    import xml.etree.ElementTree as ET
    from pathlib import Path

    # Создаём tmp-файл, закрываем — winsat сам в него запишет XML.
    with tempfile.NamedTemporaryFile(
        suffix=".xml", delete=False, prefix="apexcore_winsat_dwm_",
    ) as fh:
        tmp_path = Path(fh.name)

    try:
        if _is_admin():
            # Уже от админа (CLI запущен elevated) — прямой вызов без UAC.
            try:
                res = subprocess.run(
                    ["winsat", "dwm", "-xml", str(tmp_path)],
                    capture_output=True,
                    text=True,
                    timeout=60.0,
                    check=False,
                )
            except FileNotFoundError:
                return None, None, None, None, "winsat не найден (нужен Windows ADK)"
            except subprocess.TimeoutExpired:
                return None, None, None, None, "winsat dwm: таймаут 60 с"
            except OSError as exc:
                if getattr(exc, "winerror", None) == 740:
                    return None, None, None, None, (
                        "Требуется запуск с правами администратора "
                        "(winsat dwm требует elevation)"
                    )
                return None, None, None, None, f"winsat dwm: ошибка запуска ({exc})"
            if res.returncode != 0:
                stderr_short = (res.stderr or "").strip().splitlines()[:1]
                return None, None, None, None, (
                    f"winsat dwm упал (код {res.returncode}): "
                    + (stderr_short[0] if stderr_short else "нет деталей")
                )
        else:
            # Web UI без admin — поднимаем стадию winsat dwm от админа (UAC),
            # чтобы графика/D3D считались как в CLI. Один UAC на эту стадию.
            elev_err = _run_winsat_dwm_elevated(tmp_path)
            if elev_err is not None:
                return None, None, None, None, elev_err

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return None, None, None, None, "winsat dwm: XML не сохранён"

        try:
            tree = ET.parse(tmp_path)
            root = tree.getroot()
        except ET.ParseError as exc:
            return None, None, None, None, f"не удалось разобрать XML: {exc}"

        def _find_float(path: str) -> float | None:
            el = root.find(path)
            if el is None or not el.text:
                return None
            try:
                return float(el.text.strip())
            except ValueError:
                return None

        graphics = _find_float(".//WinSPR/GraphicsScore")
        gaming   = _find_float(".//WinSPR/GamingScore")
        dwm_fps  = _find_float(".//GraphicsMetrics/DWMFps")
        vmem_bw  = _find_float(".//GraphicsMetrics/VideoMemBandwidth")

        if graphics is None and gaming is None:
            return None, None, None, None, "XML не содержит GraphicsScore/GamingScore"
        return graphics, gaming, dwm_fps, vmem_bw, None
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


__all__ = ["STAGES", "ProgressCallback", "StageName", "WinsatService"]
