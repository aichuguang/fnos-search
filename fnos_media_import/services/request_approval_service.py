from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..notifications import events as notification_events
from ..repositories.worker_task_repository import WorkerTaskConflictError


class RequestQueries(Protocol):
    def get(self, request_id: int) -> dict[str, Any] | None: ...


class RequestCommands(Protocol):
    def transition_with_event(self, request_id: int, **kwargs: Any) -> bool: ...
    def bind_job_with_event(self, request_id: int, **kwargs: Any) -> str: ...


class JobQueries(Protocol):
    def get(self, job_id: int) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class RequestApprovalDependencies:
    requests: RequestQueries
    commands: RequestCommands
    jobs: JobQueries
    coordinate_import: Callable[..., Any]
    public_status: Callable[[str], str]
    safe_result: Callable[[dict[str, Any]], dict[str, Any]]
    merge_raw_data: Callable[[Any, dict[str, Any]], dict[str, Any]]
    sanitize_request_payload: Callable[[dict[str, Any]], dict[str, Any]]
    category_label: Callable[[str], str]
    db: Any = None
    emit_notification: Callable[..., Any] | None = None


class RequestApprovalService:
    def __init__(self, dependencies: RequestApprovalDependencies) -> None:
        self._deps = dependencies

    def approve(self, request_id: int, payload: dict[str, Any], *, admin: str | None) -> tuple[dict[str, Any], int]:
        item = self._deps.requests.get(request_id)
        if not item:
            return {"success": False, "message": "访客提交不存在"}, 404
        retry_failed = payload.get("retry") is True
        if retry_failed and (item.get("status") != "failed" or item.get("job_id")):
            return {"success": False, "message": "只能重试尚未创建正式任务的失败提交，请刷新后核对状态"}, 409
        if item.get("job_id"):
            return self._sync_existing_job(request_id, item, admin)
        if item.get("status") == "failed" and not retry_failed:
            return {"success": False, "message": "该提交已失败，请使用“重试提交”明确重新入队"}, 409

        category = str(payload.get("category") or item.get("category") or "movie")
        title = str(payload.get("title") or item.get("title") or "未命名资源")
        raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
        original = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        # Use the same whitelist as intake, so approval cannot silently drop
        # a provider's scoped selection or the server-verified media identity.
        saved = self._deps.sanitize_request_payload(original)
        if original.get("quark_selection") not in (None, {}) and not saved.get("quark_selection"):
            return {"success": False, "message": "已保存的夸克文件选择无效，请重新选择后提交；不会改为转存整个分享"}, 400
        for field, label in (("cloud139_selection", "移动"), ("tianyi_selection", "天翼")):
            if original.get(field) not in (None, {}) and not saved.get(field):
                return {"success": False, "message": f"已保存的{label}文件选择无效，请重新选择后提交；不会改为转存整个分享"}, 400
        submit_payload = {
            "title": title,
            "url": item.get("source_url"),
            "password": item.get("password") or "",
            "category": category,
            **{
                key: saved[key]
                for key in (
                    "ignore_files", "sixpan_selection", "quark_selection",
                    "cloud139_selection", "tianyi_selection", "media_identity",
                )
                if key in saved
            },
        }
        if title != str(item.get("title") or "未命名资源"):
            # An explicit administrator rename must not be overwritten by the
            # previously selected TMDB title in ImportJobCreationService.
            submit_payload.pop("media_identity", None)
        current_status = str(item.get("status") or "")
        request_updates = {
            "title": title,
            "category": category,
            "category_label": self._deps.category_label(category),
        }
        approved_raw = self._deps.merge_raw_data(
            item.get("raw_data"),
            {
                "approved_by": admin,
                "approval": {
                    "status": "approved",
                    "title": title,
                    "category": category,
                },
                "request": {
                    key: value
                    for key, value in original.items()
                    if key != "destination_id"
                },
            },
        )

        def _emit_approved(conn: Any) -> None:
            self._deps.emit_notification(
                self._deps.db,
                notification_events.EVENT_GUEST_APPROVED,
                {
                    "title": title,
                    "category": category,
                    "category_label": self._deps.category_label(category),
                    "request_id": request_id,
                    "request_token": str(item.get("request_token") or ""),
                },
                idempotency_key=notification_events.idempotency_key(
                    notification_events.EVENT_GUEST_APPROVED, request_id
                ),
                connection=conn,
            )

        emit = _emit_approved if (self._deps.emit_notification is not None and self._deps.db is not None) else None
        transition = {
            "expected_statuses": {current_status}, "status": "submitted", "public_status": "处理中",
            "raw_data": approved_raw, "level": "info",
            "message": f"管理员重试提交：{title}" if retry_failed else f"管理员批准入库：{title}",
            "event_data": {"admin": admin, "title": title, "category": category},
            "request_updates": request_updates, "emit": emit,
        }
        coordinate_args = {
            "guest_request_id": request_id, "request_token": str(item.get("request_token") or request_id),
            "submit_payload": submit_payload, "request_updates": request_updates,
        }
        if retry_failed:
            if self._deps.db is None or not callable(getattr(self._deps.db, "connect", None)):
                return {"success": False, "message": "持久化重试事务未装配，未改变原提交"}, 503
            try:
                with self._deps.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT status, job_id, request_token FROM guest_requests WHERE id=?", (request_id,),
                    ).fetchone()
                    if current is None or current["status"] != "failed" or current["job_id"] or (
                        current["request_token"] != item.get("request_token")
                    ):
                        raise WorkerTaskConflictError("提交状态已变化，未重新入队，请刷新后核对")
                    coordinated = self._deps.coordinate_import(**coordinate_args, retry_failed=True, connection=connection)
                    queued_result = getattr(coordinated, "result", {})
                    if not queued_result.get("success") or not queued_result.get("queued"):
                        raise WorkerTaskConflictError("原失败提交未成功加入持久队列，未改变原提交")
                    transition["raw_data"] = self._deps.merge_raw_data(approved_raw, {
                        "error": None, "reason": "管理员已重试提交，等待后台执行",
                        "worker": {
                            "task_id": int(queued_result.get("worker_task_id") or 0),
                            "task_type": "public_import_create", "status": "queued", "error": None,
                        },
                    })
                    if not self._deps.commands.transition_with_event(request_id, connection=connection, **transition):
                        raise WorkerTaskConflictError("提交状态已变化，重试未执行，请刷新后核对")
            except WorkerTaskConflictError as exc:
                return {"success": False, "message": str(exc)}, 409
        else:
            changed = self._deps.commands.transition_with_event(request_id, **transition)
            if not changed:
                return {"success": False, "message": "提交状态已变化，请刷新后重试"}, 409
            coordinated = self._deps.coordinate_import(**coordinate_args)
        result = coordinated.result if hasattr(coordinated, "result") else coordinated
        result = result if isinstance(result, dict) else {}
        job = coordinated.job if hasattr(coordinated, "job") else result.get("job")
        job = job if isinstance(job, dict) else {}
        bind_outcome = str(getattr(coordinated, "bind_outcome", "") or "")
        queued = bind_outcome == "queued" or bool(result.get("queued"))
        success = bool(result.get("success", True))
        return {
            "success": success,
            "queued": queued,
            "message": result.get("message") or ("已批准并加入后台入库队列" if queued else "已批准入库"),
            "request": self._deps.requests.get(request_id),
            "job": job,
            "bind_outcome": bind_outcome,
        }, (202 if queued else 200)

    def _sync_existing_job(self, request_id: int, item: dict[str, Any], admin: str | None) -> tuple[dict[str, Any], int]:
        job = self._deps.jobs.get(int(item["job_id"]))
        if job and item.get("status") == "pending_review":
            job_status = str(job.get("status") or "submitted")

            def _emit_approved(conn: Any) -> None:
                if self._deps.emit_notification is None or self._deps.db is None:
                    return
                self._deps.emit_notification(
                    self._deps.db,
                    notification_events.EVENT_GUEST_APPROVED,
                    {
                        "title": str(item.get("title") or "未命名资源"),
                        "category": str(item.get("category") or "movie"),
                        "category_label": str(item.get("category_label") or ""),
                        "request_id": request_id,
                        "request_token": str(item.get("request_token") or ""),
                    },
                    idempotency_key=notification_events.idempotency_key(
                        notification_events.EVENT_GUEST_APPROVED, request_id
                    ),
                    connection=conn,
                )

            self._deps.commands.transition_with_event(
                request_id,
                expected_statuses={"pending_review"},
                status=job_status,
                public_status=self._deps.public_status(job_status),
                raw_data=self._deps.merge_raw_data(item.get("raw_data"), {"approval_sync": {"job_id": job.get("id"), "job_status": job.get("status")}}),
                level="info",
                message="审核状态已同步到关联正式任务",
                event_data={"admin": admin, "job_id": job.get("id"), "job_status": job.get("status")},
                emit=_emit_approved,
            )
        return {"success": True, "message": "该提交已创建正式任务", "request": self._deps.requests.get(request_id) or item, "job": job}, 200
