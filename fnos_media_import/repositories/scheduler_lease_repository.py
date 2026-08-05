from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Callable

from ..time_utils import utc_now_iso, utc_now_iso_offset


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def _utc_now() -> str:
    return utc_now_iso()


def _utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


class SchedulerLeaseRepository:
    """Owns persistence and atomic coordination for named scheduler leases."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def acquire(self, name: str, owner_id: str, ttl_seconds: int = 90) -> bool:
        now = _utc_now()
        expires_at = _utc_seconds_from_now(max(5, int(ttl_seconds or 90)))
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduler_leases(name, owner_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE scheduler_leases.owner_id = excluded.owner_id
                   OR scheduler_leases.expires_at <= excluded.updated_at
                """,
                (str(name), str(owner_id), expires_at, now),
            )
            return cursor.rowcount > 0

    def release(self, name: str, owner_id: str) -> bool:
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "DELETE FROM scheduler_leases WHERE name = ? AND owner_id = ?",
                (str(name), str(owner_id)),
            )
            return cursor.rowcount > 0

    def get(self, name: str) -> dict[str, object] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_leases WHERE name = ?",
                (str(name),),
            ).fetchone()
            return dict(row) if row else None
