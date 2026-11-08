"""Discovery tools: search, resource inspection, TMDB identity."""

from __future__ import annotations

from typing import Any

from ..args import optional_bool, optional_text, optional_text_list, require_text
from ..auth import SCOPE_SEARCH
from ..context import AgentCallContext
from ..errors import invalid_params, not_found
from ..projection import clip, compact_search_item
from ..registry import ToolRegistry, ToolSpec
from ..schema import array, boolean, integer, obj, string
from ..services import AgentServices

FILE_FIELDS = ("id", "fid", "name", "title", "path", "size", "size_text", "is_dir", "directory", "media_type")


def _compact_file(item: dict[str, Any]) -> dict[str, Any]:
    projected = {name: item[name] for name in FILE_FIELDS if item.get(name) is not None}
    for name in ("name", "title", "path"):
        if name in projected:
            projected[name] = clip(projected[name], 200)
    if "is_dir" not in projected and "directory" in projected:
        projected["is_dir"] = bool(projected["directory"])
    return projected


def _compact_inspection(value: Any, *, max_items: int = 50) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = {name: value[name] for name in ("ok", "available", "message", "provider", "summary") if value.get(name) is not None}
    items = value.get("items")
    if isinstance(items, list):
        rows = [item for item in items if isinstance(item, dict)]
        projected["item_count"] = len(rows)
        projected["items"] = [_compact_file(item) for item in rows[:max_items]]
        if len(rows) > max_items:
            projected["items_truncated"] = True
    if isinstance(value.get("inspection"), dict):
        projected["inspection"] = _compact_inspection(value["inspection"], max_items=max_items)
    return projected


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="search_resources",
            title="搜索影视资源",
            description=(
                "按关键词聚合搜索网盘分享与 BT 资源，返回带 public_id 的候选列表。"
                "拿到 public_id 后可用 get_resource 查看详情与文件清单。"
                "结果有缓存有效期，过期后需要重新搜索。"
            ),
            input_schema=obj(
                {
                    "keyword": string("影视名称或关键词，例如 沙丘2 或 三体 第二季", max_length=80),
                    "sources": array(string(), description="限定的搜索源，留空表示全部", max_items=8),
                    "limit": integer("返回条数，默认 10，最多 20", default=10, minimum=1, maximum=20),
                    "refresh": boolean("忽略缓存强制刷新，较慢", default=False),
                },
                required=["keyword"],
            ),
            scope=SCOPE_SEARCH,
            handler=_search_resources,
        )
    )
    registry.register(
        ToolSpec(
            name="get_resource",
            title="查看资源详情",
            description=(
                "根据 public_id 读取资源详情，包括提供方、是否可入库、"
                "体积、分类建议与顶层文件清单（inspection.items 含 fid）。"
                "展开子目录需要 list_resource_files。"
            ),
            input_schema=obj(
                {"public_id": string("搜索返回的资源编号", max_length=120)},
                required=["public_id"],
            ),
            scope=SCOPE_SEARCH,
            handler=_get_resource,
        )
    )
    registry.register(
        ToolSpec(
            name="list_resource_files",
            title="展开资源目录",
            description=(
                "列出资源某个目录下的文件与子目录。首次调用可传 fid=\"0\"；"
                "更稳妥的是先用 get_resource，从 inspection.items[].fid 取目录 ID。"
                "结果较大时用 offset 翻页，返回 has_more 与 next_offset。"
            ),
            input_schema=obj(
                {
                    "public_id": string("资源编号", max_length=120),
                    "fid": string("目录 ID，默认 0 表示根目录", default="0", max_length=256),
                    "limit": integer("每页条目数", default=50, minimum=1, maximum=100),
                    "offset": integer("从第几条开始，用于翻页", default=0, minimum=0, maximum=100000),
                },
                required=["public_id"],
            ),
            scope=SCOPE_SEARCH,
            handler=_list_resource_files,
        )
    )
    registry.register(
        ToolSpec(
            name="detect_link",
            title="识别链接类型",
            description="判断一个分享链接属于哪个网盘、是否支持自动入库，不会创建任务。",
            input_schema=obj(
                {
                    "url": string("分享链接或磁链", max_length=2048),
                    "password": string("提取码，没有就留空", max_length=32),
                },
                required=["url"],
            ),
            scope=SCOPE_SEARCH,
            handler=_detect_link,
        )
    )
    registry.register(
        ToolSpec(
            name="resolve_tmdb",
            title="识别影视身份",
            description=(
                "用 TMDB 查询影视条目，返回候选与 selection_token。"
                "提交入库或创建追更订阅前应该先确认身份，避免同名误判。"
            ),
            input_schema=obj(
                {
                    "keyword": string("影视名称", max_length=80),
                    "limit": integer("最多返回候选数", default=5, minimum=1, maximum=10),
                },
                required=["keyword"],
            ),
            scope=SCOPE_SEARCH,
            handler=_resolve_tmdb,
        )
    )


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def _search_resources(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    keyword = require_text(arguments, "keyword", max_length=80)
    sources = optional_text_list(arguments, "sources", max_items=8, max_length=32)
    limit = int(arguments.get("limit") or 10)
    refresh = optional_bool(arguments, "refresh", default=False)
    result = context.services.search(keyword, sources=sources, refresh=refresh)
    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    items = [compact_search_item(item) for item in raw_items[: max(1, min(limit, 20))]]
    return {
        "keyword": keyword,
        "total": len(raw_items),
        "returned": len(items),
        "items": items,
        "expires_in_minutes": result.get("expires_in_minutes"),
        "next": "选定候选用 get_resource(public_id=...) 查看详情",
    }


def _get_resource(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    public_id = require_text(arguments, "public_id", max_length=120)
    result, status = context.services.resource_detail(public_id)
    if status == 404 or not result.get("success", False):
        raise not_found("资源详情")
    detail = result.get("detail") or {}
    link = detail.get("link") if isinstance(detail.get("link"), dict) else {}
    return {
        "public_id": detail.get("public_id") or public_id,
        "title": clip(detail.get("title"), 200),
        "provider_kind": detail.get("provider_kind"),
        "source_form": detail.get("source_form"),
        "supported": detail.get("supported"),
        "reason": clip(detail.get("reason")),
        "size_text": detail.get("size_text"),
        "datetime": detail.get("datetime"),
        "category_suggestion": detail.get("category_suggestion"),
        "detail_capability": detail.get("detail_capability"),
        "link": {
            name: link[name]
            for name in ("provider_kind", "source_form", "supported", "url", "source_url", "source_url_masked")
            if link.get(name) is not None
        },
        "inspection": _compact_inspection(detail.get("inspection")),
        "expires_at": detail.get("expires_at"),
    }


def _list_resource_files(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    public_id = require_text(arguments, "public_id", max_length=120)
    fid = optional_text(arguments, "fid", max_length=256) or "0"
    limit = int(arguments.get("limit") or 50)
    offset = int(arguments.get("offset") or 0)
    result, status = context.services.resource_files(public_id, fid)
    if status == 404:
        raise not_found("资源详情")
    if not result.get("success", False):
        raise invalid_params(
            str(result.get("message") or "目录读取失败"),
            hint="确认 fid 是否正确；可先用 get_resource 读取顶层目录",
        )
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    page_size = max(1, min(limit, 100))
    start = max(0, offset)
    capped = items[start : start + page_size]
    has_more = start + page_size < len(items)
    return {
        "public_id": public_id,
        "fid": fid,
        "total": len(items),
        "returned": len(capped),
        "offset": start,
        "has_more": has_more,
        "next_offset": start + page_size if has_more else None,
        "items": [_compact_file(item) for item in capped],
        "summary": result.get("summary") or {},
        "message": clip(result.get("message"), 200),
    }


def _detect_link(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    url = require_text(arguments, "url", max_length=2048)
    password = optional_text(arguments, "password", max_length=32)
    link = context.services.detect_link(url, password)
    return {
        "provider_kind": link.get("provider_kind"),
        "source_form": link.get("source_form"),
        "search_source_id": link.get("search_source_id"),
        "supported": link.get("supported"),
        "reason": clip(link.get("reason")),
        "url": link.get("url"),
    }


def _resolve_tmdb(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    keyword = require_text(arguments, "keyword", max_length=80)
    limit = int(arguments.get("limit") or 5)
    result = context.services.tmdb_search(keyword) or {}
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    capped = []
    for item in items[: max(1, min(limit, 10))]:
        capped.append(
            {
                name: item[name]
                for name in (
                    "tmdb_id",
                    "media_type",
                    "title",
                    "original_title",
                    "year",
                    "release_date",
                    "poster_url",
                    "overview",
                    "selection_token",
                )
                if item.get(name) is not None
            }
        )
    return {
        "status": result.get("status"),
        "keyword": keyword,
        "fallback_query": result.get("fallback_query"),
        "items": capped,
        "message": clip(result.get("message")),
        "next": "把 selection_token 作为 media_identity_token 传给后续入库工具",
    }
