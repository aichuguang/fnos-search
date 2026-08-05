from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..notifications import events as notification_events


class RequestQueries(Protocol):
    def get(self, request_id: int) -> dict[str, Any] | None: ...


class RequestCommands(Protocol):
    def transition_with_event(self, request_id: int, **kwargs: Any) -> bool: ...


class JobQueries(Protocol):
    def get(self, job_id: int) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class RequestReviewCommandDependencies:
    requests: RequestQueries
    commands: RequestCommands
    jobs: JobQueries
    merge_raw_data: Callable[[Any, dict[str, Any]], dict[str, Any]]
    db: Any = None
    emit_notification: Callable[..., Any] | None = None


class RequestReviewCommandService:
    """Applies administrator reject/cancel decisions before external cleanup."""

    def __init__(self, dependencies: RequestReviewCommandDependencies) -> None:
        self._deps = dependencies

    def reject(self, request_id: int, *, reason: str, admin: str | None, force: bool):
        return self._decide(request_id, status="rejected", public_status="未通过", reason=reason, admin=admin, force=force, action="拒绝", raw_key="rejected_by")

    def cancel(self, request_id: int, *, reason: str, admin: str | None, force: bool):
        return self._decide(request_id, status="cancelled", public_status="已取消", reason=reason, admin=admin, force=force, action="取消", raw_key="cancelled_by")

    def _decide(self, request_id: int, *, status: str, public_status: str, reason: str, admin: str | None, force: bool, action: str, raw_key: str) -> tuple[dict[str, Any], int]:
        item = self._deps.requests.get(request_id)
        if not item:
            return {"success": False, "message": "访客提交不存在"}, 404
        initial_linked_job = self._deps.jobs.get(int(item["job_id"])) if item.get("job_id") else None
        if initial_linked_job and str(initial_linked_job.get("status") or "") in {"done", "success"} and not force:
            action_text = "直接拒绝" if action == "拒绝" else action
            return {"success": False, "message": f"关联任务已完成，不能{action_text}；如需删除已入库资源请走人工清理"}, 400
        current_status = str(item.get("status") or "")

        def _emit_rejected(conn: Any) -> None:
            self._deps.emit_notification(
                self._deps.db,
                notification_events.EVENT_GUEST_REJECTED,
                {
                    "title": str(item.get("title") or "未命名资源"),
                    "category": str(item.get("category") or "movie"),
                    "request_id": request_id,
                    "request_token": str(item.get("request_token") or ""),
                    "reason": reason,
                },
                idempotency_key=notification_events.idempotency_key(
                    notification_events.EVENT_GUEST_REJECTED, request_id
                ),
                connection=conn,
            )

        emit = _emit_rejected if (
            status == "rejected"
            and self._deps.emit_notification is not None
            and self._deps.db is not None
        ) else None
        changed = self._deps.commands.transition_with_event(
            request_id,
            expected_statuses={current_status},
            status=status,
            public_status=public_status,
            raw_data=self._deps.merge_raw_data(item.get("raw_data"), {raw_key: admin, "reason": reason}),
            level="warn",
            message=f"管理员{action}提交：{reason}",
            event_data={"admin": admin, "reason": reason},
            emit=emit,
        )
        if not changed:
            return {"success": False, "message": "提交状态已变化，请刷新后重试"}, 409
        updated_item = self._deps.requests.get(request_id)
        latest_job_id = int((updated_item or {}).get("job_id") or 0)
        linked_job = self._deps.jobs.get(latest_job_id) if latest_job_id else None
        return {"success": True, "request": updated_item, "linked_job": linked_job}, 200
