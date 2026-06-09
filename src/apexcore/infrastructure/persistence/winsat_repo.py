"""SQLite-репозиторий для прогонов Winsat-аналога.

Хранит ``WinsatReport`` целиком в JSON-колонке + индексные поля
(``winspr_level``, подскоры, started_at) для быстрых list/sort запросов.
Не пересекается с ``SqliteMicroRunRepository`` — это разные шкалы и разные
функциональные режимы (см. docs/winsat.md).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from uuid import UUID

from apexcore.domain.errors import RepositoryError
from apexcore.domain.ports import WinsatRepository
from apexcore.domain.winsat import WinsatReport
from apexcore.infrastructure.persistence.migrations import apply_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    apply_schema(conn)
    return conn


class SqliteWinsatRepository(WinsatRepository):
    """Репозиторий ``WinsatReport`` поверх SQLite (схема v3+)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, report: WinsatReport) -> None:
        payload = report.model_dump_json()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO winsat_runs
                        (id, started_at, ended_at,
                         cpu_score, memory_score, disk_score,
                         graphics_score, d3d_score, winspr_level,
                         cpu_model, os_name, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(report.id),
                        report.started_at.isoformat(),
                        report.ended_at.isoformat(),
                        report.cpu_score.score,
                        report.memory_score.score,
                        report.disk_score.score,
                        report.graphics_score.score,
                        report.d3d_score.score,
                        report.winspr_level,
                        report.system_info.cpu_model,
                        report.system_info.os_name,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(f"Не удалось сохранить winsat-прогон: {exc}") from exc

    def get(self, run_id: UUID | str) -> WinsatReport | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM winsat_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            return None
        return WinsatReport.model_validate_json(row["payload_json"])

    def list_runs(self, limit: int = 50) -> list[WinsatReport]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM winsat_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [WinsatReport.model_validate_json(r["payload_json"]) for r in rows]

    def delete(self, run_id: UUID) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM winsat_runs WHERE id = ?", (str(run_id),)
            )
        return cur.rowcount > 0

    def resolve_id(self, prefix: str) -> str | None:
        """Найти полный UUID winsat-прогона по префиксу (для CLI ``winsat query``)."""
        clean = prefix.strip().rstrip("…").rstrip(".")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM winsat_runs WHERE id = ? LIMIT 1", (clean,)
            ).fetchone()
            if row is not None:
                return str(row["id"])
            rows = self._conn.execute(
                "SELECT id FROM winsat_runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 2",
                (clean + "%",),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise RepositoryError(
                f"Префикс '{prefix}' соответствует более чем одному winsat-прогону, уточните"
            )
        return str(rows[0]["id"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SqliteWinsatRepository"]
