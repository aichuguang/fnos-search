"""Organizer tools: read the rename/move plan before anything is executed."""

from __future__ import annotations

from typing import Any

from ..args import optional_int, optional_text, require_int
from ..auth import SCOPE_ORGANIZE_READ
from ..context import AgentCallContext
from ..errors import not_found
from ..plans import mapping_action, organizer_plan_summary
from ..projection import compact_organizer_identity, compact_organizer_task, compact_reasons, page_meta, pick
from ..registry import ToolRegistry, ToolSpec
from ..schema import integer, obj, string
from ..services import AgentServices

# Real organizer task states (see OrganizerService transitions).
ORGANIZER_STATUSES = (
    "scanning",
    "waiting_review",
    "manual_confirmed",
    "auto_approved",
    "executing",
    "done",
    "skipped",
    "cancelled",
)
BLOCKING_MAPPING_STATUSES = {"need_edit", "conflict", "delete_ad", "skipped_existing"}


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="list_organizer_tasks",
            title="列出整理任务",
            description="分页列出 OpenList 标准化整理任务，可按状态过滤。",
            input_schema=obj(
                {
                    "status": string("整理状态过滤，例如 waiting_review", max_length=40),
                    "page": integer("页码，从 1 开始", default=1, minimum=1, maximum=10000),
                    "per_page": integer("每页条数", default=20, minimum=1, maximum=100),
                }
            ),
            scope=SCOPE_ORGANIZE_READ,
            handler=_list_tasks,
        )
    )
    registry.register(
        ToolSpec(
            name="get_organizer_task",
            title="查看整理任务详情",
            description=(
                "分页读取整理任务的真实文件清单、完整路径、映射、身份和审核原因。"
                "文件、映射与阻塞项可独立翻页；执行前核对 plan_summary 和 plan_hash。"
            ),
            input_schema=obj(
                {
                    "task_id": integer("整理任务编号", minimum=1),
                    "files_page": integer("文件列表页码", default=1, minimum=1, maximum=10000),
                    "mappings_page": integer("映射列表页码", default=1, minimum=1, maximum=10000),
                    "blocked_page": integer("阻塞项列表页码", default=1, minimum=1, maximum=10000),
                    "per_page": integer("各列表每页条数", default=50, minimum=1, maximum=100),
                },
                required=["task_id"],
            ),
            scope=SCOPE_ORGANIZE_READ,
            handler=_get_task,
        )
    )


def _list_tasks(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    page = optional_int(arguments, "page", default=1, minimum=1, maximum=10000)
    per_page = optional_int(arguments, "per_page", default=20, minimum=1, maximum=100)
    status = optional_text(arguments, "status", max_length=40) or None
    result = context.services.list_organizer_tasks(
        limit=per_page,
        offset=(page - 1) * per_page,
        status=status,
    )
    rows = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    return {
        "items": [compact_organizer_task(row) for row in rows],
        "pagination": page_meta(int(result.get("total") or 0), page, per_page),
        "organizer_status": result.get("status") or {},
    }


def _get_task(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = require_int(arguments, "task_id")
    per_page = optional_int(arguments, "per_page", default=50, minimum=1, maximum=100)
    pages = {name: optional_int(arguments, f"{name}_page", default=1, minimum=1, maximum=10000) for name in ("files", "mappings", "blocked")}
    result, status_code = context.services.organizer_task_detail(task_id)
    if status_code == 404 or not result.get("success", False):
        raise not_found("整理任务")
    task = result.get("task") or {}
    if not isinstance(task, dict):
        raise not_found("整理任务")
    mappings = [row for row in (task.get("mappings") or []) if isinstance(row, dict)]
    files = [row for row in (task.get("files") or []) if isinstance(row, dict)]
    blocked = [
        row for row in mappings if str(row.get("status") or "").strip().lower() in BLOCKING_MAPPING_STATUSES
    ]
    file_page = _page(files, pages["files"], per_page)
    mapping_page = _page(mappings, pages["mappings"], per_page)
    blocked_page = _page(blocked, pages["blocked"], per_page)
    plan = organizer_plan_summary(task)
    return {
        "task": compact_organizer_task(task),
        "organizer_status": result.get("status") or {},
        "files": _compact_files(file_page),
        "files_total": len(files),
        "files_truncated": len(files) > len(file_page),
        "files_pagination": page_meta(len(files), pages["files"], per_page),
        "mappings": _compact_mappings(mapping_page),
        "mappings_total": len(mappings),
        "mappings_truncated": len(mappings) > len(mapping_page),
        "mappings_pagination": page_meta(len(mappings), pages["mappings"], per_page),
        "blocked_count": len(blocked),
        "blocked_pagination": page_meta(len(blocked), pages["blocked"], per_page),
        "delete_count": plan.get("delete_count", 0),
        "legacy_delete_count": sum(1 for row in mappings if str(row.get("status") or "") == "delete_ad"),
        "blocked": [{"mapping_id": row.get("id"), **row} for row in _compact_mappings(blocked_page)],
        "plan_summary": plan,
        "next": (
            f"{len(blocked)} 项需要处理；逐页核对审核原因，修正身份、季集或冲突。修改 ready 不能绕过校验；修正后重新预览计划。"
            if blocked
            else "核对身份、目标路径和 plan_summary；没有阻塞状态不等于已通过身份校验，执行仍需最新计划确认。"
        ),
    }


def _page(rows: list[dict[str, Any]], page: int, per_page: int) -> list[dict[str, Any]]:
    offset = (page - 1) * per_page
    return rows[offset:offset + per_page]


def _compact_files(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pick(row, ("id", "path", "name", "parent_path", "ext", "size", "season", "episode", "media_type", "is_video")) for row in rows]


def _compact_mappings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected = []
    for row in rows:
        item = pick(row, (
            "id", "file_id", "source_path", "source_name", "target_path", "target_name", "category",
            "media_type", "title", "year", "tmdb_id", "season", "episode", "confidence", "status",
        ))
        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
        parsed = raw.get("parsed") if isinstance(raw.get("parsed"), dict) else {}
        identity = compact_organizer_identity(raw.get("organizer_identity"))
        item["action"] = mapping_action(row)
        item["companion_file"] = bool(raw.get("companion_file"))
        item["reasons"] = compact_reasons(row.get("reason"))
        item["review_reasons"] = compact_reasons(
            row.get("review_reasons"), parsed.get("review_reasons"), identity.get("errors"),
            row.get("reason") if row.get("status") in BLOCKING_MAPPING_STATUSES else None,
        )
        if isinstance(parsed.get("has_explicit_season"), bool):
            item["has_explicit_season"] = parsed["has_explicit_season"]
        if identity:
            item["identity"] = identity
        projected.append(item)
    return projected


__all__ = ["ORGANIZER_STATUSES", "register"]
