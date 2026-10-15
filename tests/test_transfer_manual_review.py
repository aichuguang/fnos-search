from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fnos_media_import.repositories.worker_task_repository import WorkerTaskRepository
from fnos_media_import.services.transfer_manual_review_service import TransferManualReviewService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from fnos_media_import.time_utils import utc_now_iso_offset
from fnos_media_import.transfer.execution_repository import ExecutionConflictError, TransferExecutionRepository
from tests.test_transfer_execution_repository_guards import _MemoryDatabase


class _Database(_MemoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.connection.executescript(
            """
            CREATE TABLE worker_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                config_revision INTEGER NOT NULL DEFAULT 1,
                owner_id TEXT,
                lease_expires_at TEXT,
                available_at TEXT NOT NULL,
                result TEXT,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );
            """
        )
        self.worker_tasks = WorkerTaskRepository(self.connect, dict)

    def get_job(self, job_id: int):
        row = self.connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


class TransferManualReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _Database()
        self.addCleanup(self.database.connection.close)
        self.repository = TransferExecutionRepository(self.database)
        self.plan = {"version": 1, "job_id": 1, "transfer": {"strategy": "native_save", "idempotency_key": "import:1"}}
        self.repository.create(1, self.plan)
        self.fence = {"state": "prepared", "phase": "native_submitting", "operation_key": "import:1", "prepared_at": "2026-10-11T00:00:00+00:00"}
        self.review = self.repository.save(
            self.repository.get(1),
            {
                "phase": "manual_review",
                "review_from": "native_submitting",
                "submission_fence": self.fence,
                "receipt": None,
                "attempt": 2,
                "phase_attempts": {"native_submitting": 2, "target_verifying": 1},
                "claim": {"owner_id": "old-worker", "lease_until": utc_now_iso_offset(minutes=-5)},
            },
            phase="manual_review",
            last_error_code="RESULT_UNKNOWN",
            last_error_message="Missing receipt after a prepared fence",
        )
        with self.database.connect() as connection:
            connection.execute("UPDATE import_jobs SET status='review', error_message='Missing receipt' WHERE id=1")
        self.enqueued: list[int] = []
        self.validated: list[tuple[dict, dict]] = []
        self.dispatcher = WorkerTaskDispatcher(
            repository=self.database.worker_tasks,
            enabled=lambda: True,
            config_revision=lambda: 1,
        )
        self.service = TransferManualReviewService(
            database=self.database,
            executions=self.repository,
            validate_plan=lambda plan, job: self.validated.append((plan, job)),
            enqueue_transfer=self._enqueue,
        )
        self.payload = {
            "action": "confirm_not_submitted",
            "confirmed": True,
            "expected_row_version": self.review.row_version,
            "note": "Checked the source, destination and provider tasks: no files or running tasks for this job.",
        }

    def _enqueue(self, job_id: int):
        self.enqueued.append(job_id)
        return self.dispatcher.import_transfer_execute(job_id, reactivate=True)

    def _confirm(self, payload=None, *, admin="administrator"):
        return self.service.confirm_not_submitted(1, self.payload if payload is None else payload, admin=admin)

    def _set_snapshot(self, **changes):
        snapshot = {**self.repository.get(1).snapshot, **changes}
        with self.database.connect() as connection:
            connection.execute("UPDATE transfer_executions SET snapshot_json=? WHERE job_id=1", (json.dumps(snapshot),))

    def _assert_not_recovered(self):
        current = self.repository.get(1)
        self.assertEqual(current.row_version, self.review.row_version)
        self.assertNotIn("manual_review_history", current.snapshot)
        self.assertEqual(self.enqueued, [])

    def test_confirmation_recovers_same_job_and_retains_complete_audit(self):
        body, status = self._confirm()
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertTrue(body["queued"])
        self.assertEqual(self.enqueued, [1])
        self.assertEqual(self.validated[0][0], self.plan)
        self.assertEqual(self.validated[0][1]["id"], 1)
        current = self.repository.get(1)
        self.assertEqual(current.job_id, 1)
        self.assertEqual(current.phase, "retry_wait")
        self.assertEqual(current.row_version, self.review.row_version + 1)
        self.assertEqual(current.attempts, 2)
        self.assertEqual(current.snapshot["attempt"], 2)
        self.assertEqual(current.snapshot["retry_from"], "native_submitting")
        self.assertEqual(current.snapshot["phase_attempts"], {"target_verifying": 1})
        self.assertFalse(current.snapshot["retry_ready"])
        self.assertIsNone(current.receipt)
        self.assertIsNone(current.snapshot["receipt"])
        self.assertNotIn("claim", current.snapshot)
        self.assertNotIn("submission_fence", current.snapshot)
        self.assertIsNone(current.resume_at)
        audit = current.snapshot["manual_review_history"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "confirm_not_submitted")
        self.assertEqual(audit[0]["admin"], "administrator")
        self.assertEqual(audit[0]["note"], self.payload["note"])
        self.assertEqual(audit[0]["submission_fence"], self.fence)
        self.assertEqual(audit[0]["previous_row_version"], self.review.row_version)
        self.assertEqual(audit[0]["previous_error"]["code"], "RESULT_UNKNOWN")
        self.assertTrue(audit[0]["confirmed_at"])
        self.assertEqual(self.database.get_job(1)["status"], "retry_requested")
        self.assertEqual(self.database.get_job(1)["error_message"], "")
        self.assertEqual(self.repository.plan(1), self.plan)
        for table in ("import_jobs", "import_execution_plans", "transfer_executions"):
            self.assertEqual(self.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_explicit_boolean_confirmation_is_required(self):
        for confirmed in (None, False, 1, "true", []):
            with self.subTest(confirmed=confirmed):
                body, status = self._confirm({**self.payload, "confirmed": confirmed})
                self.assertEqual(status, 400)
                self.assertFalse(body["success"])
                self._assert_not_recovered()

    def test_invalid_action_version_and_note_are_rejected_before_validation(self):
        changes = [
            {"action": "approve"}, {"expected_row_version": None},
            {"expected_row_version": True}, {"expected_row_version": "2"},
            {"expected_row_version": 0}, {"expected_row_version": -1},
            {"expected_row_version": 2.0}, {"note": ""}, {"note": "   "},
            {"note": "n" * 1001}, {"note": []},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self._confirm({**self.payload, **change})[1], 400)
                self._assert_not_recovered()
        self.assertEqual(self.validated, [])

    def test_admin_identity_is_required(self):
        self.assertEqual(self._confirm(admin=" ")[1], 403)
        self._assert_not_recovered()

    def test_stale_row_version_cannot_release_fence(self):
        body, status = self._confirm({**self.payload, "expected_row_version": self.review.row_version + 1})
        self.assertEqual(status, 409)
        self.assertFalse(body["success"])
        self._assert_not_recovered()

    def test_any_durable_receipt_even_empty_or_malformed_is_rejected(self):
        for receipt in ("{}", "", "not-json", '{"status":"unknown"}', '{"status":"completed"}', '{"status":"failed"}'):
            with self.subTest(receipt=receipt):
                with self.database.connect() as connection:
                    connection.execute("UPDATE transfer_executions SET receipt_json=? WHERE job_id=1", (receipt,))
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_snapshot_receipt_is_not_overridden_by_empty_durable_column(self):
        for receipt in ({}, {"status": "submitted", "operation_id": "known-operation"}, "unparsed", []):
            with self.subTest(receipt=receipt):
                self._set_snapshot(receipt=receipt)
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_non_review_jobs_cannot_be_resubmitted(self):
        for job_status in ("done", "success", "completed", "cancelled", "canceled", "rejected", "failed", "transferring", "created", "provider_submitting"):
            with self.subTest(job_status=job_status):
                with self.database.connect() as connection:
                    connection.execute("UPDATE import_jobs SET status=? WHERE id=1", (job_status,))
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_non_review_execution_cannot_be_resubmitted(self):
        for phase in ("completed", "cancelled", "planning", "native_submitting", "transferring", "retry_wait", "cancel_pending", "failed"):
            with self.subTest(phase=phase):
                with self.database.connect() as connection:
                    connection.execute("UPDATE transfer_executions SET phase=? WHERE job_id=1", (phase,))
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_cancellation_during_validation_wins_transaction_guard(self):
        def cancel_after_validation(_plan, _job):
            with self.database.connect() as connection:
                connection.execute("UPDATE import_jobs SET status='cancelled' WHERE id=1")
        self.service.validate_plan = cancel_after_validation
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.database.get_job(1)["status"], "cancelled")
        self._assert_not_recovered()

    def test_active_or_malformed_worker_claim_cannot_be_cleared(self):
        for claim in (
            {"owner_id": "active-worker", "lease_until": utc_now_iso_offset(minutes=5)},
            {"owner_id": "worker", "lease_until": "invalid"},
            {"owner_id": "worker"},
            {"lease_until": utc_now_iso_offset(minutes=5)},
            "malformed-claim",
        ):
            with self.subTest(claim=claim):
                self._set_snapshot(claim=claim)
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_job_projection_failure_rolls_back_audit_and_fence_change(self):
        with patch("fnos_media_import.transfer.execution_repository._sync_job_with_connection", return_value=False):
            self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.repository.get(1), self.review)
        self.assertEqual(self.database.get_job(1)["status"], "review")
        self._assert_not_recovered()

    def test_invalid_review_origin_cannot_be_reset_to_planning(self):
        for phase in (None, "", "planning", "native_waiting", "target_verifying", "manual_review", "completed"):
            with self.subTest(phase=phase):
                self._set_snapshot(review_from=phase)
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_missing_or_mismatched_fence_is_rejected(self):
        for fence in (None, {}, {"state": "not_started"}, {"state": "prepared", "phase": "transferring"}):
            with self.subTest(fence=fence):
                self._set_snapshot(submission_fence=fence)
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()

    def test_verified_target_evidence_cannot_be_declared_absent(self):
        for flag in ("target_verified", "staging_verified", "final_verified", "app_acked"):
            with self.subTest(flag=flag):
                self._set_snapshot(**{flag: True})
                self.assertEqual(self._confirm()[1], 409)
                self._assert_not_recovered()
                self._set_snapshot(**{flag: False})
        with self.database.connect() as connection:
            connection.execute("UPDATE transfer_executions SET app_acked=1 WHERE job_id=1")
        self.assertEqual(self._confirm()[1], 409)
        self._assert_not_recovered()

    def test_committed_fence_without_any_receipt_also_requires_explicit_confirmation(self):
        self._set_snapshot(submission_fence={**self.fence, "state": "committed"})
        with self.assertRaises(ExecutionConflictError):
            self.repository.recover_manual_review(1)
        self.assertEqual(self._confirm()[1], 200)
        self.assertEqual(self.repository.get(1).snapshot["manual_review_history"][0]["submission_fence"]["state"], "committed")

    def test_source_materialization_and_transfer_resume_their_original_phase(self):
        for job_id, phase in enumerate(("source_materializing", "transferring"), start=2):
            with self.subTest(phase=phase):
                plan = {"version": 1, "job_id": job_id, "transfer": {"strategy": "rclone_copy", "idempotency_key": f"import:{job_id}"}}
                with self.database.connect() as connection:
                    connection.execute("INSERT INTO import_jobs(id, status) VALUES (?, 'review')", (job_id,))
                self.repository.create(job_id, plan)
                execution = self.repository.save(
                    self.repository.get(job_id),
                    {"phase": "manual_review", "review_from": phase, "submission_fence": {"state": "prepared", "phase": phase}},
                    phase="manual_review",
                )
                body, status = self.service.confirm_not_submitted(
                    job_id, {**self.payload, "expected_row_version": execution.row_version}, admin="administrator",
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["job_id"], job_id)
                self.assertEqual(self.repository.get(job_id).snapshot["retry_from"], phase)
        self.assertEqual(self.enqueued, [2, 3])

    def test_double_confirmation_enqueues_only_once(self):
        self.assertEqual(self._confirm()[1], 200)
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.enqueued, [1])
        self.assertEqual(len(self.repository.get(1).snapshot["manual_review_history"]), 1)

    def test_audit_survives_worker_snapshot_without_history_and_cannot_be_overwritten(self):
        self.assertEqual(self._confirm()[1], 200)
        current = self.repository.get(1)
        audit = current.snapshot["manual_review_history"]
        current = self.repository.save(current, {"phase": "native_submitting", "receipt": None}, phase="native_submitting")
        self.assertEqual(current.snapshot["manual_review_history"], audit)
        current = self.repository.save(current, {"phase": "native_submitting", "manual_review_history": []}, phase="native_submitting")
        self.assertEqual(current.snapshot["manual_review_history"], audit)

    def test_existing_audit_history_is_appended_not_replaced(self):
        old = {"action": "previous-review", "note": "retain"}
        self._set_snapshot(manual_review_history=[old])
        self.assertEqual(self._confirm()[1], 200)
        history = self.repository.get(1).snapshot["manual_review_history"]
        self.assertEqual(history[0], old)
        self.assertEqual(len(history), 2)

    def test_invalid_audit_history_is_not_destroyed(self):
        self._set_snapshot(manual_review_history={"legacy": "preserve"})
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.repository.get(1).snapshot["manual_review_history"], {"legacy": "preserve"})
        self.assertEqual(self.enqueued, [])

    def test_failed_plan_validation_does_not_release_fence(self):
        def fail(_plan, _job):
            raise ValueError("Frozen revision is no longer available")
        self.service.validate_plan = fail
        self.assertEqual(self._confirm()[1], 409)
        self._assert_not_recovered()

    def test_missing_plan_and_job_fail_closed(self):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM import_execution_plans WHERE job_id=1")
        self.assertEqual(self._confirm()[1], 409)
        self._assert_not_recovered()
        self.assertEqual(self.service.confirm_not_submitted(999, self.payload, admin="administrator")[1], 404)

    def test_enqueue_failure_reports_committed_recovery_not_success(self):
        def fail(_job_id):
            raise RuntimeError("Queue unavailable")
        self.service.enqueue_transfer = fail
        body, status = self._confirm()
        self.assertEqual(status, 503)
        self.assertFalse(body["success"])
        self.assertTrue(body["recovered"])
        self.assertFalse(body["queued"])
        self.assertEqual(self.repository.get(1).phase, "retry_wait")
        self.assertEqual(len(self.repository.get(1).snapshot["manual_review_history"]), 1)
        self.assertEqual(self._confirm()[1], 409)

    def test_queue_acknowledgement_requires_explicit_true_fields(self):
        self.assertEqual(self._confirm()[1], 200)
        for queued in (
            {}, {"success": True}, {"queued": True},
            {"success": False, "queued": True}, {"success": True, "queued": False},
            {"success": 1, "queued": True}, {"success": True, "queued": 1},
            {"success": "true", "queued": True}, {"success": True, "queued": "true"},
            None, False, 1, "queued", [True],
        ):
            with self.subTest(queued=queued):
                self.assertFalse(self.service._queue_accepted(1, queued))
        self.assertTrue(self.service._queue_accepted(1, {"success": True, "queued": True}))
        self.assertTrue(self.service._queue_accepted(1, True))

    def test_incomplete_queue_acknowledgement_keeps_committed_audit(self):
        self.service.enqueue_transfer = lambda _job_id: {"queued": True}
        body, status = self._confirm()
        self._assert_queue_failure_after_recovery(body, status)
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0], 0)

    def test_real_dispatcher_does_not_confirm_existing_cancelled_worker_task(self):
        task = self.dispatcher.import_transfer_execute(1)
        with self.database.connect() as connection:
            connection.execute("UPDATE worker_tasks SET status='cancelled' WHERE id=?", (task["worker_task_id"],))
        body, status = self._confirm()
        self._assert_queue_failure_after_recovery(body, status)
        self.assertEqual(self.database.worker_tasks.get(task["worker_task_id"])["status"], "cancelled")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0], 1)
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.enqueued, [1])

    def test_queue_acknowledgement_checks_durable_state_and_job_binding(self):
        self.assertEqual(self._confirm()[1], 200)
        queued = self.dispatcher.import_transfer_execute(1, reactivate=True)
        task = self.database.worker_tasks.get(queued["worker_task_id"])
        for changes in (
            {"status": "cancelled"}, {"status": "completed"}, {"status": "failed"},
            {"status": "unknown"}, {"id": 999}, {"task_type": "organizer_apply"},
            {"idempotency_key": "import-transfer:999"}, {"payload": {"job_id": 999}},
            {"payload": {"job_id": True}}, {"payload": {"job_id": "1"}},
            {"payload": []}, {"payload": {}},
        ):
            with self.subTest(changes=changes), patch.object(
                self.database.worker_tasks, "get", return_value={**task, **changes},
            ):
                self.assertFalse(self.service._queue_accepted(1, queued))
        for value in (None, {}, []):
            with self.subTest(missing_task=value), patch.object(self.database.worker_tasks, "get", return_value=value):
                self.assertFalse(self.service._queue_accepted(1, queued))
        for value in (None, False, True, 0, -1, "1", 1.0):
            with self.subTest(task_id=value):
                self.assertFalse(self.service._queue_accepted(1, {**queued, "worker_task_id": value}))
        self.assertTrue(self.service._queue_accepted(1, queued))
        self.database.worker_tasks.claim("active-worker")
        self.assertTrue(self.service._queue_accepted(1, queued))

    def test_cancelled_job_after_enqueue_is_not_reported_as_retry_queued(self):
        def enqueue_then_cancel(job_id):
            queued = self._enqueue(job_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE import_jobs SET status='cancelled' WHERE id=?", (job_id,))
            return queued
        self.service.enqueue_transfer = enqueue_then_cancel
        body, status = self._confirm()
        self._assert_queue_failure_after_recovery(body, status)
        self.assertEqual(self.database.get_job(1)["status"], "cancelled")
        self.assertEqual(self._confirm()[1], 409)
        self.assertEqual(self.enqueued, [1])

    def test_completed_worker_after_enqueue_is_not_reported_as_retry_queued(self):
        def enqueue_then_finish(job_id):
            queued = self._enqueue(job_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE worker_tasks SET status='completed' WHERE id=?", (queued["worker_task_id"],))
            return queued
        self.service.enqueue_transfer = enqueue_then_finish
        body, status = self._confirm()
        self._assert_queue_failure_after_recovery(body, status)
        self.assertEqual(self._confirm()[1], 409)

    def test_queue_lookup_failure_reports_committed_recovery(self):
        with patch.object(self.database.worker_tasks, "get", side_effect=RuntimeError("Queue read unavailable")):
            body, status = self._confirm()
        self._assert_queue_failure_after_recovery(body, status)
        self.assertEqual(self.enqueued, [1])

    def _assert_queue_failure_after_recovery(self, body, status):
        self.assertEqual(status, 503)
        self.assertFalse(body["success"])
        self.assertFalse(body["queued"])
        self.assertTrue(body["recovered"])
        current = self.repository.get(1)
        self.assertEqual(current.phase, "retry_wait")
        self.assertEqual(current.row_version, self.review.row_version + 1)
        self.assertEqual(len(current.snapshot["manual_review_history"]), 1)
        self.assertNotIn("submission_fence", current.snapshot)

    def test_normal_retry_still_refuses_unconfirmed_fence(self):
        with self.assertRaises(ExecutionConflictError):
            self.repository.recover_manual_review(1)
        self._assert_not_recovered()


if __name__ == "__main__":
    unittest.main()
