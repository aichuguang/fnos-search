from __future__ import annotations

import hashlib
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def token_hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


# 申请已结束、不会再回到处理中的状态。命中这些状态且结束超过保留期后，
# 订阅中的邮箱/token 会被匿名化。
TERMINAL_REQUEST_STATUSES = frozenset(
    {
        "rejected",
        "cancelled",
        "failed",
        "unsupported",
        "done",
        "success",
    }
)


class GuestNotificationSubscriptionRepository:
    """Stores encrypted visitor contact data separately from public requests."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def create(
        self,
        *,
        request_id: int,
        email_encrypted: str,
        email_hash: str,
        verification_token_encrypted: str,
        verification_token_hash: str,
        verification_expires_at: str,
        unsubscribe_token_encrypted: str,
        unsubscribe_token_hash: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        now = utc_now_iso()

        def insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO guest_notification_subscriptions
                (request_id, email_encrypted, email_hash, verification_token_encrypted,
                 verification_token_hash,
                 verification_expires_at, unsubscribe_token_encrypted,
                 unsubscribe_token_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    email_encrypted=excluded.email_encrypted,
                    email_hash=excluded.email_hash,
                    verification_token_encrypted=excluded.verification_token_encrypted,
                    verification_token_hash=excluded.verification_token_hash,
                    verification_expires_at=excluded.verification_expires_at,
                    unsubscribe_token_encrypted=excluded.unsubscribe_token_encrypted,
                    unsubscribe_token_hash=excluded.unsubscribe_token_hash,
                    verified_at=NULL, opted_out_at=NULL, updated_at=excluded.updated_at
                """,
                (
                    int(request_id), email_encrypted, email_hash,
                    verification_token_encrypted, verification_token_hash, verification_expires_at,
                    unsubscribe_token_encrypted, unsubscribe_token_hash, now, now,
                ),
            )

        if connection is not None:
            insert(connection)
            return
        with self._connection_factory() as conn:
            insert(conn)

    def get_for_request(self, request_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM guest_notification_subscriptions WHERE request_id=?",
                (int(request_id),),
            ).fetchone()
        return dict(row) if row else None

    def verify(self, verification_token: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        digest = token_hash(verification_token)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT s.*, r.request_token
                FROM guest_notification_subscriptions s
                JOIN guest_requests r ON r.id=s.request_id
                WHERE s.verification_token_hash=? AND s.verification_expires_at>=?
                  AND s.verified_at IS NULL AND s.opted_out_at IS NULL
                """,
                (digest, now),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE guest_notification_subscriptions
                SET verified_at=?, verification_token_encrypted='', verification_token_hash='',
                    verification_expires_at='', updated_at=?
                WHERE request_id=?
                """,
                (now, now, int(row["request_id"])),
            )
        return dict(row)

    def opt_out(self, unsubscribe_token: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        digest = token_hash(unsubscribe_token)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT s.*, r.request_token
                FROM guest_notification_subscriptions s
                JOIN guest_requests r ON r.id=s.request_id
                WHERE s.unsubscribe_token_hash=?
                """,
                (digest,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE guest_notification_subscriptions
                SET opted_out_at=COALESCE(opted_out_at, ?), updated_at=?
                WHERE request_id=?
                """,
                (now, now, int(row["request_id"])),
            )
        return dict(row)

    def anonymize_terminal(
        self,
        *,
        terminal_statuses: tuple[str, ...] | list[str] | set[str] | frozenset[str] = TERMINAL_REQUEST_STATUSES,
        older_than: str,
        limit: int = 200,
    ) -> int:
        """清除终态且已结束一段时间申请里的订阅 PII。

        ``unsubscribe_token_hash`` 有 UNIQUE 约束，匿名化时用每行唯一的
        ``anon:<request_id>`` 占位，避免冲突。清除后该订阅既不能发送也不能
        退订，符合"申请结束后删除或匿名化邮箱"的隐私约定。
        """
        statuses = sorted(
            {str(value).strip() for value in terminal_statuses if str(value).strip()}
        )
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        now = utc_now_iso()
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT s.request_id
                FROM guest_notification_subscriptions s
                JOIN guest_requests r ON r.id = s.request_id
                WHERE r.status IN ({placeholders}) AND r.updated_at < ?
                ORDER BY r.updated_at, s.request_id
                LIMIT ?
                """,
                (*statuses, older_than, max(1, min(5000, int(limit)))),
            ).fetchall()
            request_ids = [int(row["request_id"]) for row in rows]
            if not request_ids:
                return 0
            count = 0
            for request_id in request_ids:
                cursor = connection.execute(
                    """
                    UPDATE guest_notification_subscriptions
                    SET email_encrypted='', email_hash='',
                        verification_token_encrypted='', verification_token_hash='',
                        verification_expires_at='',
                        unsubscribe_token_encrypted='', unsubscribe_token_hash=?,
                        updated_at=?
                    WHERE request_id=?
                    """,
                    (f"anon:{request_id}", now, request_id),
                )
                count += max(0, int(cursor.rowcount))
            return count
