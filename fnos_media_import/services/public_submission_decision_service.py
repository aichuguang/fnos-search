from __future__ import annotations

from typing import Any, Callable


class PublicSubmissionDecisionService:
    """Persists review decisions and determines whether automatic import may proceed."""

    def __init__(
        self,
        *,
        submission_service: Callable[[], Any],
        get_request: Callable[[int], dict[str, Any] | None],
        add_event: Callable[..., Any],
        update_request: Callable[..., Any],
        merge_raw_data: Callable[[Any, dict[str, Any]], dict[str, Any]],
        public_request: Callable[[dict[str, Any] | None], dict[str, Any]],
        auto_submit_allowed: Callable[[str, dict[str, Any]], bool],
    ) -> None:
        self.submission_service = submission_service
        self.get_request = get_request
        self.add_event = add_event
        self.update_request = update_request
        self.merge_raw_data = merge_raw_data
        self.public_request = public_request
        self.auto_submit_allowed = auto_submit_allowed

    def decide(
        self,
        *,
        guest_request_id: int,
        request_token: str,
        link: Any,
        submit_mode: str,
        content_guard: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        submissions = self.submission_service()
        if not link.supported:
            return self._unsupported(
                submissions, guest_request_id, request_token, submit_mode
            )
        guard = content_guard or {}
        if guard.get("review_required"):
            reason = str(
                guard.get("public_message")
                or guard.get("reason")
                or "疑似敏感内容，等待人工审核"
            )
            request_item = submissions.send_to_review(
                guest_request_id,
                reason=reason,
                event_message=reason,
                event_data={"content_guard": guard, "source_type": link.source_type},
                raw_patch={"content_guard": guard},
            )
            return self._review_response(
                request_token,
                request_item or self.get_request(guest_request_id),
                review_required=True,
            )

        self.add_event(
            guest_request_id,
            "info",
            "内容审核通过，允许自动入库",
            {"content_guard": guard, "source_type": link.source_type},
        )
        current = self.get_request(guest_request_id) or {}
        self.update_request(
            guest_request_id,
            raw_data=self.merge_raw_data(current.get("raw_data"), {"content_guard": guard}),
        )
        if not self.auto_submit_allowed(submit_mode, link.to_dict()):
            request_item = submissions.send_to_review(
                guest_request_id,
                reason="当前提交模式为人工审核",
                event_message="当前提交模式为人工审核，等待管理员处理",
                event_data={"mode": submit_mode},
                raw_patch={},
            )
            return self._review_response(
                request_token, request_item or self.get_request(guest_request_id)
            )
        return None

    def _unsupported(
        self,
        submissions: Any,
        request_id: int,
        token: str,
        submit_mode: str,
    ) -> dict[str, Any]:
        if submit_mode in {"review", "mixed"}:
            request_item = submissions.send_to_review(
                request_id,
                reason="当前资源需要管理员审核",
                event_message="资源暂不支持自动入库，进入管理员审核",
                event_data={"mode": submit_mode},
                raw_patch={},
                level="warn",
            )
            return self._review_response(token, request_item or self.get_request(request_id))
        request_item = submissions.mark_unsupported(
            request_id,
            reason="当前资源暂不支持自动入库",
            submit_mode=submit_mode,
        )
        return {
            "success": False,
            "message": "当前资源暂不支持自动入库",
            "request_token": token,
            "status": "暂不支持",
            "request": self.public_request(request_item or self.get_request(request_id)),
        }

    def _review_response(
        self,
        token: str,
        request_item: dict[str, Any] | None,
        *,
        review_required: bool = False,
    ) -> dict[str, Any]:
        response = {
            "success": True,
            "message": "提交成功，等待管理员处理",
            "request_token": token,
            "status": "等待处理",
            "request": self.public_request(request_item or {}),
        }
        if review_required:
            response["review_required"] = True
        return response
