from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .durable_worker_runtime import DurableWorkerRuntime, TaskHandler


class WorkerLaneRuntime:
    """Run independent organizer and transfer lanes in one worker process.

    Each lane has its own single execution thread and its own durable task
    claim filter.  Organizer work can therefore no longer monopolize the slot
    required by transfers (and vice versa), while tasks *inside* a lane remain
    strictly serial.
    """

    ORGANIZER_TASK_TYPES = frozenset({
        "organizer_process",
        "organizer_apply",
        "media_refresh",
        "media_category_refresh",
    })
    TRANSFER_TASK_TYPES = frozenset({
        "import_transfer_execute",
        "import_retry",
        "public_import_create",
        "notification_deliver",
    })

    def __init__(
        self,
        *,
        repository: Any,
        owner_id: str,
        lane_handlers: dict[str, dict[str, TaskHandler]],
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        retry_delay_seconds: int = 30,
        log: Callable[[str], None] | None = None,
        retention_days: int = 7,
        cleanup_interval_seconds: int = 3600,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id
        self._lane_lock = threading.Lock()
        self._lanes: dict[str, DurableWorkerRuntime] = {}
        for lane_name, handlers in lane_handlers.items():
            self._lanes[str(lane_name)] = DurableWorkerRuntime(
                repository=repository,
                owner_id=f"{owner_id}:{lane_name}",
                handlers=dict(handlers),
                poll_seconds=poll_seconds,
                lease_seconds=lease_seconds,
                retry_delay_seconds=retry_delay_seconds,
                log=log,
                retention_days=retention_days,
                cleanup_interval_seconds=cleanup_interval_seconds,
            )

    @property
    def handlers(self) -> dict[str, TaskHandler]:
        merged: dict[str, TaskHandler] = {}
        for runtime in self._lanes.values():
            merged.update(runtime.handlers)
        return merged

    @property
    def lanes(self) -> dict[str, DurableWorkerRuntime]:
        return dict(self._lanes)

    def _lane_for_task_type(self, task_type: str) -> str:
        if task_type in self.ORGANIZER_TASK_TYPES:
            return "organizer"
        if task_type in self.TRANSFER_TASK_TYPES:
            return "transfer"
        if "organizer" in task_type or task_type.startswith("media_"):
            return "organizer"
        return "transfer"

    def register(self, task_type: str, handler: TaskHandler) -> None:
        normalized = str(task_type).strip()
        lane_name = self._lane_for_task_type(normalized)
        runtime = self._lanes.get(lane_name)
        if runtime is None:
            runtime = next(iter(self._lanes.values()))
        runtime.register(normalized, handler)

    def start(self) -> bool:
        started = False
        for runtime in self._lanes.values():
            started = runtime.start() or started
        return started

    def shutdown(self, timeout: float = 5.0) -> None:
        for runtime in self._lanes.values():
            runtime.shutdown(timeout=timeout)

    def status(self) -> dict[str, Any]:
        lane_status = {name: runtime.status() for name, runtime in self._lanes.items()}
        if not lane_status:
            return {"runtime_running": False, "healthy": False, "lanes": {}}
        now = time.monotonic()
        heartbeat_ages = [
            value.get("heartbeat_age_seconds")
            for value in lane_status.values()
            if isinstance(value.get("heartbeat_age_seconds"), (int, float))
        ]
        running = all(bool(value.get("runtime_running")) for value in lane_status.values())
        stale = any(bool(value.get("heartbeat_stale")) for value in lane_status.values())
        lease_expired = any(bool(value.get("active_task_lease_expired")) for value in lane_status.values())
        errors = sum(int(value.get("consecutive_runtime_errors") or 0) for value in lane_status.values())
        return {
            "runtime_running": running,
            "heartbeat_stale": stale,
            "heartbeat_age_seconds": round(max(heartbeat_ages), 3) if heartbeat_ages else None,
            "active_task_lease_expired": lease_expired,
            "consecutive_runtime_errors": errors,
            "processed_count": sum(int(value.get("processed_count") or 0) for value in lane_status.values()),
            "completed_count": sum(int(value.get("completed_count") or 0) for value in lane_status.values()),
            "deferred_count": sum(int(value.get("deferred_count") or 0) for value in lane_status.values()),
            "retryable_count": sum(int(value.get("retryable_count") or 0) for value in lane_status.values()),
            "business_failed_count": sum(
                int(value.get("business_failed_count") or 0) for value in lane_status.values()
            ),
            "last_heartbeat_at": max(
                (str(value.get("last_heartbeat_at") or "") for value in lane_status.values()),
                default="",
            ),
            "healthy": running and not stale and not lease_expired and errors == 0,
            "lanes": lane_status,
            "lane_count": len(lane_status),
            "checked_at_monotonic": now,
        }

    def run_once(self) -> bool:
        """Process at most one task in each lane (used by diagnostics/tests)."""

        handled = False
        for runtime in self._lanes.values():
            handled = runtime.run_once() or handled
        return handled
