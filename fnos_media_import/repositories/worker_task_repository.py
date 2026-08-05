from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class WorkerTaskRepository:
    """Durable queue with atomic claim, lease expiry and bounded retries."""

    def __init__(self, connection_factory: ConnectionFactory, row_to_dict: Callable[[Any], dict[str, Any]]) -> None:
        self._connection_factory = connection_factory
        self._row_to_dict = row_to_dict

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        max_attempts: int = 3,
        config_revision: int = 1,
        reactivate_terminal: bool = False,
    ) -> tuple[int, bool]:
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._enqueue_on_connection(
                connection,
                task_type,
                payload,
                idempotency_key,
                max_attempts=max_attempts,
                config_revision=config_revision,
                reactivate_terminal=reactivate_terminal,
            )

    def enqueue_with_connection(
        self,
        connection: sqlite3.Connection,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        max_attempts: int = 3,
        config_revision: int = 1,
        reactivate_terminal: bool = False,
    ) -> tuple[int, bool]:
        """Enqueue a durable task inside a caller-owned transaction.

        The caller must already hold the SQLite write lock (``BEGIN IMMEDIATE``)
        and commit afterwards.  This is what lets a notification task be written
        atomically with the business state change it reports on; a bare
        ``enqueue`` would open a second connection and lose atomicity.
        """
        return self._enqueue_on_connection(
            connection,
            task_type,
            payload,
            idempotency_key,
            max_attempts=max_attempts,
            config_revision=config_revision,
            reactivate_terminal=reactivate_terminal,
        )

    def _enqueue_on_connection(
        self,
        connection: sqlite3.Connection,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        max_attempts: int = 3,
        config_revision: int = 1,
        reactivate_terminal: bool = False,
    ) -> tuple[int, bool]:
        now = _utc_now()
        normalized_max_attempts = max(1, int(max_attempts))
        normalized_config_revision = max(1, int(config_revision))
        encoded_payload = json.dumps(payload, ensure_ascii=False)
        existing = connection.execute(
            """
            SELECT id, task_type, payload, status, max_attempts, config_revision
            FROM worker_tasks
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if existing:
            task_id = int(existing["id"])
            status = str(existing["status"] or "")
            if status == "pending":
                try:
                    existing_payload = json.loads(existing["payload"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_payload = {}
                unchanged = (
                    str(existing["task_type"] or "") == task_type
                    and existing_payload == payload
                    and int(existing["max_attempts"] or 0) == normalized_max_attempts
                    and int(existing["config_revision"] or 0) == normalized_config_revision
                )
                if unchanged:
                    return task_id, False
                cursor = connection.execute(
                    """
                    UPDATE worker_tasks
                    SET task_type=?, payload=?, attempts=0, max_attempts=?,
                        config_revision=?, owner_id=NULL, lease_expires_at=NULL,
                        available_at=?, result=NULL, error_message='', updated_at=?,
                        started_at=NULL, completed_at=NULL
                    WHERE id=? AND status='pending'
                    """,
                    (
                        task_type,
                        encoded_payload,
                        normalized_max_attempts,
                        normalized_config_revision,
                        now,
                        now,
                        task_id,
                    ),
                )
                return task_id, cursor.rowcount == 1
            if reactivate_terminal and status in {"completed", "failed"}:
                cursor = connection.execute(
                    """
                    UPDATE worker_tasks
                    SET task_type=?, payload=?, status='pending', attempts=0,
                        max_attempts=?, config_revision=?, owner_id=NULL,
                        lease_expires_at=NULL, available_at=?, result=NULL,
                        error_message='', updated_at=?, started_at=NULL,
                        completed_at=NULL
                    WHERE id=? AND status IN ('completed', 'failed')
                    """,
                    (
                        task_type,
                        encoded_payload,
                        normalized_max_attempts,
                        normalized_config_revision,
                        now,
                        now,
                        task_id,
                    ),
                )
                return task_id, cursor.rowcount == 1
            return task_id, False
        cursor = connection.execute(
            """
            INSERT INTO worker_tasks
            (task_type, payload, status, idempotency_key, attempts, max_attempts,
             config_revision, created_at, updated_at, available_at)
            VALUES (?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                task_type,
                encoded_payload,
                idempotency_key,
                normalized_max_attempts,
                normalized_config_revision,
                now,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid), True

    def claim(self, owner_id: str, *, lease_seconds: int = 120, task_types: list[str] | None = None) -> dict[str, Any] | None:
        now = _utc_now()
        lease_until = _utc_after(lease_seconds)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_exhausted_leases(connection, now)
            filters = [
                "attempts < max_attempts",
                "available_at <= ?",
                "(status = 'pending' OR (status = 'running' AND lease_expires_at <= ?))",
            ]
            values: list[Any] = [now, now]
            if task_types:
                filters.append(f"task_type IN ({','.join('?' for _ in task_types)})")
                values.extend(task_types)
            row = connection.execute(
                f"SELECT id FROM worker_tasks WHERE {' AND '.join(filters)} ORDER BY available_at, id LIMIT 1",
                values,
            ).fetchone()
            if not row:
                return None
            task_id = int(row["id"])
            updated = connection.execute(
                """
                UPDATE worker_tasks
                SET status='running', owner_id=?, lease_expires_at=?, attempts=attempts+1,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=? AND (status='pending' OR lease_expires_at <= ?)
                """,
                (owner_id, lease_until, now, now, task_id, now),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute("SELECT * FROM worker_tasks WHERE id=?", (task_id,)).fetchone()
            return self._decode(claimed)

    def complete(self, task_id: int, owner_id: str, result: dict[str, Any] | None = None) -> bool:
        now = _utc_now()
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_tasks SET status='completed', result=?, error_message='',
                    lease_expires_at=NULL, completed_at=?, updated_at=?
                WHERE id=? AND status='running' AND owner_id=?
                """,
                (json.dumps(result or {}, ensure_ascii=False), now, now, task_id, owner_id),
            )
            return cursor.rowcount == 1

    def defer(
        self,
        task_id: int,
        owner_id: str,
        *,
        delay_seconds: int = 30,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Release a claimed task back to pending without consuming an attempt."""

        now = _utc_now()
        available = _utc_after(max(1, int(delay_seconds)))
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_tasks
                SET status='pending', attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    result=?, error_message='', owner_id=NULL, lease_expires_at=NULL,
                    available_at=?, completed_at=NULL, updated_at=?
                WHERE id=? AND status='running' AND owner_id=?
                """,
                (
                    json.dumps(result or {}, ensure_ascii=False),
                    available,
                    now,
                    task_id,
                    owner_id,
                ),
            )
            return cursor.rowcount == 1

    def renew(self, task_id: int, owner_id: str, *, lease_seconds: int = 120) -> bool:
        now = _utc_now()
        lease_until = _utc_after(lease_seconds)
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_tasks
                SET lease_expires_at=?, updated_at=?
                WHERE id=? AND status='running' AND owner_id=?
                """,
                (lease_until, now, task_id, owner_id),
            )
            return cursor.rowcount == 1

    def fail(
        self,
        task_id: int,
        owner_id: str,
        error: str,
        *,
        retry_delay_seconds: int = 30,
        terminal: bool = False,
        result: dict[str, Any] | None = None,
    ) -> bool:
        now = _utc_now()
        available = _utc_after(retry_delay_seconds)
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_tasks
                SET status=CASE WHEN ? OR attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                    result=?, error_message=?, owner_id=NULL, lease_expires_at=NULL,
                    available_at=CASE WHEN ? OR attempts >= max_attempts THEN available_at ELSE ? END,
                    completed_at=CASE WHEN ? OR attempts >= max_attempts THEN ? ELSE NULL END,
                    updated_at=?
                WHERE id=? AND status='running' AND owner_id=?
                """,
                (
                    bool(terminal),
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    bool(terminal),
                    available,
                    bool(terminal),
                    now,
                    now,
                    task_id,
                    owner_id,
                ),
            )
            return cursor.rowcount == 1

    def cancel_related(
        self,
        *,
        job_id: int = 0,
        organizer_task_ids: list[int] | tuple[int, ...] | None = None,
        guest_request_id: int = 0,
        reason: str = "关联业务任务已取消",
    ) -> dict[str, Any]:
        """Fence Worker tasks owned by one request or job.

        A running public-import task must keep its lease when the owning guest
        request is cancelled.  The coordinator may still need to compensate a
        formal job created concurrently and return a retryable result.  Pending
        public-import tasks are safe to cancel terminally because they have not
        started provider-side work yet.
        """

        normalized_job_id = max(0, int(job_id or 0))
        normalized_request_id = max(0, int(guest_request_id or 0))
        organizer_ids = {
            int(value)
            for value in (organizer_task_ids or [])
            if str(value or "").isdigit() and int(value) > 0
        }
        if not normalized_job_id and not normalized_request_id and not organizer_ids:
            return {
                "success": True,
                "cancelled_count": 0,
                "task_ids": [],
                "in_flight_count": 0,
                "in_flight_task_ids": [],
            }

        now = _utc_now()
        matched: list[int] = []
        in_flight: list[int] = []
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id, task_type, payload, status
                FROM worker_tasks
                WHERE status IN ('pending', 'running')
                ORDER BY id ASC
                """
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                if (
                    (normalized_job_id and _positive_int(payload.get("job_id")) == normalized_job_id)
                    or (
                        normalized_request_id
                        and _positive_int(payload.get("guest_request_id")) == normalized_request_id
                    )
                    or (organizer_ids and _positive_int(payload.get("task_id")) in organizer_ids)
                ):
                    task_id = int(row["id"])
                    if (
                        normalized_request_id
                        and str(row["task_type"] or "") == "public_import_create"
                        and str(row["status"] or "") == "running"
                    ):
                        in_flight.append(task_id)
                    else:
                        matched.append(task_id)
            if matched:
                placeholders = ",".join("?" for _ in matched)
                result = json.dumps(
                    {
                        "worker_outcome": "business_failed",
                        "cancelled": True,
                        "terminal": True,
                        "message": str(reason or "关联业务任务已取消"),
                    },
                    ensure_ascii=False,
                )
                cursor = connection.execute(
                    f"""
                    UPDATE worker_tasks
                    SET status='failed', result=?, error_message=?, owner_id=NULL,
                        lease_expires_at=NULL, completed_at=?, updated_at=?
                    WHERE id IN ({placeholders}) AND status IN ('pending', 'running')
                    """,
                    [result, str(reason or "关联业务任务已取消"), now, now, *matched],
                )
                cancelled_count = int(cursor.rowcount)
            else:
                cancelled_count = 0
        return {
            "success": True,
            "cancelled_count": cancelled_count,
            "task_ids": matched,
            "in_flight_count": len(in_flight),
            "in_flight_task_ids": in_flight,
            "job_id": normalized_job_id,
            "guest_request_id": normalized_request_id,
            "organizer_task_ids": sorted(organizer_ids),
        }

    def get(self, task_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            return self._decode(connection.execute("SELECT * FROM worker_tasks WHERE id=?", (task_id,)).fetchone())

    def list_related(
        self,
        *,
        job_id: int = 0,
        guest_request_ids: list[int] | None = None,
        organizer_task_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_job_id = max(0, int(job_id or 0))
        request_ids = {_positive_int(value) for value in (guest_request_ids or [])}
        organizer_ids = {_positive_int(value) for value in (organizer_task_ids or [])}
        request_ids.discard(0)
        organizer_ids.discard(0)
        if not normalized_job_id and not request_ids and not organizer_ids:
            return []
        with self._connection_factory() as connection:
            rows = connection.execute("SELECT * FROM worker_tasks ORDER BY id ASC").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode(row)
            payload = item.get("payload") if item else None
            if not isinstance(payload, dict):
                continue
            if (
                (normalized_job_id and _positive_int(payload.get("job_id")) == normalized_job_id)
                or _positive_int(payload.get("guest_request_id")) in request_ids
                or _positive_int(payload.get("task_id")) in organizer_ids
            ):
                result.append(item)
        return result

    def list(self, *, status: str | None = None, task_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        if status:
            filters.append("status=?")
            values.append(status)
        if task_type:
            filters.append("task_type=?")
            values.append(task_type)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(max(1, min(500, int(limit))))
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM worker_tasks {where} ORDER BY id DESC LIMIT ?", values
            ).fetchall()
            return [self._decode(row) for row in rows]

    def status(self) -> dict[str, Any]:
        now = _utc_now()
        with self._connection_factory() as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM worker_tasks GROUP BY status"
                ).fetchall()
            }
            expired = int(connection.execute(
                "SELECT COUNT(*) FROM worker_tasks WHERE status='running' AND lease_expires_at <= ?",
                (now,),
            ).fetchone()[0])
            next_task = connection.execute(
                "SELECT id, task_type, available_at FROM worker_tasks WHERE status='pending' ORDER BY available_at, id LIMIT 1"
            ).fetchone()
            oldest_pending = connection.execute(
                "SELECT id, task_type, created_at, available_at FROM worker_tasks WHERE status='pending' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return {
            "counts": counts,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "expired_leases": expired,
            "next_task": self._row_to_dict(next_task) if next_task else None,
            "oldest_pending": self._row_to_dict(oldest_pending) if oldest_pending else None,
        }

    @staticmethod
    def _expire_exhausted_leases(connection: sqlite3.Connection, now: str) -> int:
        cursor = connection.execute(
            """
            UPDATE worker_tasks
            SET status='failed', owner_id=NULL, lease_expires_at=NULL,
                error_message=CASE
                    WHEN TRIM(COALESCE(error_message, '')) = ''
                    THEN 'Worker lease expired after the final attempt'
                    ELSE error_message || '; Worker lease expired after the final attempt'
                END,
                completed_at=?, updated_at=?
            WHERE status='running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
              AND attempts >= max_attempts
            """,
            (now, now, now),
        )
        return int(cursor.rowcount)

    def prune_terminal(self, *, retention_days: int = 7, limit: int = 500) -> int:
        cutoff = (
            datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=max(1, int(retention_days)))
        ).isoformat().replace("+00:00", "Z")
        batch = max(1, min(5000, int(limit)))
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                DELETE FROM worker_tasks
                WHERE id IN (
                    SELECT id FROM worker_tasks
                    WHERE status IN ('completed', 'failed')
                      AND completed_at IS NOT NULL
                      AND completed_at < ?
                    ORDER BY completed_at, id
                    LIMIT ?
                )
                """,
                (cutoff, batch),
            )
            return int(cursor.rowcount)

    def _decode(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = self._row_to_dict(row)
        for key in ("payload", "result"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item[key] = {}
        return item


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=max(0, int(seconds)))
    ).isoformat().replace("+00:00", "Z")


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
