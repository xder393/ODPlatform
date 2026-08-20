"""Durable SQLite task-record repository for the Docker-first API runtime."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import UUID

from odp_api.modules.tasks.models import TaskRecord


class SQLiteTaskRepository:
    """Persists state transitions and atomically deduplicates idempotency keys."""

    def __init__(self, database_path: Path | str) -> None:
        self._connection = sqlite3.connect(
            database_path, check_same_thread=False, isolation_level=None, timeout=5
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_records (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    frame_status TEXT
                )
                """
            )

    def get(self, task_id: UUID) -> TaskRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM task_records WHERE task_id = ?", (str(task_id),)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM task_records WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def save(self, task: TaskRecord) -> TaskRecord:
        """Insert once by key, or update only the same persisted task record."""
        values = _task_values(task)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM task_records WHERE idempotency_key = ?", (task.idempotency_key,)
                ).fetchone()
                if existing is not None and existing["task_id"] != str(task.task_id):
                    self._connection.execute("COMMIT")
                    return _task_from_row(existing)
                self._connection.execute(
                    """
                    INSERT INTO task_records (
                        task_id, task_type, idempotency_key, payload, status, attempt_count,
                        created_at, next_attempt_at, last_error, frame_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        task_type = excluded.task_type,
                        payload = excluded.payload,
                        status = excluded.status,
                        attempt_count = excluded.attempt_count,
                        created_at = excluded.created_at,
                        next_attempt_at = excluded.next_attempt_at,
                        last_error = excluded.last_error,
                        frame_status = excluded.frame_status
                    """,
                    values,
                )
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
        return task

    def active(self) -> Sequence[TaskRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_records WHERE status IN ('PENDING', 'RETRYING')"
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _task_values(task: TaskRecord) -> tuple[object, ...]:
    values = asdict(task)
    return (
        str(values["task_id"]),
        values["task_type"],
        values["idempotency_key"],
        json.dumps(values["payload"], default=_json_default, sort_keys=True),
        values["status"],
        values["attempt_count"],
        values["created_at"].isoformat(),
        values["next_attempt_at"].isoformat() if values["next_attempt_at"] else None,
        values["last_error"],
        values["frame_status"],
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=UUID(row["task_id"]),
        task_type=row["task_type"],
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        attempt_count=row["attempt_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        next_attempt_at=(
            datetime.fromisoformat(row["next_attempt_at"])
            if row["next_attempt_at"] is not None
            else None
        ),
        last_error=row["last_error"],
        frame_status=row["frame_status"],
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
