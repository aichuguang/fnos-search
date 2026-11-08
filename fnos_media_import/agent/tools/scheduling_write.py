"""Scheduling write tools, limited to update subscriptions."""

from __future__ import annotations

from typing import Any

from ..args import optional_bool, optional_int, optional_text, optional_text_list, require_int, require_text
from ..auth import SCOPE_SCHEDULE_WRITE
from ..context import AgentCallContext
from ..errors import AgentError, invalid_params, not_found
from ..projection import clip, compact_subscription
from ..registry import RISK_DESTRUCTIVE, RISK_WRITE, ToolRegistry, ToolSpec
from ..schema import array, boolean, integer, obj, source_schema, string
from ..services import AgentServices
from .common import require_confirm, writes_of

SCHEDULE_KINDS = ("tmdb", "daily", "weekly", "interval", "manual")
SOURCE_STRATEGIES = ("mixed", "cloud_first", "search_only", "cloud_only")
STATUSES = ("enabled", "paused", "archived")


def _subscription_payload(arguments: dict[str, Any], *, include_identity: bool) -> dict[str, Any]:
    """Build a create/update payload.

    For updates a key that is *present* always wins, so an explicit ``[]`` or
    ``""`` clears a previously configured filter instead of being treated as
    "not provided".
    """

    payload: dict[str, Any] = {}
    if include_identity:
        payload["tmdb_id"] = require_int(arguments, "tmdb_id")

    def supplied(name: str) -> bool:
        return name in arguments if not include_identity else bool(arguments.get(name))

    for name, limit in (("title", 200), ("year", 10), ("timezone", 64), ("time_of_day", 8), ("quality_profile", 64)):
        if supplied(name):
            payload[name] = optional_text(arguments, name, max_length=limit)
    for name in ("category", "media_type", "source_strategy", "schedule_kind"):
        if supplied(name):
            payload[name] = optional_text(arguments, name, max_length=32)
    for name in ("season", "next_episode", "interval_minutes", "min_score"):
        if arguments.get(name) is not None:
            payload[name] = int(arguments[name])
    for name in ("days_of_week", "include_keywords", "exclude_keywords", "aliases"):
        if not supplied(name):
            continue
        values = optional_text_list(arguments, name, max_items=50, max_length=64)
        payload[name] = [int(item) for item in values] if name == "days_of_week" else values
    sources = arguments.get("sources")
    if isinstance(sources, list):
        payload["sources"] = [item for item in sources if isinstance(item, dict)]
    return payload


def _validate_choice(payload: dict[str, Any]) -> None:
    kind = payload.get("schedule_kind")
    if kind and kind not in SCHEDULE_KINDS:
        raise invalid_params(f"schedule_kind 只能是 {', '.join(SCHEDULE_KINDS)}")
    strategy = payload.get("source_strategy")
    if strategy and strategy not in SOURCE_STRATEGIES:
        raise invalid_params(f"source_strategy 只能是 {', '.join(SOURCE_STRATEGIES)}")


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="create_subscription",
            title="创建追更订阅",
            description=(
                "为一部影视创建定时追更订阅。必须先通过 resolve_tmdb 拿到 tmdb_id——"
                "服务端会回查 TMDB 校验标题与首播信息。dry_run=true 只校验并返回下次运行时间，"
                "不会创建订阅。"
            ),
            input_schema=obj(
                {
                    "tmdb_id": integer("TMDB 编号（必填，来自 resolve_tmdb）", minimum=1),
                    "category": string("分类 key，例如 tv / anime / movie", max_length=40),
                    "media_type": string("tv 或 movie", enum=["tv", "movie"], max_length=8),
                    "title": string("标题兜底值", max_length=200),
                    "season": integer("季号", minimum=0, maximum=99),
                    "year": string("年份", max_length=10),
                    "next_episode": integer("从第几集开始追", minimum=0, maximum=9999),
                    "schedule_kind": string(
                        "调度方式：tmdb 按播出时间、daily、weekly、interval、manual",
                        enum=list(SCHEDULE_KINDS),
                        max_length=16,
                    ),
                    "days_of_week": array(integer(), description="weekly 时的星期（1-7）", max_items=7),
                    "time_of_day": string("每天执行时间 HH:MM", max_length=8),
                    "interval_minutes": integer("interval 间隔分钟", minimum=5, maximum=10080),
                    "timezone": string("时区，默认 Asia/Shanghai", max_length=64),
                    "sources": array(source_schema(), description="自定义来源列表，留空用综合搜索", max_items=20),
                    "source_strategy": string(
                        "来源策略", enum=list(SOURCE_STRATEGIES), max_length=20
                    ),
                    "min_score": integer("候选最低分", minimum=0, maximum=100),
                    "quality_profile": string("画质偏好关键词", max_length=64),
                    "include_keywords": array(string(), max_items=50),
                    "exclude_keywords": array(string(), max_items=50),
                    "dry_run": boolean("只校验不创建", default=False),
                },
                required=["tmdb_id", "category"],
            ),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_WRITE,
            handler=_create_subscription,
        )
    )
    registry.register(
        ToolSpec(
            name="update_subscription",
            title="修改追更订阅",
            description=(
                "修改已有追更订阅的调度、来源或质量条件；只传需要变更的字段，"
                "未提供的字段保持原值。修改后会重算下次运行时间。"
            ),
            input_schema=obj(
                {
                    "subscription_id": integer("订阅编号", minimum=1),
                    "category": string("分类 key", max_length=40),
                    "media_type": string("tv 或 movie", enum=["tv", "movie"], max_length=8),
                    "season": integer("季号", minimum=0, maximum=99),
                    "next_episode": integer("从第几集开始追", minimum=0, maximum=9999),
                    "schedule_kind": string(
                        "调度方式", enum=list(SCHEDULE_KINDS), max_length=16
                    ),
                    "days_of_week": array(integer(), description="weekly 时的星期（1-7）", max_items=7),
                    "time_of_day": string("每天执行时间 HH:MM", max_length=8),
                    "interval_minutes": integer("interval 间隔分钟", minimum=5, maximum=10080),
                    "timezone": string("时区", max_length=64),
                    "sources": array(source_schema(), description="替换来源列表；传 [] 恢复综合搜索", max_items=20),
                    "source_strategy": string("来源策略", enum=list(SOURCE_STRATEGIES), max_length=20),
                    "min_score": integer("候选最低分", minimum=0, maximum=100),
                    "quality_profile": string("画质偏好关键词", max_length=64),
                    "include_keywords": array(string(), max_items=50),
                    "exclude_keywords": array(string(), max_items=50),
                },
                required=["subscription_id"],
            ),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_WRITE,
            handler=_update_subscription,
        )
    )
    registry.register(
        ToolSpec(
            name="set_subscription_status",
            title="启用/暂停追更订阅",
            description="把订阅切换为 enabled、paused 或 archived。",
            input_schema=obj(
                {
                    "subscription_id": integer("订阅编号", minimum=1),
                    "status": string("目标状态", enum=list(STATUSES), max_length=16),
                },
                required=["subscription_id", "status"],
            ),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_WRITE,
            handler=_set_status,
        )
    )
    registry.register(
        ToolSpec(
            name="run_subscription_now",
            title="立即执行追更",
            description=(
                "不等下次计划时间，立即执行一次追更。会真实搜索并可能自动入库，"
                "执行记录可在 list_update_runs 查看。"
            ),
            input_schema=obj({"subscription_id": integer("订阅编号", minimum=1)}, required=["subscription_id"]),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_WRITE,
            idempotent=False,
            handler=_run_now,
        )
    )
    registry.register(
        ToolSpec(
            name="delete_subscription",
            title="删除追更订阅",
            description=(
                "永久删除一条追更订阅。必须显式确认；已入库的文件不受影响。"
                "只想暂停时请用 set_subscription_status。"
            ),
            input_schema=obj(
                {
                    "subscription_id": integer("订阅编号", minimum=1),
                    "confirm": boolean("确认删除", default=False),
                },
                required=["subscription_id"],
            ),
            scope=SCOPE_SCHEDULE_WRITE,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_delete_subscription,
        )
    )


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def _create_subscription(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    payload = _subscription_payload(arguments, include_identity=True)
    _validate_choice(payload)
    if not payload.get("category"):
        raise invalid_params("缺少 category", hint="movie / tv / anime 等分类 key")

    if optional_bool(arguments, "dry_run", default=False):
        body, status_code = writes.preview_subscription(payload)
        if status_code >= 400 or not body.get("success", True):
            raise _subscription_error(body, status_code)
        return {
            "dry_run": True,
            "title": clip(body.get("title"), 200),
            "category": body.get("category"),
            "media_type": body.get("media_type"),
            "season": body.get("season"),
            "schedule_kind": body.get("schedule_kind"),
            "time_of_day": body.get("time_of_day"),
            "days_of_week": body.get("days_of_week"),
            "timezone": body.get("timezone"),
            "next_run_at": body.get("next_run_at"),
            "message": "预检通过，未创建订阅",
        }

    body, status_code = writes.create_subscription(payload)
    if status_code >= 400 or not body.get("success", True):
        raise _subscription_error(body, status_code)
    item = body.get("item") if isinstance(body.get("item"), dict) else body
    result = compact_subscription(item)
    result["created"] = bool(item.get("_created", True))
    result["next"] = "用 run_subscription_now 可立即验证一次，或等待 next_run_at"
    return result


def _update_subscription(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    subscription_id = require_int(arguments, "subscription_id")
    payload = _subscription_payload(arguments, include_identity=False)
    _validate_choice(payload)
    if not payload:
        raise invalid_params("没有要修改的字段", hint="至少提供一个要变更的字段")
    body, status_code = writes.update_subscription(subscription_id, payload)
    if status_code == 404:
        raise not_found("追更订阅")
    if status_code >= 400 or not body.get("success", True):
        raise _subscription_error(body, status_code)
    item = body.get("item") if isinstance(body.get("item"), dict) else body
    return compact_subscription(item)


def _set_status(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    subscription_id = require_int(arguments, "subscription_id")
    status = require_text(arguments, "status", max_length=16)
    if status not in STATUSES:
        raise invalid_params(f"status 只能是 {', '.join(STATUSES)}")
    body, status_code = writes.set_subscription_status(subscription_id, status)
    if status_code == 404:
        raise not_found("追更订阅")
    if status_code >= 400 or not body.get("success", True):
        raise _subscription_error(body, status_code)
    item = body.get("item") if isinstance(body.get("item"), dict) else body
    result = compact_subscription(item)
    result["message"] = clip(body.get("message"), 100)
    return result


def _run_now(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    body, status_code = writes.run_subscription(require_int(arguments, "subscription_id"))
    if status_code == 404:
        raise not_found("追更订阅")
    if status_code >= 400 or not body.get("success", True):
        raise _subscription_error(body, status_code)
    return {
        "success": True,
        "run_id": body.get("run_id") or body.get("id"),
        "status": body.get("status"),
        "message": clip(body.get("message"), 200),
        "next": "用 list_update_runs 或 get_subscription 查看本次结果",
    }


def _subscription_error(body: dict[str, Any], status_code: int) -> AgentError:
    return AgentError(
        "STATE_CONFLICT" if status_code == 409 else "REJECTED",
        clip(body.get("message"), 300) or "订阅操作未执行",
        hint="确认 tmdb_id 与分类后重试；同名同季订阅可能已存在",
    )


def _delete_subscription(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    require_confirm(arguments, action="删除追更订阅")
    subscription_id = require_int(arguments, "subscription_id")
    body, status_code = writes.delete_subscription(subscription_id)
    if status_code == 404:
        raise not_found("追更订阅")
    if status_code >= 400 or not body.get("success", True):
        raise _subscription_error(body, status_code)
    return {
        "success": True,
        "subscription_id": subscription_id,
        "message": clip(body.get("message"), 200) or "追更订阅已删除",
    }


__all__ = ["register"]
