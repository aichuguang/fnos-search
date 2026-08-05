from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .public_submission_preparation_service import PreparedPublicSubmission


@dataclass(frozen=True)
class PublicSubmissionIntakeResult:
    preflight: dict[str, Any]
    request_token: str = ""
    guest_request_id: int = 0
    response: dict[str, Any] | None = None


class PublicSubmissionIntakeService:
    """Runs duplicate/preflight gates and persists the initial guest request."""

    def __init__(
        self,
        *,
        submission_service: Callable[[], Any],
        preflight: Callable[[PreparedPublicSubmission], dict[str, Any]],
        new_token: Callable[[], str],
        cached_item: Callable[[dict[str, Any] | None], Any],
        request_payload: Callable[[dict[str, Any]], dict[str, Any]],
        duplicate_minutes: Callable[[], int],
        duplicate_enabled: Callable[[], bool],
        prepare_notification: Callable[[dict[str, Any], str], Callable[..., Any] | None] | None = None,
    ) -> None:
        self.submission_service = submission_service
        self.preflight = preflight
        self.new_token = new_token
        self.cached_item = cached_item
        self.request_payload = request_payload
        self.duplicate_minutes = duplicate_minutes
        self.duplicate_enabled = duplicate_enabled
        self.prepare_notification = prepare_notification

    def begin(
        self,
        prepared: PreparedPublicSubmission,
        *,
        client_ip_hash: str,
        user_agent: str,
    ) -> PublicSubmissionIntakeResult:
        submissions = self.submission_service()
        source_url = str(prepared.payload.get("url") or "")
        duplicate = submissions.duplicate_response(
            source_url=source_url,
            category=prepared.category_key,
            within_minutes=self.duplicate_minutes(),
            enabled=self.duplicate_enabled(),
            scoped_selection=prepared.scoped_selection,
        )
        if duplicate:
            return PublicSubmissionIntakeResult(preflight={}, response=duplicate)

        preflight = self.preflight(prepared)
        if not preflight.get("allowed", True):
            link = prepared.link
            return PublicSubmissionIntakeResult(
                preflight=preflight,
                response={
                    "success": False,
                    "message": preflight.get("message")
                    or "资源检测未通过，请确认分享链接有效后再提交",
                    "status": "检测未通过",
                    "public_id": prepared.public_id,
                    "link": {
                        "source_type": link.source_type,
                        "supported": link.supported,
                        "route": link.route,
                        "reason": link.reason,
                    },
                    "inspection": preflight.get("inspection"),
                },
            )

        token = self.new_token()
        link = prepared.link
        payload = prepared.payload
        notification_emit = (
            self.prepare_notification(payload, token)
            if self.prepare_notification is not None
            else None
        )
        request_id = submissions.create_initial_request(
            {
                "request_token": token,
                "title": payload.get("title") or "未命名资源",
                "category": prepared.category_key,
                "category_label": prepared.category.get("label", prepared.category_key),
                "source_type": link.source_type,
                "source_url": source_url,
                "password": link.password,
                "note": payload.get("note") or "",
                "status": "submitted",
                "public_status": "处理中",
                "client_ip_hash": client_ip_hash,
                "user_agent": user_agent,
                "raw_data": {
                    "public_id": prepared.public_id,
                    "cached": self.cached_item(prepared.cached),
                    "request": self.request_payload(payload),
                    "preflight": preflight,
                },
            },
            preflight_checked=bool(preflight.get("checked")),
            event_data={
                "public_id": prepared.public_id,
                "category": prepared.category_key,
                "source_type": link.source_type,
                "supported": link.supported,
                "preflight": preflight,
            },
            emit=notification_emit,
        )
        return PublicSubmissionIntakeResult(
            preflight=preflight,
            request_token=token,
            guest_request_id=request_id,
        )
