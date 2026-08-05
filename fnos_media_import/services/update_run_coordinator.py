from __future__ import annotations

import threading
from typing import Any, Callable


class UpdateRunCoordinator:
    """Coordinates non-blocking global and per-subscription update execution locks."""

    def __init__(self, execute: Callable[..., dict[str, Any]]) -> None:
        self.execute = execute
        self.global_lock = threading.Lock()
        self.subscription_locks: dict[int, threading.Lock] = {}
        self.registry_lock = threading.Lock()

    def run(self, subscription_id: int, *, trigger_type: str = "manual") -> dict[str, Any]:
        normalized_id = int(subscription_id)
        if not self.global_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "已有追更任务正在运行，请稍后再试",
                "locked": True,
            }
        subscription_lock = self._subscription_lock(normalized_id)
        if not subscription_lock.acquire(blocking=False):
            self.global_lock.release()
            return {
                "success": False,
                "message": "该订阅已有追更任务正在运行",
                "locked": True,
                "subscription_id": normalized_id,
            }
        try:
            return self.execute(normalized_id, trigger_type=trigger_type)
        finally:
            subscription_lock.release()
            self.global_lock.release()

    def _subscription_lock(self, subscription_id: int) -> threading.Lock:
        with self.registry_lock:
            return self.subscription_locks.setdefault(subscription_id, threading.Lock())
