from __future__ import annotations

import threading
import time
from typing import Any, Callable


class WorkerWatchdog:
    """Terminates a worker process after a sustained critical runtime failure."""

    def __init__(
        self,
        *,
        status: Callable[[], dict[str, Any]],
        terminate: Callable[[], None],
        log_critical: Callable[[str], None],
        startup_grace_seconds: float = 120,
        failure_timeout_seconds: float = 90,
        interval_seconds: float = 15,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.status_callback = status
        self.terminate = terminate
        self.log_critical = log_critical
        self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        self.failure_timeout_seconds = max(1.0, float(failure_timeout_seconds))
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_at: float | None = None
        self.unhealthy_since: float | None = None
        self.triggered = False
        self.last_status: dict[str, Any] = {}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.started_at = self.monotonic()
        self.thread = threading.Thread(target=self._run, name="worker-watchdog", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(5.0, self.interval_seconds + 1.0))

    def check_once(self, now: float | None = None) -> bool:
        current = self.monotonic() if now is None else float(now)
        started_at = self.started_at if self.started_at is not None else current
        if current - started_at < self.startup_grace_seconds:
            return False
        try:
            status = self.status_callback()
        except Exception:  # noqa: BLE001
            status = {"success": False, "healthy": False, "status": "probe_failed"}
        self.last_status = dict(status) if isinstance(status, dict) else {}
        healthy = bool(self.last_status.get("success") and self.last_status.get("healthy"))
        if healthy:
            self.unhealthy_since = None
            return False
        if self.unhealthy_since is None:
            self.unhealthy_since = current
            return False
        if current - self.unhealthy_since < self.failure_timeout_seconds or self.triggered:
            return False
        self.triggered = True
        failed = self.last_status.get("failed_checks") or []
        detail = "、".join(str(item) for item in failed) or str(self.last_status.get("status") or "unknown")
        self.log_critical(f"Worker 核心运行时持续异常，准备退出并重建：{detail}")
        self.terminate()
        return True

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            if self.check_once():
                return
