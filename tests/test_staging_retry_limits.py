from __future__ import annotations

import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.constants import EVENT_ERROR, JOB_REVIEW
from fnos_media_import.services.rclone_service import RcloneService
from tests.test_rclone_persisted_staging_plan import _persisted_plan, _staging_run


class _FakeDatabase:
    def __init__(
        self,
        job: dict,
        *,
        file_events: list[dict] | None = None,
        organizer_tasks: list[dict] | None = None,
    ) -> None:
        self.job = copy.deepcopy(job)
        self.events: list[tuple] = []
        self.file_events = copy.deepcopy(file_events or [])
        self.organizer_tasks = copy.deepcopy(organizer_tasks or [])

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **changes) -> None:
        self.job.update(copy.deepcopy(changes))

    def add_event(self, *values) -> None:
        self.events.append(values)

    def list_all_rclone_file_events(self, **filters) -> list[dict]:
        job_id = int(filters.get("job_id") or 0)
        run_id = int(filters.get("run_id") or 0)
        return [
            copy.deepcopy(item)
            for item in self.file_events
            if (not job_id or int(item.get("job_id") or 0) == job_id)
            and (not run_id or int(item.get("run_id") or 0) == run_id)
        ]

    def list_jobs(self, *, status: str, **_kwargs) -> list[dict]:
        if str(self.job.get("status") or "") != status:
            return []
        return [copy.deepcopy(self.job)]

    def list_organizer_tasks_by_job(self, _job_id: int, **_kwargs) -> list[dict]:
        return copy.deepcopy(self.organizer_tasks)


class _FakeTimer:
    created: list["_FakeTimer"] = []

    def __init__(self, interval, function, args=()) -> None:
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def is_alive(self) -> bool:
        return self.started and not self.cancelled


def _job(*, attempts: int, status: str = "waiting_transfer") -> dict:
    return {
        "id": 42,
        "status": status,
        "category": "tv",
        "source_type": "quark",
        "target_route": "quark_to_mobile",
        "raw_data": {
            "staging_plan": _persisted_plan(42),
            "completion": {
                "stage": "waiting_transfer",
                "staging_retry_attempts": attempts,
            },
        },
    }


def _service(database: _FakeDatabase, *, max_attempts: int = 8) -> RcloneService:
    service = RcloneService.__new__(RcloneService)
    service.config = {
        "staging_enabled": True,
        "staging_retry_delay_seconds": 5,
        "staging_retry_max_delay_seconds": 20,
        "staging_retry_max_attempts": max_attempts,
    }
    service.db = database
    service.lock = threading.Lock()
    service._staging_retry_timers = {}
    service._staging_retry_attempts = {}
    service._append_log = lambda _message: None
    service.categories = {"tv": {"label": "电视剧", "fnos_lib": "电视剧"}}
    service._run_ready_handler = None
    service._pending_run_ready_results = []
    return service


def _completed_job(*, attempts: int = 0, status: str = "transferring") -> tuple[dict, list[dict]]:
    job = _job(attempts=attempts, status=status)
    source_path = "/旧夸克/电视剧/job-42/E01.mkv"
    target_path = "/旧移动云/_入库暂存/电视剧/job-42/E01.mkv"
    job["raw_data"]["rclone_staging_manifest"] = {
        "version": 1,
        "source_paths": [source_path],
        "expected_file_count": 1,
    }
    return job, [
        {
            "id": 1,
            "run_id": 19,
            "job_id": 42,
            "status": "done",
            "category": "tv",
            "source_path": source_path,
            "target_path": target_path,
        }
    ]


class RcloneStagingRetryLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeTimer.created = []

    def test_exhausted_attempts_move_job_to_review_without_timer(self) -> None:
        database = _FakeDatabase(_job(attempts=2))
        service = _service(database, max_attempts=2)

        with patch("fnos_media_import.services.rclone_service.threading.Timer", _FakeTimer):
            result = service._schedule_incomplete_staging_retry(
                _staging_run(42),
                run_id=9,
                exit_code=1,
            )

        self.assertTrue(result["review"])
        self.assertFalse(result["queued"])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("达到上限 2 次", database.job["error_message"])
        completion = database.job["raw_data"]["completion"]
        self.assertEqual(completion["staging_retry_attempts"], 2)
        self.assertTrue(completion["staging_retry_exhausted"])
        self.assertEqual(_FakeTimer.created, [])
        self.assertEqual(database.events[-1][1], EVENT_ERROR)
        self.assertTrue(database.events[-1][3]["terminal"])

    def test_new_service_continues_from_persisted_attempt_count(self) -> None:
        database = _FakeDatabase(_job(attempts=3))
        service = _service(database, max_attempts=8)

        with patch("fnos_media_import.services.rclone_service.threading.Timer", _FakeTimer):
            result = service._schedule_incomplete_staging_retry(
                _staging_run(42),
                run_id=10,
                exit_code=0,
            )

        self.assertEqual(result["attempt"], 4)
        self.assertEqual(database.job["raw_data"]["completion"]["staging_retry_attempts"], 4)
        self.assertEqual(_FakeTimer.created[-1].args[1], 4)
        self.assertTrue(_FakeTimer.created[-1].started)

    def test_manual_start_cancels_timer_without_clearing_persisted_attempts(self) -> None:
        database = _FakeDatabase(_job(attempts=5))
        service = _service(database)
        service.enabled = True
        service.worker_thread = None
        service.run_queue = SimpleNamespace(begin_direct_run=lambda: None)
        service.status_locked = lambda: {}
        service._start_worker_locked = lambda *_args, **_kwargs: None
        timer = _FakeTimer(30, lambda: None)
        timer.start()
        service._staging_retry_timers[42] = timer
        service._staging_retry_attempts[42] = 5

        result = service.start(reason="manual", staging_run=_staging_run(42))

        self.assertTrue(result["success"])
        self.assertTrue(timer.cancelled)
        self.assertEqual(database.job["raw_data"]["completion"]["staging_retry_attempts"], 5)
        self.assertEqual(service._staging_retry_attempts[42], 5)

    def test_startup_recovery_honors_persisted_limit_without_requeue(self) -> None:
        database = _FakeDatabase(_job(attempts=8))
        service = _service(database, max_attempts=8)
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertEqual(starts, [])
        self.assertEqual(result["exhausted_retry_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertEqual(database.job["raw_data"]["completion"]["staging_retry_attempts"], 8)

    def test_startup_complete_evidence_is_handed_to_organizer_before_retry_exhaustion(self) -> None:
        job, file_events = _completed_job(attempts=8, status="transferring")
        database = _FakeDatabase(job, file_events=file_events)
        service = _service(database, max_attempts=8)
        starts: list[dict] = []
        dispatches: list[tuple[dict, dict]] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}
        service._run_ready_handler = (
            lambda refresh, payload: dispatches.append((refresh, payload))
            or {"success": True, "items": [{"success": True, "task_id": 91}]}
        )

        result = service.recover_unstarted_staging_jobs()

        self.assertEqual(starts, [])
        self.assertEqual(result["completed_evidence_job_ids"], [42])
        self.assertEqual(result["organizer_recovered_job_ids"], [42])
        self.assertEqual(result["exhausted_retry_job_ids"], [])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(database.job["status"], "waiting_organizer")
        self.assertEqual(database.job["raw_data"]["completion"]["stage"], "waiting_organizer")
        self.assertEqual(dispatches[0][0]["completed_items"][0]["target_paths"], [file_events[0]["target_path"]])

    def test_retry_callback_with_complete_evidence_hands_off_instead_of_only_clearing_timer(self) -> None:
        job, file_events = _completed_job(attempts=2)
        database = _FakeDatabase(job, file_events=file_events)
        service = _service(database)
        dispatches: list[tuple[dict, dict]] = []
        service._run_ready_handler = (
            lambda refresh, payload: dispatches.append((refresh, payload))
            or {"success": True, "items": [{"success": True, "task_id": 92}]}
        )

        with patch("fnos_media_import.services.rclone_service.threading.Timer", _FakeTimer):
            result = service._schedule_incomplete_staging_retry(
                _staging_run(42),
                run_id=19,
                exit_code=0,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["skipped"])
        self.assertEqual(_FakeTimer.created, [])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(database.job["status"], "waiting_organizer")

    def test_waiting_organizer_without_task_is_still_handed_off_from_complete_evidence(self) -> None:
        job, file_events = _completed_job(status="transferring")
        database = _FakeDatabase(job, file_events=file_events)
        database.job["status"] = "waiting_organizer"
        service = _service(database)
        dispatches: list[tuple[dict, dict]] = []
        service._run_ready_handler = (
            lambda refresh, payload: dispatches.append((refresh, payload))
            or {"success": True, "items": [{"success": True, "task_id": 93}]}
        )
        verdict = service._rclone_job_feasibility(job, file_events, 0)

        result = service._handoff_completed_staging_job_to_organizer(
            job,
            file_events,
            verdict,
            trigger="waiting_organizer_gap_recovery",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertEqual(len(dispatches), 1)

    def test_waiting_organizer_with_existing_task_skips_duplicate_handoff(self) -> None:
        job, file_events = _completed_job(status="transferring")
        database = _FakeDatabase(job, file_events=file_events, organizer_tasks=[{"id": 91}])
        database.job["status"] = "waiting_organizer"
        service = _service(database)
        dispatches: list[tuple[dict, dict]] = []
        service._run_ready_handler = lambda refresh, payload: dispatches.append((refresh, payload))
        verdict = service._rclone_job_feasibility(job, file_events, 0)

        result = service._handoff_completed_staging_job_to_organizer(
            job,
            file_events,
            verdict,
            trigger="waiting_organizer_existing_task",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["skipped"])
        self.assertEqual(dispatches, [])

    def test_startup_recovery_moves_invalid_enabled_plan_to_review(self) -> None:
        job = _job(attempts=0)
        job["raw_data"]["staging_plan"].pop("storage_backend")
        database = _FakeDatabase(job)
        service = _service(database)
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertFalse(result["success"])
        self.assertEqual(result["invalid_plan_review_job_ids"], [42])
        self.assertEqual(starts, [])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("staging_plan 无效", database.job["error_message"])

    def test_startup_recovery_requeues_failed_staging_job_with_valid_plan(self) -> None:
        database = _FakeDatabase(_job(attempts=1, status="failed"))
        service = _service(database)
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True, "queued": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertTrue(result["success"])
        self.assertEqual(result["requeued_job_ids"], [42])
        self.assertEqual(len(starts), 1)

    def test_provider_failed_job_without_rclone_evidence_is_not_requeued(self) -> None:
        job = _job(attempts=0, status="failed")
        job["raw_data"]["completion"] = {
            "stage": "failed",
            "message": "Quark 分享链接检测失败或资源已失效",
        }
        database = _FakeDatabase(job)
        service = _service(database)
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertTrue(result["success"])
        self.assertEqual(result["failed_without_staging_evidence_job_ids"], [42])
        self.assertEqual(result["requeued_job_ids"], [])
        self.assertEqual(starts, [])
        self.assertEqual(database.job["status"], "failed")

    def test_shutdown_fence_prevents_retry_timer_recreation(self) -> None:
        database = _FakeDatabase(_job(attempts=1))
        service = _service(database)
        service._shutdown_requested = True

        with patch("fnos_media_import.services.rclone_service.threading.Timer", _FakeTimer):
            result = service._schedule_incomplete_staging_retry(
                _staging_run(42),
                run_id=18,
                exit_code=1,
            )

        self.assertTrue(result["skipped"])
        self.assertEqual(_FakeTimer.created, [])
        self.assertEqual(database.job["raw_data"]["completion"]["staging_retry_attempts"], 1)


class Cloud139SubmittedStartupRecoveryTests(unittest.TestCase):
    def test_submitted_native_staging_save_is_handed_to_organizer_without_provider_retry(self) -> None:
        plan = _persisted_plan(42)
        plan.update(
            {
                "route": "cloud139_direct",
                "storage_backend": "cmcc_api",
                "provider_target_path": plan["storage_job_root"],
            }
        )
        job = {
            "id": 42,
            "status": "submitted",
            "category": "tv",
            "source_type": "cloud139",
            "target_route": "cloud139_direct",
            "raw_data": {
                "provider": "cmcc_native",
                "save": {"success": True},
                "staging_plan": plan,
            },
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            @staticmethod
            def list_jobs(**filters) -> list[dict]:
                return [copy.deepcopy(job)] if filters.get("status") == "submitted" else []

            @staticmethod
            def get_job(_job_id: int) -> dict:
                return copy.deepcopy(job)

            def add_event(self, *values) -> None:
                self.events.append(values)

        database = FakeDatabase()
        service = RcloneService.__new__(RcloneService)
        service.db = database
        dispatches: list[tuple] = []
        service._direct_ready_handler = (
            lambda result, reason: dispatches.append((result, reason))
            or {"success": True, "queued": True, "task_id": 9}
        )

        result = service.recover_submitted_cloud139_staging_dispatches()

        self.assertEqual(result["recovered_job_ids"], [42])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0][1], "startup_recover_cloud139_staging")
        self.assertEqual(dispatches[0][0]["job"]["id"], 42)
        self.assertIn("补投 Organizer", database.events[-1][2])

    def test_invalid_enabled_cloud139_plan_moves_submitted_job_to_review(self) -> None:
        plan = _persisted_plan(42)
        plan.update(
            {
                "route": "cloud139_direct",
                "storage_backend": "cmcc_api",
                "provider_target_path": plan["storage_job_root"],
            }
        )
        plan.pop("openlist_job_root")
        job = {
            "id": 42,
            "status": "submitted",
            "category": "tv",
            "source_type": "cloud139",
            "target_route": "cloud139_direct",
            "raw_data": {
                "provider": "cmcc_native",
                "save": {"success": True},
                "staging_plan": plan,
            },
        }
        database = _FakeDatabase(job)
        service = _service(database)
        service._direct_ready_handler = lambda *_args: {"success": True, "queued": True}

        result = service.recover_submitted_cloud139_staging_dispatches()

        self.assertFalse(result["success"])
        self.assertEqual(result["review_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("staging_plan 无效", database.job["error_message"])


class WaitingOrganizerInvalidPlanRecoveryTests(unittest.TestCase):
    def test_invalid_enabled_plan_moves_waiting_organizer_job_to_review(self) -> None:
        job = _job(attempts=0, status="waiting_organizer")
        job["raw_data"]["staging_plan"].pop("storage_job_root")
        database = _FakeDatabase(job)
        service = _service(database)

        result = service.recover_waiting_organizer_dispatches()

        self.assertFalse(result["success"])
        self.assertEqual(result["review_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("staging_plan 无效", database.job["error_message"])

    def test_waiting_organizer_recovery_requires_explicit_dispatch_success(self) -> None:
        job, file_events = _completed_job(status="waiting_organizer")
        database = _FakeDatabase(job, file_events=file_events)
        service = _service(database)
        service._run_ready_handler = lambda *_args: None

        result = service.recover_waiting_organizer_dispatches()

        self.assertFalse(result["success"])
        self.assertEqual(result["review_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("未返回有效结果", database.job["error_message"])

    def test_legacy_direct_jobs_without_staging_plan_are_recovered(self) -> None:
        for source_type, route in (("cloud139", "cloud139_direct"), ("magnet", "sixpan_offline")):
            job = {
                "id": 42,
                "status": "waiting_organizer",
                "category": "tv",
                "source_type": source_type,
                "target_route": route,
                "target_path": "/历史直转/电视剧/测试剧",
                "raw_data": {
                    "completion": {
                        "stage": "waiting_organizer",
                        "organizer_scan_path": "/OpenList/电视剧/测试剧",
                    }
                },
            }
            database = _FakeDatabase(job)
            service = _service(database)
            calls: list[tuple[dict, str]] = []
            service._direct_ready_handler = lambda result, reason: (
                calls.append((result, reason))
                or {"success": True, "queued": True, "task_id": 88}
            )

            result = service.recover_waiting_organizer_dispatches()

            self.assertEqual(result["recovered_job_ids"], [42])
            self.assertEqual(calls[0][0]["job"]["target_route"], route)
            self.assertEqual(calls[0][1], "waiting_organizer_startup_recovery")

    def test_legacy_direct_job_without_recoverable_directory_moves_to_review(self) -> None:
        job = {
            "id": 42,
            "status": "waiting_organizer",
            "category": "tv",
            "source_type": "magnet",
            "target_route": "sixpan_offline",
            "target_path": "",
            "raw_data": {},
        }
        database = _FakeDatabase(job)
        service = _service(database)
        service._direct_ready_handler = lambda *_args: {
            "success": False,
            "message": "缺少 OpenList 可扫描路径",
        }

        result = service.recover_waiting_organizer_dispatches()

        self.assertFalse(result["success"])
        self.assertEqual(result["review_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("缺少 OpenList 可扫描路径", database.job["error_message"])

    def test_legacy_quark_job_without_staging_plan_requires_manual_review(self) -> None:
        job = {
            "id": 42,
            "status": "waiting_organizer",
            "category": "tv",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "target_path": "/共享电视剧目录",
            "raw_data": {},
        }
        database = _FakeDatabase(job)
        service = _service(database)
        service._direct_ready_handler = lambda *_args: self.fail(
            "缺少 staging_plan 的历史夸克任务不能按直转任务扫描共享目录"
        )

        result = service.recover_waiting_organizer_dispatches()

        self.assertFalse(result["success"])
        self.assertEqual(result["review_job_ids"], [42])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertIn("历史夸克任务缺少 staging_plan", database.job["error_message"])


if __name__ == "__main__":
    unittest.main()
