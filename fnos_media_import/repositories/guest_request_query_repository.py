from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
import hashlib
from typing import Any, Callable

from ..time_utils import utc_now_iso_offset


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


class GuestRequestQueryRepository:
    """Read model for guest requests and their public processing history."""

    def __init__(self, connection_factory: ConnectionFactory, row_decoder: RowDecoder) -> None:
        self._connection_factory = connection_factory
        self._row_decoder = row_decoder

    def get(self, request_id: int) -> dict[str, Any] | None:
        return self._get_by("id", request_id)

    def get_by_token(self, request_token: str) -> dict[str, Any] | None:
        return self._get_by("request_token", request_token)

    def list_by_job(self, job_id: int) -> list[dict[str, Any]]:
        with self._connection_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM guest_requests WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
        return [item for row in rows if (item := self._row_decoder(row)) is not None]

    def list(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit or 100))
        safe_offset = max(0, int(offset or 0))
        with self._connection_factory() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM guest_requests WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (status, safe_limit, safe_offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM guest_requests ORDER BY id DESC LIMIT ? OFFSET ?",
                    (safe_limit, safe_offset),
                ).fetchall()
        return [item for row in rows if (item := self._row_decoder(row)) is not None]

    def count(self, status: str | None = None) -> int:
        with self._connection_factory() as connection:
            if status:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM guest_requests WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS total FROM guest_requests").fetchone()
        return int(row["total"] if row else 0)

    def find_recent_by_url(
        self, *, source_url: str, category: str, within_minutes: int = 1440
    ) -> dict[str, Any] | None:
        threshold = utc_now_iso_offset(minutes=-max(1, int(within_minutes)))
        source_hash = hashlib.sha256(str(source_url).encode("utf-8")).hexdigest()
        with self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT * FROM guest_requests
                WHERE source_url_hash = ? AND category = ? AND created_at >= ?
                  AND status NOT IN ('rejected', 'cancelled', 'failed', 'unsupported')
                  AND COALESCE(status, '') NOT LIKE '%取消%'
                  AND COALESCE(status, '') NOT LIKE '%拒绝%'
                  AND COALESCE(status, '') NOT LIKE '%未通过%'
                  AND COALESCE(status, '') NOT LIKE '%失败%'
                  AND COALESCE(status, '') NOT LIKE '%暂不支持%'
                  AND COALESCE(public_status, '') NOT LIKE '%取消%'
                  AND COALESCE(public_status, '') NOT LIKE '%拒绝%'
                  AND COALESCE(public_status, '') NOT LIKE '%未通过%'
                  AND COALESCE(public_status, '') NOT LIKE '%失败%'
                  AND COALESCE(public_status, '') NOT LIKE '%暂不支持%'
                ORDER BY id DESC LIMIT 1
                """,
                (source_hash, category, threshold),
            ).fetchone()
        return self._row_decoder(row)

    def list_events(self, request_id: int) -> list[dict[str, Any]]:
        return self.list_events_for_requests([request_id]).get(request_id, [])

    def list_events_for_requests(self, request_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        normalized = self._normalize_ids(request_ids)
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM guest_request_events WHERE request_id IN ({placeholders}) ORDER BY request_id ASC, id ASC",
                normalized,
            ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {request_id: [] for request_id in normalized}
        for row in rows:
            item = dict(row)
            if item.get("raw_data"):
                try:
                    item["raw_data"] = json.loads(item["raw_data"])
                except json.JSONDecodeError:
                    pass
            grouped.setdefault(int(item["request_id"]), []).append(item)
        return grouped

    def _get_by(self, column: str, value: object) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                f"SELECT * FROM guest_requests WHERE {column} = ?",
                (value,),
            ).fetchone()
        return self._row_decoder(row)

    @staticmethod
    def _normalize_ids(request_ids: list[int]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for request_id in request_ids:
            try:
                value = int(request_id)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in seen:
                seen.add(value)
                result.append(value)
        return result
