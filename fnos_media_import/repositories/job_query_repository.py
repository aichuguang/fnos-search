from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


class JobQueryRepository:
    """Read model for import jobs and their event history."""

    def __init__(self, connection_factory: ConnectionFactory, row_decoder: RowDecoder) -> None:
        self._connection_factory = connection_factory
        self._row_decoder = row_decoder

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_decoder(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM import_jobs WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            return self._row_decoder(row)

    def get_many(self, job_ids: list[int]) -> dict[int, dict[str, Any]]:
        normalized = self._normalize_ids(job_ids)
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM import_jobs WHERE id IN ({placeholders})",
                normalized,
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = self._row_decoder(row)
            if item and item.get("id") is not None:
                result[int(item["id"])] = item
        return result

    def list(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        category: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where_sql, values = self._filter_sql(status, category, source_type, keyword)
        values.extend([max(1, int(limit or 100)), max(0, int(offset or 0))])
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT import_jobs.*,
                    (SELECT gr.request_token FROM guest_requests gr WHERE gr.job_id = import_jobs.id ORDER BY gr.id DESC LIMIT 1) AS request_token,
                    (SELECT gr.id FROM guest_requests gr WHERE gr.job_id = import_jobs.id ORDER BY gr.id DESC LIMIT 1) AS request_id,
                    (SELECT gr.public_status FROM guest_requests gr WHERE gr.job_id = import_jobs.id ORDER BY gr.id DESC LIMIT 1) AS request_public_status
                FROM import_jobs
                {where_sql}
                ORDER BY import_jobs.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [item for row in rows if (item := self._row_decoder(row)) is not None]

    def count(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
    ) -> int:
        where_sql, values = self._filter_sql(status, category, source_type, keyword)
        with self._connection_factory() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS total FROM import_jobs {where_sql}", values).fetchone()
            return int(row["total"] if row else 0)

    def list_events(self, job_id: int) -> list[dict[str, Any]]:
        with self._connection_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("raw_data"):
                try:
                    item["raw_data"] = json.loads(item["raw_data"])
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return result

    @staticmethod
    def _normalize_ids(job_ids: list[int]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for job_id in job_ids:
            try:
                value = int(job_id)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @staticmethod
    def _filter_sql(
        status: str | None,
        category: str | None,
        source_type: str | None,
        keyword: str | None,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        values: list[Any] = []
        for column, value in (("status", status), ("category", category), ("source_type", source_type)):
            if value:
                where.append(f"{column} = ?")
                values.append(value)
        if keyword:
            where.append(
                """
                (title LIKE ? OR source_url LIKE ? OR target_path LIKE ? OR EXISTS (
                    SELECT 1 FROM guest_requests gr
                    WHERE gr.job_id = import_jobs.id AND gr.request_token LIKE ?
                ))
                """
            )
            like = f"%{keyword}%"
            values.extend([like, like, like, like])
        return (f"WHERE {' AND '.join(where)}" if where else ""), values
