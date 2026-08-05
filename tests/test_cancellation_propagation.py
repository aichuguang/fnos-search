from __future__ import annotations

import copy
import unittest
import uuid
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.job_cancellation_service import (
    JobCancellationDependencies,
    JobCancellationService,
)
from fnos_media_import.services.public_import_job_coordinator import PublicImportJobCoordinator
from fnos_media_import.services.request_review_command_service import (
    RequestReviewCommandDependencies,
    RequestReviewCommandService,
)


class WorkerTaskCancellationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"worker-cancel-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_cancels_only_tasks_linked_to_requested_job_and_organizer(self) -> None:
        job_task, _ = self.database.worker_tasks.enqueue(
            "import_retry", {"job_id": 42}, "import-retry:42"
        )
        organizer_task, _ = self.database.worker_tasks.enqueue(
            "organizer_process", {"task_id": 7}, "organizer-process:7"
        )
        other_task, _ = self.database.worker_tasks.enqueue(
            "import_retry", {"job_id": 43}, "import-retry:43"
        )
        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["id"], job_task)

        result = self.database.worker_tasks.cancel_related(
            job_id=42,
            organizer_task_ids=[7],
            reason="job cancelled",
        )

        self.assertEqual(result["cancelled_count"], 2)
        self.assertEqual(self.database.worker_tasks.get(job_task)["status"], "failed")
        self.assertEqual(self.database.worker_tasks.get(organizer_task)["status"], "failed")
        self.assertEqual(self.database.worker_tasks.get(other_task)["status"], "pending")
        self.assertTrue(self.database.worker_tasks.get(job_task)["result"]["cancelled"])

    def test_cancels_public_import_worker_by_request_without_touching_other_request(self) -> None:
        cancelled_task, _ = self.database.worker_tasks.enqueue(
            "public_import_create",
            {"guest_request_id": 9, "submit_payload": {"title": "A"}},
            "public-import:req-9",
        )
        other_task, _ = self.database.worker_tasks.enqueue(
            "public_import_create",
            {"guest_request_id": 10, "submit_payload": {"title": "B"}},
            "public-import:req-10",
        )

        result = self.database.worker_tasks.cancel_related(
            guest_request_id=9,
            reason="request cancelled",
        )

        self.assertEqual(result["cancelled_count"], 1)
        self.assertEqual(self.database.worker_tasks.get(cancelled_task)["status"], "failed")
        self.assertEqual(self.database.worker_tasks.get(other_task)["status"], "pending")
        self.assertEqual(result["in_flight_task_ids"], [])

    def test_running_public_import_worker_keeps_lease_for_retryable_compensation(self) -> None:
        task_id, _ = self.database.worker_tasks.enqueue(
            "public_import_create",
            {"guest_request_id": 9, "submit_payload": {"title": "A"}},
            "public-import:req-9-running",
        )
        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["id"], task_id)

        result = self.database.worker_tasks.cancel_related(
            guest_request_id=9,
            reason="request cancelled during compensation",
        )

        running = self.database.worker_tasks.get(task_id)
        self.assertEqual(result["cancelled_count"], 0)
        self.assertEqual(result["task_ids"], [])
        self.assertEqual(result["in_flight_task_ids"], [task_id])
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["owner_id"], "worker-a")
        self.assertIsNotNone(running["lease_expires_at"])

        requeued = self.database.worker_tasks.fail(
            task_id,
            "worker-a",
            "formal job compensation needs retry",
            retry_delay_seconds=0,
            result={"worker_outcome": "retryable", "compensation_failed": True},
        )

        self.assertTrue(requeued)
        pending = self.database.worker_tasks.get(task_id)
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["owner_id"])
        self.assertTrue(pending["result"]["compensation_failed"])


class _RequestReviewRaceQueries:
    def __init__(self) -> None:
        self.item = {
            "id": 9,
            "status": "submitted",
            "public_status": "处理中",
            "job_id": None,
            "raw_data": {},
        }

    def get(self, _request_id: int) -> dict:
        return copy.deepcopy(self.item)


class _RequestReviewRaceCommands:
    def __init__(self, queries: _RequestReviewRaceQueries) -> None:
        self.queries = queries

    def transition_with_event(self, _request_id: int, **kwargs) -> bool:
        self.queries.item["job_id"] = 42
        if self.queries.item["status"] not in kwargs["expected_statuses"]:
            return False
        self.queries.item.update(
            status=kwargs["status"],
            public_status=kwargs["public_status"],
            raw_data=copy.deepcopy(kwargs["raw_data"]),
        )
        return True


class _RequestReviewRaceJobs:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def get(self, job_id: int) -> dict:
        self.calls.append(job_id)
        return {"id": job_id, "status": "submitted"}


class RequestReviewCancellationRaceTests(unittest.TestCase):
    def test_cancel_reloads_job_bound_concurrently_with_request_transition(self) -> None:
        requests = _RequestReviewRaceQueries()
        jobs = _RequestReviewRaceJobs()
        service = RequestReviewCommandService(
            RequestReviewCommandDependencies(
                requests=requests,
                commands=_RequestReviewRaceCommands(requests),
                jobs=jobs,
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
            )
        )

        result, status_code = service.cancel(
            9,
            reason="cancel while binding",
            admin="admin",
            force=False,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(result["request"]["status"], "cancelled")
        self.assertEqual(result["request"]["job_id"], 42)
        self.assertEqual(result["linked_job"]["id"], 42)
        self.assertEqual(jobs.calls, [42])


class _Jobs:
    def __init__(self) -> None:
        self.job = {
            "id": 42,
            "status": "submitted",
            "source_type": "magnet",
            "target_route": "sixpan_offline",
            "external_task_id": "sixpan-42",
            "raw_data": {},
        }
        self.cas_expected_statuses: list[set[str]] = []

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **updates) -> None:
        self.job.update(copy.deepcopy(updates))

    def update_job_if_status(self, _job_id: int, expected_statuses, **updates) -> bool:
        expected = {str(value) for value in expected_statuses}
        self.cas_expected_statuses.append(expected)
        if str(self.job.get("status") or "") not in expected:
            return False
        self.job.update(copy.deepcopy(updates))
        return True

    @staticmethod
    def add_event(*_args) -> int:
        return 1


class _Cleaner:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict:
        self.calls.append((job_id, stop_running))
        return {"success": True, "job_id": job_id, "message": "cancelled"}

    def cleanup_cancelled_task(self, **_kwargs) -> dict:
        raise AssertionError("cleanup=false must not delete files")


class _Organizer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def cancel_job_tasks(self, job_id: int, *, reason: str) -> dict:
        self.calls.append((job_id, reason))
        return {"success": True, "task_ids": [70], "cancelled_task_ids": [70]}


class _WorkerTasks:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def cancel_related(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"success": True, "cancelled_count": 2, "task_ids": [1, 2]}


class _SixPan:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def delete_offline_tasks(self, identities: list[str], *, delete_files: bool = False) -> dict:
        self.calls.append((identities, delete_files))
        return {"success": True}


class JobCancellationPropagationTests(unittest.TestCase):
    def test_admin_completed_job_guard_cannot_be_bypassed_with_force(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "fnos_media_import" / "app.py").read_text(
            encoding="utf-8"
        )
        endpoint = source.split("    def api_admin_job_cancel(job_id: int):", 1)[1].split(
            "    def _job_cancellation_service()", 1
        )[0]

        completed_guard = 'if str(job.get("status") or "") in {"done", "success"}:'
        completed_message = "任务已完成，不能取消；如需删除已入库资源请走人工清理"
        self.assertIn(completed_guard, endpoint)
        self.assertIn(completed_message, endpoint)
        self.assertNotIn('"force"', endpoint)
        self.assertLess(endpoint.index(completed_guard), endpoint.index("_cancel_job_and_cleanup("))

    def test_completed_linked_job_cleanup_stays_skipped_for_request_force(self) -> None:
        jobs = _Jobs()
        jobs.job["status"] = "success"
        cleaner = _Cleaner()
        organizer = _Organizer()
        workers = _WorkerTasks()
        sixpan = _SixPan()
        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=jobs,
                cleaner=cleaner,
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
                worker_tasks=workers,
                organizer=organizer,
                sixpan_importer=lambda: sixpan,
            )
        )

        result = service.cancel(
            copy.deepcopy(jobs.job),
            reason="cancel completed request only",
            payload={"force": True, "cleanup": True, "stop_running": True},
            request_item={"id": 9, "status": "cancelled"},
            cleanup_default=True,
            stop_running_default=True,
            admin_username="admin",
        )

        self.assertEqual(jobs.job["status"], "success")
        self.assertFalse(result["cancelled"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["cleanup"]["skipped"])
        self.assertIn("任务已完成，取消未执行", result["message"])
        self.assertEqual(jobs.cas_expected_statuses, [])
        self.assertEqual(cleaner.calls, [])
        self.assertEqual(organizer.calls, [])
        self.assertEqual(workers.calls, [])
        self.assertEqual(sixpan.calls, [])

    def test_job_cancel_propagates_without_cross_job_or_sixpan_file_deletion(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()
        organizer = _Organizer()
        workers = _WorkerTasks()
        sixpan = _SixPan()
        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=jobs,
                cleaner=cleaner,
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
                worker_tasks=workers,
                organizer=organizer,
                sixpan_importer=lambda: sixpan,
            )
        )

        result = service.cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False, "stop_running": True},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertEqual(jobs.job["status"], "cancelled")
        self.assertEqual(cleaner.calls, [(42, True)])
        self.assertEqual(organizer.calls[0][0], 42)
        self.assertEqual(workers.calls, [{
            "job_id": 42,
            "organizer_task_ids": [70],
            "reason": "关联入库任务 #42 已取消：admin cancel",
        }])
        self.assertEqual(sixpan.calls, [(["sixpan-42"], False)])
        item_types = {item["type"] for item in result["cleanup"]["items"]}
        self.assertEqual(
            item_types,
            {"cancel_execution", "cancel_organizer", "cancel_worker_tasks", "cancel_sixpan_task"},
        )

    def test_concurrent_completion_wins_without_any_cancel_side_effect(self) -> None:
        class RacingJobs(_Jobs):
            def update_job_if_status(self, _job_id: int, expected_statuses, **_updates) -> bool:
                self.cas_expected_statuses.append({str(value) for value in expected_statuses})
                self.job.update(
                    status="done",
                    error_message="",
                    raw_data={"completion": {"verified": True}},
                )
                return False

        class SideEffectGuardCleaner:
            @staticmethod
            def cancel_job(*_args, **_kwargs) -> dict:
                raise AssertionError("rclone cancellation must not run after concurrent completion")

            @staticmethod
            def cleanup_cancelled_task(**_kwargs) -> dict:
                raise AssertionError("file cleanup must not run after concurrent completion")

        jobs = RacingJobs()
        organizer = _Organizer()
        workers = _WorkerTasks()
        sixpan = _SixPan()
        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=jobs,
                cleaner=SideEffectGuardCleaner(),
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
                worker_tasks=workers,
                organizer=organizer,
                sixpan_importer=lambda: sixpan,
            )
        )

        result = service.cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": True, "stop_running": True},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertEqual(jobs.cas_expected_statuses, [{"submitted"}])
        self.assertEqual(jobs.job["status"], "done")
        self.assertEqual(jobs.job["raw_data"], {"completion": {"verified": True}})
        self.assertFalse(result["cancelled"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["state_conflict"])
        self.assertTrue(result["cleanup"]["skipped"])
        self.assertEqual(result["cleanup"]["items"], [])
        self.assertEqual(organizer.calls, [])
        self.assertEqual(workers.calls, [])
        self.assertEqual(sixpan.calls, [])

    def test_repeated_cancel_updates_metadata_without_incrementing_generation(self) -> None:
        jobs = _Jobs()
        jobs.job.update(
            status="cancelled",
            raw_data={"cancel": {"active": True, "generation": 3, "reason": "first"}},
        )
        cleaner = _Cleaner()
        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=jobs,
                cleaner=cleaner,
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
            )
        )

        result = service.cancel(
            copy.deepcopy(jobs.job),
            reason="repeat cancel",
            payload={"cleanup": False, "stop_running": False},
            request_item=None,
            cleanup_default=True,
            stop_running_default=True,
            admin_username="admin",
        )

        self.assertTrue(result["cancelled"])
        self.assertTrue(result["already_cancelled"])
        self.assertEqual(jobs.cas_expected_statuses, [{"cancelled"}])
        self.assertEqual(jobs.job["raw_data"]["cancel"]["generation"], 3)
        self.assertEqual(jobs.job["raw_data"]["cancel"]["reason"], "repeat cancel")


class _Timer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _OrganizerDb:
    def __init__(self) -> None:
        self.updates: list[tuple[int, dict]] = []

    @staticmethod
    def list_organizer_tasks_by_job(_job_id: int, limit: int = 20) -> list[dict]:
        return [
            {"id": 7, "status": "scanning", "revision": 3},
            {"id": 8, "status": "done", "revision": 2},
        ]

    def update_organizer_task(self, task_id: int, **updates) -> bool:
        self.updates.append((task_id, updates))
        return True


class OrganizerImmediateCancellationTests(unittest.TestCase):
    def test_cancellation_stops_timer_and_bumps_revision(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.db = _OrganizerDb()
        timer = _Timer()
        service._timers = {7: timer}

        result = service.cancel_job_tasks(42, reason="cancelled")

        self.assertTrue(result["success"])
        self.assertTrue(timer.cancelled)
        self.assertEqual(result["cancelled_task_ids"], [7])
        task_id, updates = service.db.updates[0]
        self.assertEqual(task_id, 7)
        self.assertEqual(updates["status"], "cancelled")
        self.assertEqual(updates["expected_revision"], 3)
        self.assertEqual(updates["expected_statuses"], {"scanning"})
        self.assertTrue(updates["bump_revision"])
        self.assertTrue(updates["clear_scan_lease"])


class _CancelledSubmission:
    @staticmethod
    def worker_execution_allowed(_request_id: int):
        return False, "访客提交状态已变为 cancelled，停止后台入库", {
            "id": 9,
            "status": "cancelled",
            "public_status": "已取消",
        }


class LatePublicWorkerFenceTests(unittest.TestCase):
    def test_late_worker_does_not_create_job_after_request_cancel(self) -> None:
        class ReadOnlyImports:
            @staticmethod
            def get_job_by_idempotency_key(_key: str):
                return None

            @staticmethod
            def create_import_job(_payload: dict):
                raise AssertionError("cancelled request must not create an import job")

        coordinator = PublicImportJobCoordinator(
            import_service=lambda: ReadOnlyImports(),
            submission_service=lambda: _CancelledSubmission(),
            runtime_revision=lambda: 1,
            executor_id=lambda: "worker",
            start_rclone=lambda *_args: None,
            public_status=lambda status: status,
            safe_result=lambda result: result,
            warn=lambda *_args: None,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertFalse(outcome.result["success"])
        self.assertTrue(outcome.result["cancelled"])
        self.assertEqual(outcome.result["worker_outcome"], "business_failed")
        self.assertEqual(outcome.bind_outcome, "cancelled")

    def test_cancelled_request_recovers_provider_submitting_orphan_for_manual_review(self) -> None:
        class ReadOnlyImports:
            @staticmethod
            def get_job_by_idempotency_key(key: str):
                return {
                    "id": 42,
                    "status": "provider_submitting",
                    "idempotency_key": key,
                    "raw_data": {
                        "provider_submission_fence": {
                            "version": 1,
                            "state": "submitting",
                            "attempt": 1,
                        }
                    },
                }

            @staticmethod
            def create_import_job(_payload: dict):
                raise AssertionError("cancelled request must not recreate the orphan job")

        coordinator = PublicImportJobCoordinator(
            import_service=lambda: ReadOnlyImports(),
            submission_service=lambda: _CancelledSubmission(),
            runtime_revision=lambda: 1,
            executor_id=lambda: "worker",
            start_rclone=lambda *_args: self.fail("ambiguous orphan must not start rclone"),
            public_status=lambda status: status,
            safe_result=lambda result: result,
            warn=lambda *_args: None,
            cancel_unbound_job=lambda *_args, **_kwargs: self.fail(
                "provider_submitting orphan must not be falsely cancelled"
            ),
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(outcome.job["id"], 42)
        self.assertFalse(outcome.result["cancelled"])
        self.assertTrue(outcome.result["manual_review"])
        self.assertTrue(outcome.result["orphan_job_recovered"])
        self.assertEqual(outcome.bind_outcome, "manual_review")


class _PublicImportRaceSubmission:
    def __init__(self) -> None:
        self.request = {
            "id": 9,
            "status": "submitted",
            "public_status": "处理中",
            "job_id": None,
        }
        self.bind_calls = 0

    def cancel(self) -> None:
        self.request.update(status="cancelled", public_status="已取消")

    def worker_execution_allowed(self, _request_id: int):
        if self.request["status"] != "submitted":
            return False, "访客提交状态已变为 cancelled，停止后台入库", copy.deepcopy(self.request)
        if self.request.get("job_id"):
            return False, "访客提交已绑定正式任务，停止重复后台入库", copy.deepcopy(self.request)
        return True, "", copy.deepcopy(self.request)

    def bind_import_job(self, _request_id: int, **_kwargs):
        self.bind_calls += 1
        return "bound", copy.deepcopy(self.request)

    def mark_import_failed(self, _request_id: int, *, error: str):
        raise AssertionError(f"compensated cancellation must not mark request failed: {error}")


class _PublicImportRaceService:
    def __init__(
        self,
        submission: _PublicImportRaceSubmission,
        *,
        cancel_during_create: bool,
        created: bool | None,
        owned_idempotency: bool,
        job_status: str,
        retry_status: str,
        retry_calls: list[int],
        provider_fence: bool,
    ) -> None:
        self.submission = submission
        self.cancel_during_create = cancel_during_create
        self.created = created
        self.owned_idempotency = owned_idempotency
        self.job_status = job_status
        self.retry_status = retry_status
        self.retry_calls = retry_calls
        self.provider_fence = provider_fence

    def create_import_job(self, payload: dict) -> dict:
        if self.cancel_during_create:
            self.submission.cancel()
        result = {
            "success": True,
            "job": {
                "id": 42,
                "status": self.job_status,
                "idempotency_key": (
                    payload.get("idempotency_key")
                    if self.owned_idempotency
                    else "guest-request:another-request"
                ),
                "raw_data": (
                    {
                        "provider_submission_fence": {
                            "version": 1,
                            "state": "not_started",
                            "attempt": 0,
                        }
                    }
                    if self.job_status == "created" and self.provider_fence
                    else {}
                ),
            },
        }
        if self.created is not None:
            result["created"] = self.created
        return result

    def retry_job(self, job_id: int) -> dict:
        self.retry_calls.append(job_id)
        return {
            "success": True,
            "created": True,
            "job": {
                "id": job_id,
                "status": self.retry_status,
                "idempotency_key": "guest-request:req-9",
                "raw_data": {},
            },
        }


class PublicImportCreationRaceTests(unittest.TestCase):
    @staticmethod
    def _coordinator(
        submission: _PublicImportRaceSubmission,
        *,
        cancel_during_create: bool,
        created: bool | None = True,
        owned_idempotency: bool = True,
        job_status: str = "submitted",
        retry_status: str = "waiting_transfer",
        retry_calls: list[int] | None = None,
        provider_fence: bool = True,
        rclone_calls: list[tuple[dict, str]],
        cancel_calls: list[dict],
    ) -> PublicImportJobCoordinator:
        def cancel_unbound(job: dict, *, reason: str, request_item: dict | None) -> dict:
            cancel_calls.append(
                {
                    "job": copy.deepcopy(job),
                    "reason": reason,
                    "request_item": copy.deepcopy(request_item),
                }
            )
            return {
                "cancelled": True,
                "job": {**job, "status": "cancelled", "error_message": reason},
            }

        recorded_retry_calls = retry_calls if retry_calls is not None else []

        return PublicImportJobCoordinator(
            import_service=lambda: _PublicImportRaceService(
                submission,
                cancel_during_create=cancel_during_create,
                created=created,
                owned_idempotency=owned_idempotency,
                job_status=job_status,
                retry_status=retry_status,
                retry_calls=recorded_retry_calls,
                provider_fence=provider_fence,
            ),
            submission_service=lambda: submission,
            runtime_revision=lambda: 1,
            executor_id=lambda: "worker",
            start_rclone=lambda result, reason: rclone_calls.append((result, reason)) or {"started": True},
            public_status=lambda status: status,
            safe_result=lambda result: result,
            warn=lambda *_args: None,
            cancel_unbound_job=cancel_unbound,
        )

    def test_cancel_during_provider_creation_compensates_job_before_rclone_or_bind(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(rclone_calls, [])
        self.assertEqual(submission.bind_calls, 0)
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0]["job"]["id"], 42)
        self.assertEqual(cancel_calls[0]["request_item"]["status"], "cancelled")
        self.assertEqual(outcome.job["status"], "cancelled")
        self.assertFalse(outcome.result["success"])
        self.assertTrue(outcome.result["cancelled"])
        self.assertEqual(outcome.result["worker_outcome"], "business_failed")
        self.assertEqual(outcome.bind_outcome, "cancelled")
        self.assertIsNone(outcome.rclone_start)

    def test_bind_state_conflict_compensates_started_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        def bind_after_cancel(_request_id: int, **_kwargs):
            submission.bind_calls += 1
            submission.cancel()
            return "state_conflict", copy.deepcopy(submission.request)

        submission.bind_import_job = bind_after_cancel
        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(len(rclone_calls), 1)
        self.assertEqual(submission.bind_calls, 1)
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0]["request_item"]["status"], "cancelled")
        self.assertEqual(outcome.job["status"], "cancelled")
        self.assertFalse(outcome.result["success"])
        self.assertEqual(outcome.bind_outcome, "state_conflict")
        self.assertEqual(outcome.rclone_start, {"started": True})

    def test_cancel_during_same_key_idempotent_lookup_compensates_owned_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(rclone_calls, [])
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(submission.bind_calls, 0)
        self.assertEqual(outcome.job["status"], "cancelled")
        self.assertFalse(outcome.result["created"])
        self.assertTrue(outcome.result["cancelled"])
        self.assertEqual(outcome.public_status, "已取消")
        self.assertEqual(outcome.bind_outcome, "cancelled")
        self.assertIsNone(outcome.rclone_start)

    def test_same_key_idempotent_retry_resumes_dispatch_and_binds(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            created=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(len(rclone_calls), 1)
        self.assertEqual(cancel_calls, [])
        self.assertEqual(submission.bind_calls, 1)
        self.assertEqual(outcome.job["status"], "submitted")
        self.assertEqual(outcome.bind_outcome, "bound")
        self.assertEqual(outcome.rclone_start, {"started": True})

    def test_foreign_key_reused_job_bind_conflict_never_starts_or_cancels_shared_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            created=False,
            owned_idempotency=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        def bind_after_cancel(_request_id: int, **_kwargs):
            submission.bind_calls += 1
            submission.cancel()
            return "state_conflict", copy.deepcopy(submission.request)

        submission.bind_import_job = bind_after_cancel
        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(rclone_calls, [])
        self.assertEqual(cancel_calls, [])
        self.assertEqual(submission.bind_calls, 1)
        self.assertEqual(outcome.job["status"], "submitted")
        self.assertFalse(outcome.result["created"])
        self.assertEqual(outcome.bind_outcome, "state_conflict")
        self.assertIsNone(outcome.rclone_start)

    def test_foreign_key_reused_job_never_starts_or_cancels_on_request_cancel(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=False,
            owned_idempotency=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(rclone_calls, [])
        self.assertEqual(cancel_calls, [])
        self.assertEqual(outcome.job["status"], "submitted")
        self.assertEqual(outcome.bind_outcome, "existing")

    def test_same_key_created_status_replays_provider_before_dispatch(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        retry_calls: list[int] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            created=False,
            job_status="created",
            retry_status="waiting_transfer",
            retry_calls=retry_calls,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(retry_calls, [42])
        self.assertEqual(len(rclone_calls), 1)
        self.assertFalse(outcome.result["created"])
        self.assertTrue(outcome.result["idempotent_recovered"])
        self.assertEqual(outcome.job["status"], "waiting_transfer")
        self.assertEqual(outcome.bind_outcome, "bound")

    def test_same_key_created_status_does_not_replay_provider_after_request_cancel(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        retry_calls: list[int] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=False,
            job_status="created",
            retry_calls=retry_calls,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertEqual(retry_calls, [])
        self.assertEqual(rclone_calls, [])
        self.assertEqual(len(cancel_calls), 1)
        self.assertTrue(outcome.result["cancelled"])

    def test_legacy_created_without_provider_fence_requires_manual_review(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        retry_calls: list[int] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            created=False,
            job_status="created",
            retry_calls=retry_calls,
            provider_fence=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "测试", "url": "https://example.test"},
        )

        self.assertEqual(retry_calls, [])
        self.assertEqual(rclone_calls, [])
        self.assertTrue(outcome.result["manual_review"])
        self.assertTrue(outcome.result["provider_submission_ambiguous"])
        self.assertEqual(outcome.public_status, "等待人工核对网盘提交状态")
        self.assertEqual(outcome.bind_outcome, "bound")

    def test_cancel_during_provider_submitting_lookup_never_claims_compensation_success(self) -> None:
        submission = _PublicImportRaceSubmission()
        submission.bind_import_job = lambda *_args, **_kwargs: (
            "state_conflict",
            copy.deepcopy(submission.request),
        )
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=False,
            job_status="provider_submitting",
            retry_calls=[],
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "测试"},
        )

        self.assertEqual(cancel_calls, [])
        self.assertEqual(rclone_calls, [])
        self.assertFalse(outcome.result.get("cancelled", False))
        self.assertTrue(outcome.result["manual_review"])
        self.assertEqual(outcome.bind_outcome, "state_conflict")

    def test_compensation_cas_failure_is_retryable_while_job_is_nonterminal(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )
        coordinator.cancel_unbound_job = lambda job, **_kwargs: {
            "cancelled": False,
            "state_conflict": True,
            "message": "任务状态持续变化，取消未执行，请刷新后重试",
            "job": {**job, "status": "waiting_transfer"},
        }

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertFalse(outcome.result["success"])
        self.assertTrue(outcome.result["retryable"])
        self.assertTrue(outcome.result["compensation_failed"])
        self.assertFalse(outcome.result["cancelled"])
        self.assertFalse(outcome.result["terminal"])
        self.assertEqual(outcome.result["worker_outcome"], "retryable")

    def test_cancelled_request_retries_failed_compensation_for_same_owned_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )
        compensation_attempts = 0

        def compensate(job: dict, **_kwargs) -> dict:
            nonlocal compensation_attempts
            compensation_attempts += 1
            if compensation_attempts == 1:
                return {
                    "cancelled": False,
                    "state_conflict": True,
                    "message": "job status is moving",
                    "job": {**job, "status": "waiting_transfer"},
                }
            return {
                "cancelled": True,
                "job": {**job, "status": "cancelled"},
            }

        coordinator.cancel_unbound_job = compensate
        first = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )
        second = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
            compensation_retry_job_id=42,
        )

        self.assertTrue(first.result["compensation_failed"])
        self.assertEqual(first.result["worker_outcome"], "retryable")
        self.assertEqual(compensation_attempts, 2)
        self.assertTrue(second.result["cancelled"])
        self.assertEqual(second.result["worker_outcome"], "business_failed")
        self.assertEqual(second.job["status"], "cancelled")
        self.assertEqual(rclone_calls, [])
        self.assertEqual(submission.bind_calls, 0)

    def test_compensation_retry_never_cancels_a_different_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        submission.cancel()
        coordinator = self._coordinator(
            submission,
            cancel_during_create=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
            compensation_retry_job_id=99,
        )

        self.assertTrue(outcome.result["retryable"])
        self.assertTrue(outcome.result["recovery_failed"])
        self.assertEqual(outcome.bind_outcome, "compensation_recovery_identity_conflict")
        self.assertEqual(cancel_calls, [])
        self.assertEqual(rclone_calls, [])
        self.assertEqual(submission.bind_calls, 0)

    def test_compensation_cas_loss_to_completed_job_is_terminal_without_cancel(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )
        coordinator.cancel_unbound_job = lambda job, **_kwargs: {
            "cancelled": False,
            "state_conflict": True,
            "message": "任务已完成，取消未执行",
            "job": {**job, "status": "done"},
        }

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertTrue(outcome.result["success"])
        self.assertTrue(outcome.result["completed_without_cancel"])
        self.assertFalse(outcome.result["retryable"])
        self.assertFalse(outcome.result["cancelled"])
        self.assertTrue(outcome.result["terminal"])
        self.assertEqual(outcome.result["worker_outcome"], "completed")

    def test_missing_created_flag_fails_safe_for_shared_job(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=None,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertNotIn("created", outcome.result)
        self.assertEqual(rclone_calls, [])
        self.assertEqual(cancel_calls, [])
        self.assertEqual(outcome.job["status"], "submitted")

    def test_created_claim_with_foreign_idempotency_key_fails_safe(self) -> None:
        submission = _PublicImportRaceSubmission()
        rclone_calls: list[tuple[dict, str]] = []
        cancel_calls: list[dict] = []
        coordinator = self._coordinator(
            submission,
            cancel_during_create=True,
            created=True,
            owned_idempotency=False,
            rclone_calls=rclone_calls,
            cancel_calls=cancel_calls,
        )

        outcome = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-9",
            submit_payload={"title": "Test"},
        )

        self.assertTrue(outcome.result["created"])
        self.assertEqual(rclone_calls, [])
        self.assertEqual(cancel_calls, [])
        self.assertEqual(outcome.job["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
