from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    EVENT_INFO,
    EVENT_WARN,
    JOB_REVIEW,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)
from .rclone_job_feasibility import RcloneJobFeasibilityEvaluator


class RcloneRunImportFinalizer:
    """Fallback-finalizes import jobs when a complete rclone run ends."""

    def __init__(
        self,
        *,
        database: Any,
        categories: Callable[[], dict[str, dict[str, Any]]],
        log: Callable[[str], None],
        feasibility: Callable[[dict[str, Any], list[dict[str, Any]], int], dict[str, Any]],
        finish_ready: Callable[..., dict[str, Any]],
        dispatch_ready: Callable[[dict[str, Any], dict[str, Any]], Any],
        execution_repository: Any = None,
    ) -> None:
        self.database = database
        self.categories = categories
        self.log = log
        self.feasibility = feasibility
        self.finish_ready = finish_ready
        self.dispatch_ready = dispatch_ready
        self.execution_repository = execution_repository

    def finalize(self, run_id: int, exit_code: int) -> None:
        if not self.database:
            return
        events = _all_file_events(self.database, run_id=run_id)
        grouped = _group_by_job(events)
        if not grouped:
            message = f"rclone run #{run_id} 未匹配到入库任务，跳过自动媒体库刷新"
            self.log(message)
            self.database.add_rclone_event(run_id, EVENT_WARN, message)
            return
        ready = self._classify(run_id, exit_code, grouped)
        for category_key, items in ready.items():
            category = self.categories().get(category_key, {})
            all_events = [event for _, job_events, _ in items for event in job_events]
            category_refresh = self.finish_ready(
                run_id,
                category_key,
                category,
                items,
                all_events,
                trigger="rclone_run_finished",
                success_message="rclone 完整搬运通过，已在 run 结束兜底移交 Organizer",
            )
            if isinstance(category_refresh, dict) and category_refresh.get("completed_items"):
                self.dispatch_ready(
                    category_refresh,
                    {
                        "run_id": run_id,
                        "status": "run_done",
                        "category": category_key,
                        "trigger": "rclone_run_finished",
                        "exit_code": exit_code,
                    },
                )

    def _classify(
        self,
        run_id: int,
        exit_code: int,
        grouped: dict[int, list[dict[str, Any]]],
    ) -> dict[str, list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]]]:
        ready: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]]] = {}
        jobs = self.database.get_jobs_by_ids(list(grouped.keys()))
        for job_id, _current_run_events in grouped.items():
            job = jobs.get(job_id)
            if not job:
                continue
            if self._is_v1_managed(job_id):
                # Run-end fallback is retained for legacy diagnostics only.
                # A V1 execution has its own durable phase machine; allowing
                # this callback to finish it would bypass frozen plans,
                # receipts and ACK/cleanup gates.
                self.database.add_rclone_event(
                    run_id,
                    EVENT_INFO,
                    f"任务 #{job_id} 已由 V1 durable transfer 接管，跳过旧 run finalizer",
                    {"job_id": job_id, "delegated_to_v1_worker": True},
                )
                continue
            status = str(job.get("status") or "")
            if status == "cancelled":
                self.database.add_event(
                    job_id, EVENT_WARN, "任务已取消，rclone run 结束兜底仅记录不更新状态"
                )
                continue
            if status in {
                "done",
                JOB_WAITING_OPENLIST,
                JOB_WAITING_ORGANIZER,
                "organizing",
                "confirming",
                JOB_REVIEW,
            }:
                self.database.add_rclone_event(
                    run_id,
                    EVENT_INFO,
                    f"任务 #{job_id} 已由分类完成回调处理，run 结束跳过重复刷新",
                )
                continue
            all_job_events = _all_file_events(self.database, job_id=job_id)
            terminal_events = RcloneJobFeasibilityEvaluator.latest_terminal_events(
                all_job_events,
                prefer_source=RcloneJobFeasibilityEvaluator.requires_staging_manifest(job),
            )
            verdict = self.feasibility(job, terminal_events, exit_code)
            level = EVENT_INFO if verdict["ready"] else EVENT_WARN
            self.database.add_event(job_id, level, verdict["message"], verdict)
            self.database.add_rclone_event(run_id, level, f"任务 #{job_id} {verdict['message']}", verdict)
            if not verdict["ready"]:
                if not _update_job_from_snapshot(
                    self.database,
                    job,
                    status=verdict["status"],
                    error_message=verdict["message"],
                ):
                    latest = self.database.get_job(job_id) or job
                    self.database.add_event(
                        job_id,
                        EVENT_WARN,
                        "任务状态已变化，rclone run 结束兜底未覆盖当前状态",
                        {
                            "expected_status": status,
                            "current_status": str(latest.get("status") or ""),
                            "verdict": verdict,
                        },
                    )
                continue
            category_key = str(job.get("category") or "movie")
            ready.setdefault(category_key, []).append((job, terminal_events, verdict))
        return ready

    def _is_v1_managed(self, job_id: int) -> bool:
        repository = self.execution_repository
        if repository is None:
            return False
        for method_name in ("get", "plan"):
            method = getattr(repository, method_name, None)
            if not callable(method):
                continue
            try:
                value = method(int(job_id))
            except Exception:  # noqa: BLE001 - old fallback must remain best-effort
                continue
            if value is not None:
                return True
        return False


def _group_by_job(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        try:
            job_id = int(event.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if job_id > 0:
            grouped.setdefault(job_id, []).append(event)
    return grouped


def _all_file_events(database: Any, **filters: Any) -> list[dict[str, Any]]:
    loader = getattr(database, "list_all_rclone_file_events", None)
    if callable(loader):
        return loader(**filters)
    return database.list_rclone_file_events(limit=1000, **filters)


def _update_job_from_snapshot(database: Any, job: dict[str, Any], **updates: Any) -> bool:
    try:
        job_id = int(job.get("id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    expected_status = str(job.get("status") or "").strip()
    if job_id <= 0 or not expected_status:
        return False
    updater = getattr(database, "update_job_if_status", None)
    if callable(updater):
        return bool(updater(job_id, {expected_status}, **updates))
    latest = database.get_job(job_id) or {}
    if str(latest.get("status") or "").strip() != expected_status:
        return False
    database.update_job(job_id, **updates)
    return True
