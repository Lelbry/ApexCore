"""Тесты `interfaces/cli/render.render_stress_final_report`.

Этап 1 (объяснять «нет данных»): подсказка про LHM, GPU-фон, тротлинг.

Рендер читает поля отчёта через ``getattr`` (см. комментарий в самом
``render_stress_final_report``), поэтому в тестах используем
``types.SimpleNamespace`` вместо собирания полного pydantic-объекта.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from rich.console import Console

from apexcore.domain.models import MetricSnapshot
from apexcore.interfaces.cli import render as render_mod


def _make_report(
    *,
    passed: bool = True,
    cpu_avg_temp_c: float | None = 62.0,
    cpu_peak_temp_c: float | None = 78.0,
    cpu_temp_source_ok: bool = True,
    cpu_temp_source_message: str | None = None,
    cpu_temp_source_advice: list[str] | None = None,
    gpu_peak_temp_c: float | None = 45.0,
    gpu_peak_load_pct: float | None = 30.0,
    gpu_was_stressed: bool = False,
    throttle_observed: bool = False,
    frame_rate_stability_pct: float | None = 99.5,
    stress_score: float | None = None,
    stress_r_dgemm: float | None = None,
    stress_r_stream: float | None = None,
    stress_r_stability: float | None = None,
    stress_r_thermal: float | None = None,
    stress_t_max_c: float | None = None,
    stress_tjmax_c: int | None = None,
    stress_duration_sec: float | None = None,
    roofline_dgemm_peak_gflops: float | None = None,
    roofline_stream_peak_gb_s: float | None = None,
    roofline_simd_level: str | None = None,
    roofline_clock_ghz: float | None = None,
    roofline_dram_mts: float | None = None,
    roofline_dram_modules: int | None = None,
    metrics_history: list | None = None,
    cpu_avg_power_w: float | None = None,
    cpu_peak_power_w: float | None = None,
) -> SimpleNamespace:
    verdict = SimpleNamespace(
        passed=passed,
        reason="все защитные пороги в норме" if passed else "не пройдено",
    )
    thermal = SimpleNamespace(
        throttle_observed=throttle_observed,
        frame_rate_stability_pct=frame_rate_stability_pct,
    )
    parallel = SimpleNamespace(results=[], cancelled=False)
    safety = SimpleNamespace(warn_reasons=[], cooling_sanity_ok=True)
    return SimpleNamespace(
        duration_actual_sec=30.0,
        requested_duration_sec=30.0,
        watchdog_triggered=False,
        watchdog_trigger=None,
        parallel=parallel,
        thermal=thermal,
        verdict=verdict,
        safety=safety,
        # CPU / RAM / GPU
        cpu_avg_load_pct=99.0,
        cpu_peak_load_pct=100.0,
        cpu_avg_temp_c=cpu_avg_temp_c,
        cpu_peak_temp_c=cpu_peak_temp_c,
        cpu_thermal_limit_c=100.0,
        cpu_avg_vcore_v=None,
        cpu_peak_vcore_v=None,
        ram_avg_load_pct=85.0,
        ram_peak_load_pct=92.0,
        ram_avg_vcore_v=None,
        ram_peak_vcore_v=None,
        gpu_avg_temp_c=42.0 if gpu_peak_temp_c is not None else None,
        gpu_peak_temp_c=gpu_peak_temp_c,
        gpu_avg_load_pct=28.0 if gpu_peak_load_pct is not None else None,
        gpu_peak_load_pct=gpu_peak_load_pct,
        gpu_peak_mem_gb=2.0,
        gpu_mem_total_gb=8.0,
        gpu_thermal_limit_c=88.0,
        gpu_avg_vcore_v=None,
        gpu_peak_vcore_v=None,
        gpu_name="Test GPU",
        # Этап 1
        cpu_temp_source_ok=cpu_temp_source_ok,
        cpu_temp_source_message=cpu_temp_source_message,
        cpu_temp_source_advice=cpu_temp_source_advice or [],
        gpu_was_stressed=gpu_was_stressed,
        # Этапы 3a/3b
        stress_score=stress_score,
        stress_r_dgemm=stress_r_dgemm,
        stress_r_stream=stress_r_stream,
        stress_r_stability=stress_r_stability,
        stress_r_thermal=stress_r_thermal,
        stress_t_max_c=stress_t_max_c,
        stress_tjmax_c=stress_tjmax_c,
        stress_duration_sec=stress_duration_sec,
        roofline_dgemm_peak_gflops=roofline_dgemm_peak_gflops,
        roofline_stream_peak_gb_s=roofline_stream_peak_gb_s,
        roofline_simd_level=roofline_simd_level,
        roofline_clock_ghz=roofline_clock_ghz,
        roofline_dram_mts=roofline_dram_mts,
        roofline_dram_modules=roofline_dram_modules,
        metrics_history=metrics_history or [],
        cpu_avg_power_w=cpu_avg_power_w,
        cpu_peak_power_w=cpu_peak_power_w,
    )


def _capture(report: SimpleNamespace, monkeypatch, width: int = 160) -> str:
    fake = Console(width=width, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(render_mod, "console", fake)
    render_mod.render_stress_final_report(report)
    return fake.export_text()


# ─── CPU temperature advice ─────────────────────────────────────────────────


def test_cpu_temp_reminder_shown_when_no_temp_and_source_broken(monkeypatch):
    """Если температуры нет — финал даёт короткую напоминалку (без полного
    advice, он уже выводился в шапке прогона)."""
    out = _capture(
        _make_report(
            cpu_avg_temp_c=None,
            cpu_peak_temp_c=None,
            cpu_temp_source_ok=False,
            cpu_temp_source_message="реальная температура CPU недоступна",
            cpu_temp_source_advice=[
                "запустите от админа",
                "scripts/fetch_lhm.ps1",
            ],
            frame_rate_stability_pct=None,
        ),
        monkeypatch,
    )
    assert "Температура CPU не считывалась" in out
    # Подсказку «полная диагностика — apexcore doctor» всё же оставляем.
    assert "apexcore doctor" in out
    # Полного advice в финале быть НЕ должно — он есть в шапке прогона.
    assert "запустите от админа" not in out
    assert "scripts/fetch_lhm.ps1" not in out


def test_cpu_temp_reminder_not_shown_when_temp_available(monkeypatch):
    out = _capture(
        _make_report(
            cpu_avg_temp_c=62.0,
            cpu_peak_temp_c=78.0,
            cpu_temp_source_ok=False,  # источник «не ок», но данные есть
            cpu_temp_source_message="что-то странное",
            cpu_temp_source_advice=["совет"],
        ),
        monkeypatch,
    )
    assert "Температура CPU не считывалась" not in out


# ─── GPU «фон, не нагружалась» ──────────────────────────────────────────────


def test_gpu_not_stressed_labeled_as_background(monkeypatch):
    out = _capture(
        _make_report(gpu_was_stressed=False, gpu_peak_temp_c=45.0),
        monkeypatch,
    )
    # В Сводке GPU помечен «(фон)» и статус «фон» — короткий лейбл,
    # чтобы колонка не расползалась.
    assert "(фон)" in out
    assert "фон" in out.lower()


def test_gpu_stressed_shows_normal_status(monkeypatch):
    out = _capture(
        _make_report(gpu_was_stressed=True, gpu_peak_temp_c=70.0),
        monkeypatch,
    )
    # 70 < 88 → «лимит не превышен»
    assert "лимит не превышен" in out
    # И НЕ должно быть пометки «(фон)» при активной нагрузке.
    assert "(фон)" not in out


# ─── Тротлинг: «нет данных» vs «не зафиксирован» ────────────────────────────


def test_throttling_says_no_data_without_any_thermal_telemetry(monkeypatch):
    out = _capture(
        _make_report(
            cpu_avg_temp_c=None,
            cpu_peak_temp_c=None,
            frame_rate_stability_pct=None,
            throttle_observed=False,
        ),
        monkeypatch,
    )
    assert "нет данных" in out
    assert "не зафиксирован" not in out


def test_throttling_says_no_data_when_temps_missing_even_if_freq_present(monkeypatch):
    """Регрессия: даже если psutil.cpu_freq дал stability, без CPU-температуры
    нельзя различить термальный и power-throttle → «нет данных».
    """
    out = _capture(
        _make_report(
            cpu_avg_temp_c=None,
            cpu_peak_temp_c=None,
            frame_rate_stability_pct=99.0,  # частоты есть
            throttle_observed=False,
        ),
        monkeypatch,
    )
    assert "нет данных" in out
    assert "не зафиксирован" not in out


def test_throttling_says_not_observed_when_temps_present(monkeypatch):
    out = _capture(
        _make_report(
            cpu_avg_temp_c=62.0,
            cpu_peak_temp_c=78.0,
            throttle_observed=False,
        ),
        monkeypatch,
    )
    assert "не зафиксирован" in out


def test_throttling_says_observed_when_throttled(monkeypatch):
    out = _capture(
        _make_report(
            cpu_avg_temp_c=85.0,
            cpu_peak_temp_c=95.0,
            throttle_observed=True,
        ),
        monkeypatch,
    )
    assert "зафиксирован" in out
    # «не зафиксирован» НЕ должно тут встречаться отдельно
    assert "не зафиксирован" not in out


# ─── Этап 3a/3b: Стресс-балл и Roofline-блок ────────────────────────────────


def test_stress_score_panel_shown_when_score_present(monkeypatch):
    """Плашка показывает число + однострочное пояснение (новый UI-лейбл).

    duration_sec=180 (≥ RELIABLE_DURATION_SEC) — Panel идёт по else-ветке
    с подписью «Чистая оценка производительности — в разделе меню...».
    """
    out = _capture(
        _make_report(
            stress_score=2410.0,
            stress_r_dgemm=0.10,
            stress_r_stream=0.14,
            stress_r_stability=1.00,
            stress_duration_sec=180.0,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой" in out
    assert "CPU+RAM+охлаждение" in out
    assert "2 410" in out or "2410" in out
    # Пояснение и отсылка на реализованный раздел «Общая оценка
    # производительности системы» (cb85dba) присутствуют. Подстроки
    # проверяем раздельно: Rich Panel может word-wrap длинную dim-строку.
    assert "стабильности" in out  # «стабильности частот и теплового запаса»
    assert "Общая оценка" in out
    assert "производительности системы" in out
    # Компоненты ratio в плашке НЕ показываем.
    assert "DGEMM-ratio" not in out
    assert "GM =" not in out


def test_stress_score_panel_hidden_when_all_components_none(monkeypatch):
    """Если ни один из ratio не вычислился — ни плашки, ни строки про балл."""
    out = _capture(
        _make_report(
            stress_score=None,
            stress_r_dgemm=None,
            stress_r_stream=None,
            stress_r_stability=None,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой" not in out


def test_stress_score_says_unavailable_when_partially_computed(monkeypatch):
    """Регрессия фото2 (под админом без LHM): DGEMM + STREAM ratio есть, но
    r_stability=None (нет cpu_freq) → балл = None. Показываем строку с
    перечислением отсутствующих компонентов, а не молча скрываем.
    """
    out = _capture(
        _make_report(
            stress_score=None,
            stress_r_dgemm=0.066,
            stress_r_stream=0.167,
            stress_r_stability=None,
            roofline_dgemm_peak_gflops=819.0,
            roofline_stream_peak_gb_s=102.4,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой недоступна" in out
    assert "стабильность частот CPU" in out


def test_stress_score_unavailable_lists_thermal_when_temp_missing(monkeypatch):
    """Если r_thermal=None из-за отсутствия CPU temp — конкретное сообщение
    в missing-list, а не общее «r_thermal» (UX актуален для не-admin).
    """
    out = _capture(
        _make_report(
            stress_score=None,
            stress_r_dgemm=0.20,
            stress_r_stream=0.40,
            stress_r_stability=0.99,
            stress_r_thermal=None,
            stress_t_max_c=None,
            stress_tjmax_c=100,
            stress_duration_sec=180.0,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой недоступна" in out
    assert "температура CPU" in out


def test_stress_score_short_run_shows_warning_in_panel(monkeypatch):
    """Балл < 90 сек: Panel показан, но с warning «приближённая оценка».

    Гейт 90 сек убран по запросу пользователя 2026-05-17 — балл выводится
    всегда, рендер добавляет warning о точности.
    """
    out = _capture(
        _make_report(
            stress_score=4250.0,
            stress_r_dgemm=0.20,
            stress_r_stream=0.40,
            stress_r_stability=0.99,
            stress_r_thermal=0.95,
            stress_t_max_c=72.0,
            stress_tjmax_c=100,
            stress_duration_sec=30.0,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой" in out
    assert "CPU+RAM+охлаждение" in out
    assert "4 250" in out or "4250" in out
    # Warning виден.
    assert "приближённая" in out
    assert "10–60 мин" in out or "10-60 мин" in out


def test_stress_score_unavailable_lists_unknown_tjmax(monkeypatch):
    """Если r_thermal=None из-за нераспознанного CPU (tjmax=None) —
    отдельное сообщение."""
    out = _capture(
        _make_report(
            stress_score=None,
            stress_r_dgemm=0.20,
            stress_r_stream=0.40,
            stress_r_stability=0.99,
            stress_r_thermal=None,
            stress_t_max_c=70.0,
            stress_tjmax_c=None,
            stress_duration_sec=180.0,
        ),
        monkeypatch,
    )
    assert "Оценка под нагрузкой недоступна" in out
    assert "TJmax" in out


def test_performance_block_combines_measured_and_roofline(monkeypatch):
    """Блок «Производительность» — голые GFLOPS/GB/s + Roofline-контекст
    в одной строке на нагрузку. Отдельной таблицы «Результаты нагрузки»
    нет (убрана по запросу пользователя).
    """
    parallel = SimpleNamespace(
        results=[
            SimpleNamespace(
                throughput=84.58, throughput_unit="GFLOPS",
                error_count=0, duration_actual_sec=30.0, engine="dgemm",
            ),
            SimpleNamespace(
                throughput=13.88, throughput_unit="GB/s",
                error_count=0, duration_actual_sec=31.0, engine="stream",
            ),
        ],
        cancelled=False,
    )
    report = _make_report(
        stress_r_dgemm=0.103,
        stress_r_stream=0.136,
        roofline_dgemm_peak_gflops=819.0,
        roofline_stream_peak_gb_s=102.4,
        roofline_simd_level="avx2",
        roofline_clock_ghz=3.2,
        roofline_dram_mts=6400.0,
        roofline_dram_modules=2,
    )
    report.parallel = parallel
    out = _capture(report, monkeypatch)
    # Голые числа результатов есть.
    assert "84.58" in out
    assert "GFLOPS" in out
    assert "13.88" in out
    assert "GB/s" in out
    # Roofline-контекст в той же строке (% от пика).
    assert "10.3" in out  # DGEMM %
    assert "13.6" in out  # STREAM %
    assert "AVX2" in out
    assert "6400" in out
    # Отдельной таблицы-обёртки «Результаты нагрузки» больше нет.
    assert "Результаты нагрузки" not in out


def test_performance_block_hidden_when_no_throughput(monkeypatch):
    out = _capture(_make_report(), monkeypatch)
    # Без results нет блока «Производительность».
    assert "Производительность" not in out


# ─── Дополнение A: sparkline-тренды в финальной Сводке ─────────────────────


def test_summary_does_not_have_trend_column(monkeypatch):
    """Регрессия: колонка «Тренд» убрана из финала по запросу пользователя
    (тренд он уже видел в Live)."""
    out = _capture(_make_report(), monkeypatch)
    assert "Тренд" not in out


def test_summary_has_power_column_with_value(monkeypatch):
    """Колонка «Потребление, Вт ср/пик» показывает значения из cpu_*_power_w.

    Значения форматируются без дробной части и без единицы (единица — в
    заголовке) — это компактнее, чтобы таблица не вылезала за 160 столбцов.
    """
    out = _capture(
        _make_report(cpu_avg_power_w=85.4, cpu_peak_power_w=176.2),
        monkeypatch,
    )
    # Лейбл колонки: пользователь хочет «Потребление», единица в заголовке.
    assert "Потребление" in out
    assert "Вт" in out
    # Значения округлены до целых: 85, 176.
    assert "85/176" in out


def test_summary_power_column_dash_when_no_data(monkeypatch):
    out = _capture(_make_report(), monkeypatch)
    # Колонка «Потребление, Вт» есть даже без данных, значение — прочерк.
    assert "Потребление" in out


def test_summary_voltage_column_renamed(monkeypatch):
    """Колонка переименована с «Vcore» на «Напряжение»."""
    out = _capture(
        _make_report(),
        monkeypatch,
    )
    assert "Напряжение" in out
    # Старый англицизм «Vcore» больше не должен фигурировать в заголовке.
    assert "Vcore" not in out


def test_summary_no_trend_column_in_final(monkeypatch):
    """Колонка «Тренд» убрана из финальной Сводки.

    Тренды пользователь уже видит в Live во время прогона; в финале они
    избыточны — пользователь это явно запросил.
    """
    history = [
        MetricSnapshot(
            timestamp=datetime.now(timezone.utc),
            cpu_percent=50.0 + i * 5,
            ram_percent=30.0 + i,
            temperatures={"cpu/package": 60.0 + i * 2},
        )
        for i in range(5)
    ]
    out = _capture(_make_report(metrics_history=history), monkeypatch)
    # Заголовок колонки «Тренд» больше не выводится в финале.
    assert "Тренд" not in out
