"""Import write tools: submit, review and job lifecycle."""

from __future__ import annotations

from typing import Any

from ..args import optional_bool, optional_int, optional_text, optional_text_list, require_int, require_text
from ..auth import SCOPE_IMPORT_WRITE
from ..context import AgentCallContext
from ..errors import invalid_params, not_found
from ..projection import clip
from ..registry import RISK_DESTRUCTIVE, RISK_WRITE, ToolRegistry, ToolSpec
from ..schema import (
    array,
    boolean,
    cloud139_selection_schema,
    integer,
    obj,
    quark_selection_schema,
    sixpan_selection_schema,
    string,
    tianyi_selection_schema,
)
from ..services import AgentServices
from .common import actor, compact_write_result, require_confirm, require_reason, writes_of

_SELECTION_FIELDS = ("quark_selection", "cloud139_selection", "tianyi_selection", "sixpan_selection")


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="submit_import",
            title="提交入库",
            description=(
                "把选中的资源提交为一个入库请求/任务。优先用 public_id（来自 search_resources），"
                "此时标题与分享链接会自动从资源缓存补全。不传 selection 表示整份分享入库；"
                "要只保存部分文件，请按网盘类型传 quark_selection / cloud139_selection / "
                "tianyi_selection / sixpan_selection，其结构与网页勾选时提交的一致。"
                "dry_run=true 只做校验并返回识别结果，不会创建任何记录。"
            ),
            input_schema=obj(
                {
                    "public_id": string("搜索返回的资源编号（优先）", max_length=120),
                    "url": string("分享链接；没有 public_id 时使用", max_length=2048),
                    "password": string("提取码", max_length=32),
                    "title": string("覆盖标题；留空则用资源原标题", max_length=300),
                    "category": string("目标分类 key，见 fnos://config/categories", max_length=40),
                    "note": string("备注", max_length=500),
                    "ignore_files": array(string(), description="BT/磁链要忽略的文件名", max_items=2000),
                    "quark_selection": quark_selection_schema(),
                    "cloud139_selection": cloud139_selection_schema(),
                    "tianyi_selection": tianyi_selection_schema(),
                    "sixpan_selection": sixpan_selection_schema(),
                    "media_identity_token": string("resolve_tmdb 返回的 selection_token", max_length=8192),
                    "dry_run": boolean("只校验不提交", default=False),
                },
                required=["category"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_WRITE,
            idempotent=False,
            handler=_submit_import,
        )
    )
    registry.register(
        ToolSpec(
            name="approve_import_request",
            title="批准入库请求",
            description=(
                "把待审核的提交单批准并进入入库流程。仅当提交单处于待审核状态时需要；"
                "响应里会返回关联的任务编号。"
            ),
            input_schema=obj(
                {
                    "request_id": integer("提交单编号", minimum=1),
                    "note": string("审核备注", max_length=500),
                },
                required=["request_id"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_WRITE,
            handler=_approve_request,
        )
    )
    registry.register(
        ToolSpec(
            name="reject_import_request",
            title="拒绝入库请求",
            description=(
                "拒绝一个提交单。必须填写原因。默认不会清理已产生的文件（cleanup=false），"
                "只有确认需要一并清理时才传 cleanup=true。"
            ),
            input_schema=obj(
                {
                    "request_id": integer("提交单编号", minimum=1),
                    "reason": string("拒绝原因，会写入审计与通知", max_length=300),
                    "cleanup": boolean("同时清理已产生的文件", default=False),
                },
                required=["request_id", "reason"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_reject_request,
        )
    )
    registry.register(
        ToolSpec(
            name="retry_job",
            title="重试入库任务",
            description="把一个失败或可恢复的入库任务重新排队。",
            input_schema=obj(
                {
                    "job_id": integer("任务编号", minimum=1),
                    "reason": string("重试原因", max_length=300),
                },
                required=["job_id"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_WRITE,
            handler=_retry_job,
        )
    )
    registry.register(
        ToolSpec(
            name="cancel_job",
            title="取消入库任务",
            description=(
                "取消进行中的入库任务。必须填写原因并显式确认；已完成的任务不能取消。"
                "默认不删除已产生的文件。"
            ),
            input_schema=obj(
                {
                    "job_id": integer("任务编号", minimum=1),
                    "reason": string("取消原因", max_length=300),
                    "confirm": boolean("确认执行取消", default=False),
                    "cleanup": boolean("同时清理已产生的文件", default=False),
                },
                required=["job_id", "reason"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_cancel_job,
        )
    )
    registry.register(
        ToolSpec(
            name="confirm_job_not_submitted",
            title="确认任务未执行并重新提交",
            description=(
                "仅用于“提交记录存在但回执缺失”的人工复核：确认网盘里没有本次文件、"
                "也没有进行中的外部任务后，把任务按原计划重新提交。"
                "必须提供 get_job 返回的 row_version 与核对说明。误判会产生重复文件。"
            ),
            input_schema=obj(
                {
                    "job_id": integer("任务编号", minimum=1),
                    "row_version": integer("任务详情里的执行版本号", minimum=1),
                    "note": string("人工核对说明，1-1000 字", max_length=1000),
                    "confirm": boolean("确认执行", default=False),
                },
                required=["job_id", "row_version", "note"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_confirm_not_submitted,
        )
    )


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def _submit_import(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    public_id = optional_text(arguments, "public_id", max_length=120)
    url = optional_text(arguments, "url", max_length=2048)
    if not public_id and not url:
        raise invalid_params(
            "需要 public_id 或 url 其中之一",
            hint="先用 search_resources 搜索拿到 public_id",
        )

    payload: dict[str, Any] = {"category": require_text(arguments, "category", max_length=40)}
    if public_id:
        payload["public_id"] = public_id
    if url:
        payload["url"] = url
    for name, limit in (("password", 32), ("title", 300), ("note", 500)):
        value = optional_text(arguments, name, max_length=limit)
        if value:
            payload[name] = value
    ignore_files = optional_text_list(arguments, "ignore_files", max_items=2000, max_length=512)
    if ignore_files:
        payload["ignore_files"] = ignore_files
    for name in _SELECTION_FIELDS:
        value = arguments.get(name)
        if isinstance(value, dict) and value:
            payload[name] = value
    identity_token = optional_text(arguments, "media_identity_token", max_length=8192)
    if identity_token:
        payload["tmdb_selection_token"] = identity_token

    if optional_bool(arguments, "dry_run", default=False):
        body, status_code = writes.preview_import(payload)
    else:
        body, status_code = writes.submit_import(payload)
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    if body.get("dry_run"):
        return {
            "dry_run": True,
            "public_id": body.get("public_id"),
            "title": clip(body.get("title"), 200),
            "category": body.get("category"),
            "provider_kind": body.get("provider_kind"),
            "supported": body.get("supported"),
            "scoped_selection": body.get("scoped_selection"),
            "message": clip(body.get("message")),
        }

    # The public submission response is visitor-facing and carries no internal
    # ids; the bridge resolves them from the request token and exposes them here.
    request_token = body.get("request_token")
    request_id = body.get("request_id")
    job_id = body.get("job_id")
    request_status = str(body.get("request_status") or "")
    approve_required = bool(body.get("success", True)) and not job_id
    return {
        "success": True,
        "message": clip(body.get("message"), 200),
        "request_token": request_token,
        "public_status": body.get("status"),
        "request_id": request_id,
        "job_id": job_id,
        "request_status": request_status or None,
        "title": clip(body.get("title"), 200),
        "category": body.get("category"),
        "approve_required": approve_required,
        "next": (
            "提交处于待审核，调用 approve_import_request 批准"
            if approve_required
            else "用 get_job(job_id) 轮询任务状态"
        ),
    }


def _approve_request(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    request_id = require_int(arguments, "request_id")
    payload = {}
    note = optional_text(arguments, "note", max_length=500)
    if note:
        payload["note"] = note
    body, status_code = writes.approve_request(request_id, payload, actor(context))
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    return compact_write_result(body)


def _reject_request(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    request_id = require_int(arguments, "request_id")
    payload = {
        "reason": require_reason(arguments),
        "cleanup": optional_bool(arguments, "cleanup", default=False),
    }
    body, status_code = writes.reject_request(request_id, payload, actor(context))
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    return compact_write_result(body)


def _retry_job(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    job_id = require_int(arguments, "job_id")
    reason = optional_text(arguments, "reason", max_length=300) or f"Agent({context.token_name}) 重试"
    body, status_code = writes.retry_job(job_id, reason)
    if status_code == 404:
        raise not_found("入库任务")
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    return compact_write_result(body)


def _cancel_job(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    require_confirm(arguments, action="取消任务")
    job_id = require_int(arguments, "job_id")
    payload = {
        "reason": require_reason(arguments),
        "cleanup": optional_bool(arguments, "cleanup", default=False),
        "stop_running": True,
    }
    body, status_code = writes.cancel_job(job_id, payload, actor(context))
    if status_code == 404:
        raise not_found("入库任务")
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    return compact_write_result(body)


def _confirm_not_submitted(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    require_confirm(arguments, action="重新提交任务")
    job_id = require_int(arguments, "job_id")
    payload = {
        "action": "confirm_not_submitted",
        "confirmed": True,
        "expected_row_version": require_int(arguments, "row_version"),
        "note": require_text(arguments, "note", max_length=1000),
    }
    body, status_code = writes.review_job(job_id, payload, actor(context))
    if status_code >= 400 or not body.get("success", True):
        raise _submission_error(body, status_code)
    result = compact_write_result(body)
    result["queued"] = body.get("queued")
    result["warning"] = clip(body.get("warning"), 400)
    return result


def _submission_error(body: dict[str, Any], status_code: int):
    from ..errors import AgentError

    code = "NOT_FOUND" if status_code == 404 else "REJECTED"
    if status_code == 409:
        code = "STATE_CONFLICT"
    return AgentError(
        code,
        str(body.get("message") or "操作未执行"),
        retryable=status_code >= 500,
        hint="确认对象编号与当前状态后重试",
    )


__all__ = ["register"]
