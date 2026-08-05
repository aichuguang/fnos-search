from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


class ResourceRepository:
    """Persists normalized search resources and exposes lookup by ID."""

    def __init__(self, connection_factory: ConnectionFactory, row_decoder: RowDecoder) -> None:
        self._connection_factory = connection_factory
        self._row_decoder = row_decoder

    def save_many(self, items: list[dict[str, Any]]) -> list[int]:
        if not items:
            return []
        now = utc_now_iso()
        rows: list[tuple[Any, ...]] = []
        lookup_keys: list[tuple[str, str]] = []
        for item in items:
            raw_data = item.get("raw_data")
            source = str(item.get("source") or "unknown")
            url = str(item.get("url") or "")
            rows.append(
                (
                    item.get("title") or "", item.get("keyword"), source,
                    item.get("source_type") or "unknown", url, item.get("password") or "",
                    item.get("size"), json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None,
                    now,
                )
            )
            lookup_keys.append((source, url))
        with self._connection_factory() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO resources
                (title, keyword, source, source_type, url, password, size, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            ids_by_key: dict[tuple[str, str], int] = {}
            unique_keys = list(dict.fromkeys(lookup_keys))
            for start in range(0, len(unique_keys), 400):
                chunk = unique_keys[start : start + 400]
                where_sql = " OR ".join("(source = ? AND url = ?)" for _ in chunk)
                values: list[Any] = []
                for source, url in chunk:
                    values.extend([source, url])
                result_rows = connection.execute(
                    f"SELECT id, source, url FROM resources WHERE {where_sql}",
                    values,
                ).fetchall()
                for row in result_rows:
                    ids_by_key[(str(row["source"] or ""), str(row["url"] or ""))] = int(row["id"])
        return [ids_by_key.get(key, 0) for key in lookup_keys]

    def get(self, resource_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
        return self._row_decoder(row)

    def find_by_url(self, source_url: str, *, source: str = "") -> dict[str, Any] | None:
        url = str(source_url or "").strip()
        if not url:
            return None
        source_hint = str(source or "").strip().lower()
        with self._connection_factory() as connection:
            row = None
            if source_hint:
                row = connection.execute(
                    """
                    SELECT * FROM resources
                    WHERE url = ? AND (LOWER(source) = ? OR LOWER(source_type) = ?)
                    ORDER BY CASE WHEN LOWER(source) = ? THEN 0 ELSE 1 END, id DESC
                    LIMIT 1
                    """,
                    (url, source_hint, source_hint, source_hint),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM resources WHERE url = ? ORDER BY id DESC LIMIT 1",
                    (url,),
                ).fetchone()
        return self._row_decoder(row)
