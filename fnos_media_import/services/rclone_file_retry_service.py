from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class RetryDatabase(Protocol):
    def get_rclone_file_event(self, event_id: int) -> dict[str, Any] | None: ...
    def add_rclone_file_event(self, **data: Any) -> int: ...
    def add_event(self, job_id: int, level: str, message: str, raw_data: Any = None) -> int: ...


class FileRetryRunner(Protocol):
    def start_file_retry(self, file_event: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RcloneFileRetryDependencies:
    database: RetryDatabase
    runner: FileRetryRunner


class RcloneFileRetryService:
    def __init__(self, dependencies: RcloneFileRetryDependencies) -> None:
        self._deps = dependencies

    def retry(self, event_id: int, *, force: bool) -> tuple[dict[str, Any], int]:
        file_event = self._deps.database.get_rclone_file_event(event_id)
        if not file_event:
            return {"success": False, "message": "文件搬运记录不存在"}, 404
        status = str(file_event.get("status") or "").strip().lower()
        if status not in {"failed", "error"} and not force:
            return {"success": False, "message": "只有失败文件默认允许单独重试，如需强制重试请传 force=true"}, 400
        result = self._deps.runner.start_file_retry(file_event)
        http_status = int(result.pop("_http_status", 200) or 200)
        if result.get("success"):
            message = f"已请求单文件重试：{file_event.get('filename') or ''}"
            retry_event_id = self._deps.database.add_rclone_file_event(
                run_id=None,
                job_id=file_event.get("job_id"),
                status="retry_requested",
                level="info",
                category=str(file_event.get("category") or ""),
                filename=str(file_event.get("filename") or ""),
                source_path=str(file_event.get("source_path") or ""),
                target_path=str(file_event.get("target_path") or ""),
                message=message,
                raw_data={"source_event_id": event_id, "force": force},
            )
            if file_event.get("job_id"):
                self._deps.database.add_event(int(file_event["job_id"]), "info", message, {"source_file_event_id": event_id, "retry_file_event_id": retry_event_id})
            result["retry_file_event_id"] = retry_event_id
        return {
            "success": bool(result.get("success")),
            **result,
            "source_event": file_event,
        }, http_status
