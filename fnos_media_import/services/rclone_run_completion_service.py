from __future__ import annotations

from typing import Any, Callable

from ..constants import EVENT_ERROR, RCLONE_RUN_FAILED, RCLONE_RUN_SUCCESS
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
        finalize_imports: Callable[[int, int], None],
    ) -> None:
        self.database = database
        self.state = state
        self.state_lock = state_lock
        self.log = log
        self.now = now
        self.finalize_imports = finalize_imports

    def finish(self, exit_code: int, error: str) -> None:
        with self.state_lock:
            run_id = self.state.mark_finished(exit_code, error, self.now())
            self.log(error or "rclone 搬运任务执行完成")
        if not self.database or not run_id:
            return
        self.database.update_rclone_run(
            run_id,
            status=RCLONE_RUN_SUCCESS if exit_code == 0 else RCLONE_RUN_FAILED,
            exit_code=exit_code,
            error_message=error,
        )
        try:
            self.finalize_imports(run_id, exit_code)
        except Exception as exc:  # noqa: BLE001
            message = f"rclone run 结束兜底处理异常：{exc}"
            self.log(message)
            self.database.add_rclone_event(run_id, EVENT_ERROR, message)
