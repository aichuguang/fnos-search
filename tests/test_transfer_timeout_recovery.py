from __future__ import annotations

import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.time_utils import utc_now_iso_offset
from fnos_media_import.transfer import ErrorCode, OperationStatus, TransferOrchestrator
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.recovery import TransferRecoveryScanner
from tests.test_transfer_manual_review import _Database
from tests.test_transfer_v1 import make_plan


TIMEOUT_MESSAGE = "传输任务超过最大等待时间（10800 秒），已停止自动轮询，请人工核对"


class TransferTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.database = _Database()
        self.addCleanup(self.database.connection.close)
        self.repository = TransferExecutionRepository(self.database)
        self.plan = make_plan(
            strategy="rclone_copy", dest_kind="sixpan", landing="/landing/job-1",
            staging="/staging/job-1", final="/library",
        )
        self.repository.create(1, self.plan, phase="transferring")
        self.receipt = {
            "operation_id": "rclone:1:26", "status": "running", "provider_kind": "rclone",
            "metadata": {"operation_key": self.plan["transfer"]["idempotency_key"], "run_id": 26},
        }
        self.snapshot = {
            "phase": "manual_review", "review_from": "transferring", "executor_strategy": "rclone_copy",
            "receipt": self.receipt, "message": TIMEOUT_MESSAGE, "error_code": ErrorCode.RESULT_UNKNOWN,
            "attempt": 2, "phase_attempts": {"transferring": 1},
            "phase_wait_started_at": {"transferring": "2020-01-01T00:00:00Z"},
            "phase_wait_polls": {"transferring": 150},
            "submission_fence": {
                "state": "committed", "phase": "transferring",
                "operation_key": self.plan["transfer"]["idempotency_key"],
            },
            "manual_review_history": [{"action": "previous_operator_review"}],
        }
        self.original = self.repository.save(
            self.repository.get(1), self.snapshot, phase="manual_review",
            last_error_code=ErrorCode.RESULT_UNKNOWN, last_error_message=TIMEOUT_MESSAGE,
            job_status="review", job_error_message=TIMEOUT_MESSAGE,
        )
        self.scanner = TransferRecoveryScanner(self.repository, self.database.worker_tasks)

    def _records(self, *, snapshot=None, receipt=None, job_status="review", message=TIMEOUT_MESSAGE):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE transfer_executions SET snapshot_json=?, receipt_json=?, last_error_message=? WHERE job_id=1",
                (json.dumps(snapshot if snapshot is not None else self.snapshot), json.dumps(receipt), message),
            )
            connection.execute("UPDATE import_jobs SET status=? WHERE id=1", (job_status,))

    def _plan_record(self, plan):
        encoded = json.dumps(plan)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE import_execution_plans SET plan_json=?, plan_hash=? WHERE job_id=1",
                (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()),
            )

    def _assert_review(self):
        before = self.repository.get(1)
        self.assertEqual(self.repository.resume_legacy_transfer_timeouts(), 0)
        current = self.repository.get(1)
        self.assertEqual(current.phase, "manual_review")
        self.assertEqual(current.row_version, self.original.row_version)
        self.assertEqual(current.snapshot, before.snapshot)

    def test_scanner_resumes_same_job_and_preserves_receipt_fence_and_audit(self):
        result = self.scanner.scan_once()
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(result.errors, ())
        current = self.repository.get(1)
        self.assertEqual(current.phase, "retry_wait")
        self.assertEqual(current.snapshot["retry_from"], "transferring")
        self.assertTrue(current.snapshot["preserve_receipt_on_retry"])
        self.assertEqual(current.receipt, self.receipt)
        self.assertEqual(current.snapshot["receipt"], self.receipt)
        self.assertEqual(current.snapshot["submission_fence"], self.snapshot["submission_fence"])
        self.assertEqual(current.snapshot["manual_review_history"], self.snapshot["manual_review_history"])
        self.assertEqual(current.attempts, self.original.attempts)
        self.assertEqual(current.last_error_message, "")
        self.assertEqual(self.database.get_job(1)["status"], "retry_requested")
        audit = current.snapshot["transfer_timeout_recovery"]
        self.assertEqual(audit["message"], TIMEOUT_MESSAGE)
        self.assertEqual(audit["receipt"], self.receipt)
        self.assertEqual(audit["phase_wait_started_at"], self.snapshot["phase_wait_started_at"])
        self.assertEqual(self.repository.resume_legacy_transfer_timeouts(), 0)
        self.assertEqual(self.scanner.scan_once().already_queued, 1)
        with self.database.connect() as connection:
            task = dict(connection.execute("SELECT * FROM worker_tasks").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 1)
        self.assertEqual(task["idempotency_key"], "import-transfer:1")
        self.assertEqual(json.loads(task["payload"])["job_id"], 1)

    def test_resumed_execution_polls_original_operation_and_still_requires_verification(self):
        self.scanner.scan_once()
        executor = SimpleNamespace(
            plan=None, submit=Mock(side_effect=AssertionError("must not resubmit")),
            poll=Mock(return_value=OperationStatus("completed")),
        )
        ack = Mock()
        cleanup = Mock()
        execution = self.repository.get(1)
        runner = TransferOrchestrator(
            self.plan, {"rclone_copy": executor}, ack_service=ack, cleanup=cleanup,
        ).restore(execution.snapshot)
        runner.mark_retry_ready()
        self.assertEqual(runner.step().phase, "transferring")
        self.assertEqual(runner.step().phase, "target_verifying")
        executor.submit.assert_not_called()
        self.assertEqual(executor.poll.call_args.args[0].operation_id, self.receipt["operation_id"])
        ack.ack.assert_not_called()
        cleanup.cleanup.assert_not_called()
        current = self.repository.save(execution, runner.snapshot(), phase=runner.state.phase)
        self.assertEqual(current.snapshot["transfer_timeout_recovery"], execution.snapshot["transfer_timeout_recovery"])
        self.assertFalse(current.app_acked)
        self.assertFalse(current.final_verified)

    def test_queue_failure_does_not_lose_durable_recovery(self):
        queue = SimpleNamespace(enqueue=Mock(side_effect=RuntimeError("queue unavailable")))
        result = TransferRecoveryScanner(self.repository, queue).scan_once()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.repository.get(1).phase, "retry_wait")
        self.assertEqual(self.database.get_job(1)["status"], "retry_requested")
        self.assertEqual(self.scanner.scan_once().enqueued, 1)

    def test_recovery_failure_rolls_back_job_and_execution_together(self):
        with patch.object(self.database, "update_unless_status_with_connection", side_effect=RuntimeError("write failed")):
            result = self.scanner.scan_once()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.repository.get(1), self.original)
        self.assertEqual(self.database.get_job(1)["status"], "review")
        self.assertEqual(self.scanner.scan_once().enqueued, 1)

    def test_unrelated_reviews_or_prior_recovery_are_never_reopened(self):
        changes = [
            {"review_from": "native_waiting"}, {"review_from": "source_waiting_visible"},
            {"review_from": "cancel_pending"}, {"phase": "transferring"},
            {"error_code": "AUTH_EXPIRED"}, {"message": "传输结果未知"},
            {"executor_strategy": "native_save"}, {"transfer_timeout_recovery": {}},
            {"final_verified": True}, {"app_acked": True},
        ]
        for change in changes:
            with self.subTest(change=change):
                self._records(snapshot={**self.snapshot, **change}, receipt=self.receipt)
                self._assert_review()

    def test_terminal_business_statuses_are_not_resumed(self):
        for status in ("cancelled", "canceled", "rejected", "unsupported", "done", "failed", "completed"):
            with self.subTest(status=status):
                self._records(receipt=self.receipt, job_status=status)
                self._assert_review()
                self.assertEqual(self.database.get_job(1)["status"], status)

    def test_missing_unknown_terminal_or_conflicting_receipt_is_not_resumed(self):
        invalid = [None, {}, {**self.receipt, "operation_id": ""},
                   {**self.receipt, "metadata": {"operation_key": "another-job"}}]
        invalid += [{**self.receipt, "status": status} for status in ("unknown", "failed", "cancelled")]
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self._records(snapshot={**self.snapshot, "receipt": receipt}, receipt=receipt)
                self._assert_review()
        self._records(snapshot={**self.snapshot, "receipt": {**self.receipt, "operation_id": "other"}}, receipt=self.receipt)
        self._assert_review()

    def test_timeout_message_must_match_exact_old_reason(self):
        for message in ("传输轮询异常：timeout", TIMEOUT_MESSAGE + "；需要核验文件", TIMEOUT_MESSAGE.replace("传输任务", "取消确认")):
            with self.subTest(message=message):
                self._records(snapshot={**self.snapshot, "message": message}, receipt=self.receipt, message=message)
                self._assert_review()

    def test_fence_and_claim_must_be_safe_to_resume(self):
        changes = [
            {"submission_fence": {**self.snapshot["submission_fence"], "state": "prepared"}},
            {"submission_fence": {**self.snapshot["submission_fence"], "phase": "native_submitting"}},
            {"submission_fence": {**self.snapshot["submission_fence"], "operation_key": "other"}},
            {"claim": {"owner_id": "active", "lease_until": utc_now_iso_offset(minutes=5)}},
            {"claim": {"owner_id": "uncertain", "lease_until": "invalid"}},
        ]
        for change in changes:
            with self.subTest(change=change):
                self._records(snapshot={**self.snapshot, **change}, receipt=self.receipt)
                self._assert_review()
        self._records(snapshot={**self.snapshot, "claim": {"owner_id": "old", "lease_until": utc_now_iso_offset(minutes=-5)}}, receipt=self.receipt)
        self.assertEqual(self.repository.resume_legacy_transfer_timeouts(), 1)
        self.assertNotIn("claim", self.repository.get(1).snapshot)

    def test_plan_hash_identity_and_strategy_must_match(self):
        for change in ({"job_id": 2}, {"version": 2}, {"transfer": {"strategy": "native_save"}}, {"transfer": {"strategy": "rclone_copy", "idempotency_key": "other"}}):
            with self.subTest(change=change):
                self._plan_record({**self.plan, **change})
                self._assert_review()
        self._plan_record(self.plan)
        with self.database.connect() as connection:
            connection.execute("UPDATE import_execution_plans SET plan_hash='corrupt' WHERE job_id=1")
        self._assert_review()

    def test_durable_receipt_can_restore_missing_snapshot_receipt(self):
        snapshot = {key: value for key, value in self.snapshot.items() if key != "receipt"}
        self._records(snapshot=snapshot, receipt=self.receipt)
        self.assertEqual(self.repository.resume_legacy_transfer_timeouts(), 1)
        self.assertEqual(self.repository.get(1).snapshot["receipt"], self.receipt)

    def test_relay_operation_uses_original_download_key(self):
        plan = copy.deepcopy(self.plan)
        plan["transfer"]["strategy"] = "relay_upload_provider"
        self._plan_record(plan)
        receipt = {**self.receipt, "metadata": {"operation_key": "relay-dl:" + plan["transfer"]["idempotency_key"]}}
        snapshot = {**self.snapshot, "executor_strategy": "relay_upload_provider", "receipt": receipt,
                    "submission_fence": {**self.snapshot["submission_fence"], "operation_key": receipt["metadata"]["operation_key"]}}
        self._records(snapshot=snapshot, receipt=receipt)
        with self.database.connect() as connection:
            connection.execute("UPDATE transfer_executions SET strategy='relay_upload_provider' WHERE job_id=1")
        self.assertEqual(self.repository.resume_legacy_transfer_timeouts(), 1)
        self.assertEqual(self.repository.get(1).receipt, receipt)


if __name__ == "__main__":
    unittest.main()
