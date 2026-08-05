"""Periodically removes old append-only event rows under a scheduler lease.

The retention worker reuses the SearchCacheMaintenanceWorker lease pattern so
that only one process performs cleanup even when the application runs across
multiple process roles or replicas.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..constants import JOB_CANCELLED, JOB_DONE, JOB_FAILED, JOB_UNSUPPORTED
from ..time_utils import utc_now_iso_offset


# 表名来自白名单常量，不接收外部输入，SQL 拼接安全。
EVENT_RETENTION_TABLES = (
    "job_events",
    "guest_request_events",
    "rclone_events",
    "rclone_file_events",
    "update_events",
)
DEFAULT_RETENTION_DAYS = 90
TERMINAL_JOB_STATUSES = (
    JOB_DONE,
    JOB_FAILED,
    JOB_UNSUPPORTED,
    JOB_CANCELLED,
    # Historical/compatibility writers used callback-style terminal values.
    # They are immutable everywhere else and must not retain event history
    # forever merely because they predate the normalized JOB_* vocabulary.
    "success",
    "skipped_existing",
    "skipped",
    "rejected",
    "canceled",
)
RECOVERABLE_UNMATCHED_RCLONE_STATUSES = (
    "transferring",
    "processing",
    "done",
    "success",
    "skipped_existing",
)


class EventRetentionWorker:
    """Deletes append-only event rows whose ``created_at`` predates the cutoff."""

    lease_name = "event-retention"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str,
        log: Callable[[str], None],
        interval_seconds: float = 3600,
        retention_days: int | None = None,
        batch_size: int = 1000,
        max_delete_per_run: int = 20000,
        lease_ttl_seconds: int = 300,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        self.database = database
        self.owner_id = str(owner_id or f"event-retention-{id(self)}")
        self.log = log
        self.interval_seconds = max(1, float(interval_seconds or 3600))
        self.retention_days = max(
            1,
            int(retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS),
        )
        self.batch_size = max(1, int(batch_size or 1000))
        self.max_delete_per_run = max(1, int(max_delete_per_run or 20000))
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds or 300))
        self.shutdown_timeout_seconds = max(0, float(shutdown_timeout_seconds or 0))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._unmatched_scan_after_id = 0
        # 在多次运行之间保留游标。即使单次删除预算小于一批，也会让下一张表
        # 在下一轮优先执行，避免固定顺序导致后面的表长期得不到清理机会。
        self._next_table_index = 0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._loop,
            name=self.lease_name,
            daemon=True,
        )
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_timeout_seconds)
        # Do not release the lease here: an active pass still owns it, and
        # run_once() releases it in its finally block after database work exits.

    def run_once(self) -> dict[str, Any]:
        if self.stop_event.is_set():
            return {"success": True, "skipped": True, "stopped": True, "deleted": 0, "tables": 0}
        if not self._run_lock.acquire(blocking=False):
            return {"success": True, "skipped": True, "busy": True, "deleted": 0, "tables": 0}

        acquired = False
        try:
            acquired = bool(
                self.database.acquire_scheduler_lease(
                    self.lease_name,
                    self.owner_id,
                    self.lease_ttl_seconds,
                )
            )
            if not acquired:
                return {"success": True, "skipped": True, "deleted": 0, "tables": 0}

            cutoff = utc_now_iso_offset(days=-self.retention_days)
            deleted_total = 0
            batches = 0
            visited_tables: set[str] = set()
            consecutive_empty = 0
            lease_lost = False

            while (
                deleted_total < self.max_delete_per_run
                and consecutive_empty < len(EVENT_RETENTION_TABLES)
                and not self.stop_event.is_set()
            ):
                table = EVENT_RETENTION_TABLES[self._next_table_index]
                if batches and not self._renew_lease():
                    lease_lost = True
                    self._safe_log("Event retention stopped because its scheduler lease could not be renewed.")
                    break

                limit = min(self.batch_size, self.max_delete_per_run - deleted_total)
                removed = self._prune_table(table, cutoff, limit)
                batches += 1
                visited_tables.add(table)
                self._next_table_index = (self._next_table_index + 1) % len(EVENT_RETENTION_TABLES)
                deleted_total += removed
                consecutive_empty = 0 if removed else consecutive_empty + 1

            if deleted_total:
                self._safe_log(
                    f"Event retention deleted {deleted_total} row(s) older than {cutoff} "
                    f"(retention_days={self.retention_days})."
                )
            return {
                "success": not lease_lost,
                "deleted": deleted_total,
                "tables": len(visited_tables),
                "batches": batches,
                "cutoff": cutoff,
                "stopped": self.stop_event.is_set(),
                "lease_lost": lease_lost,
            }
        except Exception as exc:  # noqa: BLE001
            self._safe_log(f"Event retention failed: {exc}")
            return {"success": False, "message": str(exc), "deleted": 0, "tables": 0}
        finally:
            if acquired:
                try:
                    self.database.release_scheduler_lease(self.lease_name, self.owner_id)
                except Exception as exc:  # noqa: BLE001
                    self._safe_log(f"Event retention lease release failed: {exc}")
            self._run_lock.release()

    def _renew_lease(self) -> bool:
        return bool(
            self.database.acquire_scheduler_lease(
                self.lease_name,
                self.owner_id,
                self.lease_ttl_seconds,
            )
        )

    def _prune_table(self, table: str, cutoff: str, limit: int) -> int:
        if table == "rclone_file_events":
            return self._prune_rclone_file_events(cutoff, limit)

        predicate = ""
        parameters: list[Any] = [cutoff]
        if table == "job_events":
            placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
            predicate = f"""
                AND (
                    NOT EXISTS (
                        SELECT 1 FROM import_jobs AS parent_job
                        WHERE parent_job.id = event_row.job_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM import_jobs AS parent_job
                        WHERE parent_job.id = event_row.job_id
                          AND parent_job.status IN ({placeholders})
                    )
                )
            """
            parameters.extend(TERMINAL_JOB_STATUSES)
        parameters.append(limit)

        with self.database.connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM {table}
                WHERE id IN (
                    SELECT event_row.id FROM {table} AS event_row
                    WHERE event_row.created_at < ?
                    {predicate}
                    ORDER BY event_row.created_at, event_row.id
                    LIMIT ?
                )
                """,
                parameters,
            )
            return max(0, int(cursor.rowcount))

    def _prune_rclone_file_events(self, cutoff: str, limit: int) -> int:
        """Delete old file events without destroying startup recovery evidence.

        Linked rows are safe once their parent job is terminal (or gone).  Null
        rows need extra care because the history-repair worker can still attach
        successful callback evidence to an active job after a restart.  Statuses
        that recovery never consumes are deleted directly; recoverable statuses
        are deleted only when the normal callback matcher cannot find an active
        job.  A persistent scan cursor prevents one preserved old row from
        starving later orphan rows forever.
        """

        terminal_placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        recoverable_placeholders = ",".join("?" for _ in RECOVERABLE_UNMATCHED_RCLONE_STATUSES)
        parameters: list[Any] = [
            cutoff,
            *TERMINAL_JOB_STATUSES,
            *RECOVERABLE_UNMATCHED_RCLONE_STATUSES,
            limit,
        ]
        with self.database.connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM rclone_file_events
                WHERE id IN (
                    SELECT event_row.id
                    FROM rclone_file_events AS event_row
                    WHERE event_row.created_at < ?
                      AND (
                        (
                          event_row.job_id IS NOT NULL
                          AND (
                            NOT EXISTS (
                              SELECT 1 FROM import_jobs AS parent_job
                              WHERE parent_job.id = event_row.job_id
                            )
                            OR EXISTS (
                              SELECT 1 FROM import_jobs AS parent_job
                              WHERE parent_job.id = event_row.job_id
                                AND parent_job.status IN ({terminal_placeholders})
                            )
                          )
                        )
                        OR (
                          event_row.job_id IS NULL
                          AND LOWER(COALESCE(event_row.status, '')) NOT IN ({recoverable_placeholders})
                        )
                      )
                    ORDER BY event_row.created_at, event_row.id
                    LIMIT ?
                )
                """,
                parameters,
            )
            removed = max(0, int(cursor.rowcount))

        remaining = max(0, int(limit) - removed)
        if not remaining:
            return removed
        return removed + self._prune_recoverable_unmatched_rclone_events(cutoff, remaining)

    def _prune_recoverable_unmatched_rclone_events(self, cutoff: str, limit: int) -> int:
        matcher = getattr(self.database, "find_job_for_rclone_callback", None)
        if not callable(matcher):
            # Compatibility adapters without the production matcher cannot
            # prove that a row is orphaned, so preserve it rather than risk a
            # false cleanup during startup recovery.
            return 0

        scan_limit = max(100, min(2000, int(limit) * 4))
        rows = self._load_recoverable_unmatched_rows(cutoff, scan_limit, self._unmatched_scan_after_id)
        if not rows and self._unmatched_scan_after_id:
            self._unmatched_scan_after_id = 0
            rows = self._load_recoverable_unmatched_rows(cutoff, scan_limit, 0)
        if not rows:
            return 0

        deletable_ids: list[int] = []
        for row in rows:
            event_id = int(row["id"] or 0)
            self._unmatched_scan_after_id = max(self._unmatched_scan_after_id, event_id)
            try:
                matched = matcher(
                    category=str(row["category"] or ""),
                    filename=str(row["filename"] or ""),
                    source_path=str(row["source_path"] or ""),
                    target_path=str(row["target_path"] or ""),
                )
            except Exception as exc:  # noqa: BLE001
                self._safe_log(f"Event retention could not classify unmatched rclone event #{event_id}: {exc}")
                continue
            if not matched:
                deletable_ids.append(event_id)
                if len(deletable_ids) >= limit:
                    break

        if len(rows) < scan_limit:
            self._unmatched_scan_after_id = 0
        if not deletable_ids:
            return 0

        placeholders = ",".join("?" for _ in deletable_ids)
        with self.database.connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM rclone_file_events
                WHERE job_id IS NULL
                  AND created_at < ?
                  AND id IN ({placeholders})
                """,
                (cutoff, *deletable_ids),
            )
            return max(0, int(cursor.rowcount))

    def _load_recoverable_unmatched_rows(self, cutoff: str, limit: int, after_id: int) -> list[Any]:
        placeholders = ",".join("?" for _ in RECOVERABLE_UNMATCHED_RCLONE_STATUSES)
        with self.database.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT id, category, filename, source_path, target_path
                    FROM rclone_file_events
                    WHERE job_id IS NULL
                      AND created_at < ?
                      AND id > ?
                      AND LOWER(COALESCE(status, '')) IN ({placeholders})
                    ORDER BY id
                    LIMIT ?
                    """,
                    (
                        cutoff,
                        max(0, int(after_id)),
                        *RECOVERABLE_UNMATCHED_RCLONE_STATUSES,
                        max(1, int(limit)),
                    ),
                ).fetchall()
            )

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.run_once()
            if self.stop_event.wait(self.interval_seconds):
                break

    def _safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001
            pass
