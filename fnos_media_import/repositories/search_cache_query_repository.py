from __future__ import annotations

import hashlib
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


def _utc_now() -> str:
    return utc_now_iso()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class SearchCacheQueryRepository:
    """Reads active public search cache records without owning search orchestration."""

    def __init__(self, connection_factory: ConnectionFactory, row_decoder: RowDecoder) -> None:
        self._connection_factory = connection_factory
        self._row_decoder = row_decoder

    def get_active(self, public_id: str) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM search_cache WHERE public_id = ? AND expires_at > ?",
                (public_id, _utc_now()),
            ).fetchone()
        return self._row_decoder(row)

    def find_active_by_urls(self, keyword: str, urls: list[str]) -> dict[str, dict[str, Any]]:
        url_by_hash: dict[str, str] = {}
        for url in urls:
            text = str(url or "")
            if text:
                url_by_hash.setdefault(_hash_text(text), text)
        if not url_by_hash:
            return {}
        result: dict[str, dict[str, Any]] = {}
        hashes = list(url_by_hash)
        now = _utc_now()
        with self._connection_factory() as connection:
            for start in range(0, len(hashes), 400):
                chunk = hashes[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT public_id, source_url_hash, source_url
                    FROM search_cache
                    WHERE keyword = ? AND expires_at > ?
                      AND source_url_hash IN ({placeholders})
                    ORDER BY id DESC
                    """,
                    [keyword, now, *chunk],
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    url = url_by_hash.get(str(item.get("source_url_hash") or ""))
                    if url and url not in result:
                        result[url] = item
        return result
