from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
Decoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


class UpdateSubscriptionQueryRepository:
    """Read model for update subscriptions and their configured sources."""

    def __init__(self, connection_factory: ConnectionFactory, subscription_decoder: Decoder, source_decoder: Decoder) -> None:
        self._connection_factory = connection_factory
        self._subscription_decoder = subscription_decoder
        self._source_decoder = source_decoder

    def get(self, subscription_id: int, include_sources: bool = True) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM update_subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            item = self._subscription_decoder(row)
            if item and include_sources:
                source_rows = connection.execute(
                    "SELECT * FROM update_sources WHERE subscription_id = ? ORDER BY priority ASC, id ASC",
                    (subscription_id,),
                ).fetchall()
                item["sources"] = [
                    source
                    for row in source_rows
                    if (source := self._source_decoder(row)) is not None
                ]
            return item

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        due_before: str | None = None,
        include_sources: bool = False,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if status:
            where.append("status = ?")
            values.append(status)
        if due_before:
            where.append("status = 'enabled' AND next_run_at IS NOT NULL AND next_run_at != '' AND next_run_at <= ?")
            values.append(due_before)
        where_sql = "WHERE " + " AND ".join(f"({item})" for item in where) if where else ""
        values.extend([max(1, int(limit or 100)), max(0, int(offset or 0))])
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*,
                    (SELECT status FROM update_runs r WHERE r.subscription_id = s.id ORDER BY r.id DESC LIMIT 1) AS last_run_status,
                    (SELECT COUNT(*) FROM update_candidates c WHERE c.subscription_id = s.id AND c.decision = 'review') AS review_candidate_count
                FROM update_subscriptions s
                {where_sql}
                ORDER BY CASE WHEN s.status = 'enabled' THEN 0 ELSE 1 END,
                         COALESCE(NULLIF(s.next_run_at, ''), '9999') ASC,
                         s.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
            items = [item for row in rows if (item := self._subscription_decoder(row)) is not None]
            if include_sources:
                self._attach_sources(connection, items)
            return items

    def count(self, status: str | None = None) -> int:
        where = "WHERE status = ?" if status else ""
        values = [status] if status else []
        with self._connection_factory() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM update_subscriptions {where}",
                values,
            ).fetchone()
            return int(row["total"] if row else 0)

    def _attach_sources(self, connection: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
        ids = [int(item["id"]) for item in items if item.get("id")]
        if not ids:
            return
        rows = connection.execute(
            f"SELECT * FROM update_sources WHERE subscription_id IN ({','.join('?' for _ in ids)}) ORDER BY priority ASC, id ASC",
            ids,
        ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            source = self._source_decoder(row)
            if source:
                grouped.setdefault(int(source["subscription_id"]), []).append(source)
        for item in items:
            item["sources"] = grouped.get(int(item["id"]), [])
