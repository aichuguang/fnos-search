from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def _utc_now() -> str:
    return utc_now_iso()


class GuestRequestCommandRepository:
    """Writes guest requests and append-only request events."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def create(self, data: dict[str, Any]) -> int:
        with self._connection_factory() as connection:
            return self._insert_request(connection, data)

    def create_with_event(
        self,
        data: dict[str, Any],
        *,
        level: str,
        message: str,
        event_data: Any = None,
        connection: sqlite3.Connection | None = None,
        emit: Callable[[sqlite3.Connection, int], Any] | None = None,
    ) -> int:
        """Create a request and its first audit event in one transaction.

        ``connection`` lets a caller enqueue the corresponding notification in
        the same transaction; when omitted a fresh transaction is opened.
        """
        if connection is not None:
            return self._create_with_event_on_connection(
                connection, data, level=level, message=message, event_data=event_data, emit=emit
            )
        with self._connection_factory() as connection:
            return self._create_with_event_on_connection(
                connection, data, level=level, message=message, event_data=event_data, emit=emit
            )

    def _create_with_event_on_connection(
        self,
        connection: sqlite3.Connection,
        data: dict[str, Any],
        *,
        level: str,
        message: str,
        event_data: Any = None,
        emit: Callable[[sqlite3.Connection, int], Any] | None = None,
    ) -> int:
        request_id = self._insert_request(connection, data)
        raw_text = json.dumps(event_data, ensure_ascii=False) if event_data is not None else None
        connection.execute(
            "INSERT INTO guest_request_events (request_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, level, message, raw_text, _utc_now()),
        )
        if emit is not None:
            emit(connection, request_id)
        return request_id

    def _insert_request(self, connection: sqlite3.Connection, data: dict[str, Any]) -> int:
        now = _utc_now()
        source_url = str(data.get("source_url") or "")
        raw_data = data.get("raw_data")
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        source_hash = data.get("source_url_hash") or hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        cursor = connection.execute(
                """
                INSERT INTO guest_requests
                (request_token, job_id, title, category, category_label, source_type,
                 source_url, source_url_hash, password, note, status, public_status,
                 client_ip_hash, user_agent, raw_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["request_token"], data.get("job_id"), data.get("title") or "未命名资源",
                    data.get("category") or "movie", data.get("category_label") or "",
                    data.get("source_type") or "unknown", source_url, source_hash,
                    data.get("password") or "", data.get("note") or "",
                    data.get("status") or "submitted", data.get("public_status") or "处理中",
                    data.get("client_ip_hash") or "", data.get("user_agent") or "",
                    raw_text, now, now,
                ),
        )
        return int(cursor.lastrowid)

    def update(self, request_id: int, updates: dict[str, Any]) -> None:
        if not updates:
            return
        values_to_write = {**updates, "updated_at": _utc_now()}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in values_to_write.items():
            if key == "raw_data" and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(request_id)
        with self._connection_factory() as connection:
            connection.execute(f"UPDATE guest_requests SET {', '.join(assignments)} WHERE id = ?", values)

    def transition_with_event(
        self,
        request_id: int,
        *,
        expected_statuses: set[str],
        status: str,
        public_status: str,
        raw_data: dict[str, Any] | None,
        level: str,
        message: str,
        event_data: Any = None,
        request_updates: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
        emit: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> bool:
        """Conditionally change status and append its audit event atomically.

        ``emit``（可选）在事务内、事件写完之后执行，用于把通知任务与本次
        状态变更一起原子提交；异常由调用方自行处理，不会自动吞掉。
        """
        if connection is not None:
            return self._transition_with_event_on_connection(
                connection, request_id,
                expected_statuses=expected_statuses,
                status=status, public_status=public_status, raw_data=raw_data,
                level=level, message=message, event_data=event_data,
                request_updates=request_updates, emit=emit,
            )
        with self._connection_factory() as connection:
            return self._transition_with_event_on_connection(
                connection, request_id,
                expected_statuses=expected_statuses,
                status=status, public_status=public_status, raw_data=raw_data,
                level=level, message=message, event_data=event_data,
                request_updates=request_updates, emit=emit,
            )

    def _transition_with_event_on_connection(
        self,
        connection: sqlite3.Connection,
        request_id: int,
        *,
        expected_statuses: set[str],
        status: str,
        public_status: str,
        raw_data: dict[str, Any] | None,
        level: str,
        message: str,
        event_data: Any = None,
        request_updates: dict[str, Any] | None = None,
        emit: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> bool:
        if not expected_statuses:
            return False
        placeholders = ",".join("?" for _ in expected_statuses)
        now = _utc_now()
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        event_text = json.dumps(event_data, ensure_ascii=False) if event_data is not None else None
        request_updates = request_updates or {}
        cursor = connection.execute(
            f"""
            UPDATE guest_requests
            SET status = ?, public_status = ?, raw_data = ?,
                title = COALESCE(?, title), category = COALESCE(?, category),
                category_label = COALESCE(?, category_label), updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (
                status,
                public_status,
                raw_text,
                request_updates.get("title"),
                request_updates.get("category"),
                request_updates.get("category_label"),
                now,
                request_id,
                *sorted(expected_statuses),
            ),
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            "INSERT INTO guest_request_events (request_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, level, message, event_text, now),
        )
        if emit is not None:
            emit(connection)
        return True

    def bind_job_with_event(
        self,
        request_id: int,
        *,
        job_id: int,
        status: str,
        public_status: str,
        raw_data: dict[str, Any] | None,
        level: str,
        message: str,
        event_data: Any = None,
        expected_statuses: set[str] | None = None,
        request_updates: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
        emit: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> str:
        """Bind one formal job once and record the binding in the same transaction."""
        if connection is not None:
            return self._bind_job_with_event_on_connection(
                connection, request_id,
                job_id=job_id, status=status, public_status=public_status,
                raw_data=raw_data, level=level, message=message,
                event_data=event_data, expected_statuses=expected_statuses,
                request_updates=request_updates, emit=emit,
            )
        with self._connection_factory() as connection:
            return self._bind_job_with_event_on_connection(
                connection, request_id,
                job_id=job_id, status=status, public_status=public_status,
                raw_data=raw_data, level=level, message=message,
                event_data=event_data, expected_statuses=expected_statuses,
                request_updates=request_updates, emit=emit,
            )

    def _bind_job_with_event_on_connection(
        self,
        connection: sqlite3.Connection,
        request_id: int,
        *,
        job_id: int,
        status: str,
        public_status: str,
        raw_data: dict[str, Any] | None,
        level: str,
        message: str,
        event_data: Any = None,
        expected_statuses: set[str] | None = None,
        request_updates: dict[str, Any] | None = None,
        emit: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> str:
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        event_text = json.dumps(event_data, ensure_ascii=False) if event_data is not None else None
        now = _utc_now()
        row = connection.execute(
            "SELECT job_id, status FROM guest_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return "missing"
        existing_job_id = int(row["job_id"]) if row["job_id"] is not None else None
        if existing_job_id is not None:
            return "existing" if existing_job_id == int(job_id) else "conflict"
        allowed_statuses = expected_statuses or {"submitted"}
        if str(row["status"] or "") not in allowed_statuses:
            return "state_conflict"
        request_updates = request_updates or {}
        connection.execute(
            """
            UPDATE guest_requests
            SET job_id = ?, status = ?, public_status = ?, raw_data = ?,
                title = COALESCE(?, title), category = COALESCE(?, category),
                category_label = COALESCE(?, category_label), updated_at = ?
            WHERE id = ?
            """,
            (
                job_id, status, public_status, raw_text,
                request_updates.get("title"), request_updates.get("category"),
                request_updates.get("category_label"), now, request_id,
            ),
        )
        connection.execute(
            "INSERT INTO guest_request_events (request_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, level, message, event_text, now),
        )
        if emit is not None:
            emit(connection)
        return "bound"

    def add_event(self, request_id: int, level: str, message: str, raw_data: Any = None) -> int:
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "INSERT INTO guest_request_events (request_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, level, message, raw_text, _utc_now()),
            )
            return int(cursor.lastrowid)
