from __future__ import annotations

from typing import Any, Callable

from ..constants import RCLONE_RUN_FAILED, RCLONE_RUN_SUCCESS
from .rclone_run_state import RcloneRunState


class RcloneRunCompletionService:
    """Finalizes rclone process state, run records and linked import jobs."""

    def __init__(
        self,
        *,
        database: Any,
        state: RcloneRunState,
        state_lock: Any,
        log: Callable[[str], None],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.state = state
        self.state_lock = state_lock
        self.log = log
        self.now = now

    def finish(self, exit_code: int, error: str) -> None:
        with self.state_lock:
            run_id = self.state.mark_finished(exit_code, error, self.now())
            self.log(error or "rclone 搬运任务执行完成")
        if not self.database or not run_id:
            return
        # A scoped V1 cancellation is persisted before the process receives
        # SIGTERM.  Do not turn that intentional stop into a generic failed
        # copy (and do not run completion/Organizer finalization for it).
        current = self.database.rclone.get_rclone_run(int(run_id)) or {}
        cancelled = str(current.get("status") or "").strip().lower() in {
            "cancel_pending",
            "cancelled",
            "canceled",
        }
        final_status = "cancelled" if cancelled else (RCLONE_RUN_SUCCESS if exit_code == 0 else RCLONE_RUN_FAILED)
        self.database.rclone.update_rclone_run(
            run_id,
            status=final_status,
            exit_code=None if cancelled else exit_code,
            error_message=("rclone 运行已取消" if cancelled else error),
        )
        if cancelled:
            self.database.rclone.add_rclone_event(
                run_id,
                "info",
                "已收敛取消的 task-scoped rclone 运行",
                {"cancelled": True, "exit_code": exit_code},
            )
