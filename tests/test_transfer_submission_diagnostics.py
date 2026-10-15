from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.transfer import ErrorCode, OperationReceipt, TransferError
from fnos_media_import.transfer.dispatch import TransferDispatchService
from fnos_media_import.transfer.execution_repository import ExecutionConflictError, TransferExecution
from fnos_media_import.transfer.orchestrator import TransferOrchestrator


_LOGGER_NAME = "fnos_media_import.transfer.dispatch"


class _Executions:
    def __init__(self, plan, snapshot):
        self.frozen_plan = plan
        self.receipt_error = None
        self.execution = TransferExecution(
            job_id=14, strategy="native_save", phase=snapshot["phase"],
            snapshot=copy.deepcopy(snapshot), receipt=snapshot.get("receipt"), attempts=0,
            resume_at=None, organizer_task_id=None, staging_verified=False,
            final_verified=False, app_acked=False, cleanup={}, last_error_code="",
            last_error_message="", row_version=1,
        )
        self.saves = []

    def get(self, _job_id):
        return self.execution

    def plan(self, _job_id):
        return self.frozen_plan

    def save(self, execution, snapshot, **fields):
        if snapshot.get("receipt") and self.receipt_error is not None:
            raise self.receipt_error
        snapshot = copy.deepcopy(snapshot)
        self.saves.append(snapshot)
        self.execution = replace(
            execution, phase=fields["phase"], snapshot=snapshot,
            receipt=snapshot.get("receipt"), row_version=execution.row_version + 1,
            last_error_code=fields.get("last_error_code", ""),
            last_error_message=fields.get("last_error_message", ""),
        )
        return self.execution


class TransferSubmissionDiagnosticsTests(unittest.TestCase):
    def _service(self, *, phase="native_submitting", snapshot=None):
        plan = {
            "version": 1, "job_id": 14,
            "source": {"provider_kind": "quark", "source_form": "share"},
            "destination": {"provider_kind": "quark"},
            "transfer": {"strategy": "native_save", "idempotency_key": "operation:14"},
            "paths": {"destination_final": {"kind": "native", "namespace_id": "native:quark", "path": "/test/job-14"}},
        }
        executor = Mock()
        executor.submit.return_value = OperationReceipt(
            operation_id="provider-task-14", status="completed", provider_kind="quark",
            metadata={"operation_key": "operation:14"},
        )
        executions = _Executions(plan, {"executor_strategy": "native_save", **(snapshot or {"phase": phase})})
        add_event = Mock()
        service = object.__new__(TransferDispatchService)
        service.database = SimpleNamespace(
            job_queries=SimpleNamespace(get=lambda _job_id: {"id": 14, "status": "submitted"}),
            job_commands=SimpleNamespace(add_event=add_event),
        )
        service.executions = executions
        service.execution_lease_seconds = 60
        service._validation_deps = lambda: {}
        service._orchestrator = lambda _plan: TransferOrchestrator(plan, {"native_save": executor})
        self.enterContext(patch("fnos_media_import.transfer.dispatch.validate_plan"))
        self.enterContext(patch("fnos_media_import.transfer.dispatch.v1_plan_or_review", return_value=(plan, None)))
        return service, executions, executor, add_event

    @staticmethod
    def _handle(service):
        return service.handle_worker({"job_id": 14}, {"id": 3, "owner_id": "test-worker"})

    def test_receipt_validation_failure_reports_actual_cause_without_resubmitting(self):
        service, executions, executor, add_event = self._service()
        executions.receipt_error = ExecutionConflictError("回执与提交 fence 不匹配")
        with self.assertLogs(_LOGGER_NAME, "WARNING") as logs:
            result = self._handle(service)
        self.assertEqual(result["worker_outcome"], "deferred")
        self.assertIn("回执与提交 fence 不匹配", result["message"])
        self.assertNotIn("其他 Worker", result["message"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("job_id=14", logs.output[0])
        self.assertIn("回执与提交 fence 不匹配", logs.output[0])
        self.assertEqual(add_event.call_count, 1)
        self.assertEqual(add_event.call_args.args[0:2], (14, "warn"))
        self.assertIsNone(executions.execution.receipt)
        self.assertEqual(executions.execution.snapshot["submission_fence"]["state"], "prepared")
        with self.assertLogs(_LOGGER_NAME, "WARNING"):
            result = self._handle(service)
        self.assertEqual(result["phase"], "manual_review")
        self.assertEqual(executor.submit.call_count, 1)

    def test_real_cas_conflict_is_also_reported_without_inventing_another_cause(self):
        service, executions, _executor, add_event = self._service()
        executions.receipt_error = ExecutionConflictError("执行状态已被其他 Worker 更新")
        with self.assertLogs(_LOGGER_NAME, "WARNING"):
            result = self._handle(service)
        self.assertIn("执行状态已被其他 Worker 更新", result["message"])
        self.assertEqual(add_event.call_count, 1)

    def test_prepared_fence_review_preserves_prior_error_and_sets_unknown_code(self):
        service, executions, executor, add_event = self._service(snapshot={
            "phase": "native_submitting",
            "submission_fence": {"state": "prepared", "operation_key": "operation:14"},
            "error_code": ErrorCode.NETWORK_TRANSIENT,
            "message": '网盘连接超时 https://provider.invalid/share/secret-path?pwd=secret-code Cookie: secret-cookie',
        })
        with self.assertLogs(_LOGGER_NAME, "WARNING") as logs:
            result = self._handle(service)
        self.assertEqual(result["phase"], "manual_review")
        self.assertIn("网盘连接超时", result["message"])
        self.assertEqual(executions.execution.last_error_code, ErrorCode.RESULT_UNKNOWN)
        self.assertEqual(executions.execution.snapshot["review_from"], "native_submitting")
        self.assertEqual(executions.execution.snapshot["submission_fence"]["state"], "prepared")
        self.assertEqual(executor.submit.call_count, 0)
        combined = str(result) + str(add_event.call_args) + str(logs.output)
        for secret in ("secret-path", "secret-code", "secret-cookie", "https://provider.invalid"):
            self.assertNotIn(secret, combined)

    def test_retryable_submission_error_is_logged_once_before_retry_state_transition(self):
        for phase, method in (("native_submitting", "submit"), ("source_materializing", "materialize_source"), ("transferring", "submit")):
            with self.subTest(phase=phase):
                service, executions, executor, add_event = self._service(phase=phase)
                getattr(executor, method).side_effect = TransferError(
                    ErrorCode.NETWORK_TRANSIENT, "Provider timeout; token=secret-token", retryable=True,
                )
                with self.assertLogs(_LOGGER_NAME, "WARNING") as logs:
                    result = self._handle(service)
                self.assertEqual(result["phase"], "retry_wait")
                self.assertEqual(executions.execution.snapshot["submission_fence"]["state"], "prepared")
                self.assertIsNone(executions.execution.receipt)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(f"phase={phase}", logs.output[0])
                self.assertIn("Provider timeout", logs.output[0])
                self.assertEqual(add_event.call_count, 1)
                self.assertNotIn("secret-token", str(logs.output) + str(add_event.call_args))
                with self.assertNoLogs(_LOGGER_NAME, "WARNING"):
                    retry_result = self._handle(service)
                self.assertEqual(retry_result["phase"], phase)
                self.assertEqual(add_event.call_count, 1)
                with self.assertLogs(_LOGGER_NAME, "WARNING"):
                    reviewed = self._handle(service)
                self.assertEqual(reviewed["phase"], "manual_review")
                self.assertIn("Provider timeout", reviewed["message"])
                self.assertNotIn("secret-token", reviewed["message"])
                self.assertEqual(getattr(executor, method).call_count, 1)

    def test_healthy_submission_does_not_emit_warning_or_diagnostic_event(self):
        service, executions, executor, add_event = self._service()
        with self.assertNoLogs(_LOGGER_NAME, "WARNING"):
            result = self._handle(service)
        self.assertEqual(result["phase"], "target_verifying")
        self.assertEqual(executions.execution.snapshot["submission_fence"]["state"], "committed")
        self.assertEqual(executor.submit.call_count, 1)
        add_event.assert_not_called()

    def test_failed_event_sink_cannot_change_fence_or_leak_its_exception(self):
        service, executions, executor, add_event = self._service(snapshot={
            "phase": "native_submitting", "submission_fence": {"state": "prepared"},
        })
        add_event.side_effect = RuntimeError("Event storage failed password=event-secret")
        with self.assertLogs(_LOGGER_NAME, "WARNING") as logs:
            result = self._handle(service)
        self.assertEqual(result["phase"], "manual_review")
        self.assertEqual(executions.execution.phase, "manual_review")
        self.assertEqual(executor.submit.call_count, 0)
        self.assertNotIn("event-secret", str(logs.output))

    def test_safe_error_redacts_urls_credentials_and_control_characters(self):
        messages = (
            ('timeout https://user:secret-password@provider.invalid/share/secret-link', ("secret-password", "secret-link")),
            ('bad response {"Cookie": "secret-cookie"}', ("secret-cookie",)),
            ('forbidden Authorization: Bearer secret-bearer', ("secret-bearer",)),
            ('forbidden bearer secret-bearer', ("secret-bearer",)),
            ('request url: /share/secret-link', ("secret-link",)),
            ('failed magnet:?xt=urn:btih:secret-magnet', ("secret-magnet",)),
            ('timeout\r\n\x1bCookie=secret-cookie', ("secret-cookie", "\r", "\n", "\x1b")),
            ('timeout refresh_token=secret-refresh', ("secret-refresh",)),
        )
        for message, secrets in messages:
            with self.subTest(message=message):
                safe = TransferDispatchService._safe_error("提交诊断", RuntimeError(message))
                for secret in secrets:
                    self.assertNotIn(secret, safe)
                self.assertIn("redacted", safe)


if __name__ == "__main__":
    unittest.main()
