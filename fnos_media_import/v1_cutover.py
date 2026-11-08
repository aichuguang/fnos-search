"""V1 任务契约硬切换及后续加列升级的分析、迁移与离线 CLI。

该模块只处理 ``import_jobs`` 的任务语义字段和与被清理任务直接关联的
执行数据。资源、搜索缓存、追更来源等表中的 ``source_type`` 仍表示资源
分类，不属于本次字段改名范围。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config_persistence import ADVANCED_CONFIG_KEY, migrate_stored_advanced_config
from .planning.job_plan import (
    canonical_plan_json,
    validated_v1_execution_plan_from_job,
)
from .time_utils import utc_now_iso


CUTOVER_VERSION = 29
CUTOVER_NAME = "v1_job_contract_hard_cutover"
CUTOVER_MESSAGE = "V1 硬切换已清理旧任务"
STRM_PENDING_CUTOVER_MESSAGE = (
    "V1 硬切换已退役旧 STRM 等待状态，任务已转为人工复核"
)
RETIRED_WORKER_TASK_TYPES = frozenset({"rclone_repair"})
ORGANIZER_WORKER_TASK_TYPES = frozenset({"organizer_process", "organizer_apply"})
ACTIVE_WORKER_TASK_STATUSES = frozenset({"pending", "running"})


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _id_set_hash(values: Iterable[int]) -> str:
    normalized = sorted({int(value) for value in values})
    return _sha256_text(json.dumps(normalized, separators=(",", ":")))


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _rows(connection: sqlite3.Connection, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(values)).fetchall()]


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


@dataclass(frozen=True)
class CutoverAnalysis:
    kept_sources: dict[int, tuple[str, str]]
    deleted_job_ids: tuple[int, ...]
    invalid_reasons: dict[str, int]
    related_ids: dict[str, tuple[int, ...]]
    cleanup_counts: dict[str, int]
    advanced_config_removed_paths: tuple[str, ...] = ()

    def summary(self, *, dry_run: bool, backup_path: str = "") -> dict[str, Any]:
        kept_ids = tuple(sorted(self.kept_sources))
        deleted_ids = tuple(sorted(self.deleted_job_ids))
        return {
            "migration": CUTOVER_NAME,
            "schema_version": CUTOVER_VERSION,
            "dry_run": bool(dry_run),
            "backup_path": str(backup_path or ""),
            "jobs": {
                "kept": len(kept_ids),
                "deleted": len(deleted_ids),
                "kept_job_ids_sha256": _id_set_hash(kept_ids),
                "deleted_job_ids_sha256": _id_set_hash(deleted_ids),
                "invalid_reasons": dict(sorted(self.invalid_reasons.items())),
            },
            "cleanup": dict(sorted(self.cleanup_counts.items())),
        }


class V1JobContractHardCutover:
    """分析并执行 v29 任务契约迁移。"""

    def analyze(self, connection: sqlite3.Connection) -> CutoverAnalysis:
        if not _table_exists(connection, "import_jobs"):
            removed_paths, _cleaned = self._advanced_config_cleanup(connection)
            return CutoverAnalysis(
                {},
                (),
                {},
                {},
                {"advanced_config_fields_removed": len(removed_paths)},
                removed_paths,
            )

        columns = _table_columns(connection, "import_jobs")
        jobs = _rows(connection, "SELECT * FROM import_jobs ORDER BY id")
        plan_rows = (
            _rows(connection, "SELECT * FROM import_execution_plans ORDER BY job_id")
            if _table_exists(connection, "import_execution_plans")
            else []
        )
        plans = {int(row["job_id"]): row for row in plan_rows}
        kept: dict[int, tuple[str, str]] = {}
        deleted: list[int] = []
        reasons: Counter[str] = Counter()

        # A fresh v29 schema has no legacy columns. Existing rows still pass
        # through the same immutable-plan validation; this keeps repeated
        # analysis deterministic and catches manual database corruption.
        already_cut_over = {
            "source_provider_kind",
            "source_form",
        }.issubset(columns) and "source_type" not in columns and "target_route" not in columns

        for row in jobs:
            job_id = int(row["id"])
            reason, source = self._validate_job(row, plans.get(job_id), already_cut_over)
            if reason:
                deleted.append(job_id)
                reasons[reason] += 1
            else:
                kept[job_id] = source

        related_ids = self._related_ids(connection, set(deleted))
        cleanup_counts = {
            key: len(values)
            for key, values in related_ids.items()
        }
        removed_paths, _cleaned = self._advanced_config_cleanup(connection)
        cleanup_counts["advanced_config_fields_removed"] = len(removed_paths)
        return CutoverAnalysis(
            kept_sources=kept,
            deleted_job_ids=tuple(sorted(deleted)),
            invalid_reasons=dict(reasons),
            related_ids=related_ids,
            cleanup_counts=cleanup_counts,
            advanced_config_removed_paths=removed_paths,
        )

    @staticmethod
    def _validate_job(
        job: dict[str, Any],
        plan_row: dict[str, Any] | None,
        already_cut_over: bool,
    ) -> tuple[str, tuple[str, str]]:
        if _positive_int(job.get("plan_version")) != 1:
            return "job_plan_version", ("", "")
        if not plan_row:
            return "missing_execution_plan", ("", "")
        if _positive_int(plan_row.get("schema_version")) != 1:
            return "execution_plan_schema_version", ("", "")

        encoded = str(plan_row.get("plan_json") or "")
        persisted_hash = str(plan_row.get("plan_hash") or "").strip()
        if not encoded or not persisted_hash or _sha256_text(encoded) != persisted_hash:
            return "execution_plan_row_hash", ("", "")
        try:
            plan = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "execution_plan_json", ("", "")
        if not isinstance(plan, dict):
            return "execution_plan_json", ("", "")
        if canonical_plan_json(plan) != encoded:
            return "execution_plan_not_canonical", ("", "")

        candidate = dict(job)
        candidate["raw_data"] = _decode_object(candidate.get("raw_data"))
        candidate["_canonical_execution_plan"] = plan
        try:
            validated = validated_v1_execution_plan_from_job(candidate)
        except (TypeError, ValueError):
            return "execution_plan_validation", ("", "")
        source = validated.get("source") if isinstance(validated.get("source"), dict) else {}
        provider_kind = str(source.get("provider_kind") or "").strip().lower()
        source_form = str(source.get("source_form") or "").strip().lower()
        if not provider_kind or not source_form:
            return "execution_plan_source", ("", "")
        if already_cut_over:
            if str(job.get("source_provider_kind") or "").strip().lower() != provider_kind:
                return "job_source_provider_kind", ("", "")
            if str(job.get("source_form") or "").strip().lower() != source_form:
                return "job_source_form", ("", "")
        return "", (provider_kind, source_form)

    def apply(self, connection: sqlite3.Connection) -> dict[str, Any]:
        analysis = self.analyze(connection)
        columns = _table_columns(connection, "import_jobs")
        already_cut_over = {
            "source_provider_kind",
            "source_form",
        }.issubset(columns) and "source_type" not in columns and "target_route" not in columns

        if already_cut_over:
            now = utc_now_iso()
            deleted = set(analysis.deleted_job_ids)
            related = analysis.related_ids
            self._detach_guest_requests(connection, related.get("guest_requests", ()), now)
            self._detach_update_candidates(connection, related.get("update_candidates", ()))
            self._detach_trending_candidates(
                connection,
                related.get("trending_candidates", ()),
                now,
            )
            self._migrate_advanced_config(connection)
            self._migrate_strm_pending_tasks(
                connection,
                related.get("organizer_strm_pending_tasks", ()),
                related.get("organizer_strm_worker_tasks", ()),
                now,
            )
            self._delete_ids(connection, "worker_tasks", "id", related.get("worker_tasks", ()))
            self._delete_ids(
                connection,
                "rclone_file_events",
                "id",
                related.get("rclone_file_events", ()),
            )
            self._delete_ids(connection, "rclone_events", "id", related.get("rclone_events", ()))
            self._delete_ids(
                connection,
                "organizer_locks",
                "id",
                related.get("organizer_locks", ()),
            )
            self._delete_ids(connection, "transfer_executions", "job_id", deleted)
            self._delete_ids(connection, "import_execution_plans", "job_id", deleted)
            self._delete_ids(connection, "job_events", "job_id", deleted)
            self._delete_ids(
                connection,
                "organizer_operations",
                "id",
                related.get("organizer_operations", ()),
            )
            self._delete_ids(
                connection,
                "organizer_ai_suggestions",
                "id",
                related.get("organizer_ai_suggestions", ()),
            )
            self._delete_ids(
                connection,
                "organizer_tmdb_matches",
                "id",
                related.get("organizer_tmdb_matches", ()),
            )
            self._delete_ids(
                connection,
                "organizer_runs",
                "id",
                related.get("organizer_runs", ()),
            )
            self._delete_ids(
                connection,
                "organizer_mappings",
                "id",
                related.get("organizer_mappings", ()),
            )
            self._delete_ids(
                connection,
                "organizer_files",
                "id",
                related.get("organizer_files", ()),
            )
            self._delete_ids(
                connection,
                "organizer_tasks",
                "id",
                related.get("organizer_tasks", ()),
            )
            self._delete_ids(connection, "rclone_runs", "id", related.get("rclone_runs", ()))
            self._delete_ids(connection, "import_jobs", "id", deleted)
            self._ensure_job_indexes(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v29 foreign_key_check failed: {len(violations)} violation(s)"
                )
            return analysis.summary(dry_run=False)

        # MigrationRunner may arrive here with previous migration rows in an
        # open transaction and foreign_keys still enabled.  SQLite only
        # accepts a foreign_keys mode change outside a transaction, so that
        # path must first commit and then start the v29 transaction itself.
        #
        # The offline CLI, however, deliberately acquires BEGIN EXCLUSIVE
        # *before* calling this method with foreign_keys already disabled.  In
        # that case the caller owns the transaction and must retain the lock;
        # committing or replacing it here would make the advertised atomic
        # cutover false.
        foreign_keys_enabled = int(
            connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        caller_owns_transaction = connection.in_transaction and foreign_keys_enabled == 0
        if not caller_owns_transaction:
            if connection.in_transaction:
                connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
                raise sqlite3.OperationalError(
                    "v29 requires foreign_keys=OFF for table rebuild"
                )
            connection.execute("BEGIN IMMEDIATE")

        deleted = set(analysis.deleted_job_ids)
        related = analysis.related_ids
        now = utc_now_iso()
        self._detach_guest_requests(connection, related.get("guest_requests", ()), now)
        self._detach_update_candidates(connection, related.get("update_candidates", ()))
        self._detach_trending_candidates(connection, related.get("trending_candidates", ()), now)
        self._migrate_advanced_config(connection)
        self._migrate_strm_pending_tasks(
            connection,
            related.get("organizer_strm_pending_tasks", ()),
            related.get("organizer_strm_worker_tasks", ()),
            now,
        )

        self._delete_ids(connection, "worker_tasks", "id", related.get("worker_tasks", ()))
        self._delete_ids(connection, "rclone_file_events", "id", related.get("rclone_file_events", ()))
        self._delete_ids(connection, "rclone_events", "id", related.get("rclone_events", ()))
        self._delete_ids(connection, "organizer_locks", "id", related.get("organizer_locks", ()))
        self._delete_ids(connection, "transfer_executions", "job_id", deleted)
        self._delete_ids(connection, "import_execution_plans", "job_id", deleted)
        self._delete_ids(connection, "job_events", "job_id", deleted)
        self._delete_ids(connection, "organizer_operations", "id", related.get("organizer_operations", ()))
        self._delete_ids(
            connection,
            "organizer_ai_suggestions",
            "id",
            related.get("organizer_ai_suggestions", ()),
        )
        self._delete_ids(
            connection,
            "organizer_tmdb_matches",
            "id",
            related.get("organizer_tmdb_matches", ()),
        )
        self._delete_ids(connection, "organizer_runs", "id", related.get("organizer_runs", ()))
        self._delete_ids(connection, "organizer_mappings", "id", related.get("organizer_mappings", ()))
        self._delete_ids(connection, "organizer_files", "id", related.get("organizer_files", ()))
        self._delete_ids(connection, "organizer_tasks", "id", related.get("organizer_tasks", ()))
        self._delete_ids(connection, "rclone_runs", "id", related.get("rclone_runs", ()))

        self._rebuild_import_jobs(connection, analysis)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            sample = [tuple(row) for row in violations[:5]]
            raise sqlite3.IntegrityError(
                f"v29 foreign_key_check failed: {len(violations)} violation(s): {sample}"
            )
        return analysis.summary(dry_run=False)

    @staticmethod
    def _advanced_config_cleanup(
        connection: sqlite3.Connection,
    ) -> tuple[tuple[str, ...], dict[str, Any] | None]:
        """Return the one-time v29 cleanup without exposing stored values.

        Invalid JSON, non-object values and accidentally persisted export
        envelopes cannot be interpreted as runtime overrides.  The hard
        cutover clears those values instead of carrying an ambiguous legacy
        contract into the new process.
        """

        if not _table_exists(connection, "app_settings"):
            return (), None
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (ADVANCED_CONFIG_KEY,),
        ).fetchone()
        if row is None:
            return (), None
        encoded = row["value"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            raw = json.loads(str(encoded))
        except (TypeError, ValueError, json.JSONDecodeError):
            return (ADVANCED_CONFIG_KEY,), {}
        if not isinstance(raw, dict):
            return (ADVANCED_CONFIG_KEY,), {}
        try:
            cleaned, removed_paths = migrate_stored_advanced_config(raw)
        except ValueError:
            return (ADVANCED_CONFIG_KEY,), {}
        return tuple(sorted(set(removed_paths))), cleaned

    @classmethod
    def _migrate_advanced_config(cls, connection: sqlite3.Connection) -> None:
        removed_paths, cleaned = cls._advanced_config_cleanup(connection)
        if cleaned is None or not removed_paths:
            return
        connection.execute(
            "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?",
            (
                json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
                ADVANCED_CONFIG_KEY,
            ),
        )

    def _related_ids(
        self,
        connection: sqlite3.Connection,
        deleted_job_ids: set[int],
    ) -> dict[str, tuple[int, ...]]:
        result: dict[str, tuple[int, ...]] = {}
        if not deleted_job_ids:
            for name in (
                "job_events",
                "import_execution_plans",
                "transfer_executions",
                "guest_requests",
                "update_candidates",
                "trending_candidates",
                "organizer_tasks",
                "organizer_files",
                "organizer_mappings",
                "organizer_operations",
                "organizer_ai_suggestions",
                "organizer_tmdb_matches",
                "organizer_runs",
                "organizer_locks",
                "rclone_runs",
                "rclone_events",
                "rclone_file_events",
            ):
                result[name] = ()
            strm_task_ids = self._strm_pending_task_ids(connection)
            strm_worker_ids = self._strm_pending_worker_task_ids(
                connection,
                strm_task_ids,
            )
            result["organizer_strm_pending_tasks"] = strm_task_ids
            result["organizer_strm_worker_tasks"] = strm_worker_ids
            result["worker_tasks"] = tuple(
                sorted(
                    set(self._retired_worker_task_ids(connection))
                    | set(strm_worker_ids)
                )
            )
            return result

        def ids_for(table: str, id_column: str, foreign_column: str) -> tuple[int, ...]:
            if not _table_exists(connection, table) or foreign_column not in _table_columns(connection, table):
                return ()
            return tuple(
                sorted(
                    int(row[id_column])
                    for row in _rows(connection, f"SELECT {id_column}, {foreign_column} FROM {table}")
                    if _positive_int(row.get(foreign_column)) in deleted_job_ids
                )
            )

        result["job_events"] = ids_for("job_events", "id", "job_id")
        result["import_execution_plans"] = ids_for(
            "import_execution_plans", "job_id", "job_id"
        )
        result["transfer_executions"] = ids_for(
            "transfer_executions", "job_id", "job_id"
        )
        result["guest_requests"] = ids_for("guest_requests", "id", "job_id")
        result["update_candidates"] = ids_for("update_candidates", "id", "job_id")
        result["trending_candidates"] = ids_for(
            "trending_candidates", "id", "initial_import_job_id"
        )
        organizer_tasks = ids_for("organizer_tasks", "id", "job_id")
        result["organizer_tasks"] = organizer_tasks
        organizer_task_set = set(organizer_tasks)
        strm_task_ids = self._strm_pending_task_ids(
            connection,
            excluded_task_ids=organizer_task_set,
        )
        result["organizer_strm_pending_tasks"] = strm_task_ids
        strm_task_set = set(strm_task_ids)

        def organizer_children(table: str) -> tuple[int, ...]:
            if not organizer_task_set or not _table_exists(connection, table):
                return ()
            return tuple(
                sorted(
                    int(row["id"])
                    for row in _rows(connection, f"SELECT id, task_id FROM {table}")
                    if _positive_int(row.get("task_id")) in organizer_task_set
                )
            )

        result["organizer_files"] = organizer_children("organizer_files")
        result["organizer_mappings"] = organizer_children("organizer_mappings")
        result["organizer_operations"] = organizer_children("organizer_operations")
        result["organizer_ai_suggestions"] = organizer_children(
            "organizer_ai_suggestions"
        )
        result["organizer_tmdb_matches"] = organizer_children(
            "organizer_tmdb_matches"
        )
        result["organizer_runs"] = organizer_children("organizer_runs")
        organizer_run_set = set(result["organizer_runs"])

        rclone_run_ids: set[int] = set()
        if _table_exists(connection, "rclone_runs"):
            for row in _rows(connection, "SELECT id, job_id FROM rclone_runs"):
                if _positive_int(row.get("job_id")) in deleted_job_ids:
                    rclone_run_ids.add(int(row["id"]))
        if _table_exists(connection, "organizer_tasks"):
            for row in _rows(connection, "SELECT id, rclone_run_id FROM organizer_tasks"):
                if int(row["id"]) in organizer_task_set and _positive_int(row.get("rclone_run_id")):
                    rclone_run_ids.add(int(row["rclone_run_id"]))
        result["rclone_runs"] = tuple(sorted(rclone_run_ids))

        if _table_exists(connection, "rclone_events"):
            result["rclone_events"] = tuple(
                sorted(
                    int(row["id"])
                    for row in _rows(connection, "SELECT id, run_id FROM rclone_events")
                    if _positive_int(row.get("run_id")) in rclone_run_ids
                )
            )
        else:
            result["rclone_events"] = ()
        if _table_exists(connection, "rclone_file_events"):
            result["rclone_file_events"] = tuple(
                sorted(
                    int(row["id"])
                    for row in _rows(
                        connection,
                        "SELECT id, run_id, job_id FROM rclone_file_events",
                    )
                    if _positive_int(row.get("job_id")) in deleted_job_ids
                    or _positive_int(row.get("run_id")) in rclone_run_ids
                )
            )
        else:
            result["rclone_file_events"] = ()

        if _table_exists(connection, "organizer_locks"):
            result["organizer_locks"] = tuple(
                sorted(
                    int(row["id"])
                    for row in _rows(
                        connection,
                        "SELECT id, task_id, run_id FROM organizer_locks",
                    )
                    if _positive_int(row.get("task_id")) in organizer_task_set
                    or _positive_int(row.get("run_id")) in organizer_run_set
                )
            )
        else:
            result["organizer_locks"] = ()

        guest_request_ids = set(result["guest_requests"])
        worker_task_ids: list[int] = []
        strm_worker_task_ids: list[int] = []
        if _table_exists(connection, "worker_tasks"):
            for row in _rows(
                connection,
                "SELECT id, task_type, payload, status FROM worker_tasks",
            ):
                payload = _decode_object(row.get("payload"))
                is_strm_worker = (
                    str(row.get("task_type") or "").strip()
                    in ORGANIZER_WORKER_TASK_TYPES
                    and str(row.get("status") or "").strip()
                    in ACTIVE_WORKER_TASK_STATUSES
                    and _positive_int(payload.get("task_id")) in strm_task_set
                )
                if is_strm_worker:
                    strm_worker_task_ids.append(int(row["id"]))
                if (
                    str(row.get("task_type") or "").strip() in RETIRED_WORKER_TASK_TYPES
                    or _positive_int(payload.get("job_id")) in deleted_job_ids
                    or _positive_int(payload.get("task_id")) in organizer_task_set
                    or _positive_int(payload.get("guest_request_id")) in guest_request_ids
                    or is_strm_worker
                ):
                    worker_task_ids.append(int(row["id"]))
        result["organizer_strm_worker_tasks"] = tuple(sorted(strm_worker_task_ids))
        result["worker_tasks"] = tuple(sorted(worker_task_ids))
        return result

    @staticmethod
    def _strm_pending_task_ids(
        connection: sqlite3.Connection,
        *,
        excluded_task_ids: Iterable[int] = (),
    ) -> tuple[int, ...]:
        if not _table_exists(connection, "organizer_tasks"):
            return ()
        excluded = {int(value) for value in excluded_task_ids}
        return tuple(
            sorted(
                int(row["id"])
                for row in _rows(
                    connection,
                    "SELECT id FROM organizer_tasks WHERE status='strm_pending'",
                )
                if int(row["id"]) not in excluded
            )
        )

    @staticmethod
    def _strm_pending_worker_task_ids(
        connection: sqlite3.Connection,
        task_ids: Iterable[int],
    ) -> tuple[int, ...]:
        normalized = {int(value) for value in task_ids}
        if not normalized or not _table_exists(connection, "worker_tasks"):
            return ()
        result: list[int] = []
        for row in _rows(
            connection,
            "SELECT id, task_type, payload, status FROM worker_tasks",
        ):
            payload = _decode_object(row.get("payload"))
            if (
                str(row.get("task_type") or "").strip()
                in ORGANIZER_WORKER_TASK_TYPES
                and str(row.get("status") or "").strip()
                in ACTIVE_WORKER_TASK_STATUSES
                and _positive_int(payload.get("task_id")) in normalized
            ):
                result.append(int(row["id"]))
        return tuple(sorted(result))

    @staticmethod
    def _retired_worker_task_ids(connection: sqlite3.Connection) -> tuple[int, ...]:
        if not _table_exists(connection, "worker_tasks"):
            return ()
        return tuple(
            sorted(
                int(row["id"])
                for row in _rows(connection, "SELECT id, task_type FROM worker_tasks")
                if str(row.get("task_type") or "").strip() in RETIRED_WORKER_TASK_TYPES
            )
        )

    @staticmethod
    def _migrate_strm_pending_tasks(
        connection: sqlite3.Connection,
        task_ids: Iterable[int],
        worker_task_ids: Iterable[int],
        now: str,
    ) -> None:
        normalized = sorted({int(value) for value in task_ids})
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = _rows(
                connection,
                "SELECT id, error_message, evidence, raw_data "
                f"FROM organizer_tasks WHERE id IN ({placeholders}) "
                "AND status='strm_pending'",
                chunk,
            )
            for row in rows:
                raw_data = _decode_object(row.get("raw_data"))
                evidence = _decode_object(row.get("evidence"))
                completion = (
                    dict(raw_data.get("strm_completion"))
                    if isinstance(raw_data.get("strm_completion"), dict)
                    else {}
                )
                for key in (
                    "next_retry_at",
                    "retry_at",
                    "retry_count",
                    "attempts",
                ):
                    completion.pop(key, None)
                completion.update(
                    {
                        "version": max(2, _positive_int(completion.get("version"))),
                        "status": "retired_by_v1_cutover",
                        "confirmation_required": True,
                        "retired_at": now,
                        "message": STRM_PENDING_CUTOVER_MESSAGE,
                    }
                )
                cutover_record: dict[str, Any] = {
                    "migration": CUTOVER_NAME,
                    "message": STRM_PENDING_CUTOVER_MESSAGE,
                    "migrated_at": now,
                }
                previous_error = str(row.get("error_message") or "").strip()
                if previous_error and previous_error != STRM_PENDING_CUTOVER_MESSAGE:
                    cutover_record["previous_error_message"] = previous_error[:1000]
                raw_data["strm_completion"] = completion
                raw_data["v1_cutover"] = cutover_record
                evidence["strm_completion"] = completion
                evidence["v1_cutover"] = cutover_record
                connection.execute(
                    "UPDATE organizer_tasks SET status='waiting_review', error_message=?, "
                    "raw_data=?, evidence=?, revision=revision+1, scan_owner=NULL, "
                    "scan_lease_expires_at=NULL, updated_at=? "
                    "WHERE id=? AND status='strm_pending'",
                    (
                        STRM_PENDING_CUTOVER_MESSAGE,
                        json.dumps(raw_data, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        now,
                        int(row["id"]),
                    ),
                )
        V1JobContractHardCutover._delete_ids(
            connection,
            "worker_tasks",
            "id",
            worker_task_ids,
        )

    @staticmethod
    def _delete_ids(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        values: Iterable[int],
    ) -> None:
        normalized = sorted({int(value) for value in values})
        if not normalized or not _table_exists(connection, table):
            return
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                chunk,
            )

    @staticmethod
    def _detach_guest_requests(
        connection: sqlite3.Connection,
        request_ids: Iterable[int],
        now: str,
    ) -> None:
        ids = sorted({int(value) for value in request_ids})
        if not ids or not _table_exists(connection, "guest_requests"):
            return
        rows = {
            int(row["id"]): row
            for row in _rows(
                connection,
                "SELECT id, job_id, raw_data FROM guest_requests",
            )
            if int(row["id"]) in set(ids)
        }
        for request_id in ids:
            row = rows.get(request_id, {})
            raw_data = _decode_object(row.get("raw_data"))
            raw_data["v1_job_contract_hard_cutover"] = {
                "message": CUTOVER_MESSAGE,
                "cleared_at": now,
                "deleted_job_id_sha256": _sha256_text(str(_positive_int(row.get("job_id")))),
            }
            connection.execute(
                "UPDATE guest_requests SET job_id=NULL, status='unsupported', "
                "public_status='暂不支持', raw_data=?, updated_at=? WHERE id=?",
                (
                    json.dumps(raw_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                    request_id,
                ),
            )
            if _table_exists(connection, "guest_request_events"):
                connection.execute(
                    "INSERT INTO guest_request_events(request_id, level, message, raw_data, created_at) "
                    "VALUES (?, 'warning', ?, ?, ?)",
                    (
                        request_id,
                        CUTOVER_MESSAGE,
                        json.dumps(
                            {"code": "V1_JOB_CONTRACT_HARD_CUTOVER"},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )

    @staticmethod
    def _detach_update_candidates(
        connection: sqlite3.Connection,
        candidate_ids: Iterable[int],
    ) -> None:
        ids = sorted({int(value) for value in candidate_ids})
        if not ids or not _table_exists(connection, "update_candidates"):
            return
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"UPDATE update_candidates SET job_id=NULL, decision='rejected', reason=? "
                f"WHERE id IN ({placeholders})",
                [CUTOVER_MESSAGE, *chunk],
            )

    @staticmethod
    def _detach_trending_candidates(
        connection: sqlite3.Connection,
        candidate_ids: Iterable[int],
        now: str,
    ) -> None:
        ids = sorted({int(value) for value in candidate_ids})
        if not ids or not _table_exists(connection, "trending_candidates"):
            return
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"UPDATE trending_candidates SET initial_import_job_id=NULL, "
                f"status='import_failed', ignore_reason=?, updated_at=? "
                f"WHERE id IN ({placeholders})",
                [CUTOVER_MESSAGE, now, *chunk],
            )

    @staticmethod
    def _rebuild_import_jobs(
        connection: sqlite3.Connection,
        analysis: CutoverAnalysis,
    ) -> None:
        connection.execute("DROP TABLE IF EXISTS import_jobs_v29_new")
        connection.execute(
            """
            CREATE TABLE import_jobs_v29_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                category_label TEXT NOT NULL,
                source_provider_kind TEXT NOT NULL CHECK(length(trim(source_provider_kind)) > 0),
                source_form TEXT NOT NULL CHECK(length(trim(source_form)) > 0),
                source_url TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '' CHECK(password = ''),
                target_path TEXT,
                status TEXT NOT NULL,
                external_task_id TEXT,
                error_message TEXT,
                raw_data TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                config_revision INTEGER NOT NULL DEFAULT 1,
                executor_id TEXT NOT NULL DEFAULT '',
                destination_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL CHECK(plan_version = 1)
            )
            """
        )
        rows = {
            int(row["id"]): row
            for row in _rows(connection, "SELECT * FROM import_jobs ORDER BY id")
        }
        for job_id, (provider_kind, source_form) in sorted(analysis.kept_sources.items()):
            row = rows[job_id]
            connection.execute(
                """
                INSERT INTO import_jobs_v29_new(
                    id, title, category, category_label,
                    source_provider_kind, source_form, source_url, password,
                    target_path, status, external_task_id, error_message, raw_data,
                    created_at, updated_at, idempotency_key, config_revision,
                    executor_id, destination_id, plan_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    job_id,
                    str(row.get("title") or ""),
                    str(row.get("category") or ""),
                    str(row.get("category_label") or ""),
                    provider_kind,
                    source_form,
                    str(row.get("source_url") or ""),
                    row.get("target_path"),
                    str(row.get("status") or ""),
                    row.get("external_task_id"),
                    row.get("error_message"),
                    row.get("raw_data"),
                    str(row.get("created_at") or ""),
                    str(row.get("updated_at") or ""),
                    str(row.get("idempotency_key") or f"v1:{job_id}"),
                    max(1, _positive_int(row.get("config_revision"))),
                    str(row.get("executor_id") or ""),
                    str(row.get("destination_id") or ""),
                ),
            )
        connection.execute("DROP TABLE import_jobs")
        connection.execute("ALTER TABLE import_jobs_v29_new RENAME TO import_jobs")
        V1JobContractHardCutover._ensure_job_indexes(connection)

    @staticmethod
    def _ensure_job_indexes(connection: sqlite3.Connection) -> None:
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON import_jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_provider_kind ON import_jobs(source_provider_kind)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_form ON import_jobs(source_form)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON import_jobs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_destination_id ON import_jobs(destination_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_source_category ON import_jobs(source_url, category)",
        ):
            connection.execute(statement)


def analyze_database(path: Path) -> dict[str, Any]:
    from .database import Database

    database = Database(path)
    with closing(
        sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    ) as connection:
        current = database.migration_runner.current_version(connection)
    if current > database.migration_runner.latest_version:
        raise RuntimeError(
            f"数据库迁移版本 v{current} 高于当前程序支持的 v{database.migration_runner.latest_version}；"
            "拒绝降级预检"
        )
    if current >= CUTOVER_VERSION:
        return _analyze_post_cutover_database(path, database, current)

    prepared: Path | None = None
    try:
        prepared, simulated_from, pre_cutover_migrations = _prepare_cutover_copy(path)
        with closing(
            sqlite3.connect(
                f"file:{prepared.resolve().as_posix()}?mode=ro", uri=True
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            summary = V1JobContractHardCutover().analyze(connection).summary(dry_run=True)
        _apply_v29_to_prepared_copy(prepared)
        summary.update(
            {
                "current_schema_version": current,
                "simulated_from_schema_version": simulated_from,
                "pre_cutover_migrations": pre_cutover_migrations,
                "target_schema_version": database.migration_runner.latest_version,
                "simulation_validated": True,
            }
        )
        return summary
    finally:
        if prepared is not None:
            prepared.unlink(missing_ok=True)


def _clone_database(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True
    )
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _apply_post_cutover_migrations(connection: sqlite3.Connection, database: Any) -> list[int]:
    applied = []
    for migration in database.migration_runner.pending(connection):
        if migration.version <= CUTOVER_VERSION:
            continue
        migration.apply(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, utc_now_iso()),
        )
        applied.append(migration.version)
    return applied


def _analyze_post_cutover_database(path: Path, database: Any, current: int) -> dict[str, Any]:
    prepared, _version, _pre = _prepare_cutover_copy(path)
    try:
        with closing(sqlite3.connect(prepared)) as connection:
            connection.row_factory = sqlite3.Row
            applied = _apply_post_cutover_migrations(connection, database)
            _validate_database(connection)
            connection.commit()
        return {
            "dry_run": True,
            "current_schema_version": current,
            "target_schema_version": database.migration_runner.latest_version,
            "post_cutover_migrations": applied,
            "simulation_validated": True,
            "additive_only": True,
        }
    finally:
        prepared.unlink(missing_ok=True)


def _prepare_cutover_copy(path: Path) -> tuple[Path, int, list[int]]:
    handle, raw_target = tempfile.mkstemp(
        prefix="v1-cutover-",
        suffix=".db",
        dir=str(path.parent),
    )
    os.close(handle)
    target = Path(raw_target)
    try:
        _clone_database(path, target)
        from .database import Database

        database = Database(target)
        with closing(sqlite3.connect(target)) as connection:
            connection.row_factory = sqlite3.Row
            current = database.migration_runner.current_version(connection)
            if current > database.migration_runner.latest_version:
                raise RuntimeError(
                    f"数据库迁移版本 v{current} 高于当前程序支持的 v{database.migration_runner.latest_version}"
                )
            applied: list[int] = []
            if current < database.PRE_CUTOVER_VERSION:
                database.install_base_schema(connection)
                applied = database.run_pre_cutover_migrations(connection)
                database.install_base_schema(connection)
                connection.commit()
            return target, current, applied
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _validate_database(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"v29 foreign_key_check failed: {len(violations)} violation(s)"
        )
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise sqlite3.IntegrityError(f"v29 integrity_check failed: {integrity}")


def _apply_v29_to_prepared_copy(path: Path) -> dict[str, Any]:
    from .database import Database

    database = Database(path)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        current = database.migration_runner.current_version(connection)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "PRAGMA foreign_keys=OFF"
            if current < CUTOVER_VERSION
            else "PRAGMA foreign_keys=ON"
        )
        connection.execute("BEGIN EXCLUSIVE")
        summary = V1JobContractHardCutover().apply(connection) if current < CUTOVER_VERSION else {"additive_only": True}
        if current < CUTOVER_VERSION:
            if current != database.PRE_CUTOVER_VERSION:
                raise RuntimeError(
                    f"v29 硬切换要求 v{database.PRE_CUTOVER_VERSION}，当前为 v{current}"
                )
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (CUTOVER_VERSION, CUTOVER_NAME, utc_now_iso()),
            )
        summary["post_cutover_migrations"] = _apply_post_cutover_migrations(connection, database)
        summary["target_schema_version"] = database.migration_runner.latest_version
        _validate_database(connection)
        connection.commit()
        return summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _assert_database_offline(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=200")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0] or 0) != 0:
            raise sqlite3.OperationalError("WAL checkpoint 忙")
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "数据库仍被其他进程占用；请停止 Web、Worker、Scheduler 后重试"
        ) from exc
    finally:
        connection.close()


def _replace_database(source: Path, prepared: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{source}{suffix}").unlink(missing_ok=True)
        Path(f"{prepared}{suffix}").unlink(missing_ok=True)
    os.chmod(prepared, source.stat().st_mode)
    try:
        os.replace(prepared, source)
    except OSError as exc:
        raise RuntimeError(
            "无法原子替换数据库；请确认 Web、Worker、Scheduler 及数据库查看工具"
            "均已关闭后重试"
        ) from exc


def apply_database(path: Path) -> dict[str, Any]:
    from .database import Database

    database = Database(path)
    lock_path = path.with_name(f".{path.name}.v1-hard-cutover.lock")
    lock_fd: int | None = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        with database.connect() as connection:
            current = database.migration_runner.current_version(connection)
        if current > database.migration_runner.latest_version:
            raise RuntimeError(
                f"数据库迁移版本 v{current} 高于当前程序支持的 v{database.migration_runner.latest_version}；"
                "拒绝降级执行离线升级"
            )
        if current >= CUTOVER_VERSION:
            pending = analyze_database(path)
            if not pending.get("post_cutover_migrations"):
                pending["dry_run"] = False
                pending["already_applied"] = True
                return pending

        _assert_database_offline(path)
        backup = database.backup_database(prefix=f"app_before_schema_migration_v{database.migration_runner.latest_version}")
        prepared: Path | None = None
        try:
            prepared, _simulated_from, pre_cutover_migrations = _prepare_cutover_copy(path)
            summary = _apply_v29_to_prepared_copy(prepared)
            _assert_database_offline(path)
            _replace_database(path, prepared)
            prepared = None
        finally:
            if prepared is not None:
                prepared.unlink(missing_ok=True)
        summary["backup_path"] = str(backup.get("path") or "")
        summary["backup"] = backup
        summary["upgraded_from_schema_version"] = current
        summary["pre_cutover_migrations"] = pre_cutover_migrations
        return summary
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V1 任务契约硬切换")
    parser.add_argument("--database", required=True, type=Path, help="SQLite 数据库路径")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="模拟升级并输出统计，不修改原数据库")
    action.add_argument("--apply", action="store_true", help="备份并执行离线升级；v29 以后仅执行增量迁移")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = Path(args.database)
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    summary = analyze_database(path) if args.dry_run else apply_database(path)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CUTOVER_NAME",
    "CUTOVER_VERSION",
    "CutoverAnalysis",
    "V1JobContractHardCutover",
    "analyze_database",
    "apply_database",
    "main",
]
