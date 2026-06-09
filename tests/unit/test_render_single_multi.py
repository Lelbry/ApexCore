"""Тесты `interfaces/cli/render.render_single_multi_result` — карточки Single/Multi."""

from __future__ import annotations

from rich.console import Console

from apexcore.domain.models import MicroBenchResult, SingleMultiResult
from apexcore.interfaces.cli import render as render_mod


def _make_result(
    single_val: float = 2.13,
    multi_val: float = 38.7,
    cores: int = 24,
    physical_cores: int | None = 16,
    physical_p_cores: int | None = 8,
    physical_e_cores: int | None = 8,
    pinned_cpu: int | None = 0,
    pinned_kind: str | None = "P-core",
    bench_name: str = "int_iops_64",
    backend: str = "numba",
) -> SingleMultiResult:
    single = MicroBenchResult(
        name=bench_name, category="integer", value=single_val,
        unit="GIOPS", duration_actual_sec=5.0, iterations=10, threads=1,
        extra={"backend": backend},
    )
    multi = MicroBenchResult(
        name=bench_name, category="integer", value=multi_val,
        unit="GIOPS", duration_actual_sec=5.0, iterations=cores * 10, threads=cores,
        extra={"backend": backend, "parallel_workers": cores},
    )
    return SingleMultiResult(
        bench_name=bench_name,
        duration_sec_per_test=5.0,
        single=single,
        multi=multi,
        cores_used_multi=cores,
        physical_cores=physical_cores,
        physical_p_cores=physical_p_cores,
        physical_e_cores=physical_e_cores,
        pinned_cpu=pinned_cpu,
        pinned_kind=pinned_kind,
    )


def _capture(result: SingleMultiResult, monkeypatch, width: int = 160) -> str:
    fake_console = Console(width=width, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(render_mod, "console", fake_console)
    render_mod.render_single_multi_result(result)
    return fake_console.export_text()


def test_render_shows_both_panels(monkeypatch):
    out = _capture(_make_result(), monkeypatch)
    assert "Single-Core" in out
    assert "Multi-Core" in out


def test_render_shows_metric_values(monkeypatch):
    out = _capture(_make_result(single_val=2.13, multi_val=38.7), monkeypatch)
    assert "2.13" in out
    assert "38.7" in out  # >=10 → 2 знака
    assert "GIOPS" in out


def test_render_shows_pinned_cpu_kind(monkeypatch):
    out = _capture(_make_result(pinned_cpu=0, pinned_kind="P-core"), monkeypatch)
    assert "CPU 0" in out
    assert "P-core" in out


def test_render_no_pinned_cpu_shows_scheduler(monkeypatch):
    """Если affinity недоступна — пишем 'scheduler'."""
    out = _capture(_make_result(pinned_cpu=None, pinned_kind=None), monkeypatch)
    assert "scheduler" in out.lower()


def test_render_shows_speedup_and_efficiency(monkeypatch):
    """speedup = 38.7 / 2.13 ≈ 18.17; efficiency = 18.17 / 24 ≈ 76%."""
    out = _capture(_make_result(single_val=2.13, multi_val=38.7, cores=24), monkeypatch)
    # speedup округляется до 2 знаков
    assert "18.17" in out or "×18.17" in out
    # efficiency округляется до 0 знаков
    assert "76%" in out


def test_render_does_not_show_bench_internals(monkeypatch):
    """В обновлённой карточке не показываем имя бенча / бэкенда / единицы —
    пользователю это не нужно (см. фидбек 2026-05-15)."""
    out = _capture(
        _make_result(bench_name="int_iops_64", backend="numba"), monkeypatch
    )
    assert "int_iops_64" not in out
    assert "numba" not in out


def test_render_shows_speedup_summary(monkeypatch):
    out = _capture(_make_result(), monkeypatch)
    assert "Ускорение" in out
    assert "Эффективность" in out


def test_render_shows_outer_test_title(monkeypatch):
    """Внешняя карточка имеет заголовок 'Тест Single-Core / Multi-Core'."""
    out = _capture(_make_result(), monkeypatch)
    assert "Тест Single-Core / Multi-Core" in out


def test_render_multi_card_shows_physical_cores_hybrid(monkeypatch):
    """На hybrid CPU Multi-карточка должна показывать '16 ядер (P 8 + E 8)', не 24."""
    out = _capture(
        _make_result(cores=24, physical_cores=16, physical_p_cores=8, physical_e_cores=8),
        monkeypatch,
    )
    # Метка badge содержит phys cores + P/E.
    assert "16" in out
    assert "P 8" in out or "8P" in out
    assert "E 8" in out or "8E" in out
    # А 24 идёт только в строке «Потоков».
    assert "Потоков" in out


def test_render_multi_card_shows_physical_cores_non_hybrid(monkeypatch):
    """AMD/classic Intel: только число физических ядер, без P/E."""
    out = _capture(
        _make_result(cores=32, physical_cores=16, physical_p_cores=None, physical_e_cores=None),
        monkeypatch,
    )
    assert "Все ядра: 16" in out
    assert "P 8" not in out
    assert "Потоков" in out


def test_render_uses_yadra_label_not_vorker(monkeypatch):
    """Подтверждение фидбека: 'Воркеров' заменено на 'Ядра' / 'Поток' / 'Потоков'."""
    out = _capture(_make_result(), monkeypatch)
    # «Воркеров» в подписях карточек больше нет
    assert "Воркеров" not in out
    # Есть «Ядра» в Multi и «Поток» в Single
    assert "Ядра" in out
    assert "Поток" in out


def test_render_summary_has_ascii_bar(monkeypatch):
    """Сводка содержит визуальные бары для Single и Multi."""
    out = _capture(_make_result(), monkeypatch)
    # Хотя бы один блок-символ должен присутствовать
    assert "▇" in out
    assert "░" in out
