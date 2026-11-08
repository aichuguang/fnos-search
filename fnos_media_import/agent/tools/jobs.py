"""Import tools: job state and the parent guest request."""

from __future__ import annotations

from typing import Any

from ..args import optional_int, optional_text, require_int
from ..auth import SCOPE_IMPORT_READ
from ..context import AgentCallContext
from ..errors import not_found
from ..projection import clip, compact_event, compact_job, compact_request, page_meta
from ..registry import ToolRegistry, ToolSpec
from ..schema import integer, obj, string
from ..services import AgentServices

# Real pipeline states, mirrored from constants.JOB_STATUS_LABELS.
JOB_STATUSES = (
    "created",
    "provider_submitting",
    "checking",
    "submitted",
    "waiting_transfer",
    "transferring",
    "waiting_openlist",
    "waiting_organizer",
    "organizing",
    "confirming",
    "review",
    "refreshing",
    "done",
    "failed",
    "unsupported",
    "cancelled",
)
TERMINAL_JOB_STATUSES = {"done", "cancelled", "unsupported"}


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="list_jobs",
            title="列出入库任务",
            description="分页列出入库任务，可按状态、分类、来源与关键词过滤。",
            input_schema=obj(
                {
                    "status": string("任务状态过滤，例如 failed", max_length=40),
                    "category": string("分类 key", max_length=40),
                    "source_provider_kind": string("来源类型，例如 quark / mobile / tianyi / sixpan", max_length=40),
                    "keyword": string("标题关键词", max_length=80),
                    "page": integer("页码，从 1 开始", default=1, minimum=1, maximum=10000),
                    "per_page": integer("每页条数", default=20, minimum=1, maximum=100),
                }
            ),
            scope=SCOPE_IMPORT_READ,
            handler=_list_jobs,
        )
    )
    registry.register(
        ToolSpec(
            name="get_job",
            title="查入库任务详情",
            description=(
                "读取单个入库任务的完整状态：阶段、进度、失败原因、是否等待人工确认。"
                "轮询这个接口判断任务是否结束。"
            ),
            input_schema=obj({"job_id": integer("任务编号", minimum=1)}, required=["job_id"]),
            scope=SCOPE_IMPORT_READ,
            handler=_get_job,
        )
    )
    registry.register(
        ToolSpec(
            name="get_import_request",
            title="查提交单详情",
            description="读取访客提交单及其关联的入库任务，用于确认审核状态。",
            input_schema=obj({"request_id": integer("提交单编号", minimum=1)}, required=["request_id"]),
            scope=SCOPE_IMPORT_READ,
            handler=_get_import_request,
        )
    )


def _list_jobs(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    page = optional_int(arguments, "page", default=1, minimum=1, maximum=10000)
    per_page = optional_int(arguments, "per_page", default=20, minimum=1, maximum=100)
    status = optional_text(arguments, "status", max_length=40) or None
    category = optional_text(arguments, "category", max_length=40) or None
    provider = optional_text(arguments, "source_provider_kind", max_length=40) or None
    keyword = optional_text(arguments, "keyword", max_length=80) or None
    result = context.services.list_jobs(
        limit=per_page,
        offset=(page - 1) * per_page,
        status=status,
        category=category,
        source_provider_kind=provider,
        keyword=keyword,
    )
    rows = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    return {
        "items": [compact_job(row) for row in rows],
        "pagination": page_meta(int(result.get("total") or 0), page, per_page),
        "filters": {
            "status": status,
            "category": category,
            "source_provider_kind": provider,
            "keyword": keyword,
        },
    }


def _get_job(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = require_int(arguments, "job_id")
    result, status_code = context.services.job_detail(job_id)
    if status_code == 404 or not result.get("success", False):
        raise not_found("入库任务")
    job = result.get("job") or {}
    events = [item for item in (result.get("events") or []) if isinstance(item, dict)]
    return {
        "job": compact_job(job),
        "recent_events": [compact_event(item) for item in events[:20]],
        "next": _next_hint(job),
    }


def _get_import_request(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    request_id = require_int(arguments, "request_id")
    result, status_code = context.services.request_detail(request_id)
    if status_code == 404 or not result.get("success", False):
        raise not_found("提交单")
    request = result.get("request") or result.get("item") or {}
    payload: dict[str, Any] = {"request": compact_request(request)}
    linked_job = result.get("linked_job") or result.get("job")
    if isinstance(linked_job, dict):
        payload["linked_job"] = compact_job(linked_job)
    events = [item for item in (result.get("events") or []) if isinstance(item, dict)]
    if events:
        payload["recent_events"] = [compact_event(item) for item in events[:20]]
    return payload


def _next_hint(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "").lower()
    execution = job.get("transfer_execution") if isinstance(job.get("transfer_execution"), dict) else {}
    review = execution.get("review") if isinstance(execution.get("review"), dict) else {}
    if status == "done":
        return "任务已完成。可用 list_organizer_tasks 查看后续整理任务。"
    if status == "cancelled":
        return "任务已取消。如需继续，请重新提交或调用 retry_job。"
    if status == "unsupported":
        return "该资源暂不支持自动入库，请换一个来源或手动处理。"
    if status == "review":
        if review.get("can_confirm_not_submitted"):
            return (
                "等待人工确认：先核对网盘中的来源暂存目录与目标目录。"
                "确认确实没有本次文件后，用 confirm_job_not_submitted 带上 transfer_execution.row_version 重新提交。"
            )
        if review.get("can_resume"):
            return "等待人工确认：修复提示的问题后重试，保留现有回执继续执行。"
        return f"等待人工确认：{clip(review.get('guidance'), 200) or '请核对任务详情后再处理。'}"
    if status == "failed":
        return "任务失败：先读 error_message，再决定 retry_job 或人工处理。"
    return "任务进行中：稍后用 get_job 重新查询，不要立即重复提交。"


__all__ = ["JOB_STATUSES", "register"]
