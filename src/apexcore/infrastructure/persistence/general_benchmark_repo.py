"""SQLite-репозиторий для прогонов «Оценок общей производительности».

Хранит ``GeneralBenchmarkReport`` целиком в JSON-колонке + индексные поля
(``score``, ``started_at``) для быстрых list/sort запросов. По образцу
:class:`SqliteWinsatRepository`.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from uuid import UUID

from apexcore.domain.errors import RepositoryError
from apexcore.domain.general_benchmark import GeneralBenchmarkReport
from apexcore.domain.ports import GeneralBenchmarkRepository
from apexcore.infrastructure.persistence.migrations import apply_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    apply_schema(conn)
    return conn


class SqliteGeneralBenchmarkRepository(GeneralBenchmarkRepository):
    """Репозиторий ``GeneralBenchmarkReport`` поверх SQLite (схема v4+)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, report: GeneralBenchmarkReport) -> None:
        payload = report.model_dump_json()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO general_benchmark_runs
                        (id, started_at, ended_at, score,
                         dgemm_gflops, stream_gb_s,
                         disk_seq_read_mb_s, disk_random_read_mb_s, disk_seq_write_mb_s,
                         disk_media_label,
                         cpu_model, os_name, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(report.id),
                        report.started_at.isoformat(),
                        report.ended_at.isoformat(),
                        report.score,
                        report.dgemm_gflops,
                        report.stream_gb_s,
                        report.disk_seq_read_mb_s,
                        report.disk_random_read_mb_s,
                        report.disk_seq_write_mb_s,
                        report.disk_media_label,
                        report.system_info.cpu_model,
                        report.system_info.os_name,
                        payload,
                    ),
                )
        except sqlite3.Error as exc:
            raise RepositoryError(
                f"Не удалось сохранить general_benchmark-прогон: {exc}"
            ) from exc

    def get(self, run_id: UUID | str) -> GeneralBenchmarkReport | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM general_benchmark_runs WHERE id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        return GeneralBenchmarkReport.model_validate_json(row["payload_json"])

    def list_runs(self, limit: int = 50) -> list[GeneralBenchmarkReport]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM general_benchmark_runs "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            GeneralBenchmarkReport.model_validate_json(r["payload_json"]) for r in rows
        ]

    def delete(self, run_id: UUID) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM general_benchmark_runs WHERE id = ?", (str(run_id),)
            )
        return cur.rowcount > 0

    def resolve_id(self, prefix: str) -> str | None:
        """Найти полный UUID прогона по префиксу.

        Поведение совпадает с :meth:`SqliteWinsatRepository.resolve_id`:
        точное совпадение — приоритет, иначе LIKE по префиксу. Если
        префикс соответствует двум и более прогонам — поднимает
        :class:`RepositoryError`.
        """
        clean = prefix.strip().rstrip("…").rstrip(".")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM general_benchmark_runs WHERE id = ? LIMIT 1",
                (clean,),
            ).fetchone()
            if row is not None:
                return str(row["id"])
            rows = self._conn.execute(
                "SELECT id FROM general_benchmark_runs WHERE id LIKE ? "
                "ORDER BY started_at DESC LIMIT 2",
                (clean + "%",),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise RepositoryError(
                f"Префикс '{prefix}' соответствует более чем одному прогону "
                "общей оценки, уточните"
            )
        return str(rows[0]["id"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SqliteGeneralBenchmarkRepository"]
