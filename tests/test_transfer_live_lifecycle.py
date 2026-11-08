from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.services.rclone_process_runner import RcloneProcessRunner
from fnos_media_import.transfer.dispatch import TransferDispatchService
from fnos_media_import.transfer.execution_repository import ExecutionConflictError
from fnos_media_import.transfer.orchestrator import TransferStepResult
from tests.test_transfer_submission_diagnostics import _Executions


class _FakeGate:
    def __init__(self, active_job_id: int = 0) -> None:
        self.active_job_id = int(active_job_id)

    def try_acquire(self, job_id: int):
        normalized = int(job_id)
        if self.active_job_id in {0, normalized}:
            self.active_job_id = normalized
            return True, normalized
        return False, self.active_job_id

    def renew(self, job_id: int) -> bool:
        return bool(self.try_acquire(job_id)[0])

    def release(self, job_id: int) -> bool:
        if self.active_job_id == int(job_id):
            self.active_job_id = 0
            return True
        return False


class TransferLiveLifecycleTests(unittest.TestCase):
    def _service(self, phase="cleanup_pending", next_phase="completed", *, save_error=False, concurrent_cancel=False):
        plan = {"version": 1, "job_id": 14, "transfer": {"strategy": "native_save"}}
        repository = _Executions(plan, {"phase": phase, "executor_strategy": "native_save"})
        job = {"id": 14, "status": "transferring"}
        saved = repository.save

        def save(execution, snapshot, **fields):
            if save_error:
                raise ExecutionConflictError("state changed")
            result = saved(execution, snapshot, **fields)
            job["status"] = "cancelled" if concurrent_cancel else fields.get("job_status", job["status"])
            return result

        repository.save = save
        state = SimpleNamespace(phase=phase, receipt=None, submission_fence={})
        snapshots = {"phase": phase}

        def step():
            state.phase = next_phase
            snapshots.update(phase=next_phase, message="verified result")
            return TransferStepResult(next_phase, "verified result", finished=next_phase == "completed")

        orchestrator = SimpleNamespace(
            state=state, restore=lambda _value: None, step=step,
            snapshot=lambda: copy.deepcopy(snapshots), mark_retry_ready=lambda: None,
        )
        service = object.__new__(TransferDispatchService)
        service.database = SimpleNamespace(job_queries=SimpleNamespace(get=lambda _id: dict(job)),
                                           job_commands=SimpleNamespace(add_event=Mock()))
        service.executions = repository
        service.execution_lease_seconds = 60
        service._validation_deps = lambda: {}
        service._orchestrator = lambda _plan: orchestrator
        emitted = []
        service.upload_log_bridge = lambda job_id, message: emitted.append(
            (job_id, message, repository.execution.phase, job["status"])
        )
        self.enterContext(patch("fnos_media_import.transfer.dispatch.validate_plan"))
        self.enterContext(patch("fnos_media_import.transfer.dispatch.v1_plan_or_review", return_value=(plan, None)))
        self.enterContext(patch("fnos_media_import.transfer.dispatch._LOGGER.warning"))
        return service, emitted

    def test_gate_blocks_another_job_until_the_active_job_enters_organizing(self):
        service, _emitted = self._service(phase="target_verifying", next_phase="organizing")
        gate = _FakeGate(active_job_id=87)
        service.transfer_gate = gate

        blocked = service.handle_worker({"job_id": 14}, {})
        self.assertEqual(blocked["worker_outcome"], "deferred")
        self.assertIn("#87", blocked["message"])
        self.assertEqual(gate.active_job_id, 87)

        gate.active_job_id = 0
        completed = service.handle_worker({"job_id": 14}, {})
        self.assertEqual(completed["phase"], "organizing")
        self.assertEqual(gate.active_job_id, 0)

    def test_gate_is_released_when_a_transfer_job_reaches_terminal_phase(self):
        service, _emitted = self._service(phase="target_verifying", next_phase="failed")
        gate = _FakeGate(active_job_id=14)
        service.transfer_gate = gate

        result = service.handle_worker({"job_id": 14}, {})

        self.assertEqual(result["phase"], "failed")
        self.assertEqual(gate.active_job_id, 0)

    def test_completed_message_only_follows_persisted_execution_and_done_job(self):
        service, emitted = self._service()
        result = service.handle_worker({"job_id": 14}, {})
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], 14)
        self.assertIn("入库阶段：completed", emitted[0][1])
        self.assertIn("入库完成", emitted[0][1])
        self.assertEqual(emitted[0][2:], ("completed", "done"))

    def test_failed_persistence_never_emits_success(self):
        service, emitted = self._service(save_error=True)
        self.assertEqual(service.handle_worker({"job_id": 14}, {})["worker_outcome"], "deferred")
        self.assertEqual(emitted, [])

    def test_concurrent_business_cancellation_is_not_announced_as_completed(self):
        service, emitted = self._service(concurrent_cancel=True)
        service.handle_worker({"job_id": 14}, {})
        self.assertEqual(emitted, [])

    def test_post_upload_verification_is_not_whole_job_success(self):
        service, emitted = self._service(phase="native_waiting", next_phase="target_verifying")
        service.handle_worker({"job_id": 14}, {})
        self.assertIn("入库阶段：target_verifying", emitted[0][1])
        self.assertNotIn("入库完成", emitted[0][1])

    def test_retry_phase_unfreezes_live_state_without_recreating_job(self):
        service, emitted = self._service(phase="retry_wait", next_phase="transferring")
        service.handle_worker({"job_id": 14}, {})
        self.assertEqual(len(emitted), 2)
        self.assertIn("入库阶段：retry_wait", emitted[0][1])
        self.assertEqual(emitted[0][2], "retry_wait")
        self.assertIn("入库阶段：transferring", emitted[1][1])
        self.assertTrue(all(row[0] == 14 for row in emitted))

    def test_broken_live_bridge_cannot_undo_committed_success(self):
        service, _emitted = self._service()
        service.upload_log_bridge = Mock(side_effect=RuntimeError("log channel unavailable"))
        self.assertEqual(service.handle_worker({"job_id": 14}, {})["phase"], "completed")


class RealProcessReaderTests(unittest.TestCase):
    def test_two_lines_reach_callback_while_child_is_waiting_for_parent_ack(self):
        source = """
import pathlib, sys, time
root = pathlib.Path(sys.argv[1])
for number in (1, 2):
    print('progress-' + str(number), flush=True)
    deadline = time.monotonic() + 4
    while not (root / str(number)).exists():
        if time.monotonic() > deadline:
            raise SystemExit(9)
        time.sleep(0.01)
"""
        with tempfile.TemporaryDirectory() as directory:
            current = []
            lines = []

            def on_line(message):
                self.assertIsNone(current[0].poll(), "progress was only read after process exit")
                lines.append(message)
                Path(directory, str(len(lines))).touch()

            result = RcloneProcessRunner().run(
                [sys.executable, "-u", "-c", source, directory], cwd=Path(directory),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, on_line=on_line,
                on_started=lambda process: current.append(process),
            )
        self.assertEqual(result.exit_code, 0, result.error)
        self.assertEqual(lines, ["progress-1", "progress-2"])


if __name__ == "__main__":
    unittest.main()
