from __future__ import annotations

from typing import Any

from .rclone_history_repair_worker import RcloneHistoryRepairWorker


class RcloneWorkerRuntime:
    """Owns the lifecycle of rclone components enabled for the worker role."""

    def __init__(
        self,
        *,
        rclone_service: Any,
        history_repair: RcloneHistoryRepairWorker,
    ) -> None:
        self.rclone_service = rclone_service
        self.history_repair = history_repair
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.rclone_service.start_scheduler()
        self.history_repair.start()
        self.started = True

    def shutdown(self) -> None:
        self.rclone_service.shutdown_scheduler()
        self.history_repair.shutdown()
        self.started = False
