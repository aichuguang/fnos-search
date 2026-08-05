from __future__ import annotations

import logging
from typing import Any, Callable

from ..database import utc_now

logger = logging.getLogger(__name__)


class UpdateDueRunService:
    """Scans due subscriptions and isolates failures between executions."""

    def __init__(
        self,
        *,
        database: Any,
        scheduler_config: Callable[[], dict[str, Any]],
        run_subscription: Callable[..., dict[str, Any]],
        record_result: Callable[[dict[str, Any]], None],
    ) -> None:
        self.database = database
        self.scheduler_config = scheduler_config
        self.run_subscription = run_subscription
        self.record_result = record_result

    def run_due(self, *, limit: int = 10, trigger_type: str = "external") -> dict[str, Any]:
        scheduler = self.scheduler_config()
        if scheduler.get("enabled") is False:
            return {"success": True, "count": 0, "items": [], "message": "追更调度器已停用"}
        requested_limit = _positive_int(limit or 10, 10)
        max_limit = _positive_int(scheduler.get("max_subscriptions_per_tick"), requested_limit)
        effective_limit = min(requested_limit, max_limit)
        due = self.database.list_update_subscriptions(
            limit=effective_limit,
            due_before=utc_now(),
            include_sources=True,
        )
        items = []
        for subscription in due:
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


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, number)
