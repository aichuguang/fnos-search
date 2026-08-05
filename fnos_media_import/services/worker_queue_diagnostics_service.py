from __future__ import annotations

from typing import Any, Callable


class WorkerQueueDiagnosticsService:
    def __init__(
        self,
        *,
        repository: Any,
        runtime: Any,
        dispatch_enabled: Callable[[], bool],
        runtime_required: Callable[[], bool] | bool = True,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.dispatch_enabled = dispatch_enabled
        self.runtime_required = runtime_required

    def status(self) -> dict[str, Any]:
        try:
            enabled = bool(self.dispatch_enabled())
            dispatch_error = ""
        except Exception as exc:  # noqa: BLE001
            enabled = False
            dispatch_error = str(exc)

        try:
            queue = self.repository.status()
            repository_error = ""
        except Exception as exc:  # noqa: BLE001
            queue = {
                "counts": {},
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "expired_leases": 0,
                "next_task": None,
            }
            repository_error = str(exc)

        runtime_state = self._runtime_status()
        runtime_required = self._runtime_is_required()
        runtime_healthy = bool(
            runtime_state.get(
                "healthy",
                runtime_state.get("runtime_running")
                and not runtime_state.get("heartbeat_stale", False),
            )
        )
        queue_healthy = (
            not repository_error
            and queue.get("expired_leases", 0) == 0
        )
        return {
            "enabled": enabled,
            "dispatch_enabled": enabled,
            "dispatch_error": dispatch_error,
            "runtime_required": runtime_required,
            "owner_id": str(getattr(self.runtime, "owner_id", "")),
            "registered_types": sorted(getattr(self.runtime, "handlers", {}).keys()),
            **runtime_state,
            **queue,
            "repository_error": repository_error,
            "queue_healthy": queue_healthy,
            "healthy": (
                not dispatch_error
                and queue_healthy
                and (runtime_healthy or not runtime_required)
            ),
        }

    def _runtime_status(self) -> dict[str, Any]:
        status = getattr(self.runtime, "status", None)
        if callable(status):
            try:
                value = status()
                if isinstance(value, dict):
                    return dict(value)
            except Exception as exc:  # noqa: BLE001
                return {
                    "runtime_running": False,
                    "heartbeat_stale": True,
                    "healthy": False,
                    "runtime_error": str(exc),
                }
        thread = getattr(self.runtime, "_thread", None)
        running = bool(thread and thread.is_alive())
        return {
            "runtime_running": running,
            "heartbeat_stale": not running,
            "healthy": running,
        }

    def _runtime_is_required(self) -> bool:
        try:
            if callable(self.runtime_required):
                return bool(self.runtime_required())
            return bool(self.runtime_required)
        except Exception:  # noqa: BLE001
            return True
