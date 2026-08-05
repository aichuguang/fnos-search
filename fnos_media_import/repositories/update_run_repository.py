from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso, utc_now_iso_offset


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
INTERRUPTED_MESSAGE = "追更运行因进程中断或租约过期而终止"


def _utc_now() -> str:
    return utc_now_iso()


def _utc_after(seconds: int) -> str:
    return utc_now_iso_offset(seconds=max(1, int(seconds)))


def _utc_before(seconds: int) -> str:
    return utc_now_iso_offset(seconds=-max(1, int(seconds)))


class UpdateRunRepository:
    """Coordinates durable update runs with per-subscription leases."""

    def __init__(self, connection_factory: ConnectionFactory, decode: Callable[[Any], dict[str, Any] | None]) -> None:
        self._connection_factory = connection_factory
        self._decode = decode

    def claim(
        self,
        subscription_id: int,
        trigger_type: str,
        *,
        scheduled_at: str,
        owner_id: str,
        lease_seconds: int,
        raw_data: Any = None,
    ) -> tuple[int | None, dict[str, Any] | None]:
        now = _utc_now()
        expires_at = _utc_after(lease_seconds)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_in_connection(
                connection,
                now=now,
                legacy_cutoff=_utc_before(lease_seconds),
                subscription_id=int(subscription_id),
            )
            active = connection.execute(
                """
                SELECT * FROM update_runs
                WHERE subscription_id = ? AND status = 'running' AND lease_expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(subscription_id), now),
            ).fetchone()
            if active is not None:
                return None, self._decode(active)
            cursor = connection.execute(
                """
                INSERT INTO update_runs
                (subscription_id, trigger_type, status, scheduled_at, started_at, summary, raw_data,
                 owner_id, heartbeat_at, lease_expires_at)
                VALUES (?, ?, 'running', ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    int(subscription_id),
                    str(trigger_type),
                    str(scheduled_at or ""),
                    now,
                    _json_text(raw_data),
                    str(owner_id),
                    now,
                    expires_at,
                ),
            )
            return int(cursor.lastrowid), None

    def renew(self, run_id: int, owner_id: str, *, lease_seconds: int) -> bool:
        now = _utc_now()
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE update_runs
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'running' AND owner_id = ? AND lease_expires_at > ?
                """,
                (now, _utc_after(lease_seconds), int(run_id), str(owner_id), now),
            )
            return cursor.rowcount > 0

    def owns(self, run_id: int, owner_id: str) -> bool:
        now = _utc_now()
        with self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM update_runs
                WHERE id = ? AND status = 'running' AND owner_id = ? AND lease_expires_at > ?
                """,
                (int(run_id), str(owner_id), now),
            ).fetchone()
            return row is not None

    def finish(self, run_id: int, owner_id: str, *, status: str, updates: dict[str, Any]) -> bool:
        now = _utc_now()
        assignments = ["status = ?", "finished_at = ?", "heartbeat_at = ?", "lease_expires_at = NULL"]
        values: list[Any] = [str(status), now, now]
        for key, value in updates.items():
            if key not in {
                "candidate_count", "imported_count", "skipped_count", "error_message", "stage", "summary", "raw_data"
            }:
                continue
            assignments.append(f"{key} = ?")
            values.append(_json_text(value) if key in {"summary", "raw_data"} else value)
        values.extend([int(run_id), str(owner_id), now])
        with self._connection_factory() as connection:
            cursor = connection.execute(
                f"UPDATE update_runs SET {', '.join(assignments)} WHERE id = ? AND status = 'running' AND owner_id = ? AND lease_expires_at > ?",
                values,
            )
            return cursor.rowcount > 0

    def recover_stale(self, *, older_than_seconds: int, message: str = INTERRUPTED_MESSAGE) -> list[dict[str, Any]]:
        now = _utc_now()
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._recover_in_connection(
                connection,
                now=now,
                legacy_cutoff=_utc_before(older_than_seconds),
                message=message,
            )

    def get_active(self) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT r.*, s.title AS subscription_title, s.category AS subscription_category
                FROM update_runs r
                JOIN update_subscriptions s ON s.id = r.subscription_id
                WHERE r.status = 'running' AND r.lease_expires_at > ?
                ORDER BY r.id DESC LIMIT 1
                """,
                (now,),
            ).fetchone()
            return self._decode(row)

    def _recover_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        now: str,
        legacy_cutoff: str,
        subscription_id: int | None = None,
        message: str = INTERRUPTED_MESSAGE,
    ) -> list[dict[str, Any]]:
        where = ["status = 'running'", "(lease_expires_at <= ? OR (lease_expires_at IS NULL AND started_at <= ?))"]
        values: list[Any] = [now, legacy_cutoff]
        if subscription_id is not None:
            where.append("subscription_id = ?")
            values.append(int(subscription_id))
        rows = connection.execute(
            f"SELECT id, subscription_id FROM update_runs WHERE {' AND '.join(where)}",
            values,
        ).fetchall()
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"""
            UPDATE update_runs
            SET status = 'interrupted', finished_at = ?, heartbeat_at = ?, lease_expires_at = NULL,
                error_message = ?, stage = 'interrupted'
            WHERE id IN ({placeholders})
            """,
            [now, now, str(message), *ids],
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO update_events(subscription_id, run_id, level, message, raw_data, created_at)
                VALUES (?, ?, 'warn', ?, ?, ?)
                """,
                (
                    int(row["subscription_id"]),
                    int(row["id"]),
                    str(message),
                    _json_text({"recovered": True, "status": "interrupted"}),
                    now,
                ),
            )
        return [{"id": int(row["id"]), "subscription_id": int(row["subscription_id"])} for row in rows]


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)
