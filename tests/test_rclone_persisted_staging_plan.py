from __future__ import annotations

import copy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.constants import ROUTE_QUARK_TO_MOBILE
from fnos_media_import.services.import_completion_dispatcher import ImportCompletionDispatcher
from fnos_media_import.services.import_staging_service import ImportStagingService
from fnos_media_import.services.rclone_run_queue import RcloneRunQueue
from fnos_media_import.services.rclone_service import RcloneService


def _persisted_plan(job_id: int = 42) -> dict:
    return {
        "version": 2,
        "enabled": True,
        "route": ROUTE_QUARK_TO_MOBILE,
        "category": "tv",
        "category_label": "电视剧",
        "job_id": job_id,
        "job_dir_name": f"job-{job_id}",
        "provider_target_path": f"/旧夸克/电视剧/job-{job_id}",
        "quark_source_category_root": "/旧夸克/电视剧",
        "quark_job_root": f"/旧夸克/电视剧/job-{job_id}",
        "storage_backend": "cmcc_api",
        "storage_final_category_root": "旧移动云/电视剧",
        "storage_staging_category_root": "旧移动云/_入库暂存/电视剧",
        "storage_job_root": f"旧移动云/_入库暂存/电视剧/job-{job_id}",
        "openlist_final_category_root": "/旧挂载/电视剧",
        "openlist_staging_category_root": "/旧挂载/_入库暂存/电视剧",
        "openlist_job_root": f"/旧挂载/_入库暂存/电视剧/job-{job_id}",
        "openlist_refresh_prefix": "/飞牛NAS/电视剧",
    }


def _staging_run(job_id: int = 42) -> dict:
    return {
        "job_id": job_id,
        "category": "tv",
        "category_label": "电视剧",
        "job_dir_name": f"job-{job_id}",
        "source_category_root": "/旧夸克/电视剧",
        "storage_staging_category_root": "旧移动云/_入库暂存/电视剧",
        "storage_backend": "cmcc_api",
    }


class PersistedStagingPlanBuildTests(unittest.TestCase):
    def test_new_plan_persists_the_upload_backend(self) -> None:
        config = SimpleNamespace(
            raw={
                "organizer": {
                    "enabled": True,
                    "staging_enabled": True,
                    "staging_dir_name": "_入库暂存",
                },
                "openlist": {"base_url": "http://openlist.test"},
                "rclone": {"upload_backend": "cmcc_api"},
                "cmcc_upload": {"enabled": True, "backend": "cmcc_api"},
                "cloud139": {
                    "target_root_path": "旧移动云",
                    "fnos_mount_name": "旧挂载",
                },
            }
        )
        category = {
            "label": "电视剧",
            "quark_save_path": "/旧夸克/电视剧",
            "mobile_target_path": "旧 WebDAV/电视剧",
            "cloud139_target_path": "旧移动云/电视剧",
            "cloud139_fnos_target_path": "/旧挂载/电视剧",
        }

        plan = ImportStagingService(config).build(
            job_id=42,
            route=ROUTE_QUARK_TO_MOBILE,
            category_key="tv",
            category=category,
        )

        self.assertEqual(plan["storage_backend"], "cmcc_api")
        self.assertEqual(plan["storage_staging_category_root"], "旧移动云/_入库暂存/电视剧")


class PersistedStagingDispatcherTests(unittest.TestCase):
    class FakeDatabase:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        def add_event(self, *args) -> None:
            self.events.append(args)

    def test_dispatcher_passes_the_persisted_plan_instead_of_current_category_config(self) -> None:
        class FakeRclone:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def start(self, **kwargs) -> dict:
                self.calls.append(kwargs)
                return {"success": True, "message": "started"}

        database = self.FakeDatabase()
        rclone = FakeRclone()
        dispatcher = ImportCompletionDispatcher(
            database=database,
            rclone_service=rclone,
            category=lambda _key: {
                "label": "后台已改名",
                "fnos_lib": "后台新媒体库",
                "quark_save_path": "/新夸克/电视剧",
                "mobile_target_path": "新移动云/电视剧",
            },
            enqueue_organizer=lambda *_args: None,
        )
        job = {
            "id": 42,
            "category": "tv",
            "category_label": "电视剧",
            "source_type": "quark",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": _persisted_plan()},
        }

        result = dispatcher.start_rclone_for_job(job, "quark_completed")

        self.assertTrue(result["success"])
        self.assertEqual(rclone.calls[0]["category_filter"], "tv")
        self.assertEqual(rclone.calls[0]["staging_run"], _staging_run())

    def test_historical_job_without_a_plan_keeps_the_legacy_start_contract(self) -> None:
        class LegacyRclone:
            def __init__(self) -> None:
                self.call: tuple[str, str] | None = None

            def start(self, reason: str, category_filter: str) -> dict:
                self.call = (reason, category_filter)
                return {"success": True, "message": "legacy"}

        rclone = LegacyRclone()
        dispatcher = ImportCompletionDispatcher(
            database=self.FakeDatabase(),
            rclone_service=rclone,
            category=lambda _key: {"fnos_lib": "当前电视剧库"},
            enqueue_organizer=lambda *_args: None,
        )
        job = {
            "id": 7,
            "category": "tv",
            "category_label": "电视剧",
            "source_type": "quark",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {},
        }

        result = dispatcher.start_rclone_for_job(job, "legacy_completed")

        self.assertTrue(result["success"])
        self.assertEqual(rclone.call, ("legacy_completed", "当前电视剧库"))

    def test_enabled_but_incomplete_plan_is_rejected_instead_of_using_current_paths(self) -> None:
        class FakeRclone:
            def __init__(self) -> None:
                self.called = False

            def start(self, **_kwargs) -> dict:
                self.called = True
                return {"success": True}

        rclone = FakeRclone()
        dispatcher = ImportCompletionDispatcher(
            database=self.FakeDatabase(),
            rclone_service=rclone,
            category=lambda _key: {"fnos_lib": "当前电视剧库"},
            enqueue_organizer=lambda *_args: None,
        )
        broken_plan = _persisted_plan()
        broken_plan.pop("storage_backend")
        job = {
            "id": 42,
            "category": "tv",
            "source_type": "quark",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": broken_plan},
        }

        result = dispatcher.start_rclone_for_job(job, "broken_plan")

        self.assertFalse(result["success"])
        self.assertIn("暂存计划", result["message"])
        self.assertFalse(rclone.called)


class PersistedStagingQueueTests(unittest.TestCase):
    class QueueDatabase:
        def __init__(self, *jobs: dict) -> None:
            self.jobs = {int(job["id"]): copy.deepcopy(job) for job in jobs}
            self.events: list[tuple] = []

        def get_job(self, job_id: int) -> dict | None:
            job = self.jobs.get(int(job_id))
            return copy.deepcopy(job) if job else None

        def update_job(self, job_id: int, **changes) -> None:
            self.jobs[int(job_id)].update(copy.deepcopy(changes))

        def add_event(self, *values) -> None:
            self.events.append(values)

    @staticmethod
    def _job(job_id: int, *, status: str = "waiting_transfer", plan: dict | None = None) -> dict:
        return {
            "id": job_id,
            "status": status,
            "category": "tv",
            "source_type": "quark",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": copy.deepcopy(plan or _persisted_plan(job_id))},
        }

    def test_queued_run_copies_and_forwards_the_persisted_plan(self) -> None:
        queue = RcloneRunQueue()
        staging_run = _staging_run()
        queue.enqueue(
            reason="queued_job",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=staging_run,
        )
        staging_run["source_category_root"] = "/调用方后来修改的路径"

        captured: dict = {}
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service.run_queue = queue
        service.db = self.QueueDatabase(self._job(42))
        service._append_log = lambda _message: None

        def capture_start(reason, file_retry=None, category_filter="", staging_run=None) -> None:
            captured.update(
                reason=reason,
                file_retry=file_retry,
                category_filter=category_filter,
                staging_run=staging_run,
            )

        service._start_worker_locked = capture_start

        service._start_next_queued_run()

        self.assertEqual(captured["category_filter"], "tv")
        self.assertEqual(captured["staging_run"]["source_category_root"], "/旧夸克/电视剧")
        self.assertEqual(captured["staging_run"]["storage_backend"], "cmcc_api")

    def test_invalid_first_queued_plan_is_reviewed_without_blocking_next_valid_job(self) -> None:
        queue = RcloneRunQueue()
        queue.enqueue(
            reason="stale_job",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=_staging_run(42),
        )
        queue.enqueue(
            reason="valid_job",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:01",
            staging_run=_staging_run(43),
        )
        changed_plan = _persisted_plan(42)
        changed_plan.update(
            {
                "provider_target_path": "/新夸克/电视剧/job-42",
                "quark_source_category_root": "/新夸克/电视剧",
                "quark_job_root": "/新夸克/电视剧/job-42",
            }
        )
        database = self.QueueDatabase(
            self._job(42, plan=changed_plan),
            self._job(43),
        )
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service.run_queue = queue
        service.db = database
        service._append_log = lambda _message: None
        starts: list[dict] = []
        service._start_worker_locked = (
            lambda reason, file_retry=None, category_filter="", staging_run=None: starts.append(
                {
                    "reason": reason,
                    "file_retry": file_retry,
                    "category_filter": category_filter,
                    "staging_run": staging_run,
                }
            )
        )

        service._start_next_queued_run()

        self.assertEqual([item["reason"] for item in starts], ["valid_job"])
        self.assertEqual(starts[0]["staging_run"]["job_id"], 43)
        self.assertEqual(database.jobs[42]["status"], "review")
        self.assertIn("执行前复核失败", database.jobs[42]["error_message"])

    def test_cancelled_queued_job_is_not_started_or_changed_to_review(self) -> None:
        queue = RcloneRunQueue()
        queue.enqueue(
            reason="cancelled_job",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=_staging_run(42),
        )
        database = self.QueueDatabase(self._job(42, status="cancelled"))
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service.run_queue = queue
        service.db = database
        service._append_log = lambda _message: None
        starts: list[dict] = []
        service._start_worker_locked = lambda *_args, **_kwargs: starts.append({})

        service._start_next_queued_run()

        self.assertEqual(starts, [])
        self.assertEqual(database.jobs[42]["status"], "cancelled")

    def test_shutdown_clears_queue_and_prevents_followup_worker_start(self) -> None:
        queue = RcloneRunQueue()
        queue.enqueue(
            reason="queued_job",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=_staging_run(42),
        )
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service.run_queue = queue
        service.db = self.QueueDatabase(self._job(42))
        service._staging_retry_timers = {}
        service._staging_retry_attempts = {}
        service._shutdown_requested = False
        service.scheduler = SimpleNamespace(shutdown=lambda: None)
        service._append_log = lambda _message: None
        starts: list[dict] = []
        service._start_worker_locked = lambda *_args, **_kwargs: starts.append({})

        service.shutdown_scheduler()
        service._start_next_queued_run()

        self.assertEqual(starts, [])
        self.assertEqual(queue.snapshot()["queue_count"], 0)

    def test_empty_queue_releases_finishing_worker_before_accepting_new_work(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service.run_queue = RcloneRunQueue()
        service.worker_thread = threading.current_thread()
        service._shutdown_requested = False

        service._start_next_queued_run()

        self.assertIsNone(service.worker_thread)


class PersistedStagingStartGuardTests(unittest.TestCase):
    def test_generic_scan_is_rejected_while_task_staging_is_enabled(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.enabled = True
        service.config = {"staging_enabled": True}
        service._append_log = lambda _message: None
        service.status = lambda: {"running": False}

        for reason in ("schedule", "manual"):
            with self.subTest(reason=reason):
                result = service.start(reason=reason)

                self.assertFalse(result["success"])
                self.assertIn("缺少持久化任务计划", result["message"])

    @staticmethod
    def _service_for_bound_start(job: dict) -> tuple[RcloneService, list[dict]]:
        service = RcloneService.__new__(RcloneService)
        service.enabled = True
        service.config = {"staging_enabled": True}
        service.db = SimpleNamespace(get_job=lambda _job_id: job)
        service.lock = threading.Lock()
        service.worker_thread = None
        service.run_queue = SimpleNamespace(begin_direct_run=lambda: None)
        service._staging_retry_timers = {}
        service._staging_retry_attempts = {}
        service._append_log = lambda _message: None
        service.status = lambda: {"running": False}
        service.status_locked = lambda: {"running": False}
        starts: list[dict] = []
        service._start_worker_locked = (
            lambda reason, file_retry=None, category_filter="", staging_run=None: starts.append(
                {
                    "reason": reason,
                    "file_retry": file_retry,
                    "category_filter": category_filter,
                    "staging_run": staging_run,
                }
            )
        )
        return service, starts

    def test_start_rejects_caller_paths_that_do_not_match_database_plan(self) -> None:
        job = {
            "id": 42,
            "category": "tv",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": _persisted_plan(42)},
        }
        service, starts = self._service_for_bound_start(job)
        forged = {**_staging_run(42), "storage_staging_category_root": "其它移动云/_入库暂存/电视剧"}

        result = service.start(reason="forged", staging_run=forged)

        self.assertFalse(result["success"])
        self.assertIn("与数据库固化 staging_plan 不一致", result["message"])
        self.assertEqual(starts, [])

    def test_start_uses_parameters_rederived_from_database_plan(self) -> None:
        job = {
            "id": 42,
            "category": "tv",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": _persisted_plan(42)},
        }
        service, starts = self._service_for_bound_start(job)

        result = service.start(reason="persisted", staging_run=_staging_run(42))

        self.assertTrue(result["success"])
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["staging_run"]["source_category_root"], "/旧夸克/电视剧")
        self.assertEqual(
            starts[0]["staging_run"]["storage_staging_category_root"],
            "旧移动云/_入库暂存/电视剧",
        )


class PersistedStagingStartDeduplicationTests(unittest.TestCase):
    @staticmethod
    def _job(job_id: int) -> dict:
        return {
            "id": job_id,
            "status": "waiting_transfer",
            "category": "tv",
            "source_type": "quark",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": _persisted_plan(job_id)},
        }

    @classmethod
    def _service(cls, *job_ids: int, active_job_id: int) -> tuple[RcloneService, list[str]]:
        jobs = {job_id: cls._job(job_id) for job_id in job_ids}
        service = RcloneService.__new__(RcloneService)
        service.enabled = True
        service.config = {"staging_enabled": True}
        service.db = SimpleNamespace(get_job=lambda job_id: copy.deepcopy(jobs.get(int(job_id))))
        service.lock = threading.Lock()
        service.worker_thread = SimpleNamespace(is_alive=lambda: True)
        service.run_queue = RcloneRunQueue()
        service._active_staging_job_id = active_job_id
        service._active_full_staging_job_id = active_job_id
        service._reserved_full_staging_job_id = 0
        service._shutdown_requested = False
        service._staging_retry_timers = {}
        logs: list[str] = []
        service._append_log = logs.append
        service.status_locked = lambda: {
            "running": True,
            **service.run_queue.snapshot(),
        }
        return service, logs

    def test_active_same_job_is_reported_without_being_queued(self) -> None:
        service, logs = self._service(42, active_job_id=42)

        result = service.start(reason="idempotent_retry", staging_run=_staging_run(42))

        self.assertTrue(result["success"])
        self.assertFalse(result["queued"])
        self.assertTrue(result["already_running"])
        self.assertTrue(result["deduplicated"])
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 0)
        self.assertIn("已在运行", logs[-1])

    def test_same_job_already_queued_is_not_appended_twice(self) -> None:
        service, logs = self._service(41, 42, active_job_id=41)

        first = service.start(reason="first_retry", staging_run=_staging_run(42))
        duplicate = service.start(reason="second_retry", staging_run=_staging_run(42))

        self.assertTrue(first["queued"])
        self.assertTrue(duplicate["queued"])
        self.assertTrue(duplicate["already_queued"])
        self.assertTrue(duplicate["deduplicated"])
        snapshot = service.run_queue.snapshot()
        self.assertEqual(snapshot["queue_count"], 1)
        self.assertEqual([item["job_id"] for item in snapshot["queued_runs"]], [42])
        self.assertIn("忽略重复入队请求", logs[-1])

    def test_different_jobs_keep_fifo_order(self) -> None:
        service, _logs = self._service(41, 42, 43, active_job_id=41)

        first = service.start(reason="job_42", staging_run=_staging_run(42))
        second = service.start(reason="job_43", staging_run=_staging_run(43))

        self.assertTrue(first["queued"])
        self.assertTrue(second["queued"])
        snapshot = service.run_queue.snapshot()
        self.assertEqual(snapshot["queue_count"], 2)
        self.assertEqual([item["job_id"] for item in snapshot["queued_runs"]], [42, 43])

    def test_active_file_retry_does_not_suppress_full_staging_run(self) -> None:
        service, _logs = self._service(42, active_job_id=42)
        service._active_full_staging_job_id = 0

        result = service.start(reason="full_staging", staging_run=_staging_run(42))

        self.assertTrue(result["queued"])
        self.assertNotIn("already_running", result)
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 1)

    def test_file_retries_for_same_job_are_not_deduplicated(self) -> None:
        service, _logs = self._service(41, 42, active_job_id=41)
        first_retry = {"job_id": 42, "event_id": 1, "filename": "S01E01.mkv"}
        second_retry = {"job_id": 42, "event_id": 2, "filename": "S01E02.mkv"}

        first = service.start(
            reason="file_retry_1",
            file_retry=first_retry,
            staging_run=_staging_run(42),
        )
        second = service.start(
            reason="file_retry_2",
            file_retry=second_retry,
            staging_run=_staging_run(42),
        )

        self.assertTrue(first["queued"])
        self.assertTrue(second["queued"])
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 2)

    def test_popped_full_staging_run_remains_reserved_until_worker_start(self) -> None:
        service, _logs = self._service(41, 42, active_job_id=41)
        service.run_queue.enqueue(
            reason="queued_job_42",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-31T12:00:00",
            staging_run=_staging_run(42),
        )
        validation_started = threading.Event()
        allow_validation = threading.Event()
        original_revalidate = service._database_bound_staging_run
        validation_calls = 0
        validation_lock = threading.Lock()

        def blocking_revalidate(staging_run):
            nonlocal validation_calls
            with validation_lock:
                validation_calls += 1
                should_block = validation_calls == 1
            if should_block:
                validation_started.set()
                self.assertTrue(allow_validation.wait(timeout=2))
            return original_revalidate(staging_run)

        starts: list[int] = []
        service._database_bound_staging_run = blocking_revalidate
        service._start_worker_locked = lambda _reason, **kwargs: starts.append(
            int((kwargs.get("staging_run") or {}).get("job_id") or 0)
        )

        handoff = threading.Thread(target=service._start_next_queued_run)
        handoff.start()
        self.assertTrue(validation_started.wait(timeout=2))

        duplicate = service.start(reason="idempotent_retry", staging_run=_staging_run(42))

        self.assertTrue(duplicate["success"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertTrue(duplicate["starting"])
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 0)
        allow_validation.set()
        handoff.join(timeout=2)
        self.assertFalse(handoff.is_alive())
        self.assertEqual(starts, [42])


class PersistedStagingEnvironmentTests(unittest.TestCase):
    @staticmethod
    def _service_with_changed_runtime() -> RcloneService:
        service = RcloneService.__new__(RcloneService)
        service.config = {
            "upload_backend": "webdav",
            "staging_enabled": False,
            "staging_dir_name": "_新暂存",
        }
        service.categories = {
            "tv": {
                "label": "后台已改名",
                "quark_save_path": "/新夸克/电视剧",
                "mobile_target_path": "新 WebDAV/电视剧",
                "cloud139_target_path": "新移动云/电视剧",
            }
        }
        service.cmcc_upload_config = {"enabled": False, "backend": "webdav"}
        service.cloud139_config = {"target_root_path": "新移动云"}
        return service

    def test_env_uses_persisted_source_destination_backend_and_job_filter(self) -> None:
        service = self._service_with_changed_runtime()

        env = service._env(
            category_filter="后台新媒体库",
            trigger_reason="quark_completed",
            staging_run=_staging_run(),
        )

        self.assertEqual(env["RCLONE_SRC_TV_DIR"], "旧夸克/电视剧")
        self.assertEqual(env["RCLONE_DST_TV_DIR"], "旧移动云/_入库暂存/电视剧")
        self.assertEqual(env["RCLONE_UPLOAD_BACKEND"], "cmcc_api")
        self.assertEqual(env["CMCC_TARGET_TV_PARENT_PATH"], "旧移动云/_入库暂存/电视剧")
        self.assertEqual(env["RCLONE_ONLY_CATEGORY"], "tv")
        self.assertEqual(env["RCLONE_ONLY_JOB_DIR"], "job-42")
        self.assertEqual(env["RCLONE_STAGING_ENABLED"], "true")

    def test_file_retry_uses_exact_relative_source_path_instead_of_basename(self) -> None:
        service = self._service_with_changed_runtime()

        env = service._env(
            file_retry={
                "event_id": 19,
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/旧夸克/电视剧/job-42/版本A/E01.mkv",
            },
            staging_run=_staging_run(),
        )

        self.assertEqual(env["RCLONE_ONLY_FILE"], "job-42/版本A/E01.mkv")

    def test_env_without_a_plan_keeps_using_current_runtime_config(self) -> None:
        service = self._service_with_changed_runtime()

        env = service._env(category_filter="后台新媒体库")

        self.assertEqual(env["RCLONE_SRC_TV_DIR"], "新夸克/电视剧")
        self.assertEqual(env["RCLONE_DST_TV_DIR"], "新 WebDAV/电视剧")
        self.assertEqual(env["RCLONE_UPLOAD_BACKEND"], "webdav")
        self.assertEqual(env["RCLONE_ONLY_CATEGORY"], "后台新媒体库")
        self.assertEqual(env["RCLONE_ONLY_JOB_DIR"], "")
        self.assertEqual(env["RCLONE_STAGING_ENABLED"], "false")

    def test_invalid_explicit_run_cannot_fall_back_inside_env_generation(self) -> None:
        service = self._service_with_changed_runtime()

        with self.assertRaisesRegex(ValueError, "禁止按当前后台配置降级"):
            service._env(staging_run={"job_id": 42})

    def test_worker_contract_filters_the_exact_persisted_job_directory(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh").read_text(encoding="utf-8")

        self.assertIn('RCLONE_ONLY_JOB_DIR="${RCLONE_ONLY_JOB_DIR:-}"', script)
        self.assertIn('case "$staging_job_dir" in', script)
        self.assertIn('job-*) staging_job_id="${staging_job_dir#job-}"', script)
        self.assertIn('[ "$staging_job_dir" != "$RCLONE_ONLY_JOB_DIR" ]', script)
        self.assertIn('[ "$RCLONE_STAGING_ENABLED" = "true" ] && [ -z "$RCLONE_ONLY_JOB_DIR" ]', script)


if __name__ == "__main__":
    unittest.main()
