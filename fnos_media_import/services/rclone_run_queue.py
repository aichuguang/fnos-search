from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueuedRcloneRun:
    reason: str
    file_retry: dict[str, Any] | None
    category_filter: str
    queued_at: str
    staging_run: dict[str, Any] | None = None


class RcloneRunQueue:
    """Thread-safe FIFO queue with explicit stop-and-drain semantics."""

    def __init__(self) -> None:
        self._items: list[QueuedRcloneRun] = []
        self._stop_requested = False
        self._lock = threading.Lock()

    def enqueue(
        self,
        *,
        reason: str,
        file_retry: dict[str, Any] | None,
        category_filter: str,
        queued_at: str,
        staging_run: dict[str, Any] | None = None,
    ) -> QueuedRcloneRun:
        item = QueuedRcloneRun(
            reason=str(reason or "queued"),
            file_retry=dict(file_retry) if isinstance(file_retry, dict) else None,
            category_filter=str(category_filter or "").strip(),
            queued_at=str(queued_at or ""),
            staging_run=dict(staging_run) if isinstance(staging_run, dict) else None,
        )
        with self._lock:
            self._items.append(item)
        return item

    def enqueue_if_staging_job_absent(
        self,
        *,
        reason: str,
        file_retry: dict[str, Any] | None,
        category_filter: str,
        queued_at: str,
        staging_run: dict[str, Any] | None = None,
    ) -> tuple[QueuedRcloneRun, bool]:
        """Atomically enqueue one full staging run unless it is already queued.

        File-level retries deliberately remain independent even when they belong
        to the same job.  The boolean indicates whether a new item was added.
        """

        item = QueuedRcloneRun(
            reason=str(reason or "queued"),
            file_retry=dict(file_retry) if isinstance(file_retry, dict) else None,
            category_filter=str(category_filter or "").strip(),
            queued_at=str(queued_at or ""),
            staging_run=dict(staging_run) if isinstance(staging_run, dict) else None,
        )
        job_id = _queued_staging_job_id(item)
        with self._lock:
            if job_id > 0:
                for queued in self._items:
                    if _queued_staging_job_id(queued) == job_id:
                        return queued, False
            self._items.append(item)
        return item, True

    def begin_direct_run(self) -> None:
        with self._lock:
            self._stop_requested = False

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            self._items.clear()

    def remove_job(self, job_id: int) -> list[QueuedRcloneRun]:
        """Remove queued runs owned by one persisted staging job.

        A task cancellation must never drain unrelated work.  Generic legacy
        scans do not carry a job identity and are deliberately left untouched.
        """

        normalized_job_id = _safe_job_id(job_id)
        if normalized_job_id <= 0:
            return []
        with self._lock:
            kept: list[QueuedRcloneRun] = []
            removed: list[QueuedRcloneRun] = []
            for item in self._items:
                if _queued_job_id(item) == normalized_job_id:
                    removed.append(item)
                else:
                    kept.append(item)
            self._items = kept
            return removed

    def pop_next(self) -> QueuedRcloneRun | None:
        with self._lock:
            if self._stop_requested:
                self._items.clear()
                self._stop_requested = False
                return None
            if not self._items:
                return None
            return self._items.pop(0)

    def snapshot(self, limit: int = 10) -> dict[str, Any]:
        with self._lock:
            items = list(self._items[-max(0, int(limit or 0)) :]) if limit else list(self._items)
            total = len(self._items)
        return {
            "queue_count": total,
            "queued_runs": [
                {
                    "reason": item.reason,
                    "category_filter": item.category_filter,
                    "queued_at": item.queued_at,
                    "job_id": _queued_job_id(item) or None,
                }
                for item in items
            ],
        }


def _safe_job_id(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _queued_job_id(item: QueuedRcloneRun) -> int:
    staging_run = item.staging_run if isinstance(item.staging_run, dict) else {}
    job_id = _safe_job_id(staging_run.get("job_id"))
    if job_id > 0:
        return job_id
    file_retry = item.file_retry if isinstance(item.file_retry, dict) else {}
    return _safe_job_id(file_retry.get("job_id"))


def _queued_staging_job_id(item: QueuedRcloneRun) -> int:
    if isinstance(item.file_retry, dict):
        return 0
    staging_run = item.staging_run if isinstance(item.staging_run, dict) else {}
    return _safe_job_id(staging_run.get("job_id"))
