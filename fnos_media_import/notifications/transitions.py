"""从业务状态事务中生成通知事件。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..constants import JOB_REVIEW
from . import events as event_defs


ConfiguredEmitter = Callable[..., list[dict[str, Any]]]


def emit_organizer_review_required(
    database: Any,
    connection: Any,
    current: dict[str, Any],
    emit_configured: ConfiguredEmitter,
) -> bool:
    """在入库任务进入 Organizer 审核时，与状态更新同事务入队通知。"""
    if str(current.get("status") or "").strip().lower() != JOB_REVIEW:
        return False
    raw_data = _dict_value(current.get("raw_data"))
    completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
    if str(completion.get("stage") or "").strip().lower() != "review":
        return False
    task_id = _positive_int(completion.get("organizer_task_id"))
    job_id = _positive_int(current.get("id"))
    if not task_id or not job_id:
        return False
    task = connection.execute(
        """
        SELECT id, job_id, category, revision
        FROM organizer_tasks
        WHERE id=? AND job_id=?
        """,
        (task_id, job_id),
    ).fetchone()
    if not task:
        return False
    revision = max(1, _positive_int(task["revision"]) or 1)
    issue_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM organizer_mappings
            WHERE task_id=? AND status IN ('need_edit', 'conflict')
            """,
            (task_id,),
        ).fetchone()[0]
    )
    reason = str(
        completion.get("message")
        or current.get("error_message")
        or "Organizer 识别置信度不足或存在目标冲突，需要人工确认"
    ).strip()
    emit_configured(
        database,
        event_defs.EVENT_ORGANIZER_REVIEW_REQUIRED,
        {
            "task_id": task_id,
            "task_revision": revision,
            "job_id": job_id,
            "title": str(current.get("title") or "未命名资源"),
            "category": str(task["category"] or current.get("category") or ""),
            "issue_count": issue_count,
            "reason": reason,
        },
        idempotency_key=event_defs.idempotency_key(
            event_defs.EVENT_ORGANIZER_REVIEW_REQUIRED,
            f"{task_id}:{revision}",
        ),
        connection=connection,
    )
    return True


def _dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
