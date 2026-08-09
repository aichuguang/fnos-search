from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..planning.job_plan import plan_path, plan_strategy, validated_v1_execution_plan_from_job
from ..time_utils import utc_now_iso

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]


def utc_now() -> str:
    return utc_now_iso()


def _normalize_match_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", str(value or "").lower())


def _rclone_category_match_values(value: Any) -> tuple[str, ...]:
    normalized = _normalize_match_text(value)
    groups = {
        "movie": ("movie", "电影", "离线电影"),
        "tv": ("tv", "电视剧", "剧集", "离线剧集", "离线电视剧"),
        "anime": ("anime", "动漫", "动画", "离线动漫", "离线动画"),
        "variety": ("variety", "综艺", "离线综艺"),
        "other": ("other", "其他", "离线其他"),
    }
    for aliases in groups.values():
        if normalized in {_normalize_match_text(alias) for alias in aliases}:
            return aliases
    text = str(value or "").strip()
    return (text,) if text else ()


def _rclone_job_id_from_paths(
    *values: Any,
    category_values: tuple[str, ...] = (),
) -> tuple[int | None, bool]:
    ids: set[int] = set()
    invalid = False
    authoritative = False
    category_tokens = {
        _normalize_match_text(value)
        for value in category_values
        if _normalize_match_text(value)
    }
    for value in values:
        parts = [part for part in str(value or "").replace("\\", "/").split("/") if part]
        match: re.Match[str] | None = None
        match_index = -1
        for index, part in enumerate(parts):
            match = re.fullmatch(r"job-(\d+)", part, flags=re.IGNORECASE)
            if match:
                match_index = index
                break
        if match is None:
            continue
        structural = bool(
            match_index > 0
            and _normalize_match_text(parts[match_index - 1]) in category_tokens
        )
        authoritative = authoritative or structural
        try:
            job_id = int(match.group(1))
        except (TypeError, ValueError):
            invalid = invalid or structural
            continue
        if 0 < job_id <= 999_999_999:
            ids.add(job_id)
        else:
            invalid = invalid or structural
    if invalid or (authoritative and len(ids) > 1):
        return -1, True
    if not ids:
        return None, authoritative
    if len(ids) > 1:
        return (None, False) if not authoritative else (-1, True)
    return next(iter(ids)), authoritative


def _job_owns_staging_directory(job: dict[str, Any] | None, job_id: int) -> bool:
    """Return whether a job owns callback-visible V1 transfer directories.

    Callback recovery must never infer ownership from the removed
    ``raw_data.staging_plan`` field.  Invalid/legacy jobs are deliberately
    treated as unmatched and are sent to manual review by their caller.
    """
    try:
        plan = _validated_plan(job)
    except Exception:  # noqa: BLE001 - legacy/invalid plans are not executable
        return False
    try:
        planned_job_id = int(plan.get("job_id") or 0)
    except (TypeError, ValueError):
        return False
    return plan_strategy(plan) in {"rclone_copy", "relay_upload_mobile"} and planned_job_id == int(job_id)


def _job_matches_staging_callback_paths(
    job: dict[str, Any] | None,
    *values: Any,
) -> tuple[bool, bool]:
    try:
        plan = _validated_plan(job)
    except Exception:  # noqa: BLE001 - old jobs cannot be callback targets
        return False, False
    strategy = plan_strategy(plan)
    if strategy not in {"rclone_copy", "relay_upload_mobile"}:
        return False, False
    # Only task-scoped V1 paths are callback roots.  Never include a category
    # final root because it would make an unrelated historical file appear to
    # belong to the current job.  Relay callbacks use their controlled local
    # temp root instead of the rclone-only destination staging root.
    root_keys = (
        ("source_landing", "destination_staging")
        if strategy == "rclone_copy"
        else ("source_landing", "local_temp")
    )
    roots = [
        _normalize_callback_path(plan_path(plan, key).get("path"))
        for key in root_keys
    ]
    roots = [root for root in roots if root]
    if not roots:
        return False, False
    paths = [_normalize_callback_path(value) for value in values]
    matched = any(
        _callback_path_is_same_or_child(path, root)
        for path in paths
        if path
        for root in roots
    )
    return matched, True


def _validated_plan(job: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a plan loaded from the immutable execution-plan table.

    The optional private field is populated only inside this repository while
    matching a callback and is never returned to API callers.  Production
    lookup always supplies the database-frozen copy; job ``raw_data`` is not a
    plan source.
    """

    value = dict(job or {})
    return validated_v1_execution_plan_from_job(value)


def _normalize_callback_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/").casefold()


def _callback_path_is_same_or_child(path: str, root: str) -> bool:
    return bool(path and root and (path == root or path.startswith(f"{root}/")))


def _match_score(left: Any, right: Any) -> int:
    a, b = _normalize_match_text(left), _normalize_match_text(right)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 80
    common = len(set(a) & set(b))
    return int(common * 100 / max(len(set(a) | set(b)), 1))


def _decode_json_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for field in fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


class RcloneRepository:
    """Persists rclone transfer runs and file events, and resolves the import
    job a transfer callback belongs to."""

    def __init__(self, connection_factory: ConnectionFactory, row_decoder: RowDecoder) -> None:
        self._connection_factory = connection_factory
        self._row_decoder = row_decoder

    def find_job_for_rclone_callback(self, category: str, filename: str, source_path: str = "", target_path: str = "") -> dict[str, Any] | None:
        # A callback may only attach to a job that has entered the V1 transfer
        # lifecycle.  created/submitted were legacy recovery states and
        # allowed an unrelated asynchronous callback to advance a job before
        # its durable execution worker had claimed it.
        statuses = ("waiting_transfer", "waiting_openlist", "transferring", "organizing")
        # V1 source providers only.  Removed cloud139/UC aliases are not
        # eligible for automatic callback recovery and must be reviewed.
        source_types = ("mobile", "quark", "tianyi", "sixpan", "local", "generic")
        category_values = _rclone_category_match_values(category)
        if not category_values:
            return None
        explicit_job_id, authoritative_job_path = _rclone_job_id_from_paths(
            source_path,
            target_path,
            category_values=category_values,
        )
        if explicit_job_id == -1:
            return None
        if explicit_job_id:
            with self._connection_factory() as conn:
                row = conn.execute(
                    f"""
                    SELECT * FROM import_jobs
                    WHERE id = ?
                      AND (category IN ({','.join(['?'] * len(category_values))})
                           OR category_label IN ({','.join(['?'] * len(category_values))}))
                      AND source_type IN ({','.join(['?'] * len(source_types))})
                      AND status IN ({','.join(['?'] * len(statuses))})
                    LIMIT 1
                    """,
                    (
                        explicit_job_id,
                        *category_values,
                        *category_values,
                        *source_types,
                        *statuses,
                    ),
                ).fetchone()
            matched = self._row_decoder(row) if row is not None else None
            self._attach_canonical_plan(matched)
            owns_staging = _job_owns_staging_directory(matched, explicit_job_id)
            path_matches, has_planned_roots = _job_matches_staging_callback_paths(
                matched,
                source_path,
                target_path,
            )
            if owns_staging and (path_matches or (authoritative_job_path and not has_planned_roots)):
                return self._public_job(matched)
            if authoritative_job_path:
                return None
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM import_jobs
                WHERE (category IN ({','.join(['?'] * len(category_values))})
                       OR category_label IN ({','.join(['?'] * len(category_values))}))
                  AND source_type IN ({','.join(['?'] * len(source_types))})
                  AND status IN ({','.join(['?'] * len(statuses))})
                ORDER BY id DESC
                LIMIT 50
                """,
                (*category_values, *category_values, *source_types, *statuses),
            ).fetchall()
        candidates = [self._row_decoder(row) for row in rows if row is not None]
        for candidate in candidates:
            self._attach_canonical_plan(candidate)
        # Never use title/category similarity as an ownership proof.  A V1
        # callback must point into one of the job's frozen task directories;
        # legacy or invalid plans are excluded before scoring.
        eligible: list[dict[str, Any]] = []
        for candidate in candidates:
            if not candidate or not _job_owns_staging_directory(candidate, int(candidate.get("id") or 0)):
                continue
            matched, has_roots = _job_matches_staging_callback_paths(candidate, source_path, target_path)
            if has_roots and matched:
                eligible.append(candidate)
        candidates = eligible
        if not candidates:
            return None
        if len(candidates) == 1:
            return self._public_job(candidates[0])

        best: tuple[int, dict[str, Any] | None] = (0, None)
        normalized_file = _normalize_match_text(" ".join([filename, source_path, target_path]))
        for candidate in candidates:
            title = str(candidate.get("title") or "")
            target = str(candidate.get("target_path") or "")
            score = max(
                _match_score(_normalize_match_text(title), normalized_file),
                _match_score(_normalize_match_text(target), normalized_file),
            )
            if score > best[0]:
                best = (score, candidate)
        if best[0] >= 4:
            return self._public_job(best[1])
        return None

    def _attach_canonical_plan(self, job: dict[str, Any] | None) -> None:
        if not isinstance(job, dict):
            return
        try:
            job_id = int(job.get("id") or 0)
        except (TypeError, ValueError):
            return
        if job_id <= 0:
            return
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT plan_json, plan_hash FROM import_execution_plans WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return
        encoded = str(row["plan_json"] or "")
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["plan_hash"] or ""):
            return
        try:
            plan = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if isinstance(plan, dict):
            job["_canonical_execution_plan"] = plan

    @staticmethod
    def _public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(job, dict):
            return job
        result = dict(job)
        result.pop("_canonical_execution_plan", None)
        return result

    def create_rclone_run(self, trigger_reason: str) -> int:
        now = utc_now()
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO rclone_runs (trigger_reason, status, started_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (trigger_reason, "running", now, now),
            )
            return int(cur.lastrowid)

    def create_scoped_rclone_run(
        self,
        *,
        trigger_reason: str,
        operation_key: str,
        job_id: int,
        copy_descriptor: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Create or reconcile one durable task-scoped V1 run.

        ``operation_key`` is the idempotency fence.  A failed rclone copy may
        be retried in-place because ``rclone copy`` against the same frozen
        source and destination is idempotent; successful, active and cancelled
        rows are never replaced with a second run.
        """

        key = str(operation_key or "").strip()
        normalized_job_id = int(job_id or 0)
        if not key or normalized_job_id <= 0:
            raise ValueError("scoped rclone run requires operation_key and job_id")
        descriptor_json = json.dumps(
            copy_descriptor if isinstance(copy_descriptor, dict) else {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM rclone_runs WHERE operation_key=? LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                existing = self._decode_run(row)
                if int(existing.get("job_id") or 0) != normalized_job_id:
                    raise ValueError("rclone operation_key already belongs to another job")
                existing_json = json.dumps(
                    existing.get("copy_descriptor") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if existing_json != descriptor_json:
                    raise ValueError("rclone operation_key descriptor mismatch")
                if str(existing.get("status") or "").lower() == "failed":
                    conn.execute(
                        "UPDATE rclone_runs SET status='queued', trigger_reason=?, "
                        "finished_at=NULL, exit_code=NULL, error_message='', updated_at=? "
                        "WHERE id=? AND status='failed'",
                        (str(trigger_reason or "v1_transfer"), now, int(existing["id"])),
                    )
                    row = conn.execute(
                        "SELECT * FROM rclone_runs WHERE id=?", (int(existing["id"]),)
                    ).fetchone()
                    existing = self._decode_run(row)
                return existing, False
            cursor = conn.execute(
                """
                INSERT INTO rclone_runs(
                    trigger_reason, operation_key, job_id, copy_descriptor_json,
                    status, started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    str(trigger_reason or "v1_transfer"),
                    key,
                    normalized_job_id,
                    descriptor_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM rclone_runs WHERE id=?", (int(cursor.lastrowid),)
            ).fetchone()
            return self._decode_run(row), True

    def find_rclone_run_by_operation_key(self, operation_key: str) -> dict[str, Any] | None:
        key = str(operation_key or "").strip()
        if not key:
            return None
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT * FROM rclone_runs WHERE operation_key=? LIMIT 1", (key,)
            ).fetchone()
        return self._decode_run(row) if row is not None else None

    def list_recoverable_scoped_rclone_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            rows = conn.execute(
                """
                SELECT * FROM rclone_runs
                WHERE operation_key IS NOT NULL AND operation_key <> ''
                  AND status IN ('queued', 'starting', 'running', 'recovering', 'cancel_pending')
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, int(limit or 100)),),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def mark_scoped_rclone_run_started(self, run_id: int) -> bool:
        now = utc_now()
        with self._connection_factory() as conn:
            cursor = conn.execute(
                "UPDATE rclone_runs SET status='running', started_at=?, updated_at=? "
                "WHERE id=? AND status IN ('queued','starting','recovering','running')",
                (now, now, int(run_id)),
            )
            return cursor.rowcount == 1

    def mark_scoped_rclone_run_status(
        self,
        run_id: int,
        status: str,
        *,
        error_message: str = "",
        terminal: bool = False,
    ) -> bool:
        normalized = str(status or "").strip().lower()
        if normalized not in {
            "queued",
            "starting",
            "running",
            "recovering",
            "cancel_pending",
            "cancelled",
            "failed",
            "success",
        }:
            raise ValueError(f"unsupported rclone run status: {status}")
        now = utc_now()
        with self._connection_factory() as conn:
            cursor = conn.execute(
                "UPDATE rclone_runs SET status=?, error_message=?, updated_at=?, "
                "finished_at=CASE WHEN ? THEN ? ELSE finished_at END "
                "WHERE id=?",
                (
                    normalized,
                    str(error_message or ""),
                    now,
                    int(bool(terminal)),
                    now,
                    int(run_id),
                ),
            )
            return cursor.rowcount == 1

    def update_rclone_run(self, run_id: int, status: str, exit_code: int | None = None, error_message: str = "") -> None:
        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE rclone_runs
                SET status = ?, exit_code = ?, error_message = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, exit_code, error_message, now, now, run_id),
            )

    def add_rclone_event(self, run_id: int | None, level: str, message: str, raw_data: Any = None) -> int:
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO rclone_events (run_id, level, message, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, level, message, raw_text, utc_now()),
            )
            return int(cur.lastrowid)

    def count_rclone_runs(self) -> int:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM rclone_runs").fetchone()
            return int(row["total"] if row else 0)

    def list_rclone_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            rows = conn.execute("SELECT * FROM rclone_runs ORDER BY id DESC LIMIT ? OFFSET ?", (max(1, int(limit or 50)), max(0, int(offset or 0)))).fetchall()
            return [self._decode_run(row) for row in rows]

    def get_rclone_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT * FROM rclone_runs WHERE id = ?", (int(run_id),)).fetchone()
        return self._decode_run(row) if row is not None else None

    @staticmethod
    def _decode_run(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        raw = item.pop("copy_descriptor_json", "")
        try:
            descriptor = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            descriptor = {}
        item["copy_descriptor"] = descriptor if isinstance(descriptor, dict) else {}
        return item

    def list_rclone_events(self, run_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._connection_factory() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM rclone_events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rclone_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("raw_data"):
                try:
                    item["raw_data"] = json.loads(item["raw_data"])
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return result

    def add_rclone_file_event(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str,
        level: str,
        category: str = "",
        filename: str,
        source_path: str = "",
        target_path: str = "",
        message: str = "",
        raw_data: Any = None,
    ) -> int:
        raw_text = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO rclone_file_events
                (run_id, job_id, status, level, category, filename, source_path, target_path, message, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, job_id, status, level, category, filename, source_path, target_path, message, raw_text, utc_now()),
            )
            return int(cur.lastrowid)

    def get_rclone_file_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT * FROM rclone_file_events WHERE id = ?", (event_id,)).fetchone()
        return self._row_decoder(row)

    def attach_rclone_file_event_to_job(self, event_id: int, job_id: int) -> bool:
        with self._connection_factory() as conn:
            cur = conn.execute(
                "UPDATE rclone_file_events SET job_id = ? WHERE id = ? AND job_id IS NULL",
                (int(job_id), int(event_id)),
            )
            return cur.rowcount > 0

    def list_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where_sql, values = self._rclone_file_filter_sql(run_id=run_id, job_id=job_id, status=status, category=category)
        values = list(values)
        values.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"SELECT * FROM rclone_file_events {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("raw_data"):
                try:
                    item["raw_data"] = json.loads(item["raw_data"])
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return result

    def list_unmatched_rclone_file_events(
        self,
        *,
        limit: int = 500,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read one stable keyset page of file events that still lack a job."""

        where = ["job_id IS NULL"]
        values: list[Any] = []
        if before_id is not None:
            normalized_before_id = int(before_id or 0)
            if normalized_before_id <= 0:
                return []
            where.append("id < ?")
            values.append(normalized_before_id)
        values.append(max(1, min(int(limit or 500), 1000)))
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM rclone_file_events
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [_decode_json_fields(dict(row), ("raw_data",)) for row in rows]

    def list_all_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        batch_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read a complete event set without the admin-list 1000-row ceiling."""

        page_size = max(1, min(int(batch_size or 1000), 1000))
        offset = 0
        result: list[dict[str, Any]] = []
        while True:
            page = self.list_rclone_file_events(
                run_id=run_id,
                job_id=job_id,
                status=status,
                category=category,
                limit=page_size,
                offset=offset,
            )
            result.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return result

    def count_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> int:
        where_sql, values = self._rclone_file_filter_sql(run_id=run_id, job_id=job_id, status=status, category=category)
        with self._connection_factory() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM rclone_file_events {where_sql}", values).fetchone()
            return int(row["total"] if row else 0)

    @staticmethod
    def _rclone_file_filter_sql(
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> tuple[str, list[Any]]:
        where = []
        values: list[Any] = []
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        if job_id:
            where.append("job_id = ?")
            values.append(job_id)
        if status:
            where.append("status = ?")
            values.append(status)
        if category:
            where.append("category = ?")
            values.append(category)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        return where_sql, values
