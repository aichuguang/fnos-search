"""Trending tools: review daily discoveries and adopt them."""

from __future__ import annotations

from typing import Any

from ..args import optional_text, optional_text_list, require_int, require_text
from ..auth import SCOPE_IMPORT_WRITE, SCOPE_SCHEDULE_READ, SCOPE_SCHEDULE_WRITE
from ..context import AgentCallContext
from ..errors import AgentError, invalid_params, not_found
from ..projection import clip, page_meta
from ..registry import RISK_WRITE, ToolRegistry, ToolSpec
from ..schema import array, cloud139_selection_schema, integer, obj, quark_selection_schema, sixpan_selection_schema, string
from ..services import AgentServices
from .common import writes_of

SCOPES = (SCOPE_SCHEDULE_READ, SCOPE_SCHEDULE_WRITE, SCOPE_IMPORT_WRITE)

_CANDIDATE_FIELDS = (
    "id",
    "title",
    "original_title",
    "media_type",
    "year",
    "source",
    "status",
    "score",
    "rating",
    "poster_url",
    "summary",
    "tmdb_id",
    "created_at",
)


def _compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    projected = {name: row[name] for name in _CANDIDATE_FIELDS if row.get(name) is not None}
    for name in ("title", "original_title", "summary"):
        if name in projected:
            projected[name] = clip(projected[name], 180)
    return projected


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="list_trending_candidates",
            title="列出热榜候选",
            description="分页读取每日热榜发现的影视候选，可按状态或类型过滤。",
            input_schema=obj(
                {
                    "status": string("候选状态，例如 discovered / import_ready / ignored", max_length=40),
                    "media_type": string("movie 或 tv", max_length=8),
                    "page": integer("页码", default=1, minimum=1, maximum=10000),
                    "per_page": integer("每页条数", default=20, minimum=1, maximum=100),
                }
            ),
            scope=SCOPE_SCHEDULE_READ,
            handler=_list_candidates,
        )
    )
    registry.register(
        ToolSpec(
            name="import_trending_candidate",
            title="采纳热榜候选入库",
            description=(
                "把热榜候选的某个搜索结果按首次入库流程提交。需要提供资源 public_id 与分类；"
                "不传勾选则整份分享入库。"
            ),
            input_schema=obj(
                {
                    "candidate_id": integer("热榜候选编号", minimum=1),
                    "public_id": string("候选搜索结果里的资源编号", max_length=120),
                    "category": string("目标分类 key", max_length=40),
                    "ignore_files": array(string(), description="要忽略的文件名", max_items=2000),
                    "quark_selection": quark_selection_schema(),
                    "cloud139_selection": cloud139_selection_schema(),
                    "sixpan_selection": sixpan_selection_schema(),
                },
                required=["candidate_id", "public_id", "category"],
            ),
            scope=SCOPE_IMPORT_WRITE,
            risk=RISK_WRITE,
            idempotent=False,
            handler=_import_candidate,
        )
    )
    registry.register(
        ToolSpec(
            name="subscribe_trending_candidate",
            title="把热榜候选转为追更",
            description="以 TMDB 身份把热榜候选转成一条定时追更订阅，避免标题误绑同名作品。",
            input_schema=obj({"candidate_id": integer("热榜候选编号", minimum=1)}, required=["candidate_id"]),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_WRITE,
            idempotent=False,
            handler=_subscribe_candidate,
        )
    )


def _list_candidates(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    page = int(arguments.get("page") or 1)
    per_page = int(arguments.get("per_page") or 20)
    per_page = max(1, min(per_page, 100))
    result = writes.list_trending_candidates(
        page=page,
        per_page=per_page,
        status=optional_text(arguments, "status", max_length=40) or None,
        media_type=optional_text(arguments, "media_type", max_length=8) or None,
    )
    rows = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    return {
        "items": [_compact_candidate(row) for row in rows],
        "pagination": page_meta(int(result.get("total") or 0), page, per_page),
    }


def _import_candidate(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    candidate_id = require_int(arguments, "candidate_id")
    payload: dict[str, Any] = {
        "public_id": require_text(arguments, "public_id", max_length=120),
        "category": require_text(arguments, "category", max_length=40),
    }
    ignore_files = optional_text_list(arguments, "ignore_files", max_items=2000, max_length=512)
    if ignore_files:
        payload["ignore_files"] = ignore_files
    for name in ("quark_selection", "cloud139_selection", "sixpan_selection"):
        value = arguments.get(name)
        if isinstance(value, dict) and value:
            payload[name] = value
    body, status_code = writes.import_trending_candidate(candidate_id, payload)
    if status_code == 404:
        raise not_found("热榜候选")
    if status_code >= 400 or not body.get("success", True):
        raise AgentError(
            "REJECTED",
            clip(body.get("message"), 300) or "热榜候选入库失败",
            hint="确认 public_id 与分类后重试",
        )
    # The import service reports the created job under ``job``; there is no
    # top-level ``job_id``.
    job = body.get("job") if isinstance(body.get("job"), dict) else {}
    return {
        "success": True,
        "candidate_id": candidate_id,
        "message": clip(body.get("message"), 200),
        "job_id": job.get("id"),
        "job_status": job.get("status"),
        "created": body.get("created"),
        "next": "用 get_job(job_id) 轮询入库进度",
    }


def _subscribe_candidate(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    candidate_id = require_int(arguments, "candidate_id")
    body, status_code = writes.subscribe_trending_candidate(candidate_id)
    if status_code == 404:
        raise not_found("热榜候选")
    if status_code >= 400 or not body.get("success", True):
        raise AgentError(
            "STATE_CONFLICT" if status_code == 409 else "REJECTED",
            clip(body.get("message"), 300) or "转追更订阅失败",
            hint="确认候选已有 TMDB 身份且未存在同名订阅",
        )
    # ``item`` is the trending candidate itself, not the subscription; the helper
    # returns the subscription id separately.
    return {
        "success": True,
        "candidate_id": candidate_id,
        "subscription_id": body.get("subscription_id"),
        "created": body.get("created"),
        "message": clip(body.get("message"), 200),
        "next": "用 get_subscription 查看调度与来源",
    }


__all__ = ["SCOPES", "register"]
