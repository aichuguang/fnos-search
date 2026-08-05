from __future__ import annotations

from collections import Counter
from typing import Any

from ..database import Database


PHASE_LABELS = {
    "submission": "提交申请",
    "review": "审核",
    "import": "网盘入库",
    "transfer": "rclone 搬运",
    "organize": "文件整理",
    "media": "媒体库刷新",
    "complete": "完成确认",
    "system": "系统处理",
}

TERMINAL_STATUSES = {"done", "success", "completed", "failed", "error", "cancelled"}
FAILED_STATUSES = {"failed", "error", "upload_error", "upload_exception", "cancelled"}
DONE_STATUSES = {"done", "success", "completed", "skipped", "skipped_existing"}


class JobService:
    def __init__(self, db: Database):
        self.db = db

    def list_jobs(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        return self.db.list_jobs(limit=limit, status=status)

    def get_job_with_events(self, job_id: int) -> dict[str, Any] | None:
        job = self.db.get_job(job_id)
        if not job:
            return None

        events = self.db.list_events(job_id)
        guest_requests = self.db.list_guest_requests_by_job(job_id)
        request_ids = [int(item["id"]) for item in guest_requests if item.get("id")]
        guest_events_by_request = self.db.list_guest_request_events_for_requests(request_ids)
        for guest_request in guest_requests:
            request_id = int(guest_request.get("id") or 0)
            guest_request["events"] = guest_events_by_request.get(request_id, [])

        rclone_file_events = self.db.list_all_rclone_file_events(job_id=job_id)
        organizer_task_rows = self.db.list_organizer_tasks_by_job(job_id, limit=100)
        organizer_tasks: list[dict[str, Any]] = []
        organizer_operations: list[dict[str, Any]] = []
        for task_row in organizer_task_rows:
            task_id = int(task_row.get("id") or 0)
            detail = self.db.get_organizer_task(task_id) if task_id else None
            task = detail or task_row
            organizer_tasks.append(task)
            organizer_operations.extend(task.get("operations") or [])

        organizer_task_ids = [int(item["id"]) for item in organizer_tasks if item.get("id")]
        organizer_runs = self.db.list_organizer_runs_by_task_ids(organizer_task_ids)
        worker_repository = getattr(self.db, "worker_tasks", None)
        worker_tasks = (
            worker_repository.list_related(
                job_id=job_id,
                guest_request_ids=request_ids,
                organizer_task_ids=organizer_task_ids,
            )
            if worker_repository and hasattr(worker_repository, "list_related")
            else []
        )
        latest_organizer_task = organizer_tasks[0] if organizer_tasks else None
        organizer_mappings = (latest_organizer_task or {}).get("mappings") or []

        timeline, technical_events = self._build_timelines(
            job,
            events,
            guest_requests,
            rclone_file_events,
            organizer_tasks,
            organizer_runs,
            organizer_operations,
            worker_tasks,
        )

        job["events"] = events
        job["guest_requests"] = guest_requests
        job["rclone_file_events"] = rclone_file_events
        job["organizer_tasks"] = organizer_tasks
        job["organizer_runs"] = organizer_runs
        job["latest_organizer_task"] = latest_organizer_task
        job["organizer_mappings"] = organizer_mappings
        job["organizer_operations"] = organizer_operations
        job["worker_tasks"] = worker_tasks
        job["timeline"] = timeline
        job["technical_events"] = technical_events
        job["timeline_summary"] = {
            "milestone_count": len(timeline),
            "technical_event_count": len(technical_events),
            "phase_count": len({item.get("phase") for item in timeline if item.get("phase")}),
        }
        return job

    def _build_timeline(
        self,
        job: dict[str, Any],
        events: list[dict[str, Any]],
        guest_requests: list[dict[str, Any]],
        rclone_file_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        timeline, _ = self._build_timelines(job, events, guest_requests, rclone_file_events, [], [], [], [])
        return timeline

    def _build_timelines(
        self,
        job: dict[str, Any],
        events: list[dict[str, Any]],
        guest_requests: list[dict[str, Any]],
        rclone_file_events: list[dict[str, Any]],
        organizer_tasks: list[dict[str, Any]],
        organizer_runs: list[dict[str, Any]],
        organizer_operations: list[dict[str, Any]],
        worker_tasks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        timeline: list[dict[str, Any]] = []
        technical: list[dict[str, Any]] = []
        job_id = job.get("id")

        def add(
            target: list[dict[str, Any]],
            *,
            source: str,
            source_label: str,
            phase: str,
            message: str,
            created_at: Any,
            level: str = "info",
            status: Any = None,
            raw_data: Any = None,
            **details: Any,
        ) -> None:
            target.append(
                {
                    "type": source,
                    "source": source,
                    "source_label": source_label,
                    "phase": phase,
                    "phase_label": PHASE_LABELS.get(phase, PHASE_LABELS["system"]),
                    "level": level or "info",
                    "message": str(message or ""),
                    "created_at": created_at or "",
                    "job_id": job_id,
                    "status": status,
                    "raw_data": raw_data,
                    **details,
                }
            )

        for guest_request in guest_requests:
            request_id = guest_request.get("id")
            token = guest_request.get("request_token")
            if guest_request.get("created_at"):
                add(
                    timeline,
                    source="guest_request",
                    source_label="访客申请",
                    phase="submission",
                    message="访客提交入库申请",
                    created_at=guest_request.get("created_at"),
                    status=guest_request.get("status"),
                    request_id=request_id,
                    request_token=token,
                )
                add(
                    technical,
                    source="guest_request",
                    source_label="访客申请",
                    phase="submission",
                    message="访客申请已创建",
                    created_at=guest_request.get("created_at"),
                    status=guest_request.get("status"),
                    raw_data=guest_request.get("raw_data"),
                    request_id=request_id,
                    request_token=token,
                )
            for event in guest_request.get("events") or []:
                phase = self._phase_for_message(event.get("message"), "review")
                kwargs = {
                    "source": "guest_request_event",
                    "source_label": "申请审核",
                    "phase": phase,
                    "message": event.get("message") or "",
                    "created_at": event.get("created_at"),
                    "level": event.get("level") or "info",
                    "raw_data": event.get("raw_data"),
                    "request_id": request_id,
                    "request_token": token,
                    "event_id": event.get("id"),
                }
                add(timeline, **kwargs)
                add(technical, **kwargs)

        if job.get("created_at"):
            add(
                timeline,
                source="job",
                source_label="入库任务",
                phase="import",
                message="入库任务已创建",
                created_at=job.get("created_at"),
                status=job.get("status"),
            )
            add(
                technical,
                source="job",
                source_label="入库任务",
                phase="import",
                message="入库任务已创建",
                created_at=job.get("created_at"),
                status=job.get("status"),
                raw_data=job.get("raw_data"),
            )

        for event in events:
            phase = self._phase_for_message(event.get("message"), "import")
            kwargs = {
                "source": "job_event",
                "source_label": "入库任务",
                "phase": phase,
                "message": event.get("message") or "",
                "created_at": event.get("created_at"),
                "level": event.get("level") or "info",
                "raw_data": event.get("raw_data"),
                "event_id": event.get("id"),
            }
            add(timeline, **kwargs)
            add(technical, **kwargs)

        technical.extend(self._rclone_technical_events(job_id, rclone_file_events))
        timeline.extend(self._rclone_milestones(job_id, rclone_file_events))

        for task in organizer_tasks:
            task_id = task.get("id")
            if task.get("created_at"):
                add(
                    timeline,
                    source="organizer_task",
                    source_label="文件整理",
                    phase="organize",
                    message="文件整理任务已创建",
                    created_at=task.get("created_at"),
                    status=task.get("status"),
                    organizer_task_id=task_id,
                )
                add(
                    technical,
                    source="organizer_task",
                    source_label="文件整理",
                    phase="organize",
                    message="文件整理任务已创建",
                    created_at=task.get("created_at"),
                    status=task.get("status"),
                    raw_data=task.get("raw_data"),
                    organizer_task_id=task_id,
                )

        for run in organizer_runs:
            status = str(run.get("status") or "")
            message = self._organizer_run_message(status)
            level = "error" if status in FAILED_STATUSES else "info"
            add(
                timeline,
                source="organizer_run",
                source_label="文件整理",
                phase="organize",
                message=message,
                created_at=run.get("finished_at") or run.get("started_at"),
                level=level,
                status=status,
                raw_data=run.get("summary"),
                organizer_task_id=run.get("task_id"),
                organizer_run_id=run.get("id"),
            )
            add(
                technical,
                source="organizer_run",
                source_label="文件整理运行",
                phase="organize",
                message=message,
                created_at=run.get("finished_at") or run.get("started_at"),
                level=level,
                status=status,
                raw_data=run.get("summary"),
                organizer_task_id=run.get("task_id"),
                organizer_run_id=run.get("id"),
                error_message=run.get("error_message"),
            )

        for operation in organizer_operations:
            status = str(operation.get("status") or "")
            add(
                technical,
                source="organizer_operation",
                source_label="整理操作",
                phase="organize",
                message=operation.get("description") or operation.get("type") or "整理操作",
                created_at=operation.get("updated_at") or operation.get("created_at"),
                level="error" if status in FAILED_STATUSES else "info",
                status=status,
                raw_data=operation.get("raw_data"),
                event_id=operation.get("id"),
                organizer_task_id=operation.get("task_id"),
                organizer_run_id=operation.get("run_id"),
                source_path=operation.get("source_path"),
                target_path=operation.get("target_path"),
                error_message=operation.get("error_message"),
            )

        for worker_task in worker_tasks:
            worker_status = str(worker_task.get("status") or "")
            add(
                technical,
                source="worker_task",
                source_label="后台执行任务",
                phase="system",
                message=f"{worker_task.get('task_type') or 'worker'} · {worker_status or 'unknown'}",
                created_at=worker_task.get("completed_at") or worker_task.get("updated_at") or worker_task.get("started_at") or worker_task.get("created_at"),
                level="error" if worker_status == "failed" else "info",
                status=worker_status,
                raw_data={
                    "payload": worker_task.get("payload"),
                    "result": worker_task.get("result"),
                    "attempts": worker_task.get("attempts"),
                    "max_attempts": worker_task.get("max_attempts"),
                    "owner_id": worker_task.get("owner_id"),
                },
                event_id=worker_task.get("id"),
                error_message=worker_task.get("error_message"),
            )

        status = str(job.get("status") or "").lower()
        if status in TERMINAL_STATUSES and job.get("updated_at"):
            message = {
                "done": "任务处理完成",
                "success": "任务处理完成",
                "completed": "任务处理完成",
                "cancelled": "任务已取消",
                "failed": "任务处理失败",
                "error": "任务处理失败",
            }.get(status, "任务状态已更新")
            add(
                timeline,
                source="job_status",
                source_label="任务结果",
                phase="complete",
                message=message,
                created_at=job.get("updated_at"),
                level="error" if status in FAILED_STATUSES else "info",
                status=status,
                raw_data={"error_message": job.get("error_message")} if job.get("error_message") else None,
            )

        timeline = self._deduplicate_timeline(timeline)
        technical = sorted(technical, key=self._sort_key)
        return timeline, technical

    def _rclone_technical_events(
        self, job_id: Any, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in events:
            items.append(
                {
                    "type": "rclone_file_event",
                    "source": "rclone_file_event",
                    "source_label": "rclone 文件",
                    "phase": "transfer",
                    "phase_label": PHASE_LABELS["transfer"],
                    "level": event.get("level") or "info",
                    "message": event.get("message") or event.get("filename") or "文件搬运记录",
                    "created_at": event.get("created_at") or "",
                    "job_id": job_id,
                    "event_id": event.get("id"),
                    "run_id": event.get("run_id"),
                    "status": event.get("status"),
                    "category": event.get("category"),
                    "filename": event.get("filename"),
                    "source_path": event.get("source_path"),
                    "target_path": event.get("target_path"),
                    "raw_data": event.get("raw_data"),
                }
            )
        return items

    def _rclone_milestones(self, job_id: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for event in events:
            grouped.setdefault(event.get("run_id") or "unknown", []).append(event)

        milestones: list[dict[str, Any]] = []
        for run_id, run_events in grouped.items():
            latest_by_file: dict[str, dict[str, Any]] = {}
            for event in sorted(run_events, key=self._sort_key):
                key = str(event.get("source_path") or event.get("filename") or event.get("id") or "")
                latest_by_file[key] = event
            statuses = Counter(str(item.get("status") or "unknown").lower() for item in latest_by_file.values())
            done = sum(count for name, count in statuses.items() if name in DONE_STATUSES)
            failed = sum(count for name, count in statuses.items() if name in FAILED_STATUSES)
            active = max(0, len(latest_by_file) - done - failed)
            parts = [f"完成 {done} 个文件"]
            if failed:
                parts.append(f"失败 {failed} 个")
            if active:
                parts.append(f"处理中 {active} 个")
            latest = max(run_events, key=self._sort_key)
            milestones.append(
                {
                    "type": "rclone_summary",
                    "source": "rclone_summary",
                    "source_label": "rclone 搬运",
                    "phase": "transfer",
                    "phase_label": PHASE_LABELS["transfer"],
                    "level": "error" if failed else "info",
                    "message": "搬运汇总：" + "，".join(parts),
                    "created_at": latest.get("created_at") or "",
                    "job_id": job_id,
                    "run_id": None if run_id == "unknown" else run_id,
                    "status": "failed" if failed else "done" if not active else "running",
                    "raw_data": {"file_count": len(latest_by_file), "status_counts": dict(statuses)},
                }
            )
        return milestones

    @staticmethod
    def _organizer_run_message(status: str) -> str:
        return {
            "running": "文件整理开始执行",
            "success": "文件整理执行完成",
            "done": "文件整理执行完成",
            "completed": "文件整理执行完成",
            "failed": "文件整理执行失败",
            "error": "文件整理执行失败",
            "cancelled": "文件整理已取消",
            "rolled_back": "文件整理已回滚",
        }.get(status.lower(), f"文件整理状态：{status or '未知'}")

    @staticmethod
    def _phase_for_message(message: Any, fallback: str) -> str:
        text = str(message or "").lower()
        rules = (
            ("media", ("媒体库", "媒体刷新", "fnos", "jellyfin", "emby")),
            ("organize", ("organizer", "整理", "重命名", "openlist", "strm")),
            ("transfer", ("rclone", "搬运", "转存", "传输", "移动文件")),
            ("review", ("审核", "驳回", "批准", "通过申请")),
            ("submission", ("提交申请", "访客申请")),
            ("complete", ("完成", "成功", "失败", "取消", "异常")),
        )
        for phase, keywords in rules:
            if any(keyword in text for keyword in keywords):
                return phase
        return fallback

    def _deduplicate_timeline(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in sorted(items, key=self._sort_key):
            signature = (
                item.get("phase"),
                str(item.get("message") or "").strip(),
                str(item.get("level") or "info"),
                str(item.get("status") or ""),
            )
            if result and result[-1].get("_signature") == signature:
                result[-1]["occurrence_count"] = int(result[-1].get("occurrence_count") or 1) + 1
                result[-1]["created_at"] = item.get("created_at") or result[-1].get("created_at")
                continue
            row = dict(item)
            row["_signature"] = signature
            result.append(row)
        for item in result:
            item.pop("_signature", None)
        return result

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> tuple[str, str, int]:
        return (
            str(item.get("created_at") or ""),
            str(item.get("type") or item.get("source") or ""),
            int(item.get("event_id") or item.get("id") or 0),
        )
