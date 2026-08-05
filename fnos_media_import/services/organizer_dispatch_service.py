from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    JOB_DONE,
    JOB_REVIEW,
    JOB_SUBMITTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)


class OrganizerDispatchService:
    """Coordinates completed imports entering the Organizer pipeline."""

    def __init__(
        self,
        *,
        database: Any,
        organizer: Any,
        resolve_plan: Callable[[dict[str, Any]], dict[str, Any] | None],
        resolve_rclone_plan: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        set_completion_stage: Callable[..., dict[str, Any]],
        invalid_virtual_path: Callable[[str], bool],
        dispatch_process: Callable[..., dict[str, Any] | None] | None = None,
    ) -> None:
        self.database = database
        self.organizer = organizer
        self.resolve_plan = resolve_plan
        self.resolve_rclone_plan = resolve_rclone_plan
        self.set_completion_stage = set_completion_stage
        self.invalid_virtual_path = invalid_virtual_path
        # In a split web/worker deployment the Organizer's in-process timers are
        # intentionally suspended in the producer process.  The callback is
        # optional so legacy single-process runtimes continue using those timers.
        self.dispatch_process = dispatch_process

    def set_organizer(self, organizer: Any) -> None:
        """Switch dispatches to the active runtime after a hot reload."""

        self.organizer = organizer

    def enqueue_rclone_completed_items(
        self,
        category_refresh: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(category_refresh, dict):
            return None
        completed_items = category_refresh.get("completed_items")
        if not isinstance(completed_items, list) or not completed_items:
            return None
        unavailable = self._unavailable_reason()
        if unavailable:
            for item in completed_items:
                if not isinstance(item, dict):
                    continue
                job = item.get("job") if isinstance(item.get("job"), dict) else {}
                job_id = _safe_item_job_id(item, job)
                if job_id:
                    self.set_completion_stage(
                        self.database.get_job(job_id) or job,
                        JOB_REVIEW,
                        "review",
                        unavailable,
                    )
            return {"success": False, "skipped": True, "message": unavailable}

        results: list[dict[str, Any]] = []
        for item in completed_items:
            if not isinstance(item, dict):
                continue
            result = self._enqueue_rclone_item(item, category_refresh, payload)
            results.append(result)
        return {
            "success": all(item.get("success") for item in results) if results else True,
            "items": results,
        }

    def _enqueue_rclone_item(
        self,
        item: dict[str, Any],
        category_refresh: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        job_id = _safe_item_job_id(item, job)
        plan = self.resolve_rclone_plan(item) if self.resolve_rclone_plan else None
        root_path = str((plan or {}).get("root_path") or "").strip()
        if not root_path:
            return self._reject_rclone_item(job, job_id, "缺少 rclone 完成路径，无法创建 OpenList 标准化任务")
        if self.invalid_virtual_path(root_path):
            return self._reject_rclone_item(job, job_id, f"rclone 完成路径异常：{root_path}")
        if job_id:
            job = self.set_completion_stage(
                self.database.get_job(job_id) or job,
                JOB_WAITING_ORGANIZER,
                "waiting_organizer",
                "rclone 搬运完成，等待 Organizer 标准化整理与标准目录确认",
                {
                    "openlist_visible_path": root_path,
                    "organizer_scan_path": root_path,
                    "rclone_run_id": payload.get("run_id"),
                },
            )
        try:
            organizer_result = self.organizer.enqueue_from_completed_directory(
                job=job,
                category_label=str(item.get("category_label") or job.get("category_label") or ""),
                root_path=root_path,
                payload={
                    "reason": "rclone_category_done",
                    "rclone": {"payload": payload, "category_refresh": category_refresh},
                    **((plan or {}).get("payload_extra") or {}),
                },
                trigger_type="rclone_category_done",
            )
        except Exception as exc:  # noqa: BLE001
            organizer_result = {"success": False, "message": f"OpenList 标准化任务创建异常：{exc}"}
        organizer_result = self._dispatch_process_if_needed(organizer_result)
        if job_id and organizer_result.get("success") is False:
            message = str(organizer_result.get("message") or "OpenList 标准化任务未创建").strip()
            self.set_completion_stage(
                self.database.get_job(job_id) or job,
                JOB_REVIEW,
                "review",
                message,
            )
        self._record_result(job_id, organizer_result)
        return {"job_id": job_id, **organizer_result}

    def _reject_rclone_item(self, job: dict[str, Any], job_id: int, message: str) -> dict[str, Any]:
        if job_id:
            self.set_completion_stage(
                self.database.get_job(job_id) or job,
                JOB_REVIEW,
                "review",
                message,
            )
        return {"success": False, "skipped": True, "job_id": job_id, "message": message}

    def enqueue_completed_import(
        self,
        result: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        if not self._eligible(job):
            return None
        job_id = _safe_job_id(job)
        unavailable = self._unavailable_reason()
        if unavailable:
            if job_id:
                self.set_completion_stage(job, JOB_REVIEW, "review", unavailable)
            return {"success": False, "skipped": True, "message": unavailable}

        plan = self.resolve_plan(job)
        if not plan:
            return None
        root_path = str(plan.get("root_path") or "").strip()
        if not root_path:
            if job_id:
                self.set_completion_stage(
                    job,
                    JOB_REVIEW,
                    "review",
                    "缺少 OpenList 可扫描路径，无法创建 Organizer 标准化任务",
                )
            return None
        if self.invalid_virtual_path(root_path):
            message = f"OpenList 可扫描路径异常：{root_path}"
            if job_id:
                self.set_completion_stage(job, JOB_REVIEW, "review", message)
            return {"success": False, "skipped": True, "message": message}

        job = self.set_completion_stage(
            job,
            JOB_WAITING_ORGANIZER,
            "waiting_organizer",
            "资源已提交，等待 Organizer 标准化整理与标准目录确认",
            {
                "openlist_visible_path": root_path,
                "organizer_scan_path": root_path,
                "trigger_reason": reason,
            },
        )
        payload = {
            "reason": reason,
            "job": job,
            "directory_plan": plan.get("directory_plan") or {},
            **(plan.get("payload_extra") or {}),
        }
        try:
            organizer_result = self.organizer.enqueue_from_completed_directory(
                job=job,
                category_label=str(job.get("category_label") or ""),
                root_path=root_path,
                payload=payload,
                trigger_type="direct_import_done",
            )
        except Exception as exc:  # noqa: BLE001
            organizer_result = {
                "success": False,
                "message": f"OpenList 标准化任务创建异常：{exc}",
            }
        organizer_result = self._dispatch_process_if_needed(organizer_result)
        if job_id and organizer_result.get("success") is False:
            message = str(organizer_result.get("message") or "OpenList 标准化任务未创建").strip()
            job = self.set_completion_stage(
                self.database.get_job(job_id) or job,
                JOB_REVIEW,
                "review",
                message,
            )
            result["job"] = job
        self._record_result(job_id, organizer_result)
        result["organizer"] = organizer_result
        return organizer_result

    def _dispatch_process_if_needed(self, organizer_result: dict[str, Any]) -> dict[str, Any]:
        """Durably hand an automatically-created Organizer task to a worker.

        ``OrganizerService`` persists the task before returning.  When its
        background timers are suspended (web/scheduler roles), merely
        persisting that task would leave it indefinitely in ``stabilizing`` or
        ``pending``.  The optional callback is therefore treated as a required
        hand-off: a missing/failed queue write is surfaced as a failed result
        instead of claiming that the task is runnable.

        The callback is deliberately invoked for reused task records too.  A
        process can crash after task creation but before the first queue write;
        idempotent WorkerTaskRepository enqueue then safely repairs that gap.
        """

        if not isinstance(organizer_result, dict):
            return {
                "success": False,
                "message": "Organizer 返回了无效任务结果，未提交 Worker 队列",
            }
        if self.dispatch_process is None or organizer_result.get("success") is False:
            return organizer_result

        task_id = _safe_positive_int(organizer_result.get("task_id"))
        if not task_id:
            return {
                **organizer_result,
                "success": False,
                "message": "Organizer 任务已返回但缺少 task_id，未提交 Worker 队列",
            }
        try:
            queued = self.dispatch_process(task_id, auto_apply=True)
        except Exception as exc:  # noqa: BLE001
            queued = {"success": False, "message": f"Organizer Worker 持久化投递异常：{exc}"}
        if not isinstance(queued, dict) or queued.get("success") is False:
            message = str(
                (queued or {}).get("message") if isinstance(queued, dict) else "Worker 队列未返回结果"
            ).strip()
            return {
                **organizer_result,
                "success": False,
                "message": f"Organizer 任务已创建，但持久化 Worker 投递失败：{message or '未知错误'}",
                "worker_dispatch": queued if isinstance(queued, dict) else None,
            }
        return {**organizer_result, "worker_dispatch": queued}

    def _unavailable_reason(self) -> str:
        if not self.organizer.enabled:
            return "Organizer 未启用，已提交资源但不能确认完整整理入库"
        if not self.organizer.openlist.configured:
            return "OpenList 未配置，已提交资源但不能确认标准目录"
        return ""

    def _record_result(self, job_id: int, organizer_result: dict[str, Any]) -> None:
        if not job_id:
            return
        level = "info" if organizer_result.get("success") else "warn"
        if organizer_result.get("queued") and not organizer_result.get("skipped"):
            self.database.add_event(
                job_id,
                level,
                organizer_result.get("message") or "已创建 OpenList 标准化任务",
                {"organizer": organizer_result},
            )
        elif organizer_result.get("success") is False:
            self.database.add_event(
                job_id,
                level,
                organizer_result.get("message") or "OpenList 标准化任务未创建",
                {"organizer": organizer_result},
            )

    @staticmethod
    def _eligible(job: dict[str, Any]) -> bool:
        if not job:
            return False
        status = str(job.get("status") or "").strip()
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        submitted_cloud139_native = (
            status == JOB_SUBMITTED
            and str(job.get("source_type") or "").strip().lower() == "cloud139"
            and str(raw_data.get("provider") or "").strip() == "cmcc_native"
            and isinstance(raw_data.get("save"), dict)
        )
        return status in {JOB_DONE, JOB_WAITING_OPENLIST, JOB_WAITING_ORGANIZER} or submitted_cloud139_native


def _safe_job_id(job: dict[str, Any]) -> int:
    try:
        value = int(job.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 999999999 else 0


def _safe_item_job_id(item: dict[str, Any], job: dict[str, Any]) -> int:
    return _safe_job_id({"id": item.get("job_id") or job.get("id")})


def _safe_positive_int(value: Any) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return normalized if 0 < normalized <= 999999999 else 0
