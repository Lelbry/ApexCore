"""CLI-команда `apexcore runs` — управление историей прогонов.

Объединённая лента типов прогонов:
- **stress** — длительные стресс-прогоны (таблица ``runs``).
- **micro**  — scoring v2 (таблица ``micro_runs``), реальный балл бенчмарка.
- **winsat** — Аналог Windows Winsat (таблица ``winsat_runs``, шкала 1.0–9.9).
- **general** — Общая оценка производительности системы
  (таблица ``general_benchmark_runs``, шкала ×10 000).
- **gpu** — GPU-бенчмарк по Roofline (таблица ``gpu_benchmark_runs``, шкала ×10 000).
- **gpu_stress** — GPU-стресс на термостабильность (таблица ``gpu_stress_runs``,
  headline — вердикт PASS/WARN/FAIL/UNKNOWN, без числового балла).

Хелперы ``collect_unified_listing`` / ``render_unified_listing`` /
``show_run_by_ref`` / ``export_run_by_ref`` используются интерактивным
меню (``HistoryScreen``) для работы по номеру строки, без ввода UUID.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import typer

logger = logging.getLogger(__name__)

app = typer.Typer(help="Просмотр и удаление сохранённых прогонов.")

RunKind = Literal["stress", "micro", "winsat", "general", "gpu", "gpu_stress"]

_KIND_LABELS: dict[str, str] = {
    "stress":     "Стресс",
    "micro":      "Тест CPU",
    "winsat":     "Winsat",
    "general":    "Общая оценка производительности системы",
    "gpu":        "Тест GPU",
    "gpu_stress": "GPU-стресс",
}


@dataclass(frozen=True)
class RunRef:
    """Ссылка на один прогон в объединённой ленте.

    Содержит достаточно полей для отрисовки одной строки таблицы и для
    последующего резолва в полный объект через нужный репозиторий.
    ``score_display`` может быть многострочным (для Winsat — большой
    WinSPRLevel + строка с подскорами по компонентам).
    """

    kind: RunKind
    uuid: str
    start_time: datetime
    duration_sec: float
    score_display: str   # отформатированный балл; может содержать `\n`


def collect_unified_listing(limit: int = 20) -> list[RunRef]:
    """Собрать ленту из всех репозиториев, отсортировать по start_time DESC.

    Берёт по ``limit`` записей из каждого репо — после слияния и сортировки
    общая выборка обрезается до ``limit``. Это компромисс между точностью и
    числом запросов: при сильно перекошенной активности (например, 100
    micro-прогонов подряд) старые stress-прогоны могут не попасть в верхушку,
    но для типичных дашбордов лимит 20 это всегда покрывает.

    **Устойчивость к повреждённой БД.** Чтение каждого репозитория обёрнуто
    в try/except: повреждение одной таблицы (``sqlite3.DatabaseError`` —
    «database disk image is malformed») или иной сбой слоя хранения
    (``RepositoryError``) **не** должны ронять всю ленту. Сбойный
    репозиторий пропускается с предупреждением в лог, пользователю
    показывается одна строка-нотис (см. :func:`_warn_partial_history`), а
    остальные — читаемые — прогоны всё равно отрисуются. До этого фикса
    битая страница в таблице ``runs`` обрывала весь листинг, и micro/
    winsat/general-история тоже становилась недоступной.
    """
    from apexcore.domain.errors import RepositoryError
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )
    from apexcore.shared.config import load_settings

    settings = load_settings()
    refs: list[RunRef] = []
    degraded = False

    def _safe_collect(label: str, build: Callable[[], None]) -> None:
        """Выполнить чтение одного репозитория, проглотив ошибки битой БД.

        ``list_runs`` репозиториев бросает «сырой» ``sqlite3.DatabaseError``
        при повреждённой странице таблицы; конструктор репозитория
        (``apply_schema``) — тоже, если повреждён заголовок файла.
        ``RepositoryError`` — обёрнутые ошибки слоя хранения. Любой из них
        переводит репозиторий в «пропущен», но не валит остальные.
        """
        nonlocal degraded
        try:
            build()
        except (sqlite3.DatabaseError, RepositoryError) as exc:
            logger.warning(
                "runs list: репозиторий %s недоступен (БД %s): %s — пропускаем",
                label, settings.db_path, exc,
            )
            degraded = True

    def _collect_stress() -> None:
        stress_repo = SqliteResultRepository(settings.db_path)
        for r in stress_repo.list_runs(limit=limit):
            # Стресс намеренно показывает «—» в ленте: stress_score (1000·GM)
            # сейчас выведен из истории до подтверждения валидности метода
            # (см. отдельное ТЗ). Сам прогон сохраняет температуры, throttling
            # и verdict — это видно в режиме просмотра деталей.
            refs.append(RunRef(
                kind="stress",
                uuid=str(r.id),
                start_time=r.start_time,
                duration_sec=(r.end_time - r.start_time).total_seconds(),
                score_display="—",
            ))

    def _collect_micro() -> None:
        micro_repo = SqliteMicroRunRepository(settings.db_path)
        for m in micro_repo.list_runs(limit=limit):
            # Единый балл micro удалён в 0.9.x — это детальный per-category
            # анализ, не системный балл. В списке прогонов показываем «—».
            refs.append(RunRef(
                kind="micro",
                uuid=str(m.id),
                start_time=m.start_time,
                duration_sec=(m.end_time - m.start_time).total_seconds(),
                score_display="—",
            ))

    def _collect_winsat() -> None:
        winsat_repo = SqliteWinsatRepository(settings.db_path)
        for w in winsat_repo.list_runs(limit=limit):
            refs.append(RunRef(
                kind="winsat",
                uuid=str(w.id),
                start_time=w.started_at,
                duration_sec=(w.ended_at - w.started_at).total_seconds(),
                score_display=_format_winsat_score(w),
            ))

    def _collect_general() -> None:
        gb_repo = SqliteGeneralBenchmarkRepository(settings.db_path)
        for gb in gb_repo.list_runs(limit=limit):
            refs.append(RunRef(
                kind="general",
                uuid=str(gb.id),
                start_time=gb.started_at,
                duration_sec=(gb.ended_at - gb.started_at).total_seconds(),
                score_display=("—" if gb.score is None else f"{gb.score:,.0f}".replace(",", " ")),
            ))

    def _collect_gpu() -> None:
        gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
        for g in gpu_repo.list_runs(limit=limit):
            refs.append(RunRef(
                kind="gpu",
                uuid=str(g.id),
                start_time=g.started_at,
                duration_sec=(g.ended_at - g.started_at).total_seconds(),
                score_display=(
                    "—" if g.score is None else f"{g.score:,.0f}".replace(",", " ")
                ),
            ))

    def _collect_gpu_stress() -> None:
        gs_repo = SqliteGpuStressRepository(settings.db_path)
        for gs in gs_repo.list_runs(limit=limit):
            # У GPU-стресса нет балла — headline это вердикт стабильности.
            refs.append(RunRef(
                kind="gpu_stress",
                uuid=str(gs.id),
                start_time=gs.started_at,
                duration_sec=(gs.ended_at - gs.started_at).total_seconds(),
                score_display=_format_gpu_stress_verdict(gs.verdict),
            ))

    _safe_collect("стресс (runs)", _collect_stress)
    _safe_collect("micro (micro_runs)", _collect_micro)
    _safe_collect("winsat (winsat_runs)", _collect_winsat)
    _safe_collect("общая оценка (general_benchmark_runs)", _collect_general)
    _safe_collect("gpu-бенчмарк (gpu_benchmark_runs)", _collect_gpu)
    _safe_collect("gpu-стресс (gpu_stress_runs)", _collect_gpu_stress)

    refs.sort(key=lambda r: r.start_time, reverse=True)
    if degraded:
        _warn_partial_history(settings.db_path)
    return refs[:limit]


def _warn_partial_history(db_path: Path | None) -> None:
    """Показать одну строку-нотис, когда часть истории не прочиталась.

    Срабатывает, только если хотя бы один репозиторий упал на повреждённой
    БД в :func:`collect_unified_listing`. Подсказывает путь к файлу БД:
    пересоздание (переименование/удаление) обнуляет историю, но снимает
    повреждение — это типичный способ восстановления для SQLite-файла с
    битой страницей.
    """
    from apexcore.interfaces.cli.messages import db_corrupt_recovery_hint
    from apexcore.interfaces.cli.render import console

    console.print(
        "[yellow]⚠ Часть истории недоступна (повреждённая БД).[/] "
        "Показаны только читаемые прогоны.\n  "
        + db_corrupt_recovery_hint(db_path)
    )


def _format_winsat_score(report) -> str:
    """Строка с подскорами всех компонентов Winsat.

    Формат:

        CPU 4.0 · Mem 4.6 · Disk 9.2 · GFX N/A · 3D N/A

    Аггрегатный ``WinSPRLevel`` сюда **не** включается — он по определению
    равен минимуму PASS-подскоров и визуально считывается из самой строки
    (тут это CPU = 4.0). Дубль числа в ячейке только мешал. Шкала
    1.0–9.9 пояснена в подписи под таблицей. N/A — для подскоров со
    статусом ``na`` / ``error`` (Graphics и D3D пока не реализованы — MVP).
    """
    from apexcore.domain.winsat import WinsatStatus

    def _sub(label: str, sub) -> str:
        if sub.status == WinsatStatus.PASS:
            return f"{label} {sub.score:.1f}"
        return f"{label} N/A"

    return "  ·  ".join([
        _sub("CPU", report.cpu_score),
        _sub("Mem", report.memory_score),
        _sub("Disk", report.disk_score),
        _sub("GFX", report.graphics_score),
        _sub("3D", report.d3d_score),
    ])


def _format_gpu_stress_verdict(verdict: object) -> str:
    """Короткая цветная подпись вердикта GPU-стресса для ленты истории.

    ``verdict`` — ``GpuStressVerdict`` (str-Enum). У GPU-стресса нет
    числового балла, поэтому в колонку «Балл» кладём сам вердикт
    (PASS/WARN/FAIL/UNKNOWN → русское слово с цветом).
    """
    value = str(getattr(verdict, "value", verdict) or "unknown")
    return {
        "pass":    "[green]ПРОЙДЕНО[/]",
        "warn":    "[yellow]С замечаниями[/]",
        "fail":    "[red]НЕ ПРОЙДЕНО[/]",
        "unknown": "[dim]н/д[/]",
    }.get(value, "[dim]н/д[/]")


def render_unified_listing(refs: list[RunRef], *, with_uuid: bool = False) -> None:
    """Отрисовать таблицу ленты с нумерацией 1..N.

    ``with_uuid=False`` (по умолчанию) — TUI-режим, UUID не показывается
    (нумерация 1..N — единственный идентификатор для пользователя).
    ``with_uuid=True`` — CLI-режим, добавляется сокращённый UUID для
    последующего ``apexcore runs show <uuid>`` / ``delete <uuid>``.
    """
    from rich.table import Table

    from apexcore.interfaces.cli.render import console, fmt_dt

    if not refs:
        console.print(
            "[yellow]Прогонов ещё нет.[/] "
            "Запустите стресс-нагрузку, расширенный тест CPU или Winsat — "
            "результаты появятся здесь."
        )
        return

    # Порядок: №, Дата/время, Длит., Тип, Балл (Тип непосредственно перед
    # Баллом — пользователь сначала видит «что/когда», затем «что за тест
    # и какая оценка»). «Профиль/Preset» убран как технический шум.
    tbl = Table(title=f"История прогонов (последние {len(refs)})", show_header=True)
    tbl.add_column("№", justify="right", style="bold cyan")
    tbl.add_column("Дата/время")
    tbl.add_column("Длит., с", justify="right")
    tbl.add_column("Тип", style="bold")
    tbl.add_column("Балл", justify="right", style="bold green")
    if with_uuid:
        tbl.add_column("UUID", style="dim")
    for idx, ref in enumerate(refs, start=1):
        row = [
            str(idx),
            fmt_dt(ref.start_time),
            f"{ref.duration_sec:.0f}",
            _KIND_LABELS.get(ref.kind, ref.kind),
            ref.score_display,
        ]
        if with_uuid:
            row.append(ref.uuid[:8] + "…")
        tbl.add_row(*row)
    console.print(tbl)


def show_run_by_ref(ref: RunRef) -> None:
    """Отрисовать детали одного прогона; рендер выбирается по ``ref.kind``."""
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )
    from apexcore.interfaces.cli.render import (
        console,
        render_bench_result,
        render_general_benchmark_report,
        render_gpu_report,
        render_gpu_stress_report,
        render_metric_summary,
        render_microbench_suite,
        render_overall_score,
        render_winsat_report,
    )
    from apexcore.shared.config import load_settings

    settings = load_settings()
    if ref.kind == "stress":
        repo = SqliteResultRepository(settings.db_path)
        result = repo.get(ref.uuid)
        if result is None:
            console.print(f"[red]Стресс-прогон не найден: {ref.uuid}[/]")
            return
        render_bench_result(result)
        if result.metrics_history:
            render_metric_summary(result.metrics_history)
        return

    if ref.kind == "micro":
        m_repo = SqliteMicroRunRepository(settings.db_path)
        suite = m_repo.get(ref.uuid)
        if suite is None:
            console.print(f"[red]Micro-прогон не найден: {ref.uuid}[/]")
            return
        render_microbench_suite(suite)
        if suite.overall is not None:
            render_overall_score(suite.overall, preset=suite.preset)
        return

    if ref.kind == "winsat":
        w_repo = SqliteWinsatRepository(settings.db_path)
        report = w_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]Winsat-прогон не найден: {ref.uuid}[/]")
            return
        render_winsat_report(report)
        return

    if ref.kind == "general":
        gb_repo = SqliteGeneralBenchmarkRepository(settings.db_path)
        report = gb_repo.get(ref.uuid)
        if report is None:
            console.print(
                f"[red]Прогон общей оценки не найден: {ref.uuid}[/]"
            )
            return
        render_general_benchmark_report(report)
        return

    if ref.kind == "gpu":
        gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
        report = gpu_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]GPU-прогон не найден: {ref.uuid}[/]")
            return
        render_gpu_report(report)
        return

    if ref.kind == "gpu_stress":
        gs_repo = SqliteGpuStressRepository(settings.db_path)
        report = gs_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]GPU-стресс-прогон не найден: {ref.uuid}[/]")
            return
        render_gpu_stress_report(report)
        return

    console.print(f"[red]Неизвестный тип прогона: {ref.kind!r}[/]")


def export_run_by_ref(ref: RunRef, fmt: str, out: Path | None) -> Path | None:
    """Экспортировать прогон в JSON / CSV.

    - JSON работает для всех типов (через ``model_dump_json``).
    - CSV работает только для **stress** (там есть таймсерия
      ``metrics_history``, по которой `csv_exporter` строит таблицу
      отсчётов). Для micro / winsat / general / gpu / gpu-стресс CSV выдаст
      предупреждение и не сохранит файл.
    """
    from apexcore.infrastructure.exporters import export_run_csv, export_run_json
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )
    from apexcore.interfaces.cli.render import console
    from apexcore.shared.config import load_settings

    fmt = fmt.lower().strip()
    if fmt not in ("json", "csv"):
        console.print(f"[red]Неизвестный формат '{fmt}', допустимы: json, csv[/]")
        return None

    settings = load_settings()

    if ref.kind == "stress":
        repo = SqliteResultRepository(settings.db_path)
        result = repo.get(ref.uuid)
        if result is None:
            console.print(f"[red]Стресс-прогон не найден: {ref.uuid}[/]")
            return None
        target = out or Path(f"apexcore_stress_{result.id}.{fmt}")
        if fmt == "json":
            export_run_json(result, target)
        else:
            export_run_csv(result, target)
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    if ref.kind == "micro":
        if fmt == "csv":
            console.print(
                "[yellow]CSV для scoring v2 не поддерживается[/] "
                "(нет временных рядов телеметрии). Используйте JSON."
            )
            return None
        m_repo = SqliteMicroRunRepository(settings.db_path)
        suite = m_repo.get(ref.uuid)
        if suite is None:
            console.print(f"[red]Micro-прогон не найден: {ref.uuid}[/]")
            return None
        target = out or Path(f"apexcore_micro_{suite.id}.json")
        target.write_text(suite.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    if ref.kind == "winsat":
        if fmt == "csv":
            console.print(
                "[yellow]CSV для winsat-прогона не поддерживается[/] "
                "(только финальные подскоры). Используйте JSON."
            )
            return None
        w_repo = SqliteWinsatRepository(settings.db_path)
        report = w_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]Winsat-прогон не найден: {ref.uuid}[/]")
            return None
        target = out or Path(f"apexcore_winsat_{report.id}.json")
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    if ref.kind == "general":
        if fmt == "csv":
            console.print(
                "[yellow]CSV для общей оценки не поддерживается[/] "
                "(только финальные числа). Используйте JSON."
            )
            return None
        gb_repo = SqliteGeneralBenchmarkRepository(settings.db_path)
        report = gb_repo.get(ref.uuid)
        if report is None:
            console.print(
                f"[red]Прогон общей оценки не найден: {ref.uuid}[/]"
            )
            return None
        target = out or Path(f"apexcore_general_{report.id}.json")
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    if ref.kind == "gpu":
        if fmt == "csv":
            console.print(
                "[yellow]CSV для GPU-бенчмарка не поддерживается[/] "
                "(только финальные числа). Используйте JSON."
            )
            return None
        gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
        report = gpu_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]GPU-прогон не найден: {ref.uuid}[/]")
            return None
        target = out or Path(f"apexcore_gpu_{report.id}.json")
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    if ref.kind == "gpu_stress":
        if fmt == "csv":
            console.print(
                "[yellow]CSV для GPU-стресса не поддерживается[/] "
                "(только сводки телеметрии). Используйте JSON."
            )
            return None
        gs_repo = SqliteGpuStressRepository(settings.db_path)
        report = gs_repo.get(ref.uuid)
        if report is None:
            console.print(f"[red]GPU-стресс-прогон не найден: {ref.uuid}[/]")
            return None
        target = out or Path(f"apexcore_gpu_stress_{report.id}.json")
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[green]Экспортировано → {target}[/]")
        return target

    console.print(f"[red]Неизвестный тип прогона: {ref.kind!r}[/]")
    return None


def _resolve_to_ref(run_id: str) -> RunRef | None:
    """Найти прогон по UUID/префиксу в одной из таблиц истории.

    Возвращает ``RunRef`` с заполненным ``kind`` и полным UUID; остальные поля
    оставлены неинициализированными — используется только для ``show_run`` и
    ``delete_run`` CLI-команд, где детали приходят при рендере.
    """
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )
    from apexcore.shared.config import load_settings

    settings = load_settings()

    stress_repo = SqliteResultRepository(settings.db_path)
    full = stress_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="stress", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    m_repo = SqliteMicroRunRepository(settings.db_path)
    full = m_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="micro", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    w_repo = SqliteWinsatRepository(settings.db_path)
    full = w_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="winsat", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    gb_repo = SqliteGeneralBenchmarkRepository(settings.db_path)
    full = gb_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="general", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
    full = gpu_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="gpu", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    gs_repo = SqliteGpuStressRepository(settings.db_path)
    full = gs_repo.resolve_id(run_id)
    if full is not None:
        return RunRef(kind="gpu_stress", uuid=full, start_time=datetime.min,
                      duration_sec=0.0, score_display="")

    return None


# ─── CLI commands ──────────────────────────────────────────────────────────


@app.command("list")
def list_runs(
    limit: int = typer.Option(
        20, "--limit", "-n", help="Сколько последних прогонов показать."
    ),
) -> None:
    """Перечислить последние прогоны (объединённая лента всех типов).

    В CLI-режиме показывается сокращённый UUID в последней колонке для
    последующего ``apexcore runs show <uuid>`` / ``delete <uuid>``.
    """
    refs = collect_unified_listing(limit=limit)
    render_unified_listing(refs, with_uuid=True)


@app.command("show")
def show_run(
    run_id: str = typer.Argument(..., help="UUID или префикс прогона."),
) -> None:
    """Показать сводку по одному прогону.

    Ищет во всех таблицах истории (stress / micro / winsat / general / gpu /
    gpu-стресс) и адаптирует рендер под тип. Принимает полный UUID или
    префикс (минимум 4 символа).
    """
    from apexcore.interfaces.cli.render import console

    ref = _resolve_to_ref(run_id)
    if ref is None:
        console.print(
            f"[red]Прогон '{run_id}' не найден[/] "
            "(искал в stress / micro / winsat / general / gpu / gpu-стресс)"
        )
        raise typer.Exit(code=2)
    show_run_by_ref(ref)


@app.command("delete")
def delete_run(
    run_id: str = typer.Argument(..., help="UUID прогона."),
    yes: bool = typer.Option(False, "-y"),
) -> None:
    """Удалить прогон по UUID (ищет во всех таблицах)."""
    from apexcore.infrastructure.persistence import (
        SqliteGeneralBenchmarkRepository,
        SqliteGpuBenchmarkRepository,
        SqliteGpuStressRepository,
        SqliteMicroRunRepository,
        SqliteResultRepository,
        SqliteWinsatRepository,
    )
    from apexcore.interfaces.cli.render import console
    from apexcore.shared.config import load_settings

    if not yes and not typer.confirm(f"Удалить прогон {run_id}?"):
        raise typer.Abort()

    settings = load_settings()
    deleted = False

    stress_repo = SqliteResultRepository(settings.db_path)
    full = stress_repo.resolve_id(run_id)
    if full is not None:
        deleted = stress_repo.delete(UUID(full))

    if not deleted:
        m_repo = SqliteMicroRunRepository(settings.db_path)
        full = m_repo.resolve_id(run_id)
        if full is not None:
            deleted = m_repo.delete(UUID(full))

    if not deleted:
        w_repo = SqliteWinsatRepository(settings.db_path)
        full = w_repo.resolve_id(run_id)
        if full is not None:
            deleted = w_repo.delete(UUID(full))

    if not deleted:
        gb_repo = SqliteGeneralBenchmarkRepository(settings.db_path)
        full = gb_repo.resolve_id(run_id)
        if full is not None:
            deleted = gb_repo.delete(UUID(full))

    if not deleted:
        gpu_repo = SqliteGpuBenchmarkRepository(settings.db_path)
        full = gpu_repo.resolve_id(run_id)
        if full is not None:
            deleted = gpu_repo.delete(UUID(full))

    if not deleted:
        gs_repo = SqliteGpuStressRepository(settings.db_path)
        full = gs_repo.resolve_id(run_id)
        if full is not None:
            deleted = gs_repo.delete(UUID(full))

    console.print("[green]Удалено[/]" if deleted else "[yellow]Не найдено[/]")
