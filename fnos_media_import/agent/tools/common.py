"""Helpers shared by the mutating tool modules."""

from __future__ import annotations

from typing import Any

from ..args import optional_bool, require_text
from ..context import AgentCallContext
from ..errors import AgentError
from ..services import AgentWriteServices


def writes_of(context: AgentCallContext) -> AgentWriteServices:
    writes = context.services.writes
    if writes is None:
        raise AgentError(
            "WRITES_DISABLED",
            "当前部署未启用 Agent 写操作",
            hint="确认 FNOS_AGENT_ENABLED 已开启，且服务已完整初始化",
        )
    return writes


def actor(context: AgentCallContext) -> str:
    """Identity written into domain audit fields."""

    return f"agent:{context.token_name}"[:120]


def require_confirm(arguments: dict[str, Any], *, action: str) -> None:
    if not optional_bool(arguments, "confirm", default=False):
        raise AgentError(
            "CONFIRMATION_REQUIRED",
            f"{action} 需要显式确认",
            hint="先核对计划或影响面，再带 confirm=true 重新调用",
        )


def require_reason(arguments: dict[str, Any], *, max_length: int = 300) -> str:
    return require_text(arguments, "reason", max_length=max_length)


def compact_write_result(body: dict[str, Any], *, success_default: bool = True) -> dict[str, Any]:
    """Keep only the fields an agent needs from a domain write response."""

    payload: dict[str, Any] = {
        "success": bool(body.get("success", success_default)),
        "message": str(body.get("message") or "")[:300],
    }
    for name in ("job_id", "request_id", "task_id", "run_id", "subscription_id", "status", "cancelled", "cleanup", "queued", "worker_task_id", "retry_mode", "executed", "requires_confirmation", "autonomous"):
        if body.get(name) is not None:
            payload[name] = body[name]
    job = body.get("job")
    if isinstance(job, dict):
        payload["job_status"] = job.get("status")
        payload["job_id"] = payload.get("job_id") or job.get("id")
    task = body.get("task")
    if isinstance(task, dict):
        payload["task_status"] = task.get("status")
        payload["task_revision"] = task.get("revision")
    return payload
