from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import unittest
from dataclasses import dataclass, field
from typing import Any
from types import SimpleNamespace

from fnos_media_import.services.job_cancellation_service import (
    JobCancellationDependencies,
    JobCancellationService,
)
from fnos_media_import.services.job_admin_command_service import (
    JobAdminCommandDependencies,
    JobAdminCommandService,
)
from fnos_media_import.transfer.dispatch import TransferDispatchService


class _Jobs(RepositoryFakeMixin):
    def __init__(self, *, status: str = "transferring") -> None:
        self.job = {"id": 42, "status": status, "raw_data": {}}
        self.events: list[tuple[Any, ...]] = []

    def get_job(self, _job_id: int) -> dict[str, Any]:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **updates: Any) -> None:
        self.job.update(copy.deepcopy(updates))

    def update_job_if_status(
        self,
        _job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        **updates: Any,
    ) -> bool:
        expected = {str(value) for value in expected_statuses}
        if str(self.job.get("status") or "") not in expected:
            return False
        self.job.update(copy.deepcopy(updates))
        return True

    def add_event(self, *args: Any) -> int:
        self.events.append(copy.deepcopy(args))
        return len(self.events)


class _Cleaner(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[int, bool]] = []

    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict[str, Any]:
        self.cancel_calls.append((job_id, stop_running))
        return {"success": True, "job_id": job_id, "message": "legacy rclone cancelled"}

    def cleanup_cancelled_task(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("cleanup=false test must not invoke file cleanup")


@dataclass
class _Execution:
    phase: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    app_acked: bool = False
    final_verified: bool = False
    receipt: dict[str, Any] | None = None


class _ExecutionRepository(RepositoryFakeMixin):
    def __init__(self, execution: _Execution | None) -> None:
        self.execution = execution

    def get(self, _job_id: int) -> _Execution | None:
        return self.execution


def _service(
    jobs: _Jobs,
    cleaner: _Cleaner,
    *,
    execution: _Execution | None,
    enqueue: Any = None,
) -> JobCancellationService:
    return JobCancellationService(
        JobCancellationDependencies(
            jobs=jobs,
            cleaner=cleaner,
            merge_raw_data=lambda current, patch: {**(current or {}), **patch},
            payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
            cancelled_status="cancelled",
            execution_repository=_ExecutionRepository(execution),
            enqueue_transfer=enqueue,
        )
    )


class JobCancellationV1Tests(unittest.TestCase):
    def test_cancelled_job_reactivates_durable_transfer_worker(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()
        calls: list[tuple[int, bool]] = []

        def enqueue(job_id: int, *, reactivate: bool = False) -> dict[str, Any]:
            calls.append((job_id, reactivate))
            return {"success": True, "task_id": 9, "created": True}

        result = _service(
            jobs,
            cleaner,
            execution=_Execution("transferring", {"receipt": {"operation_id": "op-42"}}),
            enqueue=enqueue,
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False, "stop_running": True},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertTrue(result["cancelled"])
        self.assertEqual(jobs.job["status"], "cancelled")
        self.assertEqual(calls, [(42, True)])
        self.assertEqual(cleaner.cancel_calls, [])
        item_types = {item["type"] for item in result["cleanup"]["items"]}
        self.assertIn("cancel_transfer_execution", item_types)

    def test_v1_cancel_without_receipt_never_calls_provider_or_legacy_cancel(self) -> None:
        jobs = _Jobs(status="submitted")
        jobs.job.update(
            {
                "source_provider_kind": "sixpan",
                "source_form": "magnet",
                "external_task_id": "sixpan-task-42",
                "plan_version": 1,
            }
        )
        cleaner = _Cleaner()
        provider_calls: list[str] = []
        result = _service(
            jobs,
            cleaner,
            execution=_Execution("native_submitting", {}),
            enqueue=lambda _job_id, **_kwargs: {"success": True, "created": True},
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False, "stop_running": True},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertTrue(result["cancelled"])
        self.assertEqual(provider_calls, [])
        self.assertEqual(cleaner.cancel_calls, [])

    def test_active_v1_execution_defers_destructive_cleanup_until_worker_confirms_cancel(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()
        calls: list[tuple[int, bool]] = []

        result = _service(
            jobs,
            cleaner,
            execution=_Execution("transferring", {"receipt": {"operation_id": "op-42"}}),
            enqueue=lambda job_id, *, reactivate=False: (
                calls.append((job_id, reactivate))
                or {"success": True, "created": True}
            ),
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={
                "cleanup": True,
                "delete_source": True,
                "delete_temp": True,
                "delete_target_partial": True,
                "include_title_matches": False,
            },
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertTrue(result["cancelled"])
        self.assertTrue(result["cleanup"]["deferred"])
        self.assertEqual(cleaner.cancel_calls, [])
        self.assertEqual(calls, [(42, True)])
        self.assertFalse(
            jobs.job["raw_data"]["cancel"]["include_title_matches_requested"]
        )

    def test_final_verified_execution_rejects_cancel_and_resumes_worker(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()
        calls: list[tuple[int, bool]] = []

        result = _service(
            jobs,
            cleaner,
            execution=_Execution(
                "ack_pending",
                {"target_verified": True, "final_verified": True, "app_acked": False},
                final_verified=True,
            ),
            enqueue=lambda job_id, *, reactivate=False: (
                calls.append((job_id, reactivate))
                or {"success": True, "created": True}
            ),
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["state_conflict"])
        self.assertIn("ACK", result["message"])
        self.assertEqual(jobs.job["status"], "transferring")
        self.assertEqual(cleaner.cancel_calls, [])
        self.assertEqual(calls, [(42, True)])

    def test_cleanup_pending_execution_cannot_be_stranded_by_cancel(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()

        result = _service(
            jobs,
            cleaner,
            execution=_Execution(
                "cleanup_pending",
                {"app_acked": True, "final_verified": True},
                app_acked=True,
                final_verified=True,
            ),
            enqueue=lambda _job_id, **_kwargs: {"success": True},
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertFalse(result["cancelled"])
        self.assertIn("来源清理", result["message"])
        self.assertEqual(jobs.job["status"], "transferring")

    def test_provider_submitting_fence_rejects_concurrent_cancel(self) -> None:
        jobs = _Jobs(status="provider_submitting")
        cleaner = _Cleaner()

        result = _service(
            jobs,
            cleaner,
            execution=_Execution("source_materializing"),
        ).cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["retryable"])
        self.assertEqual(jobs.job["status"], "provider_submitting")
        self.assertEqual(cleaner.cancel_calls, [])

    def test_worker_cleanup_uses_frozen_cancel_flags_and_never_stops_global_rclone(self) -> None:
        calls: list[dict[str, Any]] = []
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        dispatch.rclone_service = SimpleNamespace(
            cleanup_cancelled_task=lambda **kwargs: (
                calls.append(kwargs)
                or {"success": True, "items": [], "warnings": [], "errors": []}
            )
        )
        job = {
            "id": 42,
            "raw_data": {
                "cancel": {
                    "active": True,
                    "cleanup_requested": True,
                    "delete_source_requested": True,
                    "delete_temp_requested": False,
                    "delete_target_partial_requested": True,
                    "include_title_matches": False,
                }
            },
        }

        result = dispatch._cleanup_cancelled_artifacts(job, allow_target_partial=False)

        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["job"]["id"], 42)
        self.assertTrue(calls[0]["delete_source"])
        self.assertFalse(calls[0]["delete_temp"])
        self.assertFalse(calls[0]["delete_target_partial"])
        self.assertFalse(calls[0]["stop_running"])
        self.assertFalse(calls[0]["include_title_matches"])

    def test_pre_submit_cancel_cleanup_failure_is_requeued_not_terminal(self) -> None:
        saved: list[dict[str, Any]] = []
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        dispatch.rclone_service = SimpleNamespace(
            cleanup_cancelled_task=lambda **_kwargs: {
                "success": False,
                "message": "cleanup unavailable",
                "items": [],
                "warnings": [],
                "errors": ["cleanup unavailable"],
            }
        )

        class _Executions:
            @staticmethod
            def save(_execution, snapshot, **kwargs):
                saved.append({"snapshot": snapshot, **kwargs})

        dispatch.executions = _Executions()
        execution = SimpleNamespace(phase="planning", snapshot={})
        job = {
            "id": 42,
            "status": "cancelled",
            "raw_data": {
                "cancel": {
                    "active": True,
                    "cleanup_requested": True,
                    "delete_source_requested": True,
                }
            },
        }

        result = dispatch._stop_if_job_cancelled(job, execution, "worker-a")

        self.assertEqual(result["worker_outcome"], "deferred")
        self.assertEqual(result["phase"], "cancel_pending")
        self.assertEqual(saved[0]["phase"], "cancel_pending")
        self.assertEqual(saved[0]["snapshot"]["error_code"], "CANCEL_CLEANUP_PENDING")

    def test_receipt_column_prevents_pre_submit_cleanup_path(self) -> None:
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        external_calls: list[tuple[int, str]] = []
        dispatch._cancel_external_operation = lambda job, _execution, owner: (
            external_calls.append((int(job["id"]), owner))
            or {
                "worker_outcome": "deferred",
                "phase": "cancel_pending",
                "cancelled": True,
            }
        )
        dispatch._cleanup_cancelled_artifacts = lambda *_args, **_kwargs: self.fail(
            "durable receipt must prevent pre-submit destructive cleanup"
        )
        execution = SimpleNamespace(
            phase="transferring",
            snapshot={},
            receipt={"operation_id": "provider-op-42", "status": "running"},
        )
        job = {"id": 42, "status": "cancelled", "raw_data": {}}

        result = dispatch._stop_if_job_cancelled(job, execution, "worker-a")

        self.assertEqual(result["phase"], "cancel_pending")
        self.assertEqual(external_calls, [(42, "worker-a")])

    def test_ambiguous_cleanup_result_stays_cancel_pending(self) -> None:
        saved: list[dict[str, Any]] = []
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        dispatch.rclone_service = SimpleNamespace(
            cleanup_cancelled_task=lambda **_kwargs: {
                "message": "cleanup response omitted success",
                "items": [],
            }
        )

        class _Executions:
            @staticmethod
            def save(_execution, snapshot, **kwargs):
                saved.append({"snapshot": snapshot, **kwargs})

        dispatch.executions = _Executions()
        execution = SimpleNamespace(phase="planning", snapshot={}, receipt=None)
        job = {
            "id": 42,
            "status": "cancelled",
            "raw_data": {
                "cancel": {
                    "active": True,
                    "cleanup_requested": True,
                    "delete_source_requested": True,
                }
            },
        }

        result = dispatch._stop_if_job_cancelled(job, execution, "worker-a")

        self.assertEqual(result["worker_outcome"], "deferred")
        self.assertEqual(result["phase"], "cancel_pending")
        self.assertFalse(saved[0]["snapshot"]["cancel_cleanup"]["success"])


class JobRetryCommandV1Tests(unittest.TestCase):
    def test_cancelled_retry_is_not_reported_as_queued_or_auto_started(self) -> None:
        auto_start_calls: list[dict[str, Any]] = []

        class _Imports:
            @staticmethod
            def retry_job(_job_id: int) -> dict[str, Any]:
                return {
                    "success": False,
                    "requires_new_job": True,
                    "error_code": "REIMPORT_NEW_GENERATION_REQUIRED",
                    "message": "需要创建新一代任务",
                }

        service = JobAdminCommandService(
            JobAdminCommandDependencies(
                imports=_Imports(),
                job_queries=SimpleNamespace(),
                job_commands=SimpleNamespace(),
                auto_start_rclone=lambda result, _reason: auto_start_calls.append(result),
            )
        )

        result = service.retry(42)

        self.assertFalse(result["success"])
        self.assertTrue(result["requires_new_job"])
        self.assertEqual(auto_start_calls, [])


if __name__ == "__main__":
    unittest.main()
