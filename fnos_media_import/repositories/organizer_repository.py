from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso, utc_now_iso_offset

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def utc_now() -> str:
    return utc_now_iso()


def utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


def _decode_json_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for field in fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


class OrganizerRepository:
    """Persists Organizer tasks, runs, plans and their audit trail."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def create_organizer_task(
        self,
        *,
        category: str,
        openlist_root_path: str,
        category_label: str = "",
        title: str = "",
        source_keyword: str = "",
        trigger_type: str = "",
        job_id: int | None = None,
        request_id: int | None = None,
        rclone_run_id: int | None = None,
        status: str = "pending",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> int:
        now = utc_now()
        with self._connection_factory() as conn:
            return self._insert_organizer_task(
                conn,
                category=category,
                openlist_root_path=openlist_root_path,
                category_label=category_label,
                title=title,
                source_keyword=source_keyword,
                trigger_type=trigger_type,
                job_id=job_id,
                request_id=request_id,
                rclone_run_id=rclone_run_id,
                status=status,
                evidence=evidence,
                raw_data=raw_data,
                now=now,
            )

    def get_or_create_organizer_task_for_job(
        self,
        *,
        job_id: int,
        category: str,
        openlist_root_path: str,
        category_label: str = "",
        title: str = "",
        source_keyword: str = "",
        trigger_type: str = "",
        request_id: int | None = None,
        rclone_run_id: int | None = None,
        status: str = "pending",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> tuple[int, bool]:
        """Atomically reuse or create the Organizer task owned by one import job."""

        normalized_job_id = int(job_id)
        if normalized_job_id <= 0:
            raise ValueError("job_id must be a positive integer")
        now = utc_now()
        with self._connection_factory() as conn:
            # A unique index cannot be added safely because existing databases may
            # already contain historical duplicates.  The write lock serializes
            # the lookup and insert while retaining those rows unchanged.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM organizer_tasks WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (normalized_job_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False
            task_id = self._insert_organizer_task(
                conn,
                category=category,
                openlist_root_path=openlist_root_path,
                category_label=category_label,
                title=title,
                source_keyword=source_keyword,
                trigger_type=trigger_type,
                job_id=normalized_job_id,
                request_id=request_id,
                rclone_run_id=rclone_run_id,
                status=status,
                evidence=evidence,
                raw_data=raw_data,
                now=now,
            )
            return task_id, True

    @staticmethod
    def _insert_organizer_task(
        conn: sqlite3.Connection,
        *,
        category: str,
        openlist_root_path: str,
        category_label: str,
        title: str,
        source_keyword: str,
        trigger_type: str,
        job_id: int | None,
        request_id: int | None,
        rclone_run_id: int | None,
        status: str,
        evidence: Any,
        raw_data: Any,
        now: str,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO organizer_tasks
            (job_id, request_id, rclone_run_id, trigger_type, category, category_label, title, source_keyword,
             openlist_root_path, status, confidence, evidence, raw_data, revision,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                job_id,
                request_id,
                rclone_run_id,
                trigger_type,
                category,
                category_label,
                title,
                source_keyword,
                openlist_root_path,
                status,
                0,
                json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
                json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def find_recent_organizer_task(self, openlist_root_path: str, category: str = "", active_only: bool = True) -> dict[str, Any] | None:
        where = ["openlist_root_path = ?"]
        values: list[Any] = [openlist_root_path]
        if category:
            where.append("category = ?")
            values.append(category)
        if active_only:
            where.append("status NOT IN ('done', 'failed', 'cancelled', 'skipped')")
        with self._connection_factory() as conn:
            row = conn.execute(
                f"SELECT * FROM organizer_tasks WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1",
                values,
            ).fetchone()
        item = dict(row) if row else None
        return _decode_json_fields(item, ("evidence", "raw_data")) if item else None

    def update_organizer_task(self, task_id: int, **updates: Any) -> bool:
        expected_statuses = updates.pop("expected_statuses", None)
        expected_revision = updates.pop("expected_revision", None)
        bump_revision = bool(updates.pop("bump_revision", False))
        scan_owner = updates.pop("scan_owner", None) if "scan_owner" in updates else None
        clear_scan_lease = bool(updates.pop("clear_scan_lease", False))
        allowed = {
            "status",
            "confidence",
            "media_type",
            "tmdb_id",
            "tmdb_title",
            "tmdb_year",
            "error_message",
            "title",
            "source_keyword",
            "evidence",
            "raw_data",
        }
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            if key in {"evidence", "raw_data"} and value is not None:
                values.append(json.dumps(value, ensure_ascii=False))
            else:
                values.append(value)
        if bump_revision:
            assignments.append("revision = COALESCE(revision, 1) + 1")
        if scan_owner is not None:
            assignments.append("scan_owner = ?")
            values.append(str(scan_owner or "") or None)
        if clear_scan_lease:
            assignments.extend(["scan_owner = NULL", "scan_lease_expires_at = NULL"])
        if not assignments:
            return False
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(task_id)
        where = ["id = ?"]
        if expected_statuses:
            statuses = [str(item or "").strip() for item in expected_statuses if str(item or "").strip()]
            if not statuses:
                return False
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
        if expected_revision is not None:
            where.append("revision = ?")
            values.append(max(1, int(expected_revision)))
        with self._connection_factory() as conn:
            cur = conn.execute(f"UPDATE organizer_tasks SET {', '.join(assignments)} WHERE {' AND '.join(where)}", values)
            return cur.rowcount == 1

    def cancel_organizer_task(self, task_id: int, *, reason: str = "") -> bool:
        """Atomically cancel an Organizer task unless it already reached an immutable terminal state.

        ``failed`` is intentionally cancellable because Organizer can recover or
        manually retry failed scans.  ``done``, ``cancelled`` and ``skipped`` are
        immutable here so a stale cancellation snapshot can never overwrite a
        concurrently completed task.
        """

        now = utc_now()
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                UPDATE organizer_tasks
                SET status = 'cancelled', error_message = ?,
                    revision = COALESCE(revision, 1) + 1,
                    scan_owner = NULL, scan_lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND status NOT IN ('done', 'cancelled', 'skipped')
                """,
                (str(reason or ""), now, int(task_id)),
            )
            return cur.rowcount == 1

    def claim_organizer_task_for_scan(
        self,
        task_id: int,
        *,
        allowed_statuses: list[str] | tuple[str, ...] | set[str],
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> bool:
        statuses = [str(item or "").strip() for item in allowed_statuses if str(item or "").strip()]
        if not statuses:
            return False
        placeholders = ",".join(["?"] * len(statuses))
        with self._connection_factory() as conn:
            now = utc_now()
            lease_until = utc_seconds_from_now(max(30, int(lease_seconds)))
            cur = conn.execute(
                f"""
                UPDATE organizer_tasks
                SET status = 'scanning', error_message = '', revision = COALESCE(revision, 1) + 1,
                    scan_owner = ?, scan_lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND (
                    status IN ({placeholders})
                    OR (status = 'scanning' AND scan_lease_expires_at IS NOT NULL AND scan_lease_expires_at <= ?)
                )
                """,
                [str(owner_id or "") or None, lease_until, now, int(task_id), *statuses, now],
            )
            return cur.rowcount == 1

    def renew_organizer_scan(
        self,
        task_id: int,
        owner_id: str,
        *,
        lease_seconds: int = 120,
        expected_revision: int | None = None,
    ) -> bool:
        now = utc_now()
        lease_until = utc_seconds_from_now(max(30, int(lease_seconds)))
        where = [
            "id = ?",
            "status = 'scanning'",
            "scan_owner = ?",
            "scan_lease_expires_at IS NOT NULL",
            "scan_lease_expires_at > ?",
        ]
        values: list[Any] = [lease_until, now, int(task_id), str(owner_id or ""), now]
        if expected_revision is not None:
            where.append("revision = ?")
            values.append(max(1, int(expected_revision)))
        with self._connection_factory() as conn:
            cur = conn.execute(
                f"""
                UPDATE organizer_tasks
                SET scan_lease_expires_at = ?, updated_at = ?
                WHERE {' AND '.join(where)}
                """,
                values,
            )
            return cur.rowcount == 1

    def owns_organizer_scan(
        self,
        task_id: int,
        owner_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        now = utc_now()
        where = [
            "id = ?",
            "status = 'scanning'",
            "scan_owner = ?",
            "scan_lease_expires_at IS NOT NULL",
            "scan_lease_expires_at > ?",
        ]
        values: list[Any] = [int(task_id), str(owner_id or ""), now]
        if expected_revision is not None:
            where.append("revision = ?")
            values.append(max(1, int(expected_revision)))
        with self._connection_factory() as conn:
            row = conn.execute(
                f"SELECT 1 FROM organizer_tasks WHERE {' AND '.join(where)}",
                values,
            ).fetchone()
            return row is not None

    def get_organizer_task(self, task_id: int, include_children: bool = True) -> dict[str, Any] | None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT * FROM organizer_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = _decode_json_fields(dict(row), ("evidence", "raw_data"))
            if include_children:
                files = [_decode_json_fields(dict(item), ("raw_data",)) for item in conn.execute("SELECT * FROM organizer_files WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()]
                mappings = [_decode_json_fields(dict(item), ("reason", "raw_data")) for item in conn.execute("SELECT * FROM organizer_mappings WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()]
                operations = [_decode_json_fields(dict(item), ("reason", "undo_data", "raw_data")) for item in conn.execute("SELECT * FROM organizer_operations WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()]
                ai_suggestions = [
                    _decode_json_fields(dict(item), ("prompt", "response", "parsed"))
                    for item in conn.execute("SELECT * FROM organizer_ai_suggestions WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()
                ]
                tmdb_matches = [
                    _decode_json_fields(dict(item), ("raw_data",))
                    for item in conn.execute("SELECT * FROM organizer_tmdb_matches WHERE task_id = ? ORDER BY score DESC, id ASC", (task_id,)).fetchall()
                ]
                task.update({"files": files, "mappings": mappings, "operations": operations, "ai_suggestions": ai_suggestions, "tmdb_matches": tmdb_matches})
            return task

    def delete_organizer_task_if_status(
        self,
        task_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
    ) -> bool:
        statuses = sorted(
            {str(value or "").strip().lower() for value in expected_statuses if str(value or "").strip()}
        )
        if not statuses:
            return False
        placeholders = ",".join("?" for _ in statuses)
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"DELETE FROM organizer_tasks WHERE id = ? AND lower(status) IN ({placeholders})",
                (int(task_id), *statuses),
            )
            return cursor.rowcount == 1

    def count_organizer_tasks(self, status: str | None = None) -> int:
        with self._connection_factory() as conn:
            if status:
                row = conn.execute("SELECT COUNT(*) AS total FROM organizer_tasks WHERE status = ?", (status,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS total FROM organizer_tasks").fetchone()
            return int(row["total"] if row else 0)

    def list_organizer_tasks(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit or 100))
        safe_offset = max(0, int(offset or 0))
        with self._connection_factory() as conn:
            if status:
                rows = conn.execute("SELECT * FROM organizer_tasks WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?", (status, safe_limit, safe_offset)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM organizer_tasks ORDER BY id DESC LIMIT ? OFFSET ?", (safe_limit, safe_offset)).fetchall()
        return [_decode_json_fields(dict(row), ("evidence", "raw_data")) for row in rows]

    def list_organizer_tasks_by_job(self, job_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            rows = conn.execute(
                "SELECT * FROM organizer_tasks WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (int(job_id), max(1, int(limit or 20))),
            ).fetchall()
        return [_decode_json_fields(dict(row), ("evidence", "raw_data")) for row in rows]

    def replace_organizer_plan(
        self,
        task_id: int,
        *,
        files: list[dict[str, Any]],
        mappings: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        expected_revision: int | None = None,
        owner_id: str = "",
        expected_status: str | None = None,
        task_updates: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        task_update_values = dict(task_updates or {})
        allowed_task_updates = {
            "status",
            "confidence",
            "error_message",
            "evidence",
            "raw_data",
            "media_type",
            "tmdb_id",
            "tmdb_title",
            "tmdb_year",
        }
        if any(key not in allowed_task_updates for key in task_update_values):
            return False
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_revision is not None or expected_status is not None or task_update_values:
                task = conn.execute(
                    """
                    SELECT status, revision, scan_owner, scan_lease_expires_at
                    FROM organizer_tasks WHERE id = ?
                    """,
                    (int(task_id),),
                ).fetchone()
                if task is None:
                    return False
                required_status = str(expected_status if expected_status is not None else "scanning")
                if str(task["status"] or "") != required_status:
                    return False
                if expected_revision is not None and int(task["revision"] or 1) != int(expected_revision):
                    return False
                if expected_status is None and owner_id and str(task["scan_owner"] or "") != str(owner_id):
                    return False
                if expected_status is None and task["scan_lease_expires_at"] and str(task["scan_lease_expires_at"]) <= now:
                    return False
            conn.execute("DELETE FROM organizer_operations WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM organizer_mappings WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM organizer_files WHERE task_id = ?", (task_id,))
            file_id_by_path: dict[str, int] = {}
            for item in files:
                cur = conn.execute(
                    """
                    INSERT INTO organizer_files
                    (task_id, path, name, parent_path, ext, size, season, episode, raw_data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        item.get("path") or "",
                        item.get("name") or "",
                        item.get("parent_path") or "",
                        item.get("ext") or "",
                        item.get("size"),
                        item.get("season"),
                        item.get("episode"),
                        json.dumps(item.get("raw_data"), ensure_ascii=False) if item.get("raw_data") is not None else None,
                        now,
                    ),
                )
                file_id_by_path[str(item.get("path") or "")] = int(cur.lastrowid)
            mapping_id_by_source: dict[str, int] = {}
            for item in mappings:
                file_id = item.get("file_id") or file_id_by_path.get(str(item.get("source_path") or ""))
                cur = conn.execute(
                    """
                    INSERT INTO organizer_mappings
                    (task_id, file_id, source_path, source_name, target_path, target_name, media_type, title, year,
                     season, episode, tmdb_id, confidence, status, reason, raw_data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        file_id,
                        item.get("source_path") or "",
                        item.get("source_name") or "",
                        item.get("target_path") or "",
                        item.get("target_name") or "",
                        item.get("media_type") or "",
                        item.get("title") or "",
                        item.get("year") or "",
                        item.get("season"),
                        item.get("episode"),
                        item.get("tmdb_id"),
                        item.get("confidence") or 0,
                        item.get("status") or "pending",
                        json.dumps(item.get("reason") or [], ensure_ascii=False),
                        json.dumps(item.get("raw_data"), ensure_ascii=False) if item.get("raw_data") is not None else None,
                        now,
                        now,
                    ),
                )
                mapping_id_by_source[str(item.get("source_path") or "")] = int(cur.lastrowid)
            for item in operations:
                mapping_id = item.get("mapping_id") or mapping_id_by_source.get(str(item.get("source_path") or ""))
                conn.execute(
                    """
                    INSERT INTO organizer_operations
                    (task_id, mapping_id, type, source_path, target_path, description, status, reason, raw_data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        mapping_id,
                        item.get("type") or "",
                        item.get("source_path") or "",
                        item.get("target_path") or "",
                        item.get("description") or "",
                        item.get("status") or "pending",
                        json.dumps(item.get("reason") or [], ensure_ascii=False),
                        json.dumps(item.get("raw_data"), ensure_ascii=False) if item.get("raw_data") is not None else None,
                        now,
                        now,
                    ),
                )
            if task_update_values:
                assignments: list[str] = []
                values: list[Any] = []
                for key, value in task_update_values.items():
                    assignments.append(f"{key} = ?")
                    values.append(
                        json.dumps(value, ensure_ascii=False)
                        if key in {"evidence", "raw_data"} and value is not None
                        else value
                    )
                assignments.extend(
                    [
                        "revision = COALESCE(revision, 1) + 1",
                        "scan_owner = NULL",
                        "scan_lease_expires_at = NULL",
                        "updated_at = ?",
                    ]
                )
                values.extend([now, int(task_id), required_status])
                revision_predicate = ""
                if expected_revision is not None:
                    revision_predicate = " AND revision = ?"
                    values.append(max(1, int(expected_revision)))
                cur = conn.execute(
                    f"""
                    UPDATE organizer_tasks
                    SET {', '.join(assignments)}
                    WHERE id = ? AND status = ?{revision_predicate}
                    """,
                    values,
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Organizer 任务状态已变化，直通计划已回滚")
            return True

    def replace_organizer_operations(self, task_id: int, operations: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self._connection_factory() as conn:
            rows = conn.execute("SELECT id, source_path FROM organizer_mappings WHERE task_id = ?", (task_id,)).fetchall()
            mapping_id_by_source = {str(row["source_path"] or ""): int(row["id"]) for row in rows}
            conn.execute("DELETE FROM organizer_operations WHERE task_id = ?", (task_id,))
            for item in operations:
                mapping_id = item.get("mapping_id") or mapping_id_by_source.get(str(item.get("source_path") or ""))
                conn.execute(
                    """
                    INSERT INTO organizer_operations
                    (task_id, mapping_id, type, source_path, target_path, description, status, reason, raw_data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        mapping_id,
                        item.get("type") or "",
                        item.get("source_path") or "",
                        item.get("target_path") or "",
                        item.get("description") or "",
                        item.get("status") or "pending",
                        json.dumps(item.get("reason") or [], ensure_ascii=False),
                        json.dumps(item.get("raw_data"), ensure_ascii=False) if item.get("raw_data") is not None else None,
                        now,
                        now,
                    ),
                )

    def update_organizer_mapping(self, mapping_id: int, **updates: Any) -> None:
        allowed = {"target_path", "target_name", "media_type", "title", "year", "season", "episode", "tmdb_id", "confidence", "status", "reason", "raw_data"}
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if key in {"reason", "raw_data"} and value is not None else value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(mapping_id)
        with self._connection_factory() as conn:
            conn.execute(f"UPDATE organizer_mappings SET {', '.join(assignments)} WHERE id = ?", values)

    def update_organizer_mappings_and_plan(
        self,
        task_id: int,
        *,
        mapping_updates: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        evidence: dict[str, Any],
        expected_status: str,
        expected_revision: int | None = None,
        task_updates: dict[str, Any] | None = None,
        clear_scan_lease: bool = False,
    ) -> bool:
        """Atomically replace all edited mappings and the derived operation plan.

        The task status/revision guard prevents an administrator edit from racing
        an apply/scan worker.  Every mapping id is validated before the first
        write, and the connection context rolls the whole transaction back if
        rebuilding operations or serialising JSON fails. Optional task fields
        let approval commit mapping status, rebuilt operations and the task
        transition under the same revision fence.
        """

        allowed = {
            "target_path",
            "target_name",
            "media_type",
            "title",
            "year",
            "season",
            "episode",
            "tmdb_id",
            "confidence",
            "status",
            "reason",
            "raw_data",
        }
        task_update_values = dict(task_updates or {})
        allowed_task_updates = {"status", "confidence", "error_message"}
        if any(key not in allowed_task_updates for key in task_update_values):
            return False

        prepared: list[tuple[int, list[str], list[Any]]] = []
        mapping_ids: list[int] = []
        now = utc_now()
        for item in mapping_updates:
            mapping_id = int(item.get("id") or 0)
            updates = item.get("updates") if isinstance(item.get("updates"), dict) else {}
            if mapping_id <= 0:
                return False
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in updates.items():
                if key not in allowed:
                    continue
                assignments.append(f"{key} = ?")
                values.append(json.dumps(value, ensure_ascii=False) if key in {"reason", "raw_data"} and value is not None else value)
            if not assignments:
                return False
            assignments.append("updated_at = ?")
            values.append(now)
            mapping_ids.append(mapping_id)
            prepared.append((mapping_id, assignments, values))
        if len(set(mapping_ids)) != len(mapping_ids):
            return False
        if not prepared and not task_update_values and not clear_scan_lease:
            return False

        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT status, revision FROM organizer_tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if task is None or str(task["status"] or "") != str(expected_status or ""):
                return False
            if expected_revision is not None and int(task["revision"] or 1) != max(1, int(expected_revision)):
                return False

            if mapping_ids:
                placeholders = ",".join("?" for _ in mapping_ids)
                rows = conn.execute(
                    f"SELECT id FROM organizer_mappings WHERE task_id = ? AND id IN ({placeholders})",
                    [int(task_id), *mapping_ids],
                ).fetchall()
                if {int(row["id"]) for row in rows} != set(mapping_ids):
                    return False

            for mapping_id, assignments, values in prepared:
                cur = conn.execute(
                    f"UPDATE organizer_mappings SET {', '.join(assignments)} WHERE id = ? AND task_id = ?",
                    [*values, mapping_id, int(task_id)],
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"Organizer 映射 #{mapping_id} 原子更新失败")

            mapping_rows = conn.execute(
                "SELECT id, source_path FROM organizer_mappings WHERE task_id = ?",
                (int(task_id),),
            ).fetchall()
            mapping_id_by_source = {str(row["source_path"] or ""): int(row["id"]) for row in mapping_rows}
            conn.execute("DELETE FROM organizer_operations WHERE task_id = ?", (int(task_id),))
            for item in operations:
                mapping_id = item.get("mapping_id") or mapping_id_by_source.get(str(item.get("source_path") or ""))
                conn.execute(
                    """
                    INSERT INTO organizer_operations
                    (task_id, mapping_id, type, source_path, target_path, description, status, reason, raw_data, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(task_id),
                        mapping_id,
                        item.get("type") or "",
                        item.get("source_path") or "",
                        item.get("target_path") or "",
                        item.get("description") or "",
                        item.get("status") or "pending",
                        json.dumps(item.get("reason") or [], ensure_ascii=False),
                        json.dumps(item.get("raw_data"), ensure_ascii=False) if item.get("raw_data") is not None else None,
                        now,
                        now,
                    ),
                )

            task_assignments = ["evidence = ?"]
            task_values: list[Any] = [json.dumps(evidence or {}, ensure_ascii=False)]
            for key, value in task_update_values.items():
                task_assignments.append(f"{key} = ?")
                task_values.append(value)
            if clear_scan_lease:
                task_assignments.extend(["scan_owner = NULL", "scan_lease_expires_at = NULL"])
            task_assignments.extend(["revision = COALESCE(revision, 1) + 1", "updated_at = ?"])
            task_values.append(now)
            task_values.extend([int(task_id), str(expected_status or "")])
            revision_predicate = ""
            if expected_revision is not None:
                revision_predicate = " AND revision = ?"
                task_values.append(max(1, int(expected_revision)))
            cur = conn.execute(
                f"""
                UPDATE organizer_tasks
                SET {', '.join(task_assignments)}
                WHERE id = ? AND status = ?{revision_predicate}
                """,
                task_values,
            )
            if cur.rowcount != 1:
                raise RuntimeError("Organizer 任务状态已变化，原子批量更新已回滚")
            return True

    def create_organizer_run(
        self,
        task_id: int,
        status: str = "running",
        *,
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> int:
        now = utc_now()
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
            INSERT INTO organizer_runs
                (task_id, status, owner_id, heartbeat_at, lease_expires_at, task_revision, started_at)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT revision FROM organizer_tasks WHERE id = ?), 1), ?)
                """,
                (
                    task_id,
                    status,
                    str(owner_id or "").strip() or None,
                    now,
                    utc_seconds_from_now(lease_seconds) if status == "running" else None,
                    int(task_id),
                    now,
                ),
            )
            return int(cur.lastrowid)

    def claim_organizer_run(
        self,
        task_id: int,
        *,
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> tuple[int | None, dict[str, Any] | None]:
        """Atomically claim one Organizer execution for a task.

        ``start_apply_task`` can be served by multiple application processes, so
        an in-memory set cannot prevent both processes from observing the same
        ready task and creating separate runs.  The immediate transaction keeps
        the task state transition and run creation in one SQLite write lock.
        """

        task_id = int(task_id)
        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_expired_organizer_runs_in_connection(
                conn,
                now=now,
                task_id=task_id,
            )
            task = conn.execute(
                "SELECT id, status, revision FROM organizer_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                return None, {"task_id": task_id, "task_status": "missing"}

            task_status = str(task["status"] or "")
            active = conn.execute(
                """
                SELECT id, task_id, status, owner_id, heartbeat_at, lease_expires_at, task_revision, started_at
                FROM organizer_runs
                WHERE task_id = ? AND status = 'running'
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if active is not None:
                current = dict(active)
                current["task_status"] = task_status
                return None, current

            # ``executing`` remains a claim even in the narrow completion window
            # after its run is finished but before the task is marked done.  It
            # also protects a crashed pre-thread-start run until startup recovery
            # explicitly resolves it.
            if task_status in {"executing", "done", "cancelled", "skipped"}:
                return None, {"task_id": task_id, "task_status": task_status}

            cur = conn.execute(
                """
                INSERT INTO organizer_runs
                (task_id, status, owner_id, heartbeat_at, lease_expires_at, task_revision, started_at)
                VALUES (?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(owner_id or "").strip() or None,
                    now,
                    utc_seconds_from_now(lease_seconds),
                    max(1, int(task["revision"] or 1)),
                    now,
                ),
            )
            task_cur = conn.execute(
                """
                UPDATE organizer_tasks
                SET status = 'executing', error_message = '', updated_at = ?
                WHERE id = ? AND revision = ? AND status = ?
                """,
                (now, task_id, max(1, int(task["revision"] or 1)), task_status),
            )
            if task_cur.rowcount != 1:
                conn.rollback()
                return None, {"task_id": task_id, "task_status": "changed"}
            return int(cur.lastrowid), None

    def renew_organizer_run(self, run_id: int, owner_id: str, *, lease_seconds: int = 120) -> bool:
        now = utc_now()
        expires_at = utc_seconds_from_now(lease_seconds)
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE organizer_runs
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'running' AND owner_id = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM organizer_tasks t
                      WHERE t.id = organizer_runs.task_id
                        AND t.status = 'executing'
                        AND COALESCE(t.revision, 1) = COALESCE(organizer_runs.task_revision, 1)
                  )
                """,
                (now, expires_at, int(run_id), str(owner_id), now),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "UPDATE organizer_locks SET expires_at = ? WHERE run_id = ?",
                (expires_at, int(run_id)),
            )
            return True

    def owns_organizer_run(self, run_id: int, owner_id: str) -> bool:
        now = utc_now()
        with self._connection_factory() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM organizer_runs
                JOIN organizer_tasks t ON t.id = organizer_runs.task_id
                WHERE organizer_runs.id = ? AND organizer_runs.status = 'running' AND organizer_runs.owner_id = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                  AND t.status = 'executing'
                  AND COALESCE(t.revision, 1) = COALESCE(organizer_runs.task_revision, 1)
                """,
                (int(run_id), str(owner_id), now),
            ).fetchone()
            return row is not None

    def update_organizer_run(
        self,
        run_id: int,
        status: str,
        *,
        summary: Any = None,
        undo_data: Any = None,
        error_message: str = "",
        owner_id: str | None = None,
    ) -> bool:
        now = utc_now()
        values: list[Any] = [
            status,
            now,
            now,
            json.dumps(summary, ensure_ascii=False) if summary is not None else None,
            json.dumps(undo_data, ensure_ascii=False) if undo_data is not None else None,
            error_message,
            int(run_id),
        ]
        where = "id = ?"
        if owner_id is not None:
            where += " AND status = 'running' AND owner_id = ? AND lease_expires_at IS NOT NULL AND lease_expires_at > ?"
            values.extend([str(owner_id), now])
        with self._connection_factory() as conn:
            cur = conn.execute(
                f"""
                UPDATE organizer_runs
                SET status = ?, finished_at = ?, heartbeat_at = ?, lease_expires_at = NULL,
                    summary = ?, undo_data = ?, error_message = ?
                WHERE {where}
                """,
                values,
            )
            return cur.rowcount == 1

    def finalize_organizer_run_and_task(
        self,
        run_id: int,
        task_id: int,
        *,
        owner_id: str,
        run_status: str,
        task_status: str,
        summary: Any = None,
        undo_data: Any = None,
        error_message: str = "",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> bool:
        """Fence and persist terminal run/task state in one transaction."""

        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE organizer_runs
                SET status = ?, finished_at = ?, heartbeat_at = ?, lease_expires_at = NULL,
                    summary = ?, undo_data = ?, error_message = ?
                WHERE id = ? AND task_id = ? AND status = 'running' AND owner_id = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM organizer_tasks t
                      WHERE t.id = organizer_runs.task_id
                        AND t.status = 'executing'
                        AND COALESCE(t.revision, 1) = COALESCE(organizer_runs.task_revision, 1)
                  )
                """,
                (
                    str(run_status),
                    now,
                    now,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    json.dumps(undo_data, ensure_ascii=False) if undo_data is not None else None,
                    str(error_message or ""),
                    int(run_id),
                    int(task_id),
                    str(owner_id),
                    now,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            task_cur = conn.execute(
                """
                UPDATE organizer_tasks
                SET status = ?, error_message = ?, evidence = COALESCE(?, evidence),
                    raw_data = COALESCE(?, raw_data), updated_at = ?
                WHERE id = ? AND status = 'executing'
                  AND revision = (
                      SELECT task_revision FROM organizer_runs WHERE id = ? AND task_id = ?
                  )
                """,
                (
                    str(task_status),
                    str(error_message or ""),
                    json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
                    json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None,
                    now,
                    int(task_id),
                    int(run_id),
                    int(task_id),
                ),
            )
            if task_cur.rowcount != 1:
                conn.rollback()
                return False
            conn.execute("DELETE FROM organizer_locks WHERE run_id = ?", (int(run_id),))
            return True

    def count_organizer_runs(self) -> int:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM organizer_runs").fetchone()
            return int(row["total"] if row else 0)

    def list_organizer_runs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            rows = conn.execute("SELECT * FROM organizer_runs ORDER BY id DESC LIMIT ? OFFSET ?", (max(1, int(limit or 100)), max(0, int(offset or 0)))).fetchall()
        return [_decode_json_fields(dict(row), ("summary", "undo_data")) for row in rows]

    def list_organizer_runs_by_task_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        normalized_ids = sorted({int(task_id) for task_id in task_ids if int(task_id or 0) > 0})
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"SELECT * FROM organizer_runs WHERE task_id IN ({placeholders}) ORDER BY id ASC",
                normalized_ids,
            ).fetchall()
        return [_decode_json_fields(dict(row), ("summary", "undo_data")) for row in rows]

    def update_organizer_operation(self, operation_id: int, **updates: Any) -> None:
        allowed = {"run_id", "status", "error_message", "undo_data", "raw_data"}
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if key in {"undo_data", "raw_data"} and value is not None else value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(operation_id)
        with self._connection_factory() as conn:
            conn.execute(f"UPDATE organizer_operations SET {', '.join(assignments)} WHERE id = ?", values)

    def add_organizer_ai_suggestion(self, task_id: int, provider: str, model: str, prompt: Any, response: Any, parsed: Any) -> int:
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO organizer_ai_suggestions (task_id, provider, model, prompt, response, parsed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    provider,
                    model,
                    json.dumps(prompt, ensure_ascii=False) if prompt is not None else None,
                    json.dumps(response, ensure_ascii=False) if response is not None else None,
                    json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def add_organizer_tmdb_match(self, task_id: int, query: str, media_type: str, item: dict[str, Any], score: float = 0) -> int:
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO organizer_tmdb_matches (task_id, query, media_type, tmdb_id, title, year, score, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    query,
                    media_type,
                    item.get("id"),
                    item.get("title") or item.get("name") or "",
                    item.get("year") or "",
                    score,
                    json.dumps(item, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def acquire_organizer_lock(self, lock_key: str, *, task_id: int | None = None, run_id: int | None = None, owner: str = "organizer") -> bool:
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_expired_organizer_runs_in_connection(conn, now=utc_now())
            expires_at: str | None = None
            if run_id:
                now = utc_now()
                run = conn.execute(
                    """
                    SELECT lease_expires_at
                    FROM organizer_runs
                    WHERE id = ? AND status = 'running' AND owner_id = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (int(run_id), str(owner), now),
                ).fetchone()
                if run is None:
                    return False
                expires_at = str(run["lease_expires_at"] or "") or None
            try:
                conn.execute(
                    """
                    INSERT INTO organizer_locks (lock_key, task_id, run_id, owner, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (lock_key, task_id, run_id, owner, expires_at, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def release_organizer_locks(self, *, task_id: int | None = None, run_id: int | None = None, lock_keys: list[str] | None = None) -> None:
        where = []
        values: list[Any] = []
        if lock_keys:
            placeholders = ",".join(["?"] * len(lock_keys))
            where.append(f"lock_key IN ({placeholders})")
            values.extend(lock_keys)
        if task_id:
            where.append("task_id = ?")
            values.append(task_id)
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        if not where:
            return
        with self._connection_factory() as conn:
            conn.execute(f"DELETE FROM organizer_locks WHERE {' AND '.join(where)}", values)

    def recover_stale_organizer_runs(
        self,
        *,
        older_than_minutes: int = 30,
        owner_id: str = "",
        message: str = "服务重启后清理遗留 Organizer 运行锁",
    ) -> dict[str, Any]:
        """Atomically recover only Organizer runs whose durable lease expired.

        The legacy arguments remain for compatibility. Neither owner mismatch
        nor an old ``started_at`` value proves that a live long-running apply is
        stale, so recovery is now based exclusively on ``lease_expires_at``.
        """

        del older_than_minutes, owner_id
        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._recover_expired_organizer_runs_in_connection(
                conn,
                now=now,
                message=message,
            )

    def _recover_expired_organizer_runs_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        task_id: int | None = None,
        message: str = "Organizer 运行租约已过期，已中止遗留执行",
    ) -> dict[str, Any]:
        where = [
            "r.status = 'running'",
            "r.lease_expires_at IS NOT NULL",
            "r.lease_expires_at <= ?",
        ]
        values: list[Any] = [now]
        if task_id is not None:
            where.append("r.task_id = ?")
            values.append(int(task_id))
        rows = conn.execute(
            f"""
            SELECT r.id, r.task_id, r.owner_id, r.task_revision,
                   t.job_id, t.title, t.status AS task_status
            FROM organizer_runs r
            LEFT JOIN organizer_tasks t ON t.id = r.task_id
            WHERE {' AND '.join(where)}
            """,
            values,
        ).fetchall()
        run_ids = [int(row["id"]) for row in rows if row["id"]]
        task_ids = sorted({int(row["task_id"]) for row in rows if row["task_id"]})
        if not run_ids:
            return {"count": 0, "run_ids": [], "task_ids": [], "jobs": []}
        affected_jobs = [
            {
                "run_id": int(row["id"]),
                "task_id": int(row["task_id"] or 0),
                "owner_id": row["owner_id"] or "",
                "job_id": int(row["job_id"] or 0),
                "title": row["title"] or "",
                "task_status": row["task_status"] or "",
            }
            for row in rows
            if int(row["job_id"] or 0) > 0
        ]
        placeholders = ",".join(["?"] * len(run_ids))
        conn.execute(
            f"""
            UPDATE organizer_runs
            SET status = 'failed', finished_at = ?, heartbeat_at = ?, lease_expires_at = NULL,
                error_message = ?
            WHERE id IN ({placeholders}) AND status = 'running'
            """,
            [now, now, message, *run_ids],
        )
        conn.execute(f"DELETE FROM organizer_locks WHERE run_id IN ({placeholders})", run_ids)
        if task_ids:
            task_placeholders = ",".join(["?"] * len(task_ids))
            conn.execute(
                f"""
                UPDATE organizer_tasks
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id IN ({task_placeholders}) AND status = 'executing'
                  AND EXISTS (
                      SELECT 1 FROM organizer_runs expired
                      WHERE expired.id IN ({placeholders})
                        AND expired.task_id = organizer_tasks.id
                        AND COALESCE(expired.task_revision, 1) = COALESCE(organizer_tasks.revision, 1)
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM organizer_runs active
                      WHERE active.task_id = organizer_tasks.id AND active.status = 'running'
                  )
                """,
                [message, now, *task_ids, *run_ids],
            )
        return {"count": len(run_ids), "run_ids": run_ids, "task_ids": task_ids, "jobs": affected_jobs}
