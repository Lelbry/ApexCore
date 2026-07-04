"""CLI-команда ``apexcore gpu`` — бенчмарк графического процессора (OpenCL).

Подкоманды
----------
- ``apexcore gpu list``  — перечислить обнаруженные GPU-устройства.
- ``apexcore gpu run``   — полный прогон (FP32 + FP64 + VRAM + PCIe) → балл
  по Roofline (шкала ×10 000) с сохранением в БД.
- ``apexcore gpu test``  — одиночный замер выбранной нагрузки без скоринга
  (сырая скорость: GFLOPS для FP32/FP64, GB/s для памяти/PCIe).

Кроссвендорный путь: OpenCL через ctypes, без внешних зависимостей.
Если OpenCL/GPU недоступен (нет ICD-loader'а или устройств) — команды
деградируют без исключений и печатают понятное сообщение по-русски.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Бенчмарк GPU через OpenCL: FP32/FP64/VRAM/PCIe, балл по Roofline (×10 000).",
)

# Человекочитаемые подписи фаз для прогресс-статуса полного прогона.
_PHASE_LABELS = {
    "fp32": "FP32 (вычисления)",
    "fp64": "FP64 (вычисления)",
    "mem_bandwidth": "Пропускная способность VRAM",
    "pcie_h2d": "PCIe host→device",
    "pcie_d2h": "PCIe device→host",
}

# Типы нагрузок для `gpu test` (значения совпадают с GpuWorkloadKind).
_WORKLOAD_ALIASES = {
    "fp32": "fp32",
    "fp64": "fp64",
    "mem": "mem_bandwidth",
    "mem_bandwidth": "mem_bandwidth",
    "pcie": "pcie_h2d",
    "pcie_h2d": "pcie_h2d",
    "pcie_d2h": "pcie_d2h",
    "h2d": "pcie_h2d",
    "d2h": "pcie_d2h",
}


@app.command("list")
def list_devices() -> None:
    """Показать все обнаруженные GPU-устройства и их характеристики."""
    from rich.table import Table

    from apexcore.infrastructure.gpu import build_default_gpu_backend
    from apexcore.interfaces.cli.render import console

    backend = build_default_gpu_backend()
    if not backend.is_available():
        console.print(
            "[yellow]GPU/OpenCL не обнаружен.[/] ICD-loader не загрузился "
            "или в системе нет OpenCL-устройств.\n"
            "[dim]На дискретных GPU установите свежий драйвер (он приносит "
            "OpenCL runtime).[/]"
        )
        return

    devices = backend.list_devices()
    if not devices:
        console.print("[yellow]OpenCL доступен, но GPU-устройств не найдено.[/]")
        return

    tbl = Table(title="GPU-устройства (OpenCL)")
    tbl.add_column("№", style="bold cyan", justify="right")
    tbl.add_column("Имя", style="bold")
    tbl.add_column("Вендор")
    tbl.add_column("Тип")
    tbl.add_column("Блоков (CU)", justify="right")
    tbl.add_column("Частота", justify="right")
    tbl.add_column("VRAM", justify="right")
    tbl.add_column("FP64")
    for d in devices:
        vram_gb = (d.global_mem_mb or 0) / 1024.0
        fp64 = "[green]да[/]" if d.fp64_supported else "[dim]нет[/]"
        tbl.add_row(
            str(d.index),
            d.name,
            d.vendor or "—",
            _type_label(d.device_type),
            str(d.compute_units or "—"),
            f"{d.max_clock_mhz} МГц" if d.max_clock_mhz else "—",
            f"{vram_gb:.1f} ГБ" if d.global_mem_mb else "—",
            fp64,
        )
    console.print(tbl)
    console.print(
        "[dim]Полный прогон: `apexcore gpu run -d <№>`  ·  "
        "одиночный замер: `apexcore gpu test -d <№> --workload fp32`[/]"
    )


@app.command("run")
def run(
    device: int = typer.Option(
        0, "--device", "-d", help="Индекс GPU-устройства (см. `gpu list`)."
    ),
    fp32_duration: float = typer.Option(
        5.0, "--fp32-duration", help="Длительность фазы FP32, секунд."
    ),
    fp64_duration: float = typer.Option(
        5.0, "--fp64-duration", help="Длительность фазы FP64, секунд."
    ),
    mem_duration: float = typer.Option(
        5.0, "--mem-duration", help="Длительность фазы пропускной способности VRAM, секунд."
    ),
    pcie_duration: float = typer.Option(
        2.0, "--pcie-duration", help="Длительность каждой фазы PCIe (H2D/D2H), секунд."
    ),
    cooldown: float = typer.Option(
        2.0, "--cooldown", help="Пауза-остывание между фазами, секунд."
    ),
) -> None:
    """Полный GPU-бенчмарк: FP32 + FP64 + VRAM + PCIe → балл и сохранение в БД.

    Балл по Roofline (шкала ×10 000) считается из FP32 и пропускной
    способности VRAM; FP64 и PCIe — информационные (в балл не входят).
    Ctrl+C прерывает прогон, уже снятые фазы сохраняются.
    """
    from apexcore.application.gpu_benchmark import (
        GpuBenchmarkOrchestrator,
        GpuBenchmarkParams,
    )
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.gpu import build_default_gpu_backend
    from apexcore.infrastructure.persistence import SqliteGpuBenchmarkRepository
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.render import console, render_gpu_report
    from apexcore.shared.config import load_settings

    backend = build_default_gpu_backend()
    if not backend.is_available():
        console.print(
            "[yellow]GPU/OpenCL не обнаружен.[/] Полный прогон невозможен — "
            "нет ни одного OpenCL-устройства.\n"
            "[dim]Проверьте наличие устройств: `apexcore gpu list`.[/]"
        )
        raise typer.Exit(code=0)

    params = GpuBenchmarkParams(
        fp32_duration_sec=fp32_duration,
        fp64_duration_sec=fp64_duration,
        mem_duration_sec=mem_duration,
        pcie_duration_sec=pcie_duration,
        cooldown_sec=cooldown,
    )
    # Грубая оценка: 5 фаз + 3 cooldown между compute-фазами + PCIe.
    total_est = (
        params.fp32_duration_sec
        + params.fp64_duration_sec
        + params.mem_duration_sec
        + 2 * params.pcie_duration_sec
        + 3 * params.cooldown_sec
    )

    adapter = AdapterFactory.detect()
    orchestrator = GpuBenchmarkOrchestrator(adapter, backend)

    console.rule("[bold]Запуск GPU-бенчмарка[/]")
    console.print(
        f"[dim]Устройство №{device}, ожидаемое время ~{total_est:.0f} с "
        "(FP32 → FP64 → VRAM → PCIe H2D/D2H с остыванием между фазами).[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — прервать с сохранением снятых фаз.")
    console.print()

    try:
        with (
            cancellable() as token,
            console.status("[cyan]Прогрев…[/]", spinner="dots") as status,
        ):
            def on_progress(phase: str, idx: int, total: int) -> None:
                pretty = _PHASE_LABELS.get(phase, phase)
                status.update(f"[cyan]Фаза {idx}/{total}: [bold]{pretty}[/cyan][/]")

            report = orchestrator.run(
                device_index=device,
                params=params,
                cancel_token=token,
                on_progress=on_progress,
            )
    except KeyboardInterrupt:
        console.print("[yellow]Прогон отменён пользователем.[/]")
        raise typer.Exit(code=0) from None

    # Тихо сохраняем в БД до рендера. Ошибку кладём в notes отчёта.
    try:
        settings = load_settings()
        repo = SqliteGpuBenchmarkRepository(settings.db_path)
        repo.save(report)
        repo.close()
    except Exception as exc:
        report.notes.append(f"Не удалось сохранить в БД: {exc}")

    console.print()
    render_gpu_report(report)


@app.command("stress")
def stress(
    device: int = typer.Option(
        0, "--device", "-d", help="Индекс GPU-устройства (см. `gpu list`)."
    ),
    duration: float = typer.Option(
        60.0, "--duration", help="Длительность стресс-нагрузки, секунд."
    ),
) -> None:
    """GPU-стресс на термостабильность: длительная FP32-нагрузка + телеметрия.

    Гоняет максимальную FP32-нагрузку выбранное время и посекундно снимает
    температуру / мощность / частоту / загрузку GPU. Итог — вердикт
    PASS/WARN/FAIL/UNKNOWN (нагрев, троттлинг, обвал частоты) с сохранением
    в БД. Ctrl+C прерывает прогон, вердикт считается по снятым данным.
    """
    from apexcore.application.gpu_stress import GpuStressOrchestrator
    from apexcore.infrastructure.adapters import AdapterFactory
    from apexcore.infrastructure.gpu import build_default_gpu_backend
    from apexcore.infrastructure.persistence import SqliteGpuStressRepository
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.render import console, render_gpu_stress_report
    from apexcore.shared.config import load_settings

    backend = build_default_gpu_backend()
    if not backend.is_available():
        console.print(
            "[yellow]GPU/OpenCL не обнаружен.[/] Стресс-тест невозможен — "
            "нет ни одного OpenCL-устройства.\n"
            "[dim]Проверьте наличие устройств: `apexcore gpu list`.[/]"
        )
        raise typer.Exit(code=0)

    adapter = AdapterFactory.detect()
    orchestrator = GpuStressOrchestrator(adapter, backend)

    console.rule("[bold]Запуск GPU-стресс-теста (термостабильность)[/]")
    console.print(
        f"[dim]Устройство №{device}, длительность ~{duration:.0f} с "
        "(максимальная FP32-нагрузка + посекундная телеметрия).[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — прервать с оценкой по снятым данным.")
    console.print()

    try:
        with (
            cancellable() as token,
            console.status("[cyan]Прогрев…[/]", spinner="dots") as status,
        ):
            def on_progress(elapsed_sec: float, duration_sec: float) -> None:
                status.update(
                    f"[cyan]Нагрузка: [bold]{elapsed_sec:.0f}[/] / "
                    f"{duration_sec:.0f} с[/cyan]"
                )

            report = orchestrator.run(
                device_index=device,
                duration_sec=duration,
                cancel_token=token,
                on_progress=on_progress,
            )
    except KeyboardInterrupt:
        console.print("[yellow]Стресс-тест отменён пользователем.[/]")
        raise typer.Exit(code=0) from None

    # Тихо сохраняем в БД до рендера. Ошибку кладём в notes отчёта.
    try:
        settings = load_settings()
        repo = SqliteGpuStressRepository(settings.db_path)
        repo.save(report)
        repo.close()
    except Exception as exc:
        report.notes.append(f"Не удалось сохранить в БД: {exc}")

    console.print()
    render_gpu_stress_report(report)


@app.command("test")
def test(
    device: int = typer.Option(
        0, "--device", "-d", help="Индекс GPU-устройства (см. `gpu list`)."
    ),
    workload: str = typer.Option(
        "fp32",
        "--workload", "-w",
        help="Тип нагрузки: fp32 | fp64 | mem | pcie (h2d) | pcie_d2h.",
    ),
    duration: float = typer.Option(
        3.0, "--duration", help="Длительность одиночного замера, секунд."
    ),
) -> None:
    """Одиночный замер одной нагрузки без скоринга — сырая скорость.

    Быстрая проверка отдельного показателя: FP32/FP64 в GFLOPS, память и
    PCIe в GB/s. Балл не считается, в БД не сохраняется.
    """
    from apexcore.domain.gpu import GpuWorkloadKind
    from apexcore.infrastructure.gpu import build_default_gpu_backend
    from apexcore.interfaces.cli.menu.cancel import cancellable
    from apexcore.interfaces.cli.render import console

    kind_value = _WORKLOAD_ALIASES.get(workload.strip().lower())
    if kind_value is None:
        console.print(
            f"[red]Неизвестная нагрузка: {workload!r}.[/] "
            "Допустимо: fp32, fp64, mem, pcie (=h2d), pcie_d2h."
        )
        raise typer.Exit(code=2)
    kind = GpuWorkloadKind(kind_value)

    backend = build_default_gpu_backend()
    if not backend.is_available():
        console.print(
            "[yellow]GPU/OpenCL не обнаружен.[/] Замер невозможен.\n"
            "[dim]Проверьте устройства: `apexcore gpu list`.[/]"
        )
        raise typer.Exit(code=0)

    devices = backend.list_devices()
    if device < 0 or device >= len(devices):
        console.print(
            f"[red]Устройство №{device} не найдено.[/] Обнаружено "
            f"{len(devices)} шт. — см. `apexcore gpu list`."
        )
        raise typer.Exit(code=2)

    dev = devices[device]
    if not backend.supports(device, kind):
        console.print(
            f"[yellow]Устройство «{dev.name}» не поддерживает нагрузку "
            f"{kind.value}[/] — замер пропущен."
        )
        raise typer.Exit(code=0)

    console.print(
        f"[bold cyan]Замер {kind.value} на «{dev.name}» "
        f"(~{duration:.1f} с)[/]"
    )
    console.print("[bold yellow]Ctrl+C[/] — прервать.")
    try:
        with cancellable() as token, console.status(
            "[cyan]Идёт замер…[/]", spinner="dots"
        ):
            m = backend.measure(device, kind, duration, token)
    except KeyboardInterrupt:
        console.print("[yellow]Замер отменён пользователем.[/]")
        raise typer.Exit(code=0) from None

    if not m.throughput or m.throughput <= 0:
        console.print(
            "[yellow]Нулевой throughput — измерение не удалось.[/] "
            "Возможно, кернел не запустился на этом устройстве."
        )
        raise typer.Exit(code=0)

    console.print(
        f"[bold green]{m.throughput:.2f} {m.unit}[/]  "
        f"[dim](итераций: {m.iterations}, факт. время: {m.duration_sec:.2f} с"
        + (f", ошибок верификации: {m.error_count}" if m.error_count else "")
        + ")[/]"
    )


def _type_label(device_type: object) -> str:
    """Русская подпись типа GPU (для таблицы `gpu list`)."""
    value = getattr(device_type, "value", device_type)
    return {
        "discrete": "дискретный",
        "integrated": "встроенный",
        "virtual": "виртуальный",
        "unknown": "неизвестно",
    }.get(str(value), str(value) or "неизвестно")
