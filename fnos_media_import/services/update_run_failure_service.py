from __future__ import annotations

from typing import Any, Callable

from ..database import utc_now


class UpdateRunFailureService:
    """Persists the shared failure outcome for an update run."""

    def __init__(
        self,
        *,
        database: Any,
        record_stage: Callable[..., None],
        next_retry_at: Callable[[dict[str, Any], str], str] | None = None,
    ) -> None:
        self.database = database
        self.record_stage = record_stage
        self.next_retry_at = next_retry_at

    def record(
        self,
        *,
        subscription_id: int,
        run_id: int,
        error: Exception,
        candidate_count: int,
        submitted_count: int,
        skipped_count: int,
        owner_id: str = "legacy",
        trigger_type: str = "schedule",
    ) -> None:
        message = str(error)
        owns = getattr(self.database, "owns_update_run", None)
        if callable(owns) and not owns(run_id, owner_id):
            return
        self.record_stage(run_id, "failed", "定时追更执行失败", {"error": message})
        finish = getattr(self.database, "finish_update_run", None)
        if callable(finish):
            finished = finish(
                run_id,
                owner_id,
                status="failed",
                candidate_count=candidate_count,
                imported_count=submitted_count,
                skipped_count=skipped_count,
                error_message=message,
            )
        else:
            self.database.update_update_run(
                run_id,
                status="failed",
                candidate_count=candidate_count,
                imported_count=submitted_count,
                skipped_count=skipped_count,
                error_message=message,
            )
            finished = True
        if finished:
            retry_at = self._schedule_retry(
                subscription_id=subscription_id,
                run_id=run_id,
                message=message,
                trigger_type=trigger_type,
            )
            self.database.add_update_event(
                subscription_id,
                run_id,
                "error",
                f"定时追更执行失败：{message}" + (f"；将在 {retry_at} 自动重试" if retry_at else ""),
            )

    def _schedule_retry(
        self,
        *,
        subscription_id: int,
        run_id: int,
        message: str,
        trigger_type: str,
    ) -> str:
        get_subscription = getattr(self.database, "get_update_subscription", None)
        update_subscription = getattr(self.database, "update_update_subscription", None)
        if not callable(get_subscription) or not callable(update_subscription):
            return ""
        subscription = get_subscription(subscription_id, include_sources=False)
        if not subscription:
            return ""
        retry_at = self.next_retry_at(subscription, trigger_type) if self.next_retry_at else ""
        raw_data = dict(subscription.get("raw_data")) if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data["last_run_failure"] = {
            "run_id": run_id,
            "error": message,
            "failed_at": utc_now(),
            "retry_at": retry_at,
            "trigger_type": trigger_type,
        }
        updates: dict[str, Any] = {
            "last_run_at": utc_now(),
            "raw_data": raw_data,
        }
        if retry_at:
            updates["next_run_at"] = retry_at
        update_subscription(subscription_id, updates)
        return retry_at
