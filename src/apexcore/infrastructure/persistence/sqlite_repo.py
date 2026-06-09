"""SQLite-репозитории для прогонов и базовых профилей.

Подход — «JSON в колонке + индексы»: полный ``BenchmarkResult`` сериализуется
в JSON и хранится в `runs.payload_json`, а ключевые поля дублируются в
индексные колонки, чтобы не парсить JSON для list/filter.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from apexcore.domain.errors import RepositoryError
from apexcore.domain.models import BaselineProfile, BenchmarkResult, MicroBenchSuiteResult
from apexcore.domain.ports import BaselineRepository, MicroRunRepository, ResultRepository
from apexcore.infrastructure.persistence.migrations import apply_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    apply_schema(conn)
    return conn


class SqliteResultRepository(ResultRepository):
    """Репозиторий ``BenchmarkResult`` поверх SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, result: BenchmarkResult) -> None:
        payload = result.model_dump_json()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO runs
                        (id, profile_name, start_time, end_time, final_score, status,
                         cpu_model, os_name, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(result.id),
                        result.config.profile_name,
                        result.start_time.isoformat(),
                        result.end_time.isoformat(),
                        result.final_score,
                        result.status,
                        result.system_info.cpu_model,
                        result.system_info.os_name,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(f"Не удалось сохранить прогон: {exc}") from exc

    def get(self, run_id: UUID | str) -> BenchmarkResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            return None
        return BenchmarkResult.model_validate_json(row["payload_json"])

    def list_runs(
        self, limit: int = 50, profile_name: str | None = None
    ) -> list[BenchmarkResult]:
        sql = "SELECT payload_json FROM runs"
        params: tuple = ()
        if profile_name:
            sql += " WHERE profile_name = ?"
            params = (profile_name,)
        sql += " ORDER BY start_time DESC LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [BenchmarkResult.model_validate_json(r["payload_json"]) for r in rows]

    def delete(self, run_id: UUID) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM runs WHERE id = ?", (str(run_id),))
        return cur.rowcount > 0

    def resolve_id(self, prefix: str) -> str | None:
        """Найти полный UUID по префиксу.

        Удобно для CLI: пользователь вводит первые 8 символов.
        """
        clean = prefix.strip().rstrip("…").rstrip(".")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM runs WHERE id = ? LIMIT 1", (clean,)
            ).fetchone()
            if row is not None:
                return str(row["id"])
            row = self._conn.execute(
                "SELECT id FROM runs WHERE id LIKE ? ORDER BY start_time DESC LIMIT 2",
                (clean + "%",),
            ).fetchall()
        if not row:
            return None
        if len(row) > 1:
            raise RepositoryError(
                f"Префикс '{prefix}' соответствует более чем одному прогону, уточните"
            )
        return str(row[0]["id"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteBaselineRepository(BaselineRepository):
    """Репозиторий BaselineProfile поверх SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, baseline: BaselineProfile) -> None:
        payload = baseline.model_dump_json()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO baselines
                        (id, name, profile_name, system_fingerprint, sample_size,
                         created_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(baseline.id),
                        baseline.name,
                        baseline.profile_name,
                        baseline.system_fingerprint,
                        baseline.sample_size,
                        baseline.created_at.isoformat(),
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(f"Не удалось сохранить baseline: {exc}") from exc

    def get(self, baseline_id: UUID) -> BaselineProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM baselines WHERE id = ?", (str(baseline_id),)
            ).fetchone()
        return None if row is None else BaselineProfile.model_validate_json(row["payload_json"])

    def find_by_name(self, name: str) -> BaselineProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM baselines WHERE name = ?", (name,)
            ).fetchone()
        return None if row is None else BaselineProfile.model_validate_json(row["payload_json"])

    def list_baselines(self) -> Iterable[BaselineProfile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM baselines ORDER BY created_at DESC"
            ).fetchall()
        return [BaselineProfile.model_validate_json(r["payload_json"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteMicroRunRepository(MicroRunRepository):
    """Репозиторий ``MicroBenchSuiteResult`` (scoring v2) поверх SQLite.

    Главное хранилище для общей оценки производительности. Полный
    ``MicroBenchSuiteResult`` (включая ``overall``) сериализуется в JSON;
    индексные поля (``overall_score``, ``preset``, ``n_runs``,
    ``scoring_version``, ``cpu_model``, ``os_name``) дублируются как
    столбцы для быстрого list/filter без парсинга JSON.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, suite: MicroBenchSuiteResult) -> None:
        payload = suite.model_dump_json()
        overall = suite.overall
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO micro_runs
                        (id, start_time, end_time, preset, n_runs,
                         overall_score, ci_lower, ci_upper,
                         cpu_model, os_name, scoring_version, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(suite.id),
                        suite.start_time.isoformat(),
                        suite.end_time.isoformat(),
                        suite.preset,
                        suite.n_runs,
                        overall.overall_score if overall else None,
                        overall.ci_lower if overall else None,
                        overall.ci_upper if overall else None,
                        suite.system_info.cpu_model,
                        suite.system_info.os_name,
                        overall.scoring_version if overall else None,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(f"Не удалось сохранить micro-прогон: {exc}") from exc

    def get(self, run_id: UUID | str) -> MicroBenchSuiteResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM micro_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            return None
        return MicroBenchSuiteResult.model_validate_json(row["payload_json"])

    def list_runs(
        self, limit: int = 50, preset: str | None = None
    ) -> list[MicroBenchSuiteResult]:
        sql = "SELECT payload_json FROM micro_runs"
        params: tuple = ()
        if preset:
            sql += " WHERE preset = ?"
            params = (preset,)
        sql += " ORDER BY start_time DESC LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [MicroBenchSuiteResult.model_validate_json(r["payload_json"]) for r in rows]

    def delete(self, run_id: UUID) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM micro_runs WHERE id = ?", (str(run_id),))
        return cur.rowcount > 0

    def resolve_id(self, prefix: str) -> str | None:
        """Найти полный UUID по префиксу (по образцу SqliteResultRepository)."""
        clean = prefix.strip().rstrip("…").rstrip(".")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM micro_runs WHERE id = ? LIMIT 1", (clean,)
            ).fetchone()
            if row is not None:
                return str(row["id"])
            rows = self._conn.execute(
                "SELECT id FROM micro_runs WHERE id LIKE ? ORDER BY start_time DESC LIMIT 2",
                (clean + "%",),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise RepositoryError(
                f"Префикс '{prefix}' соответствует более чем одному micro-прогону, уточните"
            )
        return str(rows[0]["id"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
