from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..constants import RCLONE_RUN_FAILED, RCLONE_RUN_RUNNING, RCLONE_RUN_SUCCESS


@dataclass
class RcloneRunState:
    status: str = "idle"
    last_started_at: str = ""
    last_finished_at: str = ""
    last_exit_code: int | None = None
    last_error: str = ""
    current_run_id: int | None = None

    def mark_starting(self) -> None:
        self.status = "starting"
        self.last_error = ""

    def mark_running(self, run_id: int | None, started_at: str) -> None:
        self.current_run_id = run_id
        self.last_started_at = started_at
        self.last_finished_at = ""
        self.last_exit_code = None
        self.last_error = ""
        self.status = RCLONE_RUN_RUNNING

    def mark_finished(self, exit_code: int, error: str, finished_at: str) -> int | None:
        run_id = self.current_run_id
        self.last_exit_code = exit_code
        self.last_finished_at = finished_at
        self.last_error = error
        self.status = RCLONE_RUN_SUCCESS if exit_code == 0 else RCLONE_RUN_FAILED
        self.current_run_id = None
        return run_id

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_exit_code": self.last_exit_code,
            "last_error": self.last_error,
            "current_run_id": self.current_run_id,
        }
