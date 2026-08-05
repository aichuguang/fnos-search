from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    JOB_DONE,
    JOB_SUBMITTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
    JOB_WAITING_TRANSFER,
)
from .import_staging_service import rclone_staging_run_from_job


class ImportCompletionDispatcher:
    """Dispatches completed import jobs to rclone staging or Organizer."""

    def __init__(
        self,
        *,
        database: Any,
        rclone_service: Any,
        category: Callable[[str], dict[str, Any]],
        enqueue_organizer: Callable[[dict[str, Any], str], dict[str, Any] | None],
    ) -> None:
        self.database = database
        self.rclone_service = rclone_service
        self.category = category
        self.enqueue_organizer = enqueue_organizer

    def dispatch(self, result: dict[str, Any], reason: str) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("success", True):
            return None
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        if not job:
            return None
        status = str(job.get("status") or "").strip()
        if self.uses_rclone_staging(job) and status in {JOB_WAITING_TRANSFER, JOB_WAITING_OPENLIST}:
            return self._start_and_record(result, job, reason)
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        cloud139_saved = (
            status == JOB_SUBMITTED
            and str(job.get("source_type") or "").strip().lower() == "cloud139"
            and isinstance(raw_data.get("save"), dict)
        )
        if status in {JOB_DONE, JOB_WAITING_OPENLIST, JOB_WAITING_ORGANIZER} or cloud139_saved:
            return self.enqueue_organizer(result, reason)
        if status != JOB_WAITING_TRANSFER:
            return None
        return self._start_and_record(result, job, reason)

    def start_rclone_for_job(self, job: dict[str, Any], reason: str) -> dict[str, Any] | None:
        if not job:
            return None
        job_id = _job_id(job)
        category_key = str(job.get("category") or "").strip()
        try:
            staging_run = rclone_staging_run_from_job(job)
        except ValueError as exc:
            result = {"success": False, "message": f"拒绝启动 rclone：{exc}"}
            if job_id:
                self.database.add_event(job_id, "error", result["message"], {"reason": reason})
            return result
        if staging_run:
            # The category key is a stable worker alias. Runtime labels and
            # library names may be edited after this job has been created.
            category_filter = staging_run["category"]
            result = self.rclone_service.start(
                reason=reason,
                category_filter=category_filter,
                staging_run=staging_run,
            )
        else:
            category = self.category(category_key)
            category_filter = str(
                category.get("fnos_lib")
                or job.get("category_label")
                or category.get("label")
                or category_key
            ).strip()
            result = self.rclone_service.start(reason=reason, category_filter=category_filter)
        if job_id:
            event_data = {"reason": reason, "category_filter": category_filter, "rclone": result}
            if staging_run:
                event_data["staging_run"] = staging_run
            self.database.add_event(
                job_id,
                "info" if result.get("success") else "warn",
                result.get("message") or "已通知 rclone 搬运队列",
                event_data,
            )
        return result

    @staticmethod
    def uses_rclone_staging(job: dict[str, Any]) -> bool:
        source_type = str((job or {}).get("source_type") or "").strip().lower()
        target_route = str((job or {}).get("target_route") or "").strip().lower()
        return source_type in {"quark", "uc"} or target_route == "quark_to_mobile"

    def _start_and_record(
        self,
        result: dict[str, Any],
        job: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        start_result = self.start_rclone_for_job(job, reason)
        result["rclone_start"] = start_result
        job_id = _job_id(job)
        if job_id and isinstance(start_result, dict):
            if start_result.get("queued"):
                level, message = "info", "rclone 正在搬运，当前任务已加入后续自动搬运队列"
            elif start_result.get("success"):
                level, message = "info", "已通知 rclone 开始搬运当前分类"
            else:
                level, message = "warn", start_result.get("message") or "rclone 自动搬运启动失败"
            self.database.add_event(job_id, level, message, start_result)
        return start_result


def _job_id(job: dict[str, Any]) -> int:
    try:
        value = int(job.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 999999999 else 0
