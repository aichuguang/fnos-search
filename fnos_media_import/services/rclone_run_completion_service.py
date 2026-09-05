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
        transfer_wakeup: Callable[[int], Any] | None = None,
    ) -> None:
        self.database = database
        self.state = state
        self.state_lock = state_lock
        self.log = log
        self.now = now
        self.transfer_wakeup = transfer_wakeup if callable(transfer_wakeup) else None

    def set_transfer_wakeup(self, callback: Callable[[int], Any]) -> None:
        """Bind the late-wired transfer wake-up (see app composition root)."""
        self.transfer_wakeup = callback if callable(callback) else None

    def finish(self, exit_code: int, error: str) -> None:
        with self.state_lock:
            run_id = self.state.mark_finished(exit_code, error, self.now())
            self.log(error or "rclone 搬运任务执行结束（中继任务的上传与整理进度随后显示）")
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
            return
        if final_status in {RCLONE_RUN_SUCCESS, RCLONE_RUN_FAILED}:
            self._wake_transfer_execution(run_id, current)

    def _wake_transfer_execution(self, run_id: Any, run: dict[str, Any]) -> None:
        wakeup = self.transfer_wakeup
        if wakeup is None:
            return
        job_id = 0
        try:
            job_id = int(run.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if job_id <= 0:
            return
        try:
            result = wakeup(job_id)
        except Exception as exc:  # noqa: BLE001 - 唤醒失败不能影响 run 终态落库
            self.log(f"rclone run 结束后唤醒传输执行失败：run {run_id}，job {job_id}：{exc}")
            return
        if isinstance(result, dict) and result.get("woken"):
            self.log(
                "rclone run 结束已立即唤醒传输执行："
                f"run {run_id}，job {job_id}"
            )
