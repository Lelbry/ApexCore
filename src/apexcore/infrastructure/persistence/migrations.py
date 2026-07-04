"""Миграции схемы SQLite.

Версионирование:
- v1 — оригинальная схема (только runs + baselines с composite_score v1).
- v2 — scoring v2: добавлена таблица micro_runs; runs/baselines от v1
  ДРОПАЮТСЯ при миграции с v1 → v2 (см. docs/scoring_v2.md §9 — старые
  баллы концептуально несовместимы с новой шкалой 1000·Roofline-ratio).
- v3 — Winsat-аналог: добавлена таблица winsat_runs (шкала 1.0–9.9).
  Существующие runs/baselines/micro_runs НЕ дропаются (winsat — независимый
  режим, см. docs/winsat.md).
- v4 — «Оценки общей производительности»: добавлена таблица
  general_benchmark_runs (шкала ×10 000). Additive — ничего не дропается.
- v5 — GPU-бенчмарк: добавлена таблица gpu_benchmark_runs (Roofline для
  GPU, шкала ×10 000). Additive — ничего не дропается (см. docs/gpu_benchmark.md).
- v6 — GPU-стресс-тест: добавлена таблица gpu_stress_runs (термостабильность,
  headline = вердикт PASS/WARN/FAIL/UNKNOWN, без балла). Additive — ничего
  не дропается (см. docs/gpu_benchmark.md).
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

CURRENT_VERSION = 6


def apply_schema(conn: sqlite3.Connection) -> None:
    """Применить миграции до CURRENT_VERSION.

    Алгоритм:
    1. Прочитать schema_version (если таблицы ещё нет — версия = 0).
    2. Если version < 2 — выполнить v2 миграцию (drop старых таблиц с v1
       данными + recreate из schema.sql).
    3. v2 → v3 миграция — additive: только добавить новые таблицы из
       schema.sql, ничего не дропать.
    4. Записать новую версию.
    """
    # Читаем текущую версию (если таблица schema_version уже существует).
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        user_version = row[0] if row else 0
    except sqlite3.OperationalError:
        user_version = 0

    # v2 миграция: дропаем старые runs/baselines (там старый composite_score
    # v1, который концептуально несовместим с v2).
    if 0 < user_version < 2:
        conn.execute("DROP TABLE IF EXISTS runs")
        conn.execute("DROP TABLE IF EXISTS baselines")
        # Индексы дропнутся вместе с таблицами автоматически.
        conn.commit()

    # v3 миграция: добавление winsat_runs происходит автоматически через
    # CREATE TABLE IF NOT EXISTS в schema.sql — никакого drop не нужно.

    # v4 миграция: добавление general_benchmark_runs — тоже additive,
    # CREATE IF NOT EXISTS из schema.sql достаточно.

    # v5 миграция: добавление gpu_benchmark_runs — тоже additive,
    # CREATE IF NOT EXISTS из schema.sql достаточно (существующие v4-БД
    # получают таблицу, ничего не дропается).

    # v6 миграция: добавление gpu_stress_runs — тоже additive,
    # CREATE IF NOT EXISTS из schema.sql достаточно (существующие v5-БД
    # получают таблицу, ничего не дропается).

    # Применить актуальную схему (CREATE IF NOT EXISTS — идемпотентно).
    sql = _read_schema()
    conn.executescript(sql)
    conn.execute(
        "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (CURRENT_VERSION,)
    )
    conn.commit()


def _read_schema() -> str:
    try:
        # При установленном пакете — берём ресурсный файл.
        return resources.files("apexcore.infrastructure.persistence").joinpath("schema.sql").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        # Fallback в режиме разработки, когда пакет ещё не установлен.
        path = Path(__file__).with_name("schema.sql")
        return path.read_text(encoding="utf-8")
