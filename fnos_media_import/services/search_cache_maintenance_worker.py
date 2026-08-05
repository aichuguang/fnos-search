from __future__ import annotations

import threading
from typing import Any, Callable


class SearchCacheMaintenanceWorker:
    """Periodically removes expired search-cache rows under a scheduler lease."""

    lease_name = "search-cache-maintenance"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str,
        log: Callable[[str], None],
        interval_seconds: float = 3600,
        batch_size: int = 1000,
        max_delete_per_run: int = 10000,
        lease_ttl_seconds: int = 300,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        self.database = database
        self.owner_id = str(owner_id or f"search-cache-maintenance-{id(self)}")
        self.log = log
        self.interval_seconds = max(0.05, float(interval_seconds or 3600))
        self.batch_size = max(1, int(batch_size or 1000))
        self.max_delete_per_run = max(1, int(max_delete_per_run or 10000))
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds or 300))
        self.shutdown_timeout_seconds = max(0, float(shutdown_timeout_seconds or 0))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._loop,
            name=self.lease_name,
            daemon=True,
        )
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_timeout_seconds)
        # Do not release here: an active maintenance pass still owns the lease.
        # Its run_once() finally block releases it only after database work exits.

    def run_once(self) -> dict[str, Any]:
        if self.stop_event.is_set():
            return {"success": True, "skipped": True, "stopped": True, "deleted": 0, "batches": 0}

        acquired = False
        try:
            acquired = bool(
                self.database.acquire_scheduler_lease(
                    self.lease_name,
                    self.owner_id,
                    self.lease_ttl_seconds,
                )
            )
            if not acquired:
                return {"success": True, "skipped": True, "deleted": 0, "batches": 0}

            deleted_total = 0
            batches = 0
            while deleted_total < self.max_delete_per_run and not self.stop_event.is_set():
                limit = min(self.batch_size, self.max_delete_per_run - deleted_total)
                deleted = max(0, int(self.database.prune_expired_search_cache(limit=limit) or 0))
                deleted_total += deleted
                batches += 1
                if deleted < limit:
                    break

            if deleted_total:
                self._safe_log(f"Search cache maintenance deleted {deleted_total} expired row(s).")
            return {
                "success": True,
                "deleted": deleted_total,
                "batches": batches,
                "stopped": self.stop_event.is_set(),
            }
        except Exception as exc:  # noqa: BLE001
            self._safe_log(f"Search cache maintenance failed: {exc}")
            return {"success": False, "message": str(exc), "deleted": 0, "batches": 0}
        finally:
            if acquired:
                try:
                    self.database.release_scheduler_lease(self.lease_name, self.owner_id)
                except Exception as exc:  # noqa: BLE001
                    self._safe_log(f"Search cache maintenance lease release failed: {exc}")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.run_once()
            if self.stop_event.wait(self.interval_seconds):
                break

    def _safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001
            pass
