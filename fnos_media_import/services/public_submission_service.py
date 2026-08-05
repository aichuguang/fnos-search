from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class GuestRequestQueries(Protocol):
    def get(self, request_id: int) -> dict[str, Any] | None: ...
    def get_by_token(self, request_token: str) -> dict[str, Any] | None: ...
    def find_recent_by_url(self, *, source_url: str, category: str, within_minutes: int) -> dict[str, Any] | None: ...


class GuestRequestCommands(Protocol):
    def create_with_event(self, data: dict[str, Any], *, level: str, message: str, event_data: Any = None, emit: Callable[..., Any] | None = None) -> int: ...
    def transition_with_event(self, request_id: int, **kwargs: Any) -> bool: ...
    def bind_job_with_event(self, request_id: int, **kwargs: Any) -> str: ...


@dataclass(frozen=True)
class PublicSubmissionDependencies:
    queries: GuestRequestQueries
    commands: GuestRequestCommands
    sync_request: Callable[[dict[str, Any]], dict[str, Any]]
    public_status: Callable[[str], str]
    public_request: Callable[[dict[str, Any]], dict[str, Any]]
    db: Any = None
    emit_notification: Callable[..., Any] | None = None


class PublicSubmissionService:
    TERMINAL_DUPLICATE_STATUSES = {"rejected", "cancelled", "failed", "unsupported"}

    def __init__(self, dependencies: PublicSubmissionDependencies) -> None:
        self._deps = dependencies

    def duplicate_response(
        self,
        *,
        source_url: str,
        category: str,
        within_minutes: int,
        enabled: bool,
        scoped_selection: bool,
    ) -> dict[str, Any] | None:
        if not enabled or scoped_selection:
            return None
        duplicate = self._deps.queries.find_recent_by_url(
            source_url=source_url, category=category, within_minutes=within_minutes
        )
        if not duplicate:
            return None
        duplicate = self._deps.sync_request(duplicate)
        status = str(duplicate.get("status") or "").strip().lower()
        if status in self.TERMINAL_DUPLICATE_STATUSES:
            return None
        return {
            "success": True,
            "message": "该资源近期已提交过，已返回原提交编号",
            "request_token": duplicate.get("request_token"),
            "status": duplicate.get("public_status") or self._deps.public_status(status),
            "request": self._deps.public_request(duplicate),
            "duplicate": True,
        }

    def get_public_request(self, token: str, *, query_enabled: bool) -> tuple[dict[str, Any], int]:
        if not query_enabled:
            return {"success": False, "message": "提交结果查询已关闭"}, 403
        item = self._deps.queries.get_by_token(token)
        if not item:
            return {"success": False, "message": "提交记录不存在"}, 404
        synced = self._deps.sync_request(item)
        return {"success": True, "request": self._deps.public_request(synced)}, 200

    def create_initial_request(
        self,
        request_data: dict[str, Any],
        *,
        event_data: dict[str, Any],
        preflight_checked: bool,
        emit: Callable[..., Any] | None = None,
    ) -> int:
        message = "访客提交资源，已完成提交前资源检测" if preflight_checked else "访客提交资源"
        return self._deps.commands.create_with_event(
            request_data, level="info", message=message, event_data=event_data, emit=emit
        )

    def send_to_review(
        self,
        request_id: int,
        *,
        reason: str,
        event_message: str,
        event_data: dict[str, Any],
        raw_patch: dict[str, Any],
        level: str | None = None,
    ) -> dict[str, Any] | None:
        return self._transition(
            request_id,
            status="pending_review",
            public_status="等待处理",
            reason=reason,
            event_message=event_message,
            event_data=event_data,
            raw_patch=raw_patch,
            level=level or ("warn" if event_data.get("content_guard") else "info"),
        )

    def mark_unsupported(
        self,
        request_id: int,
        *,
        reason: str,
        submit_mode: str,
    ) -> dict[str, Any] | None:
        return self._transition(
            request_id,
            status="unsupported",
            public_status="暂不支持",
            reason=reason,
            event_message=reason,
            event_data={"mode": submit_mode},
            raw_patch={},
            level="warn",
        )

    def bind_import_job(
        self,
        request_id: int,
        *,
        job: dict[str, Any],
        public_status: str,
        safe_result: dict[str, Any],
        rclone_start: dict[str, Any] | None,
        success: bool,
        request_updates: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        current = self._deps.queries.get(request_id)
        job_id = job.get("id")
        if not current or not job_id:
            return "missing", current
        raw = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
        worker = raw.get("worker") if isinstance(raw.get("worker"), dict) else {}
        worker = {**worker, "status": "completed", "job_id": int(job_id)} if worker else {}
        bound_raw = {**raw, "job_result": safe_result}
        if worker:
            bound_raw["worker"] = worker
        outcome = self._deps.commands.bind_job_with_event(
            request_id,
            job_id=int(job_id),
            status=str(job.get("status") or "submitted"),
            public_status=public_status,
            raw_data=bound_raw,
            level="info" if success else "error",
            message="已自动创建正式入库任务",
            event_data={"job_id": job_id, "result": safe_result, "rclone_start": rclone_start},
            request_updates=request_updates,
        )
        return outcome, self._deps.queries.get(request_id)

    def mark_import_failed(self, request_id: int, *, error: str) -> dict[str, Any] | None:
        message = f"自动创建正式任务失败：{error}"
        current = self._deps.queries.get(request_id)
        raw = current.get("raw_data") if isinstance((current or {}).get("raw_data"), dict) else {}
        worker = raw.get("worker") if isinstance(raw.get("worker"), dict) else {}
        worker_patch = {"worker": {**worker, "status": "failed", "error": error}} if worker else {}
        return self._transition(
            request_id,
            status="failed",
            public_status="处理失败",
            reason=message,
            event_message=message,
            event_data={"error": error},
            raw_patch={"error": error, **worker_patch},
            level="error",
        )

    def mark_worker_queued(
        self,
        request_id: int,
        *,
        worker_task_id: int,
        task_type: str,
        request_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self._deps.queries.get(request_id)
        if not current:
            return None
        raw = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
        worker = {
            "task_id": int(worker_task_id),
            "task_type": str(task_type),
            "status": "queued",
        }
        changed = self._deps.commands.transition_with_event(
            request_id,
            expected_statuses={"submitted"},
            status="submitted",
            public_status="处理中",
            raw_data={**raw, "worker": worker},
            level="info",
            message="已加入持久化 Worker 队列",
            event_data={"worker": worker},
            request_updates=request_updates,
        )
        return self._deps.queries.get(request_id) if changed else None

    def worker_execution_allowed(self, request_id: int) -> tuple[bool, str, dict[str, Any] | None]:
        current = self._deps.queries.get(request_id)
        if not current:
            return False, "访客提交不存在，停止后台入库", None
        status = str(current.get("status") or "").strip().lower()
        if status != "submitted":
            return False, f"访客提交状态已变为 {status or '未知'}，停止后台入库", current
        if current.get("job_id"):
            return False, "访客提交已绑定正式任务，停止重复后台入库", current
        return True, "", current

    def _transition(
        self,
        request_id: int,
        *,
        status: str,
        public_status: str,
        reason: str,
        event_message: str,
        event_data: dict[str, Any],
        raw_patch: dict[str, Any],
        level: str,
    ) -> dict[str, Any] | None:
        current = self._deps.queries.get(request_id)
        if not current:
            return None
        raw = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
        merged_raw = {**raw, **raw_patch, "reason": reason}

        def _emit_review_required(conn: Any) -> None:
            self._deps.emit_notification(
                self._deps.db,
                "guest_review_required",
                {
                    "request_id": request_id,
                    "request_token": str(current.get("request_token") or ""),
                    "title": str(current.get("title") or "未命名资源"),
                    "category": str(current.get("category") or "movie"),
                    "category_label": str(current.get("category_label") or ""),
                    "source_type": str(current.get("source_type") or ""),
                    "reason": reason,
                },
                idempotency_key=f"notify:guest_review_required:{request_id}",
                connection=conn,
            )

        emit = _emit_review_required if (
            status == "pending_review"
            and self._deps.db is not None
            and self._deps.emit_notification is not None
        ) else None
        changed = self._deps.commands.transition_with_event(
            request_id,
            expected_statuses={"submitted"},
            status=status,
            public_status=public_status,
            raw_data=merged_raw,
            level=level,
            message=event_message,
            event_data=event_data,
            emit=emit,
        )
        return self._deps.queries.get(request_id) if changed else None
