"""Media library tools: inspect FNOS libraries and trigger a refresh."""

from __future__ import annotations

from typing import Any

from ..args import optional_text, require_text
from ..auth import SCOPE_OPS_READ, SCOPE_ORGANIZE_WRITE
from ..context import AgentCallContext
from ..errors import AgentError, invalid_params
from ..projection import clip
from ..registry import RISK_WRITE, ToolRegistry, ToolSpec
from ..schema import integer, obj, string
from ..services import AgentServices
from .common import writes_of

_LIBRARY_FIELDS = (
    "row_key",
    "title",
    "name",
    "guid",
    "fnos_type",
    "resource_count",
    "item_count",
    "matched_category_key",
    "matched_category_label",
    "dir",
    "status",
    "message",
)


def _compact_library(row: dict[str, Any]) -> dict[str, Any]:
    projected = {name: row[name] for name in _LIBRARY_FIELDS if row.get(name) is not None}
    for name in ("title", "name", "message"):
        if name in projected:
            projected[name] = clip(projected[name], 160)
    return projected


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="list_media_libraries",
            title="列出飞牛媒体库",
            description="读取飞牛媒体库列表、分类映射与运行中的刷新任务。",
            input_schema=obj({}),
            scope=SCOPE_OPS_READ,
            handler=_list_libraries,
        )
    )
    registry.register(
        ToolSpec(
            name="search_library",
            title="检索飞牛媒体库",
            description=(
                "在飞牛影视的媒体库里按名称检索，判断某个标题是否已经入库。"
                "返回每个命中条目的本地季/集数量与 in_library 标记；"
                "搜索资源之前先查一次，可以避免重复入库。"
            ),
            input_schema=obj(
                {
                    "keyword": string("影视名称，例如 繁花", max_length=80),
                    "limit": integer("最多返回条目", default=10, minimum=1, maximum=50),
                },
                required=["keyword"],
            ),
            scope=SCOPE_OPS_READ,
            handler=_search_library,
        )
    )
    registry.register(
        ToolSpec(
            name="refresh_media_library",
            title="刷新媒体库",
            description=(
                "触发某个分类的飞牛媒体库刷新，让新入库的内容被识别。"
                "传入分类 key（与订阅/入库使用的一致）。"
            ),
            input_schema=obj({"category": string("分类 key，例如 movie / tv / anime", max_length=40)}, required=["category"]),
            scope=SCOPE_ORGANIZE_WRITE,
            risk=RISK_WRITE,
            idempotent=False,
            handler=_refresh_library,
        )
    )


def _list_libraries(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    data = writes.list_media_libraries() or {}
    rows = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    running = [item for item in (data.get("running") or []) if isinstance(item, dict)]
    return {
        "success": bool(data.get("success", True)),
        "message": clip(data.get("message"), 200),
        "libraries": [_compact_library(row) for row in rows[:50]],
        "running": [_compact_library(row) for row in running[:20]],
        "summary": data.get("summary") or {},
    }


def _search_library(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    keyword = require_text(arguments, "keyword", max_length=80)
    limit = int(arguments.get("limit") or 10)
    data = writes.search_media_library(keyword, limit)
    if not data.get("success", True):
        reason = clip(data.get("message"), 300) or "飞牛媒体库检索失败"
        upstream_code = data.get("upstream_code")
        failure = str(data.get("code") or "")
        if failure == "NOT_CONFIGURED":
            raise AgentError(
                "LIBRARY_NOT_CONFIGURED",
                reason,
                retryable=False,
                hint="到后台「系统设置 → 飞牛影视」填写地址、用户名与密码，保存并检测通过后再试",
            )
        if failure == "LOGIN_FAILED":
            raise AgentError(
                "LIBRARY_AUTH_FAILED",
                reason,
                retryable=False,
                hint=(
                    "飞牛登录失败：确认地址是容器可访问的（例如 http://NAS内网IP:5666，"
                    "不要用 127.0.0.1），以及账号密码在飞牛影视网页上能登录"
                ),
                details={"upstream_code": upstream_code},
            )
        raise AgentError(
            "LIBRARY_UNAVAILABLE",
            reason,
            retryable=True,
            hint="检查飞牛影视地址是否可从 Web 容器访问；必要时到后台「媒体库」页重新连接",
            details={"upstream_code": upstream_code},
        )
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    hits = [item for item in items if item.get("in_library")]
    return {
        "keyword": keyword,
        "in_library": bool(hits),
        "in_library_count": len(hits),
        "count": len(items),
        "total": data.get("total", len(items)),
        "items": items[: max(1, min(limit, 50))],
        "next": (
            "媒体库已有本地文件，先确认季集是否齐全，再决定是否补入库"
            if hits
            else "媒体库没有本地文件；可以按正常流程搜索并提交入库"
        ),
    }


def _refresh_library(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    category = require_text(arguments, "category", max_length=40)
    known = context.services.categories() or {}
    if category not in known:
        raise invalid_params(
            f"分类不存在：{category}",
            hint=f"可用分类：{', '.join(sorted(known))}",
        )
    body, status_code = writes.refresh_media_library({"category": category})
    if status_code >= 400 or not body.get("success", True):
        raise AgentError(
            "REJECTED",
            clip(body.get("message"), 300) or "媒体库刷新未触发",
            hint="检查飞牛媒体库配置与 Worker 状态",
        )
    return {
        "success": True,
        "category": category,
        "message": clip(body.get("message"), 200) or "已触发媒体库刷新",
        "running": body.get("running"),
    }


__all__ = ["register"]
