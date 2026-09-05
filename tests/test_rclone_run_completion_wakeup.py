from __future__ import annotations

import unittest

from fnos_media_import.services.rclone_run_completion_service import (
    RcloneRunCompletionService,
)
from fnos_media_import.services.rclone_run_state import RcloneRunState


class _RcloneRepository:
    def __init__(self, run: dict | None):
        self.run = run
        self.updates: list[tuple[int, dict]] = []

    def get_rclone_run(self, run_id: int) -> dict | None:
        return self.run

    def update_rclone_run(self, run_id: int, **fields) -> None:
        self.updates.append((run_id, fields))

    def add_rclone_event(self, *args, **kwargs) -> None:
        pass


class _Database:
    def __init__(self, run: dict | None):
        self.rclone = _RcloneRepository(run)


class RcloneRunCompletionWakeupTests(unittest.TestCase):
    def _service(
        self,
        run: dict | None,
        wakeup,
        state: RcloneRunState | None = None,
    ) -> tuple[RcloneRunCompletionService, list[str]]:
        logs: list[str] = []
        state = state or RcloneRunState()
        service = RcloneRunCompletionService(
            database=_Database(run),
            state=state,
            state_lock=_NullLock(),
            log=logs.append,
            now=lambda: "2026-09-04T12:00:00Z",
            transfer_wakeup=wakeup,
        )
        return service, logs

    def test_success_wakes_transfer_execution(self) -> None:
        state = RcloneRunState()
        state.mark_running(33, "2026-09-04T11:00:00Z")
        calls: list[int] = []

        def _wakeup(job_id: int) -> dict:
            calls.append(job_id)
            return {"woken": True, "enqueued": True, "job_id": job_id}

        service, logs = self._service(
            {"id": 33, "job_id": 7, "status": "running"}, _wakeup, state
        )

        service.finish(0, "")

        self.assertEqual(calls, [7])
        self.assertTrue(any("已立即唤醒传输执行" in line for line in logs))

    def test_failure_wakes_transfer_execution(self) -> None:
        state = RcloneRunState()
        state.mark_running(34, "2026-09-04T11:00:00Z")
        calls: list[int] = []
        service, _logs = self._service(
            {"id": 34, "job_id": 8, "status": "running"}, calls.append, state
        )

        service.finish(1, "脚本异常退出")

        self.assertEqual(calls, [8])

    def test_cancelled_run_does_not_wake(self) -> None:
        state = RcloneRunState()
        state.mark_running(35, "2026-09-04T11:00:00Z")
        calls: list[int] = []
        service, _logs = self._service(
            {"id": 35, "job_id": 9, "status": "cancel_pending"}, calls.append, state
        )

        service.finish(143, "已停止")

        self.assertEqual(calls, [])

    def test_wakeup_failure_is_logged_not_raised(self) -> None:
        state = RcloneRunState()
        state.mark_running(36, "2026-09-04T11:00:00Z")

        def _boom(job_id: int) -> None:
            raise RuntimeError("queue down")

        service, logs = self._service(
            {"id": 36, "job_id": 10, "status": "running"}, _boom, state
        )

        service.finish(0, "")

        self.assertTrue(any("唤醒传输执行失败" in line for line in logs))

    def test_run_without_job_id_does_not_wake(self) -> None:
        state = RcloneRunState()
        state.mark_running(37, "2026-09-04T11:00:00Z")
        calls: list[int] = []
        service, _logs = self._service({"id": 37, "status": "running"}, calls.append, state)

        service.finish(0, "")

        self.assertEqual(calls, [])

    def test_set_transfer_wakeup_binds_late_callback(self) -> None:
        state = RcloneRunState()
        state.mark_running(38, "2026-09-04T11:00:00Z")
        calls: list[int] = []
        service, _logs = self._service({"id": 38, "job_id": 11, "status": "running"}, None, state)

        service.set_transfer_wakeup(calls.append)
        service.finish(0, "")

        self.assertEqual(calls, [11])


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


if __name__ == "__main__":
    unittest.main()
