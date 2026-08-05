from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .update_run_lease_service import UpdateRunAlreadyActive


@dataclass(frozen=True)
class UpdateRunContext:
    subscription_id: int
    subscription: dict[str, Any]
    run_id: int
    trigger_type: str
    sync_result: dict[str, Any]
    owner_id: str
    lease_seconds: int


class UpdateRunInitializer:
    """Synchronizes previous work and creates the durable update-run record."""

    def __init__(
        self,
        *,
        database: Any,
        sync_completion: Callable[[int], dict[str, Any]],
        record_stage: Callable[..., None],
        owner_id: str = "legacy",
        lease_seconds: Callable[[], int] | None = None,
    ) -> None:
        self.database = database
        self.sync_completion = sync_completion
        self.record_stage = record_stage
        self.owner_id = str(owner_id)
        self.lease_seconds = lease_seconds or (lambda: 120)

    def initialize(self, subscription_id: int, *, trigger_type: str) -> UpdateRunContext:
        sync_result = self.sync_completion(subscription_id)
        subscription = self.database.get_update_subscription(subscription_id, include_sources=True)
        if not subscription:
            raise ValueError("追更订阅不存在")
        if sync_result.get("completed_count"):
            subscription = self.database.get_update_subscription(subscription_id, include_sources=True) or subscription
        lease_seconds = max(30, int(self.lease_seconds()))
        claim = getattr(self.database, "claim_update_run", None)
        if callable(claim):
            run_id, active_run = claim(
                subscription_id,
                trigger_type,
                scheduled_at=str(subscription.get("next_run_at") or ""),
                owner_id=self.owner_id,
                lease_seconds=lease_seconds,
            )
        else:
            run_id, active_run = self.database.create_update_run(
                subscription_id,
                trigger_type,
                scheduled_at=str(subscription.get("next_run_at") or ""),
            ), None
        if run_id is None:
            raise UpdateRunAlreadyActive(active_run)
        self.record_stage(run_id, "start", "开始执行定时追更", {"trigger_type": trigger_type})
        if sync_result.get("checked"):
            self.record_stage(
                run_id,
                "sync_completion",
                "同步已提交入库任务的整理完成状态",
                sync_result,
            )
        self.database.add_update_event(
            subscription_id,
            run_id,
            "info",
            "开始执行定时追更",
            {"trigger_type": trigger_type},
        )
        return UpdateRunContext(
            subscription_id=subscription_id,
            subscription=subscription,
            run_id=run_id,
            trigger_type=trigger_type,
            sync_result=sync_result,
            owner_id=self.owner_id,
            lease_seconds=lease_seconds,
        )
