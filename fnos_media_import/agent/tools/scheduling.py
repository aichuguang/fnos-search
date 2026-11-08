"""Scheduling tools, scoped to update subscriptions only."""

from __future__ import annotations

from typing import Any

from ..args import optional_int, optional_text, require_int
from ..auth import SCOPE_SCHEDULE_READ
from ..context import AgentCallContext
from ..errors import not_found
from ..projection import clip, compact_event, compact_subscription, page_meta
from ..registry import ToolRegistry, ToolSpec
from ..schema import integer, obj, string
from ..services import AgentServices

SUBSCRIPTION_STATUSES = ("enabled", "paused", "completed", "error")


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="list_subscriptions",
            title="列出追更订阅",
            description="分页列出定时追更订阅，包含更新节奏、下次运行时间与状态。",
            input_schema=obj(
                {
                    "status": string("状态过滤：enabled / paused / completed / error", max_length=40),
                    "page": integer("页码，从 1 开始", default=1, minimum=1, maximum=10000),
                    "per_page": integer("每页条数", default=20, minimum=1, maximum=100),
                }
            ),
            scope=SCOPE_SCHEDULE_READ,
            handler=_list_subscriptions,
        )
    )
    registry.register(
        ToolSpec(
            name="get_subscription",
            title="查看追更订阅详情",
            description=(
                "读取单个追更订阅的规则、来源、最近运行记录与候选，"
                "用于判断是不是同一部作品已存在订阅。"
            ),
            input_schema=obj({"subscription_id": integer("订阅编号", minimum=1)}, required=["subscription_id"]),
            scope=SCOPE_SCHEDULE_READ,
            handler=_get_subscription,
        )
    )


def _list_subscriptions(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    page = optional_int(arguments, "page", default=1, minimum=1, maximum=10000)
    per_page = optional_int(arguments, "per_page", default=20, minimum=1, maximum=100)
    status = optional_text(arguments, "status", max_length=40) or None
    result = context.services.list_subscriptions(page=page, per_page=per_page, status=status)
    rows = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    return {
        "items": [compact_subscription(row) for row in rows],
        "pagination": page_meta(int(result.get("total") or 0), page, per_page),
    }


def _get_subscription(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    subscription_id = require_int(arguments, "subscription_id")
    result, status_code = context.services.subscription_detail(subscription_id)
    if status_code == 404 or not result.get("success", False):
        raise not_found("追更订阅")
    item = result.get("item") or {}
    runs = [row for row in (result.get("runs") or []) if isinstance(row, dict)]
    candidates = [row for row in (result.get("candidates") or []) if isinstance(row, dict)]
    events = [row for row in (result.get("events") or []) if isinstance(row, dict)]
    return {
        "subscription": compact_subscription(item),
        "recent_runs": [_compact_run(row) for row in runs[:5]],
        "candidates": [_compact_candidate(row) for row in candidates[:15]],
        "recent_events": [compact_event(row) for row in events[:15]],
    }


def _compact_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (clip(row[name], 300) if isinstance(row.get(name), str) else row[name])
        for name in (
            "id",
            "trigger_type",
            "status",
            "started_at",
            "finished_at",
            "target_episode",
            "found_count",
            "imported_count",
            "error_message",
        )
        if row.get(name) is not None
    }


def _compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (clip(row[name], 200) if isinstance(row.get(name), str) else row[name])
        for name in (
            "id",
            "title",
            "season",
            "episode",
            "status",
            "score",
            "provider_kind",
            "created_at",
        )
        if row.get(name) is not None
    }


__all__ = ["SUBSCRIPTION_STATUSES", "register"]
