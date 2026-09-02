from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

from ..database import utc_now

logger = logging.getLogger(__name__)

# 同一 tick 内多个到期订阅逐个执行，中间留间隔 + 随机抖动，
# 避免集中打到网盘/搜索 API 触发风控（参考 CASX 的调度节流实践）。
_MIN_PER_SUBSCRIPTION_SECONDS = 5
_MAX_JITTER_SECONDS = 5


class UpdateDueRunService:
    """Scans due subscriptions and isolates failures between executions."""

    def __init__(
        self,
        *,
        database: Any,
        scheduler_config: Callable[[], dict[str, Any]],
        run_subscription: Callable[..., dict[str, Any]],
        record_result: Callable[[dict[str, Any]], None],
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.database = database
        self.scheduler_config = scheduler_config
        self.run_subscription = run_subscription
        self.record_result = record_result
        self._sleep = sleep or time.sleep

    def run_due(self, *, limit: int = 10, trigger_type: str = "external") -> dict[str, Any]:
        scheduler = self.scheduler_config()
        if scheduler.get("enabled") is False:
            return {"success": True, "count": 0, "items": [], "message": "追更调度器已停用"}
        requested_limit = _positive_int(limit or 10, 10)
        max_limit = _positive_int(scheduler.get("max_subscriptions_per_tick"), requested_limit)
        effective_limit = min(requested_limit, max_limit)
        due = self.database.update.list_update_subscriptions(
            limit=effective_limit,
            due_before=utc_now(),
            include_sources=True,
        )
        items = []
        for index, subscription in enumerate(due):
            if index and trigger_type != "manual":
                self._throttle_between_subscriptions(scheduler)
            try:
                items.append(self.run_subscription(int(subscription["id"]), trigger_type=trigger_type))
            except Exception as exc:  # noqa: BLE001
                logger.exception("scheduled update subscription failed and was isolated: %s", subscription.get("id"))
                items.append({"success": False, "subscription_id": subscription.get("id"), "message": str(exc)})
        result = {
            "success": True,
            "count": len(items),
            "items": items,
            "coalesced_missed_runs": bool(scheduler.get("coalesce_missed_runs", True)),
        }
        self.record_result(result)
        return result

    def _throttle_between_subscriptions(self, scheduler: dict[str, Any]) -> None:
        # manual 之外的批量执行（外部触发/定时 tick）逐订阅节流；
        # 单次手动 run_subscription 不经过这里，不受影响。
        base = _non_negative_float(
            scheduler.get("per_subscription_interval_seconds"),
            _MIN_PER_SUBSCRIPTION_SECONDS,
        )
        jitter = _non_negative_float(
            scheduler.get("per_subscription_jitter_seconds"),
            _MAX_JITTER_SECONDS,
        )
        delay = base + (random.uniform(0, jitter) if jitter > 0 else 0)
        if delay > 0:
            logger.info("update_due_throttle seconds=%.1f", delay)
            self._sleep(delay)


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, number)


def _non_negative_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return max(0.0, default)
    return max(0.0, number)
