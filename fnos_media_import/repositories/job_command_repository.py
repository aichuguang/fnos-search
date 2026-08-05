from __future__ import annotations

import json
import uuid
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
TerminalStatusChecker = Callable[..., bool]
StatusTransitionEmitter = Callable[[sqlite3.Connection, dict[str, Any], dict[str, Any]], None]


def _utc_now() -> str:
    return utc_now_iso()


class JobCommandRepository:
    """Creates import jobs, applies state updates, and appends job events."""

    def __init__(self, connection_factory: ConnectionFactory, terminal_status_checker: TerminalStatusChecker) -> None:
        self._connection_factory = connection_factory
        self._terminal_status_checker = terminal_status_checker
        self._status_transition_emitter: StatusTransitionEmitter | None = None

    def set_status_transition_emitter(self, emitter: StatusTransitionEmitter | None) -> None:
        self._status_transition_emitter = emitter

    def create(self, data: dict[str, Any]) -> tuple[int, bool]:
        now = _utc_now()
        raw_data = data.get("raw_data")
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        idempotency_key = str(data.get("idempotency_key") or f"legacy:{uuid.uuid4().hex}")
        with self._connection_factory() as connection:
            idempotent = connection.execute(
                "SELECT id FROM import_jobs WHERE idempotency_key = ? LIMIT 1", (idempotency_key,)
            ).fetchone()
            if idempotent:
                return int(idempotent["id"]), False
            existing = connection.execute(
                """
                SELECT id, status, error_message
                FROM import_jobs
                WHERE source_url = ? AND category = ?
                  AND status NOT IN ('cancelled', 'failed', 'unsupported', 'skipped', 'rejected')
                  AND COALESCE(status, '') NOT LIKE '%取消%'
                  AND COALESCE(status, '') NOT LIKE '%拒绝%'
                  AND COALESCE(status, '') NOT LIKE '%未通过%'
                  AND COALESCE(status, '') NOT LIKE '%失败%'
                  AND COALESCE(status, '') NOT LIKE '%暂不支持%'
                ORDER BY id DESC LIMIT 1
                """,
                (data["source_url"], data["category"]),
            ).fetchone()
            if existing and not self._terminal_status_checker(existing["status"], existing["error_message"]):
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO import_jobs
                (title, category, category_label, source_type, source_url, password, target_route,
                 target_path, status, external_task_id, error_message, raw_data, created_at, updated_at,
                 idempotency_key, config_revision, executor_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["title"], data["category"], data["category_label"], data["source_type"],
                    data["source_url"], data.get("password") or "", data["target_route"],
                    data.get("target_path"), data["status"], data.get("external_task_id"),
                    data.get("error_message"), raw_text, now, now, idempotency_key,
                    self._safe_revision(data.get("config_revision")), str(data.get("executor_id") or ""),
                ),
            )
            return int(cursor.lastrowid), True

    @staticmethod
    def _safe_revision(value: Any) -> int:
        try:
            return max(1, int(value or 1))
        except (TypeError, ValueError):
            return 1

    def update(self, job_id: int, updates: dict[str, Any]) -> None:
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
        values.append(job_id)
        with self._connection_factory() as connection:
            previous = self._transition_row(connection, job_id)
            cursor = connection.execute(f"UPDATE import_jobs SET {', '.join(assignments)} WHERE id = ?", values)
            self._emit_status_transition(connection, previous, values_to_write, cursor.rowcount)

    def update_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        updates: dict[str, Any],
    ) -> bool:
        statuses = sorted({str(value or "").strip() for value in expected_statuses if str(value or "").strip()})
        if not updates or not statuses:
            return False
        values_to_write = {**updates, "updated_at": _utc_now()}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in values_to_write.items():
            if key == "raw_data" and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        placeholders = ",".join("?" for _ in statuses)
        values.extend([int(job_id), *statuses])
        with self._connection_factory() as connection:
            previous = self._transition_row(connection, job_id)
            cursor = connection.execute(
                f"UPDATE import_jobs SET {', '.join(assignments)} WHERE id = ? AND status IN ({placeholders})",
                values,
            )
            self._emit_status_transition(connection, previous, values_to_write, cursor.rowcount)
            return cursor.rowcount == 1

    def update_if_status_and_claim_token(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        expected_claim_token: str | None,
        updates: dict[str, Any],
    ) -> bool:
        """Atomically update a job only while its durable refresh claim still matches."""

        statuses = sorted({str(value or "").strip() for value in expected_statuses if str(value or "").strip()})
        if not updates or not statuses:
            return False
        values_to_write = {**updates, "updated_at": _utc_now()}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in values_to_write.items():
            if key == "raw_data" and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)
        placeholders = ",".join("?" for _ in statuses)
        token_expression = (
            "json_extract("
            "CASE WHEN json_valid(raw_data) THEN raw_data ELSE '{}' END, "
            "'$.sixpan_media_refresh_retry.claim_token'"
            ")"
        )
        if expected_claim_token is None:
            token_clause = f"{token_expression} IS NULL"
            token_values: list[Any] = []
        else:
            token_clause = f"{token_expression} = ?"
            token_values = [str(expected_claim_token)]
        values.extend([int(job_id), *statuses, *token_values])
        with self._connection_factory() as connection:
            previous = self._transition_row(connection, job_id)
            cursor = connection.execute(
                f"UPDATE import_jobs SET {', '.join(assignments)} "
                f"WHERE id = ? AND status IN ({placeholders}) AND {token_clause}",
                values,
            )
            self._emit_status_transition(connection, previous, values_to_write, cursor.rowcount)
            return cursor.rowcount == 1

    @staticmethod
    def _transition_row(connection: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT id, title, category, category_label, source_type, status,
                   error_message, raw_data, updated_at
            FROM import_jobs WHERE id=?
            """,
            (int(job_id),),
        ).fetchone()
        return dict(row) if row else None

    def _emit_status_transition(
        self,
        connection: sqlite3.Connection,
        previous: dict[str, Any] | None,
        updates: dict[str, Any],
        rowcount: int,
    ) -> None:
        emitter = self._status_transition_emitter
        if rowcount != 1 or emitter is None or previous is None or "status" not in updates:
            return
        next_status = str(updates.get("status") or "").strip().lower()
        previous_status = str(previous.get("status") or "").strip().lower()
        if not next_status or next_status == previous_status:
            return
        current = {
            **previous,
            **updates,
            "status": next_status,
            "previous_status": previous_status,
        }
        emitter(connection, previous, current)

    def add_event(self, job_id: int, level: str, message: str, raw_data: Any = None) -> int:
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        with self._connection_factory() as connection:
            cursor = connection.execute(
                "INSERT INTO job_events (job_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, level, message, raw_text, _utc_now()),
            )
            return int(cursor.lastrowid)

    def delete_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
    ) -> bool:
        statuses = sorted(
            {str(value or "").strip().lower() for value in expected_statuses if str(value or "").strip()}
        )
        if not statuses:
            return False
        placeholders = ",".join("?" for _ in statuses)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"DELETE FROM import_jobs WHERE id = ? AND lower(status) IN ({placeholders})",
                (int(job_id), *statuses),
            )
            return cursor.rowcount == 1
