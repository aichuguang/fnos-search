from __future__ import annotations

import copy
import unittest
from contextlib import nullcontext
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError
from fnos_media_import.transfer import ErrorCode, OperationReceipt, OperationStatus, TransferError, TransferPlan, VisibilityStatus
from fnos_media_import.transfer.dispatch import TransferDispatchService
from fnos_media_import.transfer.execution_repository import ExecutionConflictError, TransferExecutionRepository
from fnos_media_import.transfer.orchestrator import TransferOrchestrator
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor
from fnos_media_import.transfer.rclone_adapter import RcloneOperationAdapter
from fnos_media_import.transfer.rclone_executor import RcloneTransferExecutor
from tests.test_rclone_managed_descriptor_v1 import _MemoryDatabase, _plan, _relay_plan, _resolver
from tests.test_transfer_execution_repository_guards import _MemoryDatabase as _GuardDatabase
from tests.test_transfer_v1 import make_plan


class _Database(_MemoryDatabase):
    update_unless_status_with_connection = staticmethod(_GuardDatabase.update_unless_status_with_connection)

    def get_job(self, job_id):
        row = self.connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        return {**self.job, **dict(row)} if row else None

    def add_event(self, *_args):
        return 1


class TransferPreflightResumeTests(unittest.TestCase):
    def setUp(self):
        self.plan = _relay_plan()
        self.plan["paths"].pop("destination_staging", None)
        self.plan["transfer"]["idempotency_key"] = compute_operation_key(self.plan)
        attach_plan_integrity(self.plan)
        self.key = self.plan["transfer"]["idempotency_key"]
        self.database = _Database(self.plan)
        self.addCleanup(self.database.connection.close)
        self.repository = TransferExecutionRepository(self.database)
        self.repository.save(
            self.repository.get(7),
            {"phase": "source_materializing", "executor_strategy": "relay_upload_provider"},
            phase="source_materializing",
        )
        self.resolver = _resolver()
        self.resolver._managed_remote_identity_validator = Mock(return_value=True)
        self.landing_receipt = OperationReceipt(
            operation_id="source-save-7", status="completed", provider_kind="quark",
            metadata={"operation_key": f"landing:{self.key}"},
        )
        self.materialize = Mock(return_value=self.landing_receipt)
        self.visible = Mock(return_value=VisibilityStatus.visible)
        self.submit_snapshots = []
        self.backend = SimpleNamespace(
            find_run_by_operation_key=Mock(return_value=None),
            start_copy=Mock(side_effect=self._start_copy),
            poll_run=Mock(side_effect=lambda run_id, **kwargs: {
                "run_id": run_id,
                "status": kwargs["receipt"].status if kwargs["receipt"].status in {"failed", "cancelled"} else "running",
                "error_code": kwargs["receipt"].error_code,
            }),
        )
        self.dispatch = object.__new__(TransferDispatchService)
        self.dispatch.database = self.database
        self.dispatch.executions = self.repository
        self.dispatch.execution_lease_seconds = 60
        self.dispatch._validation_deps = lambda: {}
        self.dispatch._orchestrator = self._orchestrator
        self.dispatch._claim_heartbeat = lambda *_args: nullcontext()
        self.enterContext(patch("fnos_media_import.transfer.dispatch.validate_plan"))
        self.enterContext(patch("fnos_media_import.transfer.dispatch.v1_plan_or_review", return_value=(self.plan, None)))
        self.warning = self.enterContext(patch("fnos_media_import.transfer.dispatch._LOGGER.warning"))

    def _orchestrator(self, plan):
        adapter = RcloneOperationAdapter(self.backend, self.resolver)
        executor = ProviderRelayExecutor(
            adapter, SimpleNamespace(provider_kind="mobile"),
            source_materializer=self.materialize, visibility_poller=self.visible,
            relay_root="/data/relay",
        )
        return TransferOrchestrator(plan, {"relay_upload_provider": executor})

    def _start_copy(self, _source, _destination, operation_key, **_kwargs):
        self.submit_snapshots.append(copy.deepcopy(self.repository.get(7).snapshot))
        return {
            "success": True, "status": "running", "run_id": 51,
            "operation_id": "rclone:7:51",
            "metadata": {"run_id": 51, "operation_key": operation_key},
        }

    def _handle(self):
        current = self.repository.get(7)
        if current.resume_at:
            self.repository.clear_resume_backoff(7, current.phase)
        result = self.dispatch.handle_worker({"job_id": 7}, {"id": 1, "owner_id": "test-worker"})
        current = self.repository.get(7)
        metadata = (current.receipt or {}).get("metadata") or {}
        # Sequential relay first commits its stable task receipt; a separate
        # worker step submits/reconciles the actual file run under that fence.
        if result["phase"] == "transferring" and result.get("message") == "逐文件中继已就绪" and not metadata.get("relay_file_receipt"):
            self.repository.clear_resume_backoff(7, "transferring")
            result = self.dispatch.handle_worker({"job_id": 7}, {"id": 1, "owner_id": "test-worker"})
        return result

    def _land_source(self):
        self.assertEqual(self._handle()["phase"], "source_waiting_visible")
        self.assertEqual(self._handle()["phase"], "transferring")
        self.materialize.assert_called_once()
        current = self.repository.get(7)
        self.assertIsNone(current.receipt)
        self.assertFalse(current.snapshot["submission_fence"])
        return current

    def test_preflight_rejection_has_no_transfer_fence_and_retains_source_receipt(self):
        self._land_source()
        self.resolver._managed_remote_identity_validator.return_value = False
        result = self._handle()
        self.assertEqual(result["phase"], "manual_review")
        self.assertIn("尚未启动本次搬运", result["message"])
        current = self.repository.get(7)
        self.assertEqual(current.last_error_code, ErrorCode.OPENLIST_UNAVAILABLE)
        self.assertEqual(current.snapshot["review_from"], "transferring")
        self.assertFalse(current.snapshot["submission_fence"])
        proof = current.snapshot["submission_preflight"]
        self.assertEqual(proof["version"], 1)
        self.assertEqual(proof["job_id"], 7)
        self.assertEqual(proof["phase"], "transferring")
        self.assertEqual(proof["state"], "not_started")
        self.assertEqual(proof["operation_key"], f"relay-dl:{self.key}")
        evidence = current.snapshot["source_landing_evidence"]
        self.assertEqual(evidence["receipt"], asdict(self.landing_receipt))
        self.assertEqual(evidence["submission_fence"]["state"], "committed")
        self.assertEqual(evidence["operation_key"], f"landing:{self.key}")
        self.assertEqual(evidence["plan_operation_key"], self.key)
        self.assertEqual(evidence["visibility"], "visible")
        self.assertTrue(evidence["visible_at"])
        self.backend.find_run_by_operation_key.assert_not_called()
        self.backend.start_copy.assert_not_called()

    def test_ordinary_retry_after_fixed_remote_resumes_copy_without_rematerializing(self):
        self._land_source()
        self.resolver._managed_remote_identity_validator.return_value = False
        self._handle()
        source_evidence = copy.deepcopy(self.repository.get(7).snapshot["source_landing_evidence"])
        self.repository.recover_manual_review(7)
        self.assertEqual(self._handle()["phase"], "transferring")
        self.resolver._managed_remote_identity_validator.return_value = True
        self.assertEqual(self._handle()["phase"], "transferring")
        self.materialize.assert_called_once()
        self.backend.start_copy.assert_called_once()
        self.assertEqual(self.submit_snapshots[0]["submission_fence"]["state"], "committed")
        self.assertEqual(self.submit_snapshots[0]["submission_fence"]["operation_key"], f"relay-dl:{self.key}")
        self.assertFalse(self.submit_snapshots[0]["submission_preflight"])
        current = self.repository.get(7)
        self.assertEqual(current.snapshot["source_landing_evidence"], source_evidence)
        self.assertEqual(current.receipt["metadata"]["operation_key"], f"relay-dl:{self.key}")
        self.assertNotEqual(current.receipt["operation_id"], self.landing_receipt.operation_id)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 1)

    def test_source_evidence_and_preflight_survive_process_snapshot_restore(self):
        self._land_source()
        self.resolver._managed_remote_identity_validator.return_value = False
        self._handle()
        snapshot = self.repository.get(7).snapshot
        restored = self._orchestrator(self.plan).restore(copy.deepcopy(snapshot)).snapshot()
        self.assertEqual(restored["source_landing_evidence"], snapshot["source_landing_evidence"])
        self.assertEqual(restored["submission_preflight"], snapshot["submission_preflight"])
        self.assertIsNone(restored["receipt"])
        self.assertEqual(restored["review_from"], "transferring")

    def test_unknown_prepared_fence_is_checked_before_preflight_or_run_lookup(self):
        current = self._land_source()
        self.repository.save(current, {
            **current.snapshot,
            "submission_fence": {"state": "prepared", "phase": "transferring", "operation_key": f"relay-dl:{self.key}"},
            "message": "source OpenList preflight failed",
            "submission_preflight": {"version": 1, "state": "not_started"},
        }, phase="transferring")
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=AssertionError("Cannot confirm an unknown submission"))
        with patch.object(RcloneOperationAdapter, "prepare_download") as prepare:
            result = self._handle()
        self.assertEqual(result["phase"], "manual_review")
        self.assertEqual(self.repository.get(7).last_error_code, ErrorCode.RESULT_UNKNOWN)
        self.assertFalse(self.repository.get(7).snapshot["submission_preflight"])
        prepare.assert_not_called()
        self.backend.find_run_by_operation_key.assert_not_called()
        self.backend.start_copy.assert_not_called()
        self.dispatch.ensure_remote_confirmation.assert_not_called()

    def test_legacy_committed_landing_marker_does_not_prove_transfer_not_started(self):
        current = self._land_source()
        self.repository.save(current, {
            **current.snapshot,
            "source_landing_evidence": {},
            "submission_fence": {"state": "committed", "phase": "source_materializing", "operation_key": f"landing:{self.key}"},
        }, phase="transferring")
        with patch.object(RcloneOperationAdapter, "prepare_download") as prepare:
            self.assertEqual(self._handle()["phase"], "manual_review")
        prepare.assert_not_called()
        self.assertFalse(self.repository.get(7).snapshot["submission_preflight"])
        self.assertEqual(self.repository.get(7).snapshot["submission_fence"]["phase"], "source_materializing")

    def test_config_changes_after_preflight_do_not_reuse_not_started_proof(self):
        current = self._land_source()
        self.repository.save(current, {
            **current.snapshot, "submission_preflight": {"version": 1, "phase": "transferring", "state": "not_started"},
        }, phase="transferring")
        self.resolver._managed_remote_identity_validator.side_effect = [True, False]
        self.assertEqual(self._handle()["phase"], "retry_wait")
        current = self.repository.get(7)
        self.assertEqual(current.snapshot["submission_fence"]["state"], "committed")
        self.assertFalse(current.snapshot["submission_preflight"])
        self.assertEqual(self._handle()["phase"], "transferring")
        self.assertEqual(self._handle()["phase"], "manual_review")
        self.backend.start_copy.assert_not_called()
        self.materialize.assert_called_once()

    def test_lost_file_response_reconciles_persisted_run_without_resubmission(self):
        self._land_source()
        self.backend.start_copy.side_effect = RuntimeError("Response lost after submission")
        self.assertEqual(self._handle()["phase"], "retry_wait")
        self.assertFalse(self.repository.get(7).snapshot["submission_preflight"])
        self.assertEqual(self._handle()["phase"], "transferring")
        self.backend.find_run_by_operation_key.return_value = {
            "success": True, "status": "running", "run_id": 51, "operation_id": "rclone:7:51",
            "operation_key": f"relay-dl:{self.key}",
        }
        self.assertEqual(self._handle()["phase"], "transferring")
        self.backend.start_copy.assert_called_once()
        self.materialize.assert_called_once()
        self.assertEqual(self.repository.get(7).snapshot["submission_fence"]["state"], "committed")

    def test_cancellation_between_preflight_and_fence_prevents_submission(self):
        self._land_source()
        original = RcloneOperationAdapter.prepare_download
        def prepare_and_cancel(adapter, **kwargs):
            descriptor = original(adapter, **kwargs)
            with self.database.connect() as connection:
                connection.execute("UPDATE import_jobs SET status='cancelled' WHERE id=7")
            return descriptor
        with patch.object(RcloneOperationAdapter, "prepare_download", prepare_and_cancel):
            with self.assertRaises(ExecutionConflictError):
                self._handle()
        self.backend.start_copy.assert_not_called()
        self.assertFalse(self.repository.get(7).snapshot["submission_fence"])
        self.assertEqual(self.repository.get(7).snapshot["source_landing_evidence"]["receipt"], asdict(self.landing_receipt))

    def test_both_executor_preflights_are_read_only(self):
        relay = self._orchestrator(self.plan)
        relay.state.phase = "transferring"
        relay.state.executor_strategy = "relay_upload_provider"
        self.assertTrue(relay.prepare_submission()["relay_mode"])
        plan = _plan(local_target=True)
        executor = RcloneTransferExecutor(RcloneOperationAdapter(self.backend, self.resolver), self.resolver)
        executor.bind(TransferPlan.from_dict(plan))
        self.assertFalse(executor.prepare_submission()["relay_mode"])
        self.materialize.assert_not_called()
        self.backend.find_run_by_operation_key.assert_not_called()
        self.backend.start_copy.assert_not_called()

    def test_automatic_remote_confirmation_precedes_preflight_and_fence(self):
        self._land_source()
        self.resolver._managed_remote_identity_validator.return_value = False
        def confirm():
            self.assertFalse(self.repository.get(7).snapshot["submission_fence"])
            self.resolver._managed_remote_identity_validator.return_value = True
            return {"success": True, "confirmed": True}
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=confirm)
        self.assertEqual(self._handle()["phase"], "transferring")
        self.dispatch.ensure_remote_confirmation.assert_called_once()
        self.backend.start_copy.assert_called_once()
        self.materialize.assert_called_once()
        self.assertEqual(self.submit_snapshots[0]["submission_fence"]["state"], "committed")

    def test_confirmation_failure_is_preflight_failure_not_unknown_submission(self):
        self._land_source()
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=RuntimeError("Remote confirmation unavailable"))
        with patch.object(RcloneOperationAdapter, "prepare_download") as prepare:
            self.assertEqual(self._handle()["phase"], "manual_review")
        prepare.assert_not_called()
        current = self.repository.get(7)
        self.assertEqual(current.last_error_code, ErrorCode.OPENLIST_UNAVAILABLE)
        self.assertEqual(current.snapshot["submission_preflight"]["state"], "not_started")
        self.assertFalse(current.snapshot["submission_fence"])
        self.backend.start_copy.assert_not_called()
        self.materialize.assert_called_once()

    def test_receipt_poll_does_not_reconfirm_remote_or_start_another_copy(self):
        self._land_source()
        self.assertEqual(self._handle()["phase"], "transferring")
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=AssertionError("Polling must not mutate Remote"))
        self.backend.poll_run = Mock(return_value={"status": "running", "run_id": 51})
        self.assertEqual(self._handle()["phase"], "transferring")
        self.dispatch.ensure_remote_confirmation.assert_not_called()
        self.backend.start_copy.assert_called_once()

    def test_known_failed_receipt_retries_only_copy_and_archives_matching_committed_fence(self):
        self._land_source()
        failed = OperationReceipt(
            operation_id="rclone:7:51", status="failed", provider_kind="rclone",
            error_code=ErrorCode.NETWORK_TRANSIENT, message="Copy failed definitively",
            metadata={"run_id": 51, "operation_key": f"relay-dl:{self.key}"},
        )
        self.backend.start_copy.side_effect = lambda *args, **kwargs: (
            failed if self.backend.start_copy.call_count == 1 else self._start_copy(*args, **kwargs)
        )
        self.assertEqual(self._handle()["phase"], "retry_wait")
        current = self.repository.get(7)
        self.assertEqual(current.receipt["status"], "failed")
        self.assertEqual(current.snapshot["submission_fence"]["state"], "committed")
        evidence = copy.deepcopy(current.snapshot["source_landing_evidence"])
        failed_receipt = copy.deepcopy(current.receipt)
        failed_fence = copy.deepcopy(current.snapshot["submission_fence"])
        self.assertEqual(self._handle()["phase"], "transferring")
        current = self.repository.get(7)
        self.assertIsNone(current.receipt)
        self.assertFalse(current.snapshot["submission_fence"])
        previous = current.snapshot["last_submission_attempt"]
        self.assertEqual(previous["version"], 1)
        self.assertEqual(previous["phase"], "transferring")
        self.assertEqual(previous["receipt"], failed_receipt)
        self.assertEqual(previous["submission_fence"], failed_fence)
        self.assertEqual(self._handle()["phase"], "transferring")
        self.assertEqual(self.backend.start_copy.call_count, 2)
        self.materialize.assert_called_once()
        current = self.repository.get(7)
        self.assertEqual(current.snapshot["source_landing_evidence"], evidence)
        self.assertEqual(current.snapshot["last_submission_attempt"], previous)
        restored = self._orchestrator(self.plan).restore(current.snapshot).snapshot()
        self.assertEqual(restored["last_submission_attempt"], previous)

    def test_only_matching_committed_failed_or_cancelled_receipts_release_fence(self):
        key = f"relay-dl:{self.key}"
        cases = (
            ("failed", "committed", "transferring", key, True),
            ("cancelled", "committed", "transferring", key, True),
            ("failed", "prepared", "transferring", key, False),
            ("failed", "committed", "source_materializing", key, False),
            ("failed", "committed", "transferring", "other-operation", False),
            ("running", "committed", "transferring", key, False),
            ("unknown", "committed", "transferring", key, False),
            ("completed", "committed", "transferring", key, False),
        )
        for receipt_status, fence_state, fence_phase, fence_key, released in cases:
            with self.subTest(status=receipt_status, fence=fence_state, phase=fence_phase, key=fence_key):
                fence = {"state": fence_state, "phase": fence_phase, "operation_key": fence_key}
                receipt = OperationReceipt(
                    operation_id="rclone:7:51", status=receipt_status, provider_kind="rclone",
                    metadata={"operation_key": key, "run_id": 51},
                )
                orch = self._orchestrator(self.plan).restore({
                    "phase": "retry_wait", "retry_from": "transferring", "retry_ready": True,
                    "executor_strategy": "relay_upload_provider", "receipt": asdict(receipt),
                    "submission_fence": fence,
                })
                result = orch.step()
                if released:
                    self.assertEqual(result.phase, "transferring")
                    self.assertFalse(orch.state.submission_fence)
                    self.assertIsNone(orch.state.receipt)
                    self.assertEqual(orch.state.last_submission_attempt["receipt"]["status"], receipt_status)
                else:
                    self.assertEqual(orch.state.submission_fence, fence)
                    self.assertEqual(orch.state.receipt, receipt)
                    self.assertFalse(orch.state.last_submission_attempt)
        self.backend.start_copy.assert_not_called()

    def test_busy_confirmation_retries_with_backoff_without_requiring_manual_review(self):
        self._land_source()
        busy = RcloneWebdavConfigError("Remote confirmation lock is temporarily busy", 409)
        busy.retryable = True
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=[busy, {"confirmed": True}])
        result = self._handle()
        self.assertEqual(result["phase"], "retry_wait")
        self.assertGreater(result["delay_seconds"], 0)
        current = self.repository.get(7)
        self.assertIsNotNone(current.resume_at)
        self.assertFalse(current.snapshot["retry_ready"])
        self.assertEqual(current.snapshot["phase_attempts"]["transferring"], 1)
        self.assertFalse(current.snapshot["submission_fence"])
        self.assertEqual(current.snapshot["submission_preflight"]["state"], "not_started")
        self.assertTrue(current.snapshot["submission_preflight"]["retryable"])
        self.backend.start_copy.assert_not_called()
        self.assertEqual(self._handle()["phase"], "transferring")
        self.dispatch.ensure_remote_confirmation.assert_called_once()
        self.assertEqual(self._handle()["phase"], "transferring")
        self.assertEqual(self.dispatch.ensure_remote_confirmation.call_count, 2)
        self.backend.start_copy.assert_called_once()
        self.materialize.assert_called_once()
        self.assertFalse(self.repository.get(7).snapshot["submission_preflight"])

    def test_confirmation_5xx_retry_is_bounded_by_existing_phase_retry_policy(self):
        self._land_source()
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=RcloneWebdavConfigError("Remote probe unavailable", 503))
        limit = self._orchestrator(self.plan).retry_policy.max_attempts
        for attempt in range(1, limit + 1):
            result = self._handle()
            current = self.repository.get(7)
            self.assertEqual(current.snapshot["phase_attempts"]["transferring"], attempt)
            self.assertFalse(current.snapshot["submission_fence"])
            self.assertEqual(current.snapshot["submission_preflight"]["state"], "not_started")
            if attempt < limit:
                self.assertEqual(result["phase"], "retry_wait")
                self.assertEqual(self._handle()["phase"], "transferring")
            else:
                self.assertEqual(result["phase"], "manual_review")
                self.assertIn("重试次数耗尽", result["message"])
        self.assertEqual(self.dispatch.ensure_remote_confirmation.call_count, limit)
        self.backend.start_copy.assert_not_called()
        self.materialize.assert_called_once()

    def test_identity_scope_conflict_is_not_automatically_retried(self):
        self._land_source()
        self.dispatch.ensure_remote_confirmation = Mock(side_effect=RcloneWebdavConfigError("Remote identity changed", 409))
        self.assertEqual(self._handle()["phase"], "manual_review")
        current = self.repository.get(7)
        self.assertFalse(current.snapshot["submission_preflight"]["retryable"])
        self.assertFalse(current.snapshot["submission_fence"])
        self.assertEqual(current.snapshot["phase_attempts"].get("transferring", 0), 0)
        self.assertEqual(self._handle()["phase"], "manual_review")
        self.dispatch.ensure_remote_confirmation.assert_called_once()
        self.backend.start_copy.assert_not_called()

    def test_local_preflight_error_cannot_inherit_confirmation_retry_permission(self):
        self._land_source()
        self.dispatch.ensure_remote_confirmation = Mock(return_value={"confirmed": True})
        with patch.object(
            RcloneOperationAdapter, "prepare_download",
            side_effect=TransferError(ErrorCode.OPENLIST_UNAVAILABLE, "Frozen identity does not match", retryable=True),
        ):
            self.assertEqual(self._handle()["phase"], "manual_review")
        self.assertFalse(self.repository.get(7).snapshot["submission_preflight"]["retryable"])
        self.assertEqual(self.repository.get(7).snapshot["phase_attempts"].get("transferring", 0), 0)
        self.backend.start_copy.assert_not_called()

    def test_running_copy_then_failed_poll_retries_copy_without_repeating_source_save(self):
        self._land_source()
        self.assertEqual(self._handle()["phase"], "transferring")
        previous = copy.deepcopy(self.repository.get(7).receipt)
        self.assertEqual(previous["status"], "running")
        self.backend.poll_run = Mock(return_value={
            "status": "failed", "run_id": 51, "message": "Copy process failed definitively",
            "error_code": ErrorCode.NETWORK_TRANSIENT,
        })
        self.assertEqual(self._handle()["phase"], "retry_wait")
        failed = self.repository.get(7).receipt
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], ErrorCode.NETWORK_TRANSIENT)
        self.assertEqual(failed["operation_id"], previous["operation_id"])
        self.assertEqual(failed["metadata"], previous["metadata"])
        self.assertEqual(self._handle()["phase"], "transferring")
        self.assertEqual(self.repository.get(7).snapshot["last_submission_attempt"]["receipt"], failed)
        self.backend.poll_run.return_value = {"status": "running", "run_id": 51}
        self.assertEqual(self._handle()["phase"], "transferring")
        self.assertEqual(self.backend.start_copy.call_count, 2)
        self.assertEqual(self.backend.poll_run.call_count, 2)
        self.materialize.assert_called_once()

    def test_unknown_copy_poll_never_converts_running_receipt_to_failed_or_resubmits(self):
        self._land_source()
        self._handle()
        previous = copy.deepcopy(self.repository.get(7).receipt)
        self.backend.poll_run = Mock(return_value={"status": "unknown", "run_id": 51})
        self.assertEqual(self._handle()["phase"], "manual_review")
        self.assertEqual(self.repository.get(7).receipt, previous)
        self.assertFalse(self.repository.get(7).snapshot["last_submission_attempt"])
        self.backend.start_copy.assert_called_once()

    def test_native_terminal_poll_status_is_retained_and_only_failure_can_retry_submission(self):
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save", idempotency_key="native-test-key")
        key = plan["transfer"]["idempotency_key"]
        for status in ("failed", "cancelled", "unknown"):
            with self.subTest(status=status):
                native = Mock()
                native.poll.return_value = OperationStatus(status, "Provider result", error_code=ErrorCode.NETWORK_TRANSIENT)
                native.submit.return_value = OperationReceipt("native-new", "submitted", "quark", metadata={"operation_key": key})
                receipt = OperationReceipt("native-old", "running", "quark", metadata={"operation_key": key, "provider_ref": "preserve"})
                orch = TransferOrchestrator(plan, {"native_save": native}).restore({
                    "phase": "native_waiting", "executor_strategy": "native_save", "receipt": asdict(receipt),
                    "submission_fence": {"state": "committed", "phase": "native_submitting", "operation_key": key},
                })
                result = orch.step()
                self.assertEqual(orch.state.receipt.operation_id, receipt.operation_id)
                self.assertEqual(orch.state.receipt.metadata, receipt.metadata)
                if status == "failed":
                    self.assertEqual(result.phase, "retry_wait")
                    self.assertEqual(orch.state.receipt.status, "failed")
                    self.assertEqual(orch.state.retry_from, "native_submitting")
                    orch.mark_retry_ready()
                    self.assertEqual(orch.step().phase, "native_submitting")
                    self.assertFalse(orch.state.submission_fence)
                    self.assertEqual(orch.step().phase, "native_waiting")
                    native.submit.assert_called_once()
                else:
                    self.assertEqual(result.phase, "cancelled" if status == "cancelled" else "manual_review")
                    self.assertEqual(orch.state.receipt.status, "cancelled" if status == "cancelled" else "running")
                    native.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
