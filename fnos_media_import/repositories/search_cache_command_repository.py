from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso, utc_now_iso_offset


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def _utc_now() -> str:
    return utc_now_iso()


def _utc_minutes_from_now(minutes: int) -> str:
    return utc_now_iso_offset(minutes=minutes)


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class SearchCacheCommandRepository:
    """Writes and refreshes public search cache records."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def save_many(
        self,
        records: list[tuple[str, dict[str, Any]]],
        *,
        keyword: str,
        expires_minutes: int = 60,
    ) -> list[int]:
        if not records:
            return []
        now = _utc_now()
        expires_at = _utc_minutes_from_now(expires_minutes)
        rows: list[tuple[Any, ...]] = []
        for public_id, item in records:
            source_url = str(item.get("url") or "")
            raw_data = item.get("raw_data", item)
            raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
            rows.append(
                (
                    public_id, keyword, item.get("title") or "未命名资源",
                    item.get("source_type") or "unknown", source_url, _hash_text(source_url),
                    item.get("password") or "", str(item.get("size") or item.get("size_text") or ""),
                    raw_text, expires_at, now,
                )
            )
        with self._connection_factory() as connection:
            connection.execute(
                """
                DELETE FROM search_cache
                WHERE id IN (
                    SELECT id FROM search_cache
                    WHERE expires_at <= ?
                    ORDER BY id ASC
                    LIMIT 1000
                )
                """,
                (now,),
            )
            connection.executemany(
                """
                INSERT INTO search_cache
                (public_id, keyword, title, source_type, source_url, source_url_hash,
                 password, size, raw_data, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            public_ids = list(dict.fromkeys(str(public_id) for public_id, _item in records))
            ids_by_public_id: dict[str, int] = {}
            for start in range(0, len(public_ids), 900):
                chunk = public_ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                result_rows = connection.execute(
                    f"SELECT id, public_id FROM search_cache WHERE public_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in result_rows:
                    ids_by_public_id[str(row["public_id"])] = int(row["id"])
        return [ids_by_public_id.get(str(public_id), 0) for public_id, _item in records]

    def update(self, public_id: str, item: dict[str, Any], expires_minutes: int = 60) -> bool:
        source_url = str(item.get("url") or item.get("source_url") or "")
        raw_data = item.get("raw_data", item)
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE search_cache
                SET title = ?, source_type = ?, source_url = ?, source_url_hash = ?,
                    password = ?, size = ?, raw_data = ?, expires_at = ?
                WHERE public_id = ? AND expires_at > ?
                """,
                (
                    item.get("title") or "未命名资源", item.get("source_type") or "unknown",
                    source_url, _hash_text(source_url), item.get("password") or "",
                    str(item.get("size") or item.get("size_text") or ""), raw_text,
                    _utc_minutes_from_now(expires_minutes), public_id, _utc_now(),
                ),
            )
            return cursor.rowcount > 0

    def prune_expired(self, *, limit: int = 1000) -> int:
        """Delete one bounded batch of expired cache rows."""
        now = _utc_now()
        normalized_limit = max(1, int(limit or 1000))
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                DELETE FROM search_cache
                WHERE id IN (
                    SELECT id FROM search_cache
                    WHERE expires_at <= ?
                    ORDER BY expires_at ASC, id ASC
                    LIMIT ?
                )
                """,
                (now, normalized_limit),
            )
            return int(cursor.rowcount)
