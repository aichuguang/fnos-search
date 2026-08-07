"""V1.0 一次性切换预检（设计文档 §21.1）。

切换前提：非终态旧版本计划任务数量为 0。发现任何 plan_version != 4 的非终态
任务时，预检必须失败，要求取消旧任务后重新提交，而不是继续执行旧计划。
"""

from __future__ import annotations

from typing import Any

NON_TERMINAL_STATUSES = frozenset(
    {
        "created",
        "checking",
        "submitted",
        "provider_submitting",
        "waiting_transfer",
        "transferring",
        "waiting_openlist",
        "waiting_organizer",
        "organizing",
        "confirming",
        "refreshing",
        "review",
    }
)


def switch_precheck(database: Any) -> dict[str, Any]:
    """扫描 import_jobs，返回切换预检结果。

    ``database`` 提供 ``connect()`` 上下文管理器（sqlite3.Row）。
    """
    if database is None:
        return {"ok": True, "old_inflight": [], "message": "数据库未配置，跳过预检"}
    if not callable(getattr(database, "connect", None)):
        return {"ok": True, "old_inflight": [], "message": "数据库不支持扫描，跳过预检"}
    try:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, status, plan_version, raw_data
                FROM import_jobs
                WHERE COALESCE(plan_version, 0) != 4
                  AND lower(COALESCE(status, '')) NOT IN (
                    'done', 'failed', 'cancelled', 'unsupported', 'skipped', 'rejected'
                  )
                ORDER BY id ASC
                """
            ).fetchall()
    except Exception:  # noqa: BLE001
        return {"ok": False, "old_inflight": [], "message": "预检扫描失败，无法确认旧任务状态"}
    inflight = [
        {
            "id": int(row["id"]),
            "title": str(row["title"] or ""),
            "status": str(row["status"] or ""),
            "plan_version": int(row["plan_version"] or 0),
        }
        for row in rows
    ]
    if inflight:
        return {
            "ok": False,
            "old_inflight": inflight,
            "message": f"发现 {len(inflight)} 个非 v4 在途任务，按一次性切换策略须先取消并重新提交",
        }
    return {"ok": True, "old_inflight": [], "message": "切换预检通过：无非 v4 在途任务"}
