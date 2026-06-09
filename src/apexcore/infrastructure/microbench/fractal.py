"""Single-Precision Julia / Double-Precision Mandelbrot — fractal escape-time.

На сетке размером ``WIDTH×HEIGHT`` для каждого пикселя итерируется
комплексное отображение до расходимости (|z| > 2) или до ``max_iter``.
Это представительная FP-нагрузка с большим количеством зависимых
операций (z = z² + c) и условных проверок: типичный сценарий, где
проверяется устойчивость FPU и скорость инициализации регистров.

Throughput выражается в FPS: сколько полных кадров (одна сетка) в
секунду удаётся посчитать. Такая метрика принята и в AIDA64 GPGPU
Benchmark, и в типичных fractal-бенчмарках.

Реализация — на ``numba``-JIT, который позволяет писать «горячий»
цикл на чистом Python и получать машинный код, близкий по качеству к
C. Без numba — fallback на NumPy-векторизацию (другая характеристика
нагрузки, но тест отрабатывает).

Источники
---------
Mandelbrot, B. B. (1980). Fractal aspects of the iteration of
z → λz(1 − z) for complex λ and z. Annals of the New York Academy of
Sciences, 357(1), 249-259.
DOI: https://doi.org/10.1111/j.1749-6632.1980.tb29690.x

Devaney, R. L. (1989). An Introduction to Chaotic Dynamical Systems
(2nd ed.). Addison-Wesley. — раздел про escape-time для семейства
квадратичных отображений (Julia / Mandelbrot).
"""

from __future__ import annotations

import threading

import numpy as np

from apexcore.domain.models import MicroBenchResult
from apexcore.infrastructure.microbench.base import time_loop

# Размеры сетки и максимум итераций — компромисс между «успеть посчитать
# хотя бы 5-10 кадров за 5 с» и «нагрузить FPU всерьёз».
WIDTH = 512
HEIGHT = 512
MAX_ITER = 256

# Параметры Julia: классическая «дендритная» константа c = -0.7269 + 0.1889i.
JULIA_CX = -0.7269
JULIA_CY = 0.1889

try:
    from numba import njit  # type: ignore

    HAVE_NUMBA = True
except ImportError:  # pragma: no cover
    HAVE_NUMBA = False


# ─── Numba-ядра ──────────────────────────────────────────────────────────

if HAVE_NUMBA:

    @njit(cache=True, fastmath=True)  # type: ignore[misc]
    def _julia_fp32(width: int, height: int, max_iter: int, cx: float, cy: float):
        out = np.empty((height, width), dtype=np.int32)
        cxf = np.float32(cx)
        cyf = np.float32(cy)
        for j in range(height):
            zy0 = np.float32(-1.5 + j * 3.0 / height)
            for i in range(width):
                zx = np.float32(-1.5 + i * 3.0 / width)
                zy = zy0
                k = 0
                while k < max_iter:
                    zx2 = zx * zx
                    zy2 = zy * zy
                    if zx2 + zy2 > np.float32(4.0):
                        break
                    zy = np.float32(2.0) * zx * zy + cyf
                    zx = zx2 - zy2 + cxf
                    k += 1
                out[j, i] = k
        return out

    @njit(cache=True, fastmath=True)  # type: ignore[misc]
    def _mandelbrot_fp64(width: int, height: int, max_iter: int):
        out = np.empty((height, width), dtype=np.int32)
        for j in range(height):
            cy = -1.0 + j * 2.0 / height
            for i in range(width):
                cx = -2.0 + i * 3.0 / width
                zx = 0.0
                zy = 0.0
                k = 0
                while k < max_iter:
                    zx2 = zx * zx
                    zy2 = zy * zy
                    if zx2 + zy2 > 4.0:
                        break
                    zy = 2.0 * zx * zy + cy
                    zx = zx2 - zy2 + cx
                    k += 1
                out[j, i] = k
        return out

else:
    # Fallback: NumPy-векторизация. Не зависимая цепочка по элементам, но
    # суммарный счёт честный (тот же объём арифметики на пиксель в среднем).
    def _julia_fp32(width: int, height: int, max_iter: int, cx: float, cy: float):  # type: ignore[misc]
        zx = np.linspace(-1.5, 1.5, width, dtype=np.float32)
        zy = np.linspace(-1.5, 1.5, height, dtype=np.float32)
        Zx, Zy = np.meshgrid(zx, zy)
        out = np.full(Zx.shape, max_iter, dtype=np.int32)
        active = np.ones_like(Zx, dtype=bool)
        for k in range(max_iter):
            Zx2 = Zx * Zx
            Zy2 = Zy * Zy
            diverged = (Zx2 + Zy2) > np.float32(4.0)
            new_div = diverged & active
            out[new_div] = k
            active &= ~diverged
            if not active.any():
                break
            Zy_new = np.float32(2.0) * Zx * Zy + np.float32(cy)
            Zx_new = Zx2 - Zy2 + np.float32(cx)
            Zx = np.where(active, Zx_new, Zx)
            Zy = np.where(active, Zy_new, Zy)
        return out

    def _mandelbrot_fp64(width: int, height: int, max_iter: int):  # type: ignore[misc]
        cx = np.linspace(-2.0, 1.0, width, dtype=np.float64)
        cy = np.linspace(-1.0, 1.0, height, dtype=np.float64)
        Cx, Cy = np.meshgrid(cx, cy)
        Zx = np.zeros_like(Cx)
        Zy = np.zeros_like(Cy)
        out = np.full(Cx.shape, max_iter, dtype=np.int32)
        active = np.ones_like(Cx, dtype=bool)
        for k in range(max_iter):
            Zx2 = Zx * Zx
            Zy2 = Zy * Zy
            diverged = (Zx2 + Zy2) > 4.0
            new_div = diverged & active
            out[new_div] = k
            active &= ~diverged
            if not active.any():
                break
            Zy_new = 2.0 * Zx * Zy + Cy
            Zx_new = Zx2 - Zy2 + Cx
            Zx = np.where(active, Zx_new, Zx)
            Zy = np.where(active, Zy_new, Zy)
        return out


def _backend_label() -> str:
    return "numba" if HAVE_NUMBA else "numpy-vectorized"


# ─── Движки ──────────────────────────────────────────────────────────────


class JuliaSpBench:
    """Single-precision (fp32) Julia fractal — FPS."""

    name = "julia_sp"
    category = "fractal"
    unit = "FPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        # Прогрев JIT (первый вызов компилирует ядро).
        _julia_fp32(64, 64, 32, JULIA_CX, JULIA_CY)

        def work() -> None:
            _julia_fp32(WIDTH, HEIGHT, MAX_ITER, JULIA_CX, JULIA_CY)

        iterations, elapsed = time_loop(work, duration_sec, warmup_calls=0, cancel_token=cancel_token)
        fps = iterations / elapsed
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=fps,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "width": WIDTH,
                "height": HEIGHT,
                "max_iter": MAX_ITER,
                "dtype": "float32",
                "backend": _backend_label(),
                "c": (JULIA_CX, JULIA_CY),
                "source_file": "fractal.py",
            },
        )


class MandelbrotDpBench:
    """Double-precision (fp64) Mandelbrot fractal — FPS."""

    name = "mandelbrot_dp"
    category = "fractal"
    unit = "FPS"

    def is_available(self) -> bool:
        return True

    def run(
        self,
        duration_sec: float,
        threads: int | None = None,
        cancel_token: threading.Event | None = None,
    ) -> MicroBenchResult:
        _mandelbrot_fp64(64, 64, 32)

        def work() -> None:
            _mandelbrot_fp64(WIDTH, HEIGHT, MAX_ITER)

        iterations, elapsed = time_loop(work, duration_sec, warmup_calls=0, cancel_token=cancel_token)
        fps = iterations / elapsed
        return MicroBenchResult(
            name=self.name,
            category=self.category,
            value=fps,
            unit=self.unit,
            duration_actual_sec=elapsed,
            iterations=iterations,
            extra={
                "width": WIDTH,
                "height": HEIGHT,
                "max_iter": MAX_ITER,
                "dtype": "float64",
                "backend": _backend_label(),
                "source_file": "fractal.py",
            },
        )
