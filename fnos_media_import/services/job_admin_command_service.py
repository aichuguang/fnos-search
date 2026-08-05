from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class ImportCommands(Protocol):
    def retry_job(self, job_id: int) -> dict[str, Any]: ...


class JobRecords(Protocol):
    def get_job(self, job_id: int) -> dict[str, Any] | None: ...
    def delete_job_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
    ) -> bool: ...


@dataclass(frozen=True)
class JobAdminCommandDependencies:
    imports: ImportCommands
    jobs: JobRecords
    auto_start_rclone: Callable[[dict[str, Any], str], dict[str, Any] | None]
    worker_dispatcher: Any | None = None


class JobAdminCommandService:
    MAX_BATCH_SIZE = 50

    def __init__(self, dependencies: JobAdminCommandDependencies) -> None:
        self._deps = dependencies

    def retry(self, job_id: int, *, reason: str | None = None) -> dict[str, Any]:
        if self._deps.worker_dispatcher:
            queued = self._deps.worker_dispatcher.import_retry(
                job_id, reason=reason or f"admin_retry:{job_id}"
            )
            if queued:
                return queued
        result = self._deps.imports.retry_job(job_id)
        self._deps.auto_start_rclone(result, reason or f"admin_retry:{job_id}")
        return {"success": bool(result.get("success", True)), **result}

    def delete(self, job_id: int) -> tuple[dict[str, Any], int]:
        job = self._deps.jobs.get_job(job_id)
        if not job:
            return {"success": False, "message": "任务不存在"}, 404
        status = str(job.get("status") or "").strip().lower()
        deletable_statuses = {"done", "success", "completed", "cancelled"}
        if status not in deletable_statuses:
            return {"success": False, "message": "只能删除已完成或已取消的任务记录"}, 409
        if not self._deps.jobs.delete_job_if_status(job_id, deletable_statuses):
            return {"success": False, "message": "任务状态已变化，删除未执行，请刷新后重试"}, 409
        return {
            "success": True,
            "deleted": True,
            "job_id": job_id,
            "message": "记录已删除",
        }, 200

    def batch_retry(self, raw_job_ids: Any) -> tuple[dict[str, Any], int]:
        if not isinstance(raw_job_ids, list) or not raw_job_ids:
            return {"success": False, "message": "job_ids 不能为空"}, 400
        items: list[dict[str, Any]] = []
        success_count = 0
        for raw_id in raw_job_ids[: self.MAX_BATCH_SIZE]:
            job_id = self._normalize_id(raw_id)
            if job_id is None:
                continue
            try:
                if self._deps.worker_dispatcher:
                    queued = self._deps.worker_dispatcher.import_retry(
                        job_id, reason=f"admin_batch_retry:{job_id}"
                    )
                    if queued:
                        success_count += 1
                        items.append({"job_id": job_id, **queued})
                        continue
                result = self._deps.imports.retry_job(job_id)
                rclone_start = self._deps.auto_start_rclone(result, f"admin_batch_retry:{job_id}")
                success = bool(result.get("success", True))
                success_count += int(success)
                items.append({
                    "job_id": job_id,
                    "success": success,
                    "message": result.get("message") or "",
                    "rclone_start": rclone_start,
                })
            except Exception as exc:  # noqa: BLE001
                items.append({"job_id": job_id, "success": False, "message": str(exc)})
        return {
            "success": success_count == len(items),
            "success_count": success_count,
            "items": items,
        }, 200

    @staticmethod
    def _normalize_id(value: Any) -> int | None:
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if 1 <= result <= 999_999_999 else None
