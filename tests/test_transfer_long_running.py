"""Long-running transfer receipts remain authoritative across wait windows."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from fnos_media_import.transfer import (
    CancelResult,
    ErrorCode,
    OperationReceipt,
    OperationStatus,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW,
    PHASE_RETRY_WAIT,
    PHASE_TARGET_VERIFYING,
    PHASE_TRANSFERRING,
    TransferError,
    TransferOrchestrator,
)
from tests.test_transfer_v1 import make_plan


_STRATEGIES = ("rclone_copy", "relay_upload_provider")


class _PollingExecutor:
    def __init__(self, strategy, outcomes):
        self.strategy = strategy
        self.plan = None
        self.outcomes = list(outcomes)
        self.poll_receipts = []
        self.cancel_receipts = []
        self.submit_calls = 0
        self.verify = Mock(side_effect=AssertionError("verification must be a separate step"))

    def poll(self, receipt):
        self.poll_receipts.append(receipt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def submit(self):
        self.submit_calls += 1
        raise AssertionError("an existing or uncertain operation must not be resubmitted")

    def cancel(self, receipt):
        self.cancel_receipts.append(receipt)
        return CancelResult("cancel_pending", "cancellation requested")


class LongRunningTransferTests(unittest.TestCase):
    def _orchestrator(self, strategy, outcomes):
        relay = strategy == "relay_upload_provider"
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile" if relay else "sixpan",
            strategy=strategy,
            landing="/_fnos_ingress/job-1",
            staging=None if relay else "/library/_fnos_staging/job-1",
            final="/library/movies",
            local_temp="/data/relay/job-1" if relay else None,
        )
        executor = _PollingExecutor(strategy, outcomes)
        orchestrator = TransferOrchestrator(
            plan,
            {strategy: executor},
            verifier=Mock(spec=["verify"]),
            ack_service=Mock(spec=["ack"]),
            cleanup=Mock(spec=["cleanup"]),
            transfer_wait_timeout_seconds=60,
        )
        orchestrator.state.phase = PHASE_TRANSFERRING
        orchestrator.state.executor_strategy = strategy
        orchestrator.state.receipt = OperationReceipt(
            operation_id="relay-dl:1:41" if relay else "rclone-copy:1:41",
            status="running",
            provider_kind="mobile" if relay else "rclone",
            external_ref="provider-task-41",
            metadata={"run_id": "41", "operation_key": "import:job-1"},
        )
        # Simulate two elapsed days without sleeping or changing the wall clock.
        orchestrator.state.phase_wait_started_at[PHASE_TRANSFERRING] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat(timespec="seconds")
        orchestrator.state.phase_wait_polls[PHASE_TRANSFERRING] = 17
        return orchestrator, executor

    def _assert_no_submission_or_completion(self, orchestrator, executor):
        self.assertEqual(executor.submit_calls, 0)
        executor.verify.assert_not_called()
        orchestrator.verifier.verify.assert_not_called()
        orchestrator.ack_service.ack.assert_not_called()
        orchestrator.cleanup.cleanup.assert_not_called()
        self.assertFalse(orchestrator.state.staging_verified)
        self.assertFalse(orchestrator.state.target_verified)
        self.assertFalse(orchestrator.state.final_verified)
        self.assertFalse(orchestrator.state.app_acked)

    def test_running_receipt_keeps_polling_after_wait_timeout(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy, [OperationStatus("running"), OperationStatus("running")]
                )
                receipt = orchestrator.state.receipt
                started_at = orchestrator.state.phase_wait_started_at[PHASE_TRANSFERRING]

                for _ in range(2):
                    result = orchestrator.step()
                    self.assertEqual(result.phase, PHASE_TRANSFERRING)
                    self.assertTrue(result.needs_wait)
                    self.assertIs(orchestrator.state.receipt, receipt)

                self.assertEqual(executor.poll_receipts, [receipt, receipt])
                self.assertEqual(orchestrator.state.phase_wait_polls[PHASE_TRANSFERRING], 19)
                self.assertEqual(
                    orchestrator.state.phase_wait_started_at[PHASE_TRANSFERRING], started_at
                )
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_pending_receipt_keeps_waiting_after_wait_timeout(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(strategy, [{"status": "pending"}])
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_TRANSFERRING)
                self.assertTrue(result.needs_wait)
                self.assertEqual(executor.poll_receipts, [receipt])
                self.assertIs(orchestrator.state.receipt, receipt)
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_completed_poll_still_requires_target_verification_before_ack(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy, [OperationStatus("completed")]
                )
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_TARGET_VERIFYING)
                self.assertFalse(result.finished)
                self.assertEqual(executor.poll_receipts, [receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_unknown_poll_enters_review_without_resubmission(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy, [OperationStatus("unknown", "operation cannot be located")]
                )
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_MANUAL_REVIEW)
                self.assertEqual(orchestrator.state.review_from, PHASE_TRANSFERRING)
                self.assertEqual(orchestrator.state.error_code, ErrorCode.RESULT_UNKNOWN)
                self.assertEqual(orchestrator.state.message, "operation cannot be located")
                self.assertIs(orchestrator.state.receipt, receipt)
                self.assertEqual(orchestrator.step().phase, PHASE_MANUAL_REVIEW)
                self.assertEqual(executor.poll_receipts, [receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_permanent_poll_failure_uses_existing_failure_path(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy,
                    [OperationStatus("failed", "credentials expired", ErrorCode.AUTH_EXPIRED)],
                )
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_FAILED)
                self.assertEqual(orchestrator.state.failure_from, PHASE_TRANSFERRING)
                self.assertEqual(orchestrator.state.error_code, ErrorCode.AUTH_EXPIRED)
                self.assertEqual(orchestrator.state.receipt.status, "failed")
                self.assertEqual(orchestrator.state.receipt.operation_id, receipt.operation_id)
                self.assertEqual(executor.poll_receipts, [receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_confirmed_retryable_failure_retains_terminal_evidence(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy,
                    [OperationStatus("failed", "copy failed", ErrorCode.NETWORK_TRANSIENT)],
                )
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_RETRY_WAIT)
                self.assertEqual(orchestrator.state.retry_from, PHASE_TRANSFERRING)
                self.assertEqual(orchestrator.state.receipt.status, "failed")
                self.assertEqual(orchestrator.state.receipt.operation_id, receipt.operation_id)
                self.assertEqual(orchestrator.state.receipt.metadata, receipt.metadata)
                self.assertEqual(executor.poll_receipts, [receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_confirmed_cancelled_poll_still_cancels_without_ack_or_cleanup(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy, [OperationStatus("cancelled")]
                )
                receipt = orchestrator.state.receipt

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_CANCELLED)
                self.assertEqual(orchestrator.state.receipt.status, "cancelled")
                self.assertEqual(orchestrator.state.receipt.operation_id, receipt.operation_id)
                self.assertEqual(executor.poll_receipts, [receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_expired_wait_without_receipt_requires_review_instead_of_submission(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(strategy, [])
                orchestrator.state.receipt = None

                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_MANUAL_REVIEW)
                self.assertEqual(orchestrator.state.review_from, PHASE_TRANSFERRING)
                self.assertEqual(orchestrator.state.error_code, ErrorCode.RESULT_UNKNOWN)
                self.assertEqual(executor.poll_receipts, [])
                self.assertIsNone(orchestrator.state.receipt)
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_expired_wait_with_blank_operation_id_still_requires_review(self):
        for strategy in _STRATEGIES:
            for operation_id in ("", " \t "):
                with self.subTest(strategy=strategy, operation_id=operation_id):
                    orchestrator, executor = self._orchestrator(strategy, [])
                    receipt = replace(orchestrator.state.receipt, operation_id=operation_id)
                    orchestrator.state.receipt = receipt

                    result = orchestrator.step()

                    self.assertEqual(result.phase, PHASE_MANUAL_REVIEW)
                    self.assertEqual(orchestrator.state.review_from, PHASE_TRANSFERRING)
                    self.assertEqual(executor.poll_receipts, [])
                    self.assertIs(orchestrator.state.receipt, receipt)
                    self._assert_no_submission_or_completion(orchestrator, executor)

    def test_snapshot_restoration_polls_original_receipt_without_resubmitting(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                original, original_executor = self._orchestrator(strategy, [])
                receipt = original.state.receipt
                snapshot = json.loads(json.dumps(original.snapshot()))
                started_at = snapshot["phase_wait_started_at"][PHASE_TRANSFERRING]

                for expected_polls in (18, 19):
                    restored, executor = self._orchestrator(
                        strategy, [OperationStatus("running")]
                    )
                    restored.restore(snapshot)

                    result = restored.step()

                    self.assertEqual(result.phase, PHASE_TRANSFERRING)
                    self.assertTrue(result.needs_wait)
                    self.assertEqual(executor.poll_receipts, [receipt])
                    self.assertEqual(restored.state.receipt, receipt)
                    self.assertEqual(
                        restored.state.phase_wait_started_at[PHASE_TRANSFERRING], started_at
                    )
                    self.assertEqual(
                        restored.state.phase_wait_polls[PHASE_TRANSFERRING], expected_polls
                    )
                    self._assert_no_submission_or_completion(restored, executor)
                    snapshot = json.loads(json.dumps(restored.snapshot()))

                self._assert_no_submission_or_completion(original, original_executor)

    def test_transient_poll_error_retries_same_receipt_without_resubmitting(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy,
                    [
                        TransferError(ErrorCode.NETWORK_TRANSIENT, "temporary poll timeout"),
                        OperationStatus("running"),
                    ],
                )
                receipt = orchestrator.state.receipt

                self.assertEqual(orchestrator.step().phase, PHASE_RETRY_WAIT)
                self.assertEqual(orchestrator.state.retry_from, PHASE_TRANSFERRING)
                self.assertIs(orchestrator.state.receipt, receipt)
                self.assertEqual(orchestrator.step().phase, PHASE_RETRY_WAIT)
                orchestrator.mark_retry_ready()
                self.assertEqual(orchestrator.step().phase, PHASE_TRANSFERRING)
                self.assertIs(orchestrator.state.receipt, receipt)
                result = orchestrator.step()

                self.assertEqual(result.phase, PHASE_TRANSFERRING)
                self.assertTrue(result.needs_wait)
                self.assertEqual(executor.poll_receipts, [receipt, receipt])
                self.assertEqual(orchestrator.state.phase_attempts[PHASE_TRANSFERRING], 1)
                self._assert_no_submission_or_completion(orchestrator, executor)

    def test_user_can_cancel_long_running_transfer_and_confirm_original_receipt(self):
        for strategy in _STRATEGIES:
            with self.subTest(strategy=strategy):
                orchestrator, executor = self._orchestrator(
                    strategy, [OperationStatus("running"), OperationStatus("cancelled")]
                )
                receipt = orchestrator.state.receipt

                self.assertEqual(orchestrator.step().phase, PHASE_TRANSFERRING)
                cancellation = orchestrator.cancel()

                self.assertEqual(cancellation.status, "cancel_pending")
                self.assertEqual(orchestrator.state.phase, PHASE_CANCEL_PENDING)
                self.assertEqual(executor.cancel_receipts, [receipt])
                self.assertEqual(orchestrator.step().phase, PHASE_CANCELLED)
                self.assertEqual(executor.poll_receipts, [receipt, receipt])
                self._assert_no_submission_or_completion(orchestrator, executor)


if __name__ == "__main__":
    unittest.main()
