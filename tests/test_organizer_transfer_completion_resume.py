from __future__ import annotations

import json
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from fnos_media_import.transfer import TransferOrchestrator, TransferPlan
from fnos_media_import.transfer.dispatch import _JobAckService, _OrganizerAdapter, TransferDispatchService
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.verifier import TransferVerifier
from tests import test_organizer_review_recovery as fixtures


class OrganizerTransferCompletionResumeTests(unittest.TestCase):
    setUp = fixtures.OrganizerReviewRecoveryTests.setUp
    _task = fixtures.OrganizerReviewRecoveryTests._task
    _queue = fixtures.OrganizerReviewRecoveryTests._queue
    _mapping = staticmethod(fixtures.OrganizerReviewRecoveryTests._mapping)

    def _pending(self, *, origin="organizing"):
        job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.plan = self.service._execution_repository.plan(job_id)
        self.executions = TransferExecutionRepository(self.db)
        self.executions.create(job_id, self.plan, phase="organizing")
        self.service.set_execution_repository(self.executions)
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO organizer_files(task_id, path, name, size, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, mapping["source_path"], mapping["source_name"], 100_000_000, "2026-01-01T00:00:00Z"),
            )
            conn.execute("UPDATE organizer_mappings SET file_id=? WHERE id=?", (cur.lastrowid, mapping["id"]))
        organizer = _OrganizerAdapter(self.db, self.service)
        orchestrator = TransferOrchestrator(self.plan, {}, organizer_service=organizer)
        orchestrator.state.phase = "organizing"
        orchestrator.state.organizer_task_id = task_id
        self.assertEqual(orchestrator.step().phase, "manual_review")
        snapshot = orchestrator.snapshot()
        snapshot["review_from"] = origin
        self.executions.save(
            self.executions.get(job_id), snapshot, phase="manual_review", organizer_task_id=task_id, job_status="review",
        )
        self.cleanup = Mock()
        self.cleanup.cleanup.return_value = {"ok": True}
        self.ack = _JobAckService(self.db, self.executions)
        self.ack.ack = Mock(wraps=self.ack.ack)
        self.verifier = TransferVerifier(lambda descriptor: {
            "exists": self.cloud.exists(descriptor["path"]),
            "files": [vars(item) for item in self.cloud.list_dir(descriptor["path"])],
        })
        return job_id, task_id, mapping

    def _apply(self, task_id):
        _result, status = self.admin.apply(task_id)
        self.assertEqual(status, 200)
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)

    def _step(self, job_id):
        execution = self.executions.get(job_id)
        runner = TransferOrchestrator(
            TransferPlan.from_dict(self.plan), {}, organizer_service=_OrganizerAdapter(self.db, self.service),
            verifier=self.verifier, ack_service=self.ack, cleanup=self.cleanup,
        ).restore(execution.snapshot)
        runner.mark_retry_ready()
        result = runner.step()
        snapshot = runner.snapshot()
        status, error = TransferDispatchService._job_projection(result.phase, result.message)
        self.executions.save(
            execution, snapshot, phase=result.phase, job_status=status, job_error_message=error,
            staging_verified=runner.state.staging_verified, final_verified=runner.state.final_verified,
            app_acked=runner.state.app_acked,
        )
        return result.phase

    def test_manual_approval_resumes_exact_task_and_verifies_before_ack_and_cleanup(self):
        job_id, task_id, _mapping = self._pending()
        self._apply(task_id)
        execution = self.executions.get(job_id)
        self.assertEqual(execution.phase, "retry_wait")
        self.assertEqual(execution.snapshot["retry_from"], "organizing")
        self.assertFalse(execution.final_verified)
        self.assertFalse(execution.app_acked)
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "confirming")
        with self.db.connect() as conn:
            queued = conn.execute("SELECT * FROM worker_tasks WHERE idempotency_key=?", (f"import-transfer:{job_id}",)).fetchone()
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(json.loads(queued["payload"]), {"job_id": job_id})
        for phase in ("organizing", "final_verifying", "ack_pending"):
            self.assertEqual(self._step(job_id), phase)
            self.ack.ack.assert_not_called()
            self.cleanup.cleanup.assert_not_called()
        self.assertEqual(self._step(job_id), "cleanup_pending")
        self.ack.ack.assert_called_once()
        self.cleanup.cleanup.assert_not_called()
        self.assertEqual(self._step(job_id), "completed")
        self.cleanup.cleanup.assert_called_once()
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "done")

    def test_missing_final_file_after_manual_approval_never_acks_or_cleans(self):
        job_id, task_id, mapping = self._pending()
        self._apply(task_id)
        self.cloud.files.pop(mapping["target_path"])
        self.assertEqual(self._step(job_id), "organizing")
        self.assertEqual(self._step(job_id), "final_verifying")
        self.assertEqual(self._step(job_id), "retry_wait")
        self.ack.ack.assert_not_called()
        self.cleanup.cleanup.assert_not_called()
        self.assertNotEqual(self.db.job_queries.get(job_id)["status"], "done")

    def test_unrelated_manual_review_is_not_automatically_recovered(self):
        job_id, task_id, _mapping = self._pending(origin="native_submitting")
        self._apply(task_id)
        self.assertEqual(self.executions.get(job_id).phase, "manual_review")
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "review")
        self.assertEqual(self.executions.list_due(), [])

    def test_resume_requires_matching_task_plan_and_current_completion(self):
        job_id, task_id, _mapping = self._pending()
        with patch.object(self.executions, "resume_after_organizer_completion", return_value=False):
            self._apply(task_id)
        task = self.db.organizer.get_organizer_task(task_id)
        self.assertFalse(self.executions.resume_after_organizer_completion(job_id, task_id + 1))
        for column, invalid, original in (
            ("raw_data", "{}", json.dumps(task["raw_data"])),
            ("evidence", "{}", json.dumps(task["evidence"])),
            ("status", "executing", "done"),
        ):
            with self.subTest(column=column):
                with self.db.connect() as conn:
                    conn.execute(f"UPDATE organizer_tasks SET {column}=? WHERE id=?", (invalid, task_id))
                self.assertFalse(self.executions.resume_after_organizer_completion(job_id, task_id))
                with self.db.connect() as conn:
                    conn.execute(f"UPDATE organizer_tasks SET {column}=? WHERE id=?", (original, task_id))
                self.assertEqual(self.executions.get(job_id).phase, "manual_review")
        self.assertTrue(self.executions.resume_after_organizer_completion(job_id, task_id))

    def test_queue_failure_rolls_back_recovery_and_startup_can_finish_it(self):
        job_id, task_id, _mapping = self._pending()
        with patch.object(self.db.worker_tasks, "enqueue_with_connection", side_effect=RuntimeError("queue unavailable")):
            self._apply(task_id)
        self.assertEqual(self.executions.get(job_id).phase, "manual_review")
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "review")
        result = self.service._recover_completed_linked_jobs_on_startup()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.executions.get(job_id).phase, "retry_wait")
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "confirming")

    def test_completion_racing_with_worker_review_save_is_not_stranded(self):
        job_id, task_id, _mapping = self._pending()
        snapshot = dict(self.executions.get(job_id).snapshot)
        snapshot["phase"] = "organizing"
        with self.db.connect() as conn:
            conn.execute("UPDATE transfer_executions SET phase='organizing', snapshot_json=? WHERE job_id=?", (json.dumps(snapshot), job_id))
        runner = TransferOrchestrator(self.plan, {}, organizer_service=_OrganizerAdapter(self.db, self.service))
        actual_step = runner.step

        def complete_before_park_save():
            result = actual_step()
            self.assertEqual(result.phase, "manual_review")
            self._apply(task_id)
            self.assertEqual(self.executions.get(job_id).phase, "organizing")
            return result

        runner.step = complete_before_park_save
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        dispatch.database = self.db
        dispatch.executions = self.executions
        dispatch.execution_lease_seconds = 120
        dispatch._orchestrator = lambda _plan: runner
        dispatch._validation_deps = lambda: None
        dispatch._claim_heartbeat = lambda *_args: nullcontext()
        dispatch._publish_transfer_phase = Mock()
        with patch("fnos_media_import.transfer.dispatch.validate_plan"):
            result = dispatch.handle_worker({"job_id": job_id}, {"owner_id": "test-worker"})
        self.assertEqual(result["phase"], "retry_wait", result)
        self.assertEqual(result["worker_outcome"], "deferred")
        self.assertEqual(self.executions.get(job_id).phase, "retry_wait")
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "confirming")


if __name__ == "__main__":
    unittest.main()
