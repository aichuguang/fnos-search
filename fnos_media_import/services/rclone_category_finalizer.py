from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    CALLBACK_STATUS_CATEGORY_DONE,
    EVENT_INFO,
    EVENT_WARN,
    JOB_REVIEW,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)
from .rclone_job_feasibility import RcloneJobFeasibilityEvaluator


class RcloneCategoryFinalizer:
    """Finalizes jobs after a complete rclone category callback."""

    def __init__(
        self,
        *,
        database: Any,
        categories: Callable[[], dict[str, dict[str, Any]]],
        category_key: Callable[[str, str], str],
        event_matches: Callable[..., bool],
        feasibility: Callable[[dict[str, Any], list[dict[str, Any]], int], dict[str, Any]],
        finish_ready: Callable[..., dict[str, Any]],
    ) -> None:
        self.database = database
        self.categories = categories
        self.category_key = category_key
        self.event_matches = event_matches
        self.feasibility = feasibility
        self.finish_ready = finish_ready

    def finalize(self, run_id: int, category_label: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.database:
            return {"success": False, "skipped": True, "message": "数据库未初始化，跳过分类刷新"}
        run_id = _positive_int(run_id)
        if not run_id:
            return {"success": False, "skipped": True, "message": "缺少 rclone run_id，跳过分类刷新"}
        status = str(payload.get("status") or "").strip().lower()
        if status != CALLBACK_STATUS_CATEGORY_DONE:
            return {"success": True, "skipped": True, "message": "不是分类成功回调，跳过分类刷新"}
        moved_count = _int_value(payload.get("moved_count"))
        failed_count = _int_value(payload.get("failed_count"))
        if failed_count > 0:
            message = f"分类仍有失败文件 {failed_count} 个，暂不刷新媒体库"
            self.database.add_rclone_event(run_id, EVENT_WARN, message, payload)
            return {"success": False, "skipped": True, "message": message, "failed_count": failed_count}

        target_path = str(payload.get("target_path") or "").strip()
        category_key = self.category_key(category_label, target_path)
        category = self.categories().get(category_key, {})
        file_events = _all_file_events(self.database, run_id=run_id)
        matched_events = [
            event for event in file_events
            if self.event_matches(
                event,
                category_label=category_label,
                category_key=category_key,
                category=category,
                target_path=target_path,
            )
        ]
        if moved_count <= 0 and not matched_events:
            return {
                "success": True,
                "skipped": True,
                "message": "分类没有新增搬运文件，也没有可确认的任务文件事件，跳过完成处理",
            }
        refresh_events = [*matched_events, {
            "status": status,
            "category": category_label,
            "filename": "",
            "source_path": str(payload.get("source_path") or "").strip(),
            "target_path": target_path,
            "raw_data": payload,
        }]
        grouped = _group_events_by_job(matched_events)
        ready_items, blocked_items = self._classify_jobs(run_id, grouped)
        if grouped and not ready_items:
            message = f"分类 {category_label or category_key} 搬运完成，但没有完整任务需要刷新，等待后续兜底"
            self.database.add_rclone_event(
                run_id,
                EVENT_WARN if blocked_items else EVENT_INFO,
                message,
                {"payload": payload, "blocked": blocked_items},
            )
            return {
                "success": True,
                "skipped": True,
                "message": message,
                "category": category_key,
                "blocked": blocked_items,
            }
        result = self.finish_ready(
            run_id,
            category_key,
            category,
            ready_items,
            refresh_events,
            trigger="rclone_category_done",
            success_message="rclone 分类搬运完成，已移交 Organizer 整理",
        )
        return {
            **result,
            "category": category_key,
            "matched_file_count": len(matched_events),
            "matched_job_count": len(ready_items),
            "moved_count": moved_count,
            "failed_count": failed_count,
        }

    def _classify_jobs(
        self,
        run_id: int,
        grouped: dict[int, list[dict[str, Any]]],
    ) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]], list[dict[str, Any]]]:
        ready = []
        blocked = []
        jobs = self.database.get_jobs_by_ids(list(grouped.keys()))
        for job_id, _current_run_events in grouped.items():
            job = jobs.get(job_id)
            if not job:
                continue
            status = str(job.get("status") or "")
            if status == "cancelled":
                self.database.add_event(job_id, EVENT_WARN, "任务已取消，分类完成回调仅记录不刷新该任务")
                continue
            if status in {
                "done",
                JOB_WAITING_OPENLIST,
                JOB_WAITING_ORGANIZER,
                "organizing",
                "confirming",
                JOB_REVIEW,
            }:
                continue
            all_job_events = _all_file_events(self.database, job_id=job_id)
            terminal_events = RcloneJobFeasibilityEvaluator.latest_terminal_events(
                all_job_events,
                prefer_source=RcloneJobFeasibilityEvaluator.requires_staging_manifest(job),
            )
            verdict = self.feasibility(job, terminal_events, 0)
            level = EVENT_INFO if verdict["ready"] else EVENT_WARN
            self.database.add_event(job_id, level, verdict["message"], verdict)
            self.database.add_rclone_event(run_id, level, f"任务 #{job_id} {verdict['message']}", verdict)
            if verdict["ready"]:
                ready.append((job, terminal_events, verdict))
            else:
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
                        "任务状态已变化，分类完成兜底未覆盖当前状态",
                        {
                            "expected_status": status,
                            "current_status": str(latest.get("status") or ""),
                            "verdict": verdict,
                        },
                    )
                    continue
                blocked.append({"job_id": job_id, **verdict})
        return ready, blocked


def _group_events_by_job(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        job_id = _positive_int(event.get("job_id"))
        if job_id:
            grouped.setdefault(job_id, []).append(event)
    return grouped


def _positive_int(value: Any) -> int:
    parsed = _int_value(value)
    return parsed if parsed > 0 else 0


def _int_value(value: Any) -> int:
    try:
        return int(str(value or 0).strip())
    except (TypeError, ValueError):
        return 0


def _all_file_events(database: Any, **filters: Any) -> list[dict[str, Any]]:
    loader = getattr(database, "list_all_rclone_file_events", None)
    if callable(loader):
        return loader(**filters)
    return database.list_rclone_file_events(limit=1000, **filters)


def _update_job_from_snapshot(database: Any, job: dict[str, Any], **updates: Any) -> bool:
    job_id = _positive_int(job.get("id"))
    expected_status = str(job.get("status") or "").strip()
    if not job_id or not expected_status:
        return False
    updater = getattr(database, "update_job_if_status", None)
    if callable(updater):
        return bool(updater(job_id, {expected_status}, **updates))
    latest = database.get_job(job_id) or {}
    if str(latest.get("status") or "").strip() != expected_status:
        return False
    database.update_job(job_id, **updates)
    return True
