from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from ..database import utc_now

from .update_values import as_bool

if TYPE_CHECKING:
    from .update_service import UpdateService

logger = logging.getLogger(__name__)


class UpdateScheduler:
    """Lease-protected scheduler process for update subscriptions."""

    def __init__(
        self,
        service: UpdateService,
        interval_seconds: int = 60,
        *,
        enabled: bool = True,
        max_subscriptions_per_tick: int = 5,
        coalesce_missed_runs: bool = True,
        owner_id: str = "",
    ) -> None:
        self.service = service
        self.interval_seconds = max(30, int(interval_seconds or 60))
        self.enabled = bool(enabled)
        self.max_subscriptions_per_tick = max(1, int(max_subscriptions_per_tick or 5))
        self.coalesce_missed_runs = bool(coalesce_missed_runs)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_run_at = ""
        self.last_error = ""
        self.owner_id = str(owner_id or f"update-scheduler-{id(self)}")
        self.lease_name = "update-subscription-scheduler"

    def start(self) -> None:
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name=self.lease_name, daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        try:
            self.service.db.release_scheduler_lease(self.lease_name, self.owner_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to release update scheduler lease")

    def apply_config(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        max_subscriptions_per_tick: int | None = None,
        coalesce_missed_runs: bool | None = None,
    ) -> None:
        if interval_seconds:
            self.interval_seconds = max(30, int(interval_seconds))
        if max_subscriptions_per_tick:
            self.max_subscriptions_per_tick = max(1, int(max_subscriptions_per_tick))
        if coalesce_missed_runs is not None:
            self.coalesce_missed_runs = bool(coalesce_missed_runs)
        if enabled is not None:
            was_enabled = self.enabled
            self.enabled = bool(enabled)
            if was_enabled and not self.enabled:
                self.shutdown()
            elif self.enabled:
                self.start()

    def status(self) -> dict[str, Any]:
        next_subscription: dict[str, Any] | None = None
        running_run: dict[str, Any] | None = None
        scheduler = self.service._scheduler_config()
        try:
            rows = self.service.db.list_update_subscriptions(limit=1, status="enabled", include_sources=False)
            if rows:
                item = rows[0]
                next_subscription = {
                    "id": item.get("id"),
                    "title": item.get("title") or "",
                    "category": item.get("category") or "",
                    "next_run_at": item.get("next_run_at") or "",
                    "last_success_episode": item.get("last_success_episode"),
                    "next_episode": item.get("next_episode"),
                }
        except Exception:  # noqa: BLE001
            pass
        try:
            running_run = self.service.db.get_running_update_run()
        except Exception:  # noqa: BLE001
            pass
        return {
            "enabled": self.enabled,
            "running": bool(self.thread and self.thread.is_alive()),
            "task_running": bool(running_run),
            "interval_seconds": self.interval_seconds,
            "max_subscriptions_per_tick": self.max_subscriptions_per_tick,
            "max_episodes_per_run": int(scheduler.get("max_episodes_per_run") or 10),
            "coalesce_missed_runs": self.coalesce_missed_runs,
            "retry_config": {
                "empty_retry_enabled": _as_bool(scheduler.get("empty_retry_enabled"), True),
                "empty_retry_interval_minutes": int(scheduler.get("empty_retry_interval_minutes") or 30),
                "empty_retry_max_attempts": int(scheduler.get("empty_retry_max_attempts") or 4),
                "empty_retry_max_window_hours": int(scheduler.get("empty_retry_max_window_hours") or 12),
                "failure_retry_interval_minutes": int(scheduler.get("failure_retry_interval_minutes") or scheduler.get("empty_retry_interval_minutes") or 30),
                "pending_import_check_interval_minutes": int(scheduler.get("pending_import_check_interval_minutes") or scheduler.get("empty_retry_interval_minutes") or 30),
                "source_health_warn_threshold": int(scheduler.get("source_health_warn_threshold") or scheduler.get("empty_retry_max_attempts") or 4),
            },
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "next_subscription": next_subscription,
            "current_run": running_run,
            "last_result": self.service.last_run_result,
        }

    def run_due(self, limit: int = 10) -> dict[str, Any]:
        result = self.service.run_due(
            limit=min(int(limit or self.max_subscriptions_per_tick), self.max_subscriptions_per_tick),
            trigger_type="external",
        )
        self.last_run_at = utc_now()
        self.last_error = ""
        self.service.last_run_result = result
        return result

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                lease_ttl = max(90, self.interval_seconds * 3)
                if not self.service.db.acquire_scheduler_lease(self.lease_name, self.owner_id, lease_ttl):
                    continue
                self.service.run_due(limit=self.max_subscriptions_per_tick, trigger_type="schedule")
                self.last_run_at = utc_now()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001
                logger.exception("update scheduler failed")
                self.last_error = str(exc)


def _as_bool(value: Any, default: bool = False) -> bool:
    return as_bool(value, default)
