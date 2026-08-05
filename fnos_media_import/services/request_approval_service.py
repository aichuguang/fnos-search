from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..notifications import events as notification_events


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
    sanitize_string_list: Callable[[Any], list[str]]
    sanitize_quark_selection: Callable[[Any], dict[str, Any] | None]
    sanitize_cloud139_selection: Callable[[Any], dict[str, Any] | None]
    sanitize_sixpan_selection: Callable[[Any], dict[str, Any]]
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
        if item.get("job_id"):
            return self._sync_existing_job(request_id, item, admin)

        category = str(payload.get("category") or item.get("category") or "movie")
        title = str(payload.get("title") or item.get("title") or "未命名资源")
        raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
        original = raw.get("request") if isinstance(raw.get("request"), dict) else {}
        sixpan_selection = self._deps.sanitize_sixpan_selection(original.get("sixpan_selection"))
        ignore_source = original.get("ignore_files")
        if "ignore_files" not in original:
            ignore_source = sixpan_selection.get("ignore_files")
        ignore_files = self._deps.sanitize_string_list(ignore_source)
        if sixpan_selection or ignore_files or isinstance(original.get("sixpan_selection"), dict):
            sixpan_selection = {
                **sixpan_selection,
                "ignore_files": ignore_files,
                "ignored_count": len(ignore_files),
            }
        submit_payload = {
            "title": title,
            "url": item.get("source_url"),
            "password": item.get("password") or "",
            "category": category,
            "ignore_files": ignore_files,
            "sixpan_selection": sixpan_selection,
            "quark_selection": self._deps.sanitize_quark_selection(original.get("quark_selection")),
            "cloud139_selection": self._deps.sanitize_cloud139_selection(original.get("cloud139_selection")),
            "idempotency_key": f"guest-request:{request_id}",
            "executor_id": "admin",
        }
        current_status = str(item.get("status") or "")
        request_updates = {
            "title": title,
            "category": category,
            "category_label": self._deps.category_label(category),
        }
        approved_raw = self._deps.merge_raw_data(
            item.get("raw_data"),
            {"approved_by": admin, "approval": {"status": "approved", "title": title, "category": category}},
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
        changed = self._deps.commands.transition_with_event(
            request_id,
            expected_statuses={current_status},
            status="submitted",
            public_status="处理中",
            raw_data=approved_raw,
            level="info",
            message=f"管理员批准入库：{title}",
            event_data={"admin": admin, "title": title, "category": category},
            request_updates=request_updates,
            emit=emit,
        )
        if not changed:
            return {"success": False, "message": "提交状态已变化，请刷新后重试"}, 409

        coordinated = self._deps.coordinate_import(
            guest_request_id=request_id,
            request_token=str(item.get("request_token") or request_id),
            submit_payload=submit_payload,
            request_updates=request_updates,
        )
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
