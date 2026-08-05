from __future__ import annotations

import threading
from typing import Any, Callable


class RcloneScheduler:
    """Runs periodic rclone submissions while holding a database lease."""

    lease_name = "rclone-scheduler"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str,
        submit: Callable[..., dict[str, Any]],
        log: Callable[[str], None],
    ) -> None:
        self.database = database
        self.owner_id = owner_id
        self.submit = submit
        self.log = log
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self, interval_minutes: int) -> None:
        interval = int(interval_minutes or 0)
        if interval <= 0 or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        stop_event = self.stop_event
        self.thread = threading.Thread(
            target=self._loop,
            args=(interval, stop_event),
            name=self.lease_name,
            daemon=True,
        )
        self.thread.start()
        self.log(f"已启用 rclone 内置定时：每 {interval} 分钟执行一次")

    def shutdown(self) -> None:
        self.stop_event.set()
        if not self.database:
            return
        try:
            self.database.release_scheduler_lease(self.lease_name, self.owner_id)
        except Exception:  # noqa: BLE001
            self.log("释放 rclone 调度租约失败")

    def restart(self, interval_minutes: int) -> None:
        self.shutdown()
        self.thread = None
        self.stop_event = threading.Event()
        self.start(interval_minutes)

    def _loop(self, interval_minutes: int, stop_event: threading.Event | None = None) -> None:
        active_stop_event = stop_event or self.stop_event
        interval_seconds = max(60, interval_minutes * 60)
        while not active_stop_event.wait(interval_seconds):
            if self.database and not self.database.acquire_scheduler_lease(
                self.lease_name,
                self.owner_id,
                max(180, interval_seconds * 2),
            ):
                continue
            self.submit(reason="schedule")
