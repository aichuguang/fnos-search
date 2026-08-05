from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]

DELIVERY_SUCCESS = "success"
DELIVERY_RETRYABLE = "retryable"
DELIVERY_FAILED = "failed"


class NotificationDeliveryRepository:
    """Append-only audit log for outbound notification deliveries."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def record(
        self,
        *,
        task_id: int | None,
        event_type: str,
        channel: str,
        status: str,
        attempts: int = 1,
        recipient: str = "",
        status_code: int | None = None,
        response_summary: str = "",
        error_message: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> int:
        now = utc_now_iso()

        def _insert(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO notification_deliveries
                (task_id, event_type, channel, recipient, status, attempts,
                 status_code, response_summary, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(task_id) if task_id is not None else None,
                    str(event_type),
                    str(channel),
                    str(recipient or ""),
                    str(status),
                    max(1, int(attempts)),
                    int(status_code) if status_code is not None else None,
                    str(response_summary or "")[:500],
                    str(error_message or "")[:2000],
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

        if connection is not None:
            return _insert(connection)
        with self._connection_factory() as connection:
            return _insert(connection)

    def latest_status_by_channel(self, task_id: int) -> dict[str, str]:
        """Latest delivery status per channel for one worker task."""
        with self._connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT channel, status
                FROM notification_deliveries
                WHERE task_id = ?
                  AND id IN (
                      SELECT MAX(id) FROM notification_deliveries
                      WHERE task_id = ? GROUP BY channel
                  )
                """,
                (int(task_id), int(task_id)),
            ).fetchall()
        return {str(row["channel"]): str(row["status"]) for row in rows}

    def list_deliveries(
        self,
        *,
        event_type: str | None = None,
        channel: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        if event_type:
            filters.append("event_type = ?")
            values.append(str(event_type))
        if channel:
            filters.append("channel = ?")
            values.append(str(channel))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.extend([max(1, min(500, int(limit))), max(0, min(500, int(offset)))])
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notification_deliveries
                {where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._connection_factory() as connection:
            latest_rows = """
                WITH latest AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY CASE
                            WHEN task_id IS NULL THEN 'row:' || id
                            ELSE 'task:' || task_id
                        END, channel
                        ORDER BY id DESC
                    ) AS row_number
                    FROM notification_deliveries
                )
            """
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    latest_rows + "SELECT status, COUNT(*) AS count FROM latest WHERE row_number=1 GROUP BY status"
                ).fetchall()
            }
            last_success_row = connection.execute(
                "SELECT updated_at FROM notification_deliveries WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_failed_row = connection.execute(
                "SELECT updated_at FROM notification_deliveries WHERE status IN ('retryable','failed') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "success": counts.get(DELIVERY_SUCCESS, 0),
            "retryable": counts.get(DELIVERY_RETRYABLE, 0),
            "failed": counts.get(DELIVERY_FAILED, 0),
            "total": sum(counts.values()),
            "last_success_at": last_success_row["updated_at"] if last_success_row else None,
            "last_failure_at": last_failed_row["updated_at"] if last_failed_row else None,
        }

    def prune(self, *, before: str) -> int:
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "DELETE FROM notification_deliveries WHERE created_at < ?",
                (str(before),),
            )
            return max(0, int(cursor.rowcount or 0))

    def last_delivery_for_event(self, event_type: str, after: str) -> dict[str, Any] | None:
        """Most recent delivery row for an event after a cutoff (used for cooldowns)."""
        with self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM notification_deliveries
                WHERE event_type = ? AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(event_type), str(after)),
            ).fetchone()
        return dict(row) if row else None
