from __future__ import annotations

from typing import Any


class UpdateSchedulerRuntime:
    """Owns the lifecycle of the periodic update scheduler process component."""

    def __init__(self, scheduler: Any) -> None:
        self.scheduler = scheduler
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.scheduler.start()
        self.started = True

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.started = False
