from __future__ import annotations

import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int
    remaining: int
    reset_at: int


class RateLimitRepository:
    """Atomic fixed-window rate limits shared by every local process."""

    def __init__(self, connection_factory: ConnectionFactory, clock: Callable[[], float] = time.time) -> None:
        self._connection_factory = connection_factory
        self._clock = clock

    def check(self, bucket_key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        normalized_limit = max(1, int(limit))
        normalized_window = max(1, int(window_seconds))
        now = int(self._clock())
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_started_at, request_count, expires_at FROM rate_limit_buckets WHERE bucket_key = ?",
                (str(bucket_key),),
            ).fetchone()
            if row is None or int(row["expires_at"]) <= now:
                expires_at = now + normalized_window
                connection.execute(
                    """
                    INSERT INTO rate_limit_buckets
                    (bucket_key, window_started_at, request_count, expires_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(bucket_key) DO UPDATE SET
                        window_started_at = excluded.window_started_at,
                        request_count = 1,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (str(bucket_key), now, expires_at, now),
                )
                return RateLimitDecision(True, 0, max(0, normalized_limit - 1), expires_at)

            count = int(row["request_count"])
            expires_at = int(row["expires_at"])
            if count >= normalized_limit:
                return RateLimitDecision(False, max(1, expires_at - now), 0, expires_at)
            next_count = count + 1
            connection.execute(
                "UPDATE rate_limit_buckets SET request_count = ?, updated_at = ? WHERE bucket_key = ?",
                (next_count, now, str(bucket_key)),
            )
            return RateLimitDecision(True, 0, max(0, normalized_limit - next_count), expires_at)

    def prune_expired(self, *, limit: int = 500) -> int:
        now = int(self._clock())
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                DELETE FROM rate_limit_buckets
                WHERE bucket_key IN (
                    SELECT bucket_key FROM rate_limit_buckets
                    WHERE expires_at <= ?
                    ORDER BY expires_at
                    LIMIT ?
                )
                """,
                (now, max(1, int(limit))),
            )
            return int(cursor.rowcount)
