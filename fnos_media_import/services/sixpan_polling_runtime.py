from __future__ import annotations

import threading
from typing import Any, Callable


class SixPanPollingRuntime:
    """Runs the lease-protected SixPan offline-task polling loop."""

    lease_name = "sixpan-offline-poller"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str,
        poll_once: Callable[[], dict[str, Any]],
        interval_seconds: Callable[[], int],
        log: Callable[[str], None],
    ) -> None:
        self.database = database
        self.owner_id = owner_id
        self.poll_once = poll_once
        self.interval_seconds = interval_seconds
        self.log = log
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        stop_event = self.stop_event
        self.thread = threading.Thread(
            target=self._loop,
            args=(stop_event,),
            name=self.lease_name,
            daemon=True,
        )
        self.thread.start()

    def run_once(self) -> dict[str, Any]:
        interval = self._interval()
        if not self.database.acquire_scheduler_lease(
            self.lease_name,
            self.owner_id,
            max(90, interval * 3),
        ):
            return {"success": True, "skipped": True, "message": "其他 Scheduler 正在轮询六盘任务"}
        try:
            return self.poll_once()
        except Exception as exc:  # noqa: BLE001
            message = f"六盘离线轮询异常：{exc}"
            self.log(message)
            return {"success": False, "message": message}

    def shutdown(self) -> None:
        self.stop_event.set()
        self.database.release_scheduler_lease(self.lease_name, self.owner_id)

    def _loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self._interval()):
            self.run_once()

    def _interval(self) -> int:
        try:
            return max(10, min(3600, int(self.interval_seconds() or 60)))
        except (TypeError, ValueError):
            return 60
