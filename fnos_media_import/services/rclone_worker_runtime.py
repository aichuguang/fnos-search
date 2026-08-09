from __future__ import annotations

from typing import Any


class RcloneWorkerRuntime:
    """Owns the lifecycle of rclone components enabled for the worker role."""

    def __init__(
        self,
        *,
        rclone_service: Any,
    ) -> None:
        self.rclone_service = rclone_service
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True

    def shutdown(self) -> None:
        self.rclone_service.shutdown()
        self.started = False
