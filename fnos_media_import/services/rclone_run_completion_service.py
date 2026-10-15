from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..constants import RCLONE_EXIT_LOCK_BUSY, RCLONE_RUN_FAILED, RCLONE_RUN_SUCCESS
from .rclone_run_state import RcloneRunState


@dataclass(frozen=True)
class RcloneRunResult:
    run_id: int
    exit_code: int
    error: str
    operation_key: str | None = None
    started_at: str | None = None


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

    def finish(
        self,
        exit_code: int,
        error: str,
        *,
        expected_operation_key: str | None = None,
        expected_started_at: str | None = None,
    ) -> None:
        # 互斥锁被占用时脚本一个字节都没搬：不能落成 success 去唤醒上传（否则
        # 中继目录为空直接失败），也不该落成 failed 让编排器白白消耗重试次数。
        deferred = exit_code == RCLONE_EXIT_LOCK_BUSY
        with self.state_lock:
            if deferred:
                run_id = self.state.mark_deferred(error, self.now())
            else:
                run_id = self.state.mark_finished(exit_code, error, self.now())
            self.log(error or "rclone 搬运任务执行结束（中继任务的上传与整理进度随后显示）")
        self.persist_result(
            run_id, exit_code, error,
            expected_operation_key=expected_operation_key,
            expected_started_at=expected_started_at,
        )

    def persist_result(
        self,
        run_id: Any,
        exit_code: int,
        error: str,
        *,
        expected_operation_key: str | None = None,
        expected_started_at: str | None = None,
    ) -> None:
        """Retry persistence without modifying another run's in-memory state."""
        if not self.database or not run_id:
            return
        deferred = exit_code == RCLONE_EXIT_LOCK_BUSY
        # A scoped V1 cancellation is persisted before the process receives
        # SIGTERM.  Do not turn that intentional stop into a generic failed
        # copy (and do not run completion/Organizer finalization for it).
        current = self.database.rclone.get_rclone_run(int(run_id)) or {}
        identity = {
            "expected_operation_key": expected_operation_key,
            "expected_started_at": expected_started_at,
        }
        if not self._matches_identity(current, **identity):
            # History may have been deleted, or a newer attempt may now own
            # this row. A late callback must not recreate events or wake jobs.
            return
        current_status = str(current.get("status") or "").strip().lower()
        if current_status in {RCLONE_RUN_SUCCESS, RCLONE_RUN_FAILED, "cancelled", "canceled"}:
            # A commit can succeed even if its caller loses the result. Wake
            # only a matching outcome; never overwrite an established final state.
            final_status = RCLONE_RUN_SUCCESS if exit_code == 0 else RCLONE_RUN_FAILED
            if not deferred and current_status == final_status:
                self._wake_transfer_execution(run_id, current)
            return
        cancelled = current_status == "cancel_pending"
        if deferred and not cancelled:
            if current_status in {"running", "starting", "recovering", "queued"}:
                self._defer_run(run_id, current, error, **identity)
            return
        if current_status not in {"running", "starting", "recovering", "cancel_pending"}:
            return
        final_status = "cancelled" if cancelled else (RCLONE_RUN_SUCCESS if exit_code == 0 else RCLONE_RUN_FAILED)
        updated = self.database.rclone.update_rclone_run(
            run_id,
            status=final_status,
            exit_code=None if cancelled else exit_code,
            error_message=("rclone 运行已取消" if cancelled else error),
            expected_status=current_status,
            **identity,
        )
        if updated is False:
            self._converge_cancelled_run(run_id, **identity)
            return
        if cancelled:
            try:
                self.database.rclone.add_rclone_event(
                    run_id,
                    "info",
                    "已收敛取消的 task-scoped rclone 运行",
                    {"cancelled": True, "exit_code": exit_code},
                )
            except Exception as exc:  # noqa: BLE001 - terminal state already committed
                self.log(f"rclone run #{run_id} 取消事件暂未落库：{exc}")
            return
        if final_status in {RCLONE_RUN_SUCCESS, RCLONE_RUN_FAILED}:
            self._wake_transfer_execution(run_id, current)

    @staticmethod
    def _matches_identity(
        run: dict[str, Any],
        *,
        expected_operation_key: str | None = None,
        expected_started_at: str | None = None,
    ) -> bool:
        return bool(run) and (
            expected_operation_key is None
            or str(run.get("operation_key") or "") == expected_operation_key
        ) and (
            expected_started_at is None
            or str(run.get("started_at") or "") == expected_started_at
        )

    def _converge_cancelled_run(self, run_id: Any, **identity: Any) -> None:
        current = self.database.rclone.get_rclone_run(int(run_id)) or {}
        if not self._matches_identity(current, **identity) or (
            str(current.get("status") or "").strip().lower() != "cancel_pending"
        ):
            return
        self.database.rclone.update_rclone_run(
            run_id,
            status="cancelled",
            error_message="rclone 运行已取消",
            expected_status="cancel_pending",
            **identity,
        )

    def _defer_run(self, run_id: Any, run: dict[str, Any], error: str, **identity: Any) -> None:
        """把未启动的持久 run 回滚到 queued，交给服务层延后重排；不唤醒传输。"""
        scoped = bool(str(run.get("operation_key") or "").strip())
        if scoped:
            updated = self.database.rclone.mark_scoped_rclone_run_status(
                int(run_id),
                "queued",
                error_message=error,
                expected_status=str(run.get("status") or "").strip().lower(),
                **identity,
            )
            if updated is False:
                self._converge_cancelled_run(run_id, **identity)
                return
        else:
            # 非任务级 run 没有可重排的持久描述符，只能如实记为未执行。
            self.database.rclone.update_rclone_run(
                run_id,
                status=RCLONE_RUN_FAILED,
                exit_code=RCLONE_EXIT_LOCK_BUSY,
                error_message=error,
                expected_status=str(run.get("status") or "").strip().lower(),
                **identity,
            )
        try:
            self.database.rclone.add_rclone_event(
                run_id,
                "warn",
                "rclone 本次运行未启动" + ("，已回滚到队列等待重排" if scoped else ""),
                {"deferred": True, "exit_code": RCLONE_EXIT_LOCK_BUSY, "requeued": scoped},
            )
        except Exception as exc:  # noqa: BLE001 - an audit failure must not discard a queued run
            self.log(f"rclone run #{run_id} 未启动事件暂未落库：{exc}")

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
