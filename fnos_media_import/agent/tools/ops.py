"""Read-only operational views."""

from __future__ import annotations

from typing import Any

from ..auth import SCOPE_OPS_READ
from ..context import AgentCallContext
from ..registry import ToolRegistry, ToolSpec
from ..schema import obj
from ..services import AgentServices


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="get_system_status",
            title="查看系统状态",
            description="聚合返回数据库、Worker、rclone、整理器与调度器的健康状态。",
            input_schema=obj({}),
            scope=SCOPE_OPS_READ,
            handler=_system_status,
        )
    )
    registry.register(
        ToolSpec(
            name="get_scheduler_status",
            title="查看追更调度器",
            description="读取追更调度器的启用状态、下次运行时间与最近一次执行结果。",
            input_schema=obj({}),
            scope=SCOPE_OPS_READ,
            handler=_scheduler_status,
        )
    )


def _system_status(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"status": context.services.system_status()}


def _scheduler_status(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"scheduler": context.services.scheduler_status()}


__all__ = ["register"]
