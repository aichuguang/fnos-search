from __future__ import annotations

import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService
from tests.test_rclone_persisted_staging_plan import _persisted_plan


def _create_import_job(database: Database, suffix: str) -> int:
    job_id, created = database.create_job(
        {
            "title": f"Atomic organizer task {suffix}",
            "category": "tv",
            "category_label": "TV",
            "source_type": "quark",
            "source_url": f"https://pan.quark.cn/s/{suffix}",
            "target_route": "quark_to_mobile",
            "target_path": f"/mobile/staging/tv/job-{suffix}",
            "status": "waiting_organizer",
            "idempotency_key": f"organizer-task-idempotency:{suffix}",
        }
    )
    if not created:
        raise AssertionError("test import job was unexpectedly reused")
    return job_id


def _task_values(job_id: int) -> dict:
    return {
        "job_id": job_id,
        "category": "tv",
        "category_label": "TV",
        "title": "Atomic organizer task",
        "source_keyword": "Atomic organizer task",
        "openlist_root_path": f"/openlist/staging/tv/job-{job_id}",
        "trigger_type": "direct_import_done",
        "status": "stabilizing",
        "evidence": {"job": {"id": job_id}},
        "raw_data": {"job_id": job_id},
    }


class OrganizerTaskDatabaseIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-task-idempotency-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_two_database_instances_create_only_one_task_for_the_same_job(self) -> None:
        job_id = _create_import_job(self.database, uuid.uuid4().hex)
        databases = [Database(self.db_path), Database(self.db_path)]
        start = threading.Barrier(2)
        results: list[tuple[int, bool]] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def create_task(database: Database) -> None:
            try:
                start.wait(timeout=5)
                result = database.get_or_create_organizer_task_for_job(**_task_values(job_id))
                with result_lock:
                    results.append(result)
            except BaseException as exc:  # noqa: BLE001
                with result_lock:
                    errors.append(exc)

        callers = [threading.Thread(target=create_task, args=(database,)) for database in databases]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=10)

        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({task_id for task_id, _created in results}), 1)
        self.assertEqual(sorted(created for _task_id, created in results), [False, True])
        self.assertEqual(len(self.database.list_organizer_tasks_by_job(job_id, limit=10)), 1)

    def test_historical_duplicate_rows_are_left_unchanged_and_the_newest_is_reused(self) -> None:
        job_id = _create_import_job(self.database, uuid.uuid4().hex)
        values = _task_values(job_id)
        first_id = self.database.create_organizer_task(**values)
        second_id = self.database.create_organizer_task(**values)

        task_id, created = Database(self.db_path).get_or_create_organizer_task_for_job(**values)

        self.assertFalse(created)
        self.assertEqual(task_id, second_id)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(self.database.list_organizer_tasks_by_job(job_id, limit=10)), 2)

    def test_done_task_repairs_linked_job_after_completion_crash_window(self) -> None:
        job_id = _create_import_job(self.database, uuid.uuid4().hex)
        self.database.update_job(job_id, status="confirming")
        task_id = self.database.create_organizer_task(
            **{
                **_task_values(job_id),
                "status": "done",
                "evidence": {
                    "completion_confirmation": {
                        "success": True,
                        "organized_target_path": "/媒体库/测试剧 (2026)",
                        "target_dirs": ["/媒体库/测试剧 (2026)"],
                    }
                },
                "raw_data": {
                    "strm_completion": {
                        "status": "refresh_accepted",
                        "handled_by": "openlist",
                    }
                },
            }
        )
        cancelled_job_id = _create_import_job(self.database, uuid.uuid4().hex)
        self.database.update_job(cancelled_job_id, status="cancelled")
        self.database.create_organizer_task(
            **{
                **_task_values(cancelled_job_id),
                "status": "done",
                "evidence": {"completion_confirmation": {"success": True}},
            }
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database

        result = service._recover_completed_linked_jobs_on_startup()

        repaired = self.database.get_job(job_id)
        self.assertIn(task_id, result["recovered_task_ids"])
        self.assertEqual(repaired["status"], "done")
        completion = repaired["raw_data"]["completion"]
        self.assertEqual(completion["organized_target_path"], "/媒体库/测试剧 (2026)")
        self.assertEqual(completion["target_dirs"], ["/媒体库/测试剧 (2026)"])
        self.assertEqual(completion["strm_completion"]["handled_by"], "openlist")
        self.assertEqual(self.database.get_job(cancelled_job_id)["status"], "cancelled")


class OrganizerTaskServiceIdempotencyTests(unittest.TestCase):
    class AtomicReuseDatabase:
        def __init__(self, task_id: int) -> None:
            self.task_id = task_id
            self.job_lookups = 0
            self.atomic_calls: list[dict] = []

        def list_organizer_tasks_by_job(self, _job_id: int, limit: int = 1) -> list[dict]:
            self.job_lookups += 1
            if self.job_lookups == 1:
                return []
            return [{"id": self.task_id, "status": "waiting_openlist"}]

        @staticmethod
        def find_recent_organizer_task(_root_path: str, _category: str):
            return None

        @staticmethod
        def list_guest_requests_by_job(_job_id: int) -> list[dict]:
            return []

        def get_or_create_organizer_task_for_job(self, **values) -> tuple[int, bool]:
            self.atomic_calls.append(values)
            return self.task_id, False

        @staticmethod
        def create_organizer_task(**_values) -> int:
            raise AssertionError("atomic job task creator was bypassed")

    def _service(self, database: AtomicReuseDatabase) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "TV", "openlist_root_path": "/openlist/tv"}}
        service._mark_linked_job_waiting = lambda _task_id: self.fail("reused task must not mark the job again")
        service._schedule = lambda _task_id: self.fail("reused task must not be scheduled again")
        service._schedule_initial_openlist_visibility_wait = (
            lambda _task_id: self.fail("reused task must not schedule a visibility wait")
        )
        return service

    def test_rclone_callback_does_not_reschedule_an_atomically_reused_task(self) -> None:
        database = self.AtomicReuseDatabase(task_id=31)
        service = self._service(database)

        result = service.enqueue_from_rclone_callback(
            run_id=7,
            job={"id": 81, "category": "tv", "title": "Atomic callback"},
            category_label="TV",
            filename="E01.mkv",
            source_path="/quark/staging/tv/job-81/E01.mkv",
            target_path="/openlist/staging/tv/job-81/E01.mkv",
            payload={"allow_same_root_task": True},
        )

        self.assertEqual(result["task_id"], 31)
        self.assertEqual(result["status"], "waiting_openlist")
        self.assertEqual(len(database.atomic_calls), 1)
        self.assertEqual(database.atomic_calls[0]["job_id"], 81)
        self.assertEqual(database.atomic_calls[0]["rclone_run_id"], 7)

    def test_completed_directory_does_not_reschedule_an_atomically_reused_task(self) -> None:
        database = self.AtomicReuseDatabase(task_id=32)
        service = self._service(database)

        result = service.enqueue_from_completed_directory(
            job={"id": 82, "category": "tv", "title": "Atomic direct import"},
            category_label="TV",
            root_path="/openlist/staging/tv/job-82",
            payload={"allow_same_root_task": True},
        )

        self.assertEqual(result["task_id"], 32)
        self.assertEqual(result["status"], "waiting_openlist")
        self.assertEqual(len(database.atomic_calls), 1)
        self.assertEqual(database.atomic_calls[0]["job_id"], 82)
        self.assertEqual(database.atomic_calls[0]["trigger_type"], "direct_import_done")


class OrganizerCrossJobRootIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-cross-job-root-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_different_jobs_never_reuse_the_same_root_task_record(self) -> None:
        first_job_id = _create_import_job(self.database, uuid.uuid4().hex)
        second_job_id = _create_import_job(self.database, uuid.uuid4().hex)
        shared_root = "/openlist/legacy/shared-title"
        first_task_id = self.database.create_organizer_task(
            **{
                **_task_values(first_job_id),
                "openlist_root_path": shared_root,
            }
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "TV", "openlist_root_path": "/openlist/tv"}}
        service._schedule_initial_openlist_visibility_wait = lambda _task_id: False
        service._schedule = lambda _task_id: None

        result = service.enqueue_from_completed_directory(
            job=self.database.get_job(second_job_id),
            category_label="TV",
            root_path=shared_root,
            payload={},
        )

        self.assertTrue(result["success"])
        self.assertNotEqual(result["task_id"], first_task_id)
        second_tasks = self.database.list_organizer_tasks_by_job(second_job_id, limit=10)
        self.assertEqual([item["id"] for item in second_tasks], [result["task_id"]])


class OrganizerRollbackPaginationTests(unittest.TestCase):
    def test_old_run_beyond_first_500_rows_can_be_rolled_back(self) -> None:
        pages = {
            0: [{"id": value, "undo_data": []} for value in range(1001, 501, -1)],
            500: [{"id": 7, "undo_data": [{"type": "move_file", "source_path": "/b", "target_path": "/a"}]}],
        }
        offsets: list[int] = []

        class FakeDatabase:
            @staticmethod
            def list_organizer_runs(*, limit: int, offset: int = 0) -> list[dict]:
                self.assertEqual(limit, 500)
                offsets.append(offset)
                return pages.get(offset, [])

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        inverses: list[dict] = []
        service._execute_inverse = lambda item: inverses.append(item)

        result = service.rollback_run(7)

        self.assertTrue(result["success"])
        self.assertEqual(result["done"], 1)
        self.assertEqual(offsets, [0, 500])
        self.assertEqual(inverses, pages[500][0]["undo_data"])


class OrganizerStagingTaskReuseSafetyTests(unittest.TestCase):
    class MismatchedTaskDatabase:
        def __init__(self, job: dict, task: dict) -> None:
            self.job = job
            self.task = task
            self.task_updates: list[tuple[int, dict]] = []
            self.job_updates: list[tuple[int, dict]] = []
            self.events: list[tuple] = []

        def list_organizer_tasks_by_job(self, _job_id: int, limit: int = 1) -> list[dict]:
            return [dict(self.task)]

        def update_organizer_task(self, task_id: int, **updates) -> None:
            self.task_updates.append((task_id, updates))

        def get_job(self, _job_id: int) -> dict:
            return dict(self.job)

        def update_job(self, job_id: int, **updates) -> None:
            self.job_updates.append((job_id, updates))

        def add_event(self, *args) -> None:
            self.events.append(args)

    @staticmethod
    def _job() -> dict:
        plan = _persisted_plan(42)
        return {
            "id": 42,
            "category": "tv",
            "title": "藏海传",
            "target_route": "quark_to_mobile",
            "target_path": plan["provider_target_path"],
            "status": "waiting_organizer",
            "raw_data": {"staging_plan": plan},
        }

    @staticmethod
    def _task() -> dict:
        return {
            "id": 71,
            "job_id": 42,
            "category": "tv",
            "status": "waiting_openlist",
            "openlist_root_path": "/旧挂载/电视剧",
            "raw_data": {},
        }

    def _service(self, database: MismatchedTaskDatabase) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/旧挂载/电视剧"}}
        return service

    def test_completed_directory_rejects_mismatched_existing_staging_task(self) -> None:
        job = self._job()
        database = self.MismatchedTaskDatabase(job, self._task())
        service = self._service(database)

        result = service.enqueue_from_completed_directory(
            job=job,
            category_label="电视剧",
            root_path=job["raw_data"]["staging_plan"]["openlist_job_root"],
            payload={"staging_plan": job["raw_data"]["staging_plan"]},
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["queued"])
        self.assertIn("staging_plan", result["message"])
        self.assertEqual(database.task_updates[-1][1]["status"], "waiting_review")
        self.assertEqual(database.job_updates[-1][1]["status"], "review")

    def test_rclone_callback_rejects_mismatched_existing_staging_task(self) -> None:
        job = self._job()
        database = self.MismatchedTaskDatabase(job, self._task())
        service = self._service(database)
        job_root = job["raw_data"]["staging_plan"]["openlist_job_root"]

        result = service.enqueue_from_rclone_callback(
            run_id=9,
            job=job,
            category_label="电视剧",
            filename="E01.mkv",
            source_path="/旧夸克/电视剧/job-42/E01.mkv",
            target_path=f"{job_root}/E01.mkv",
            payload={"staging_plan": job["raw_data"]["staging_plan"]},
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["queued"])
        self.assertIn("staging_plan", result["message"])
        self.assertEqual(database.task_updates[-1][1]["status"], "waiting_review")
        self.assertEqual(database.job_updates[-1][1]["status"], "review")

    def test_completed_directory_reuses_matching_staging_task(self) -> None:
        job = self._job()
        plan = job["raw_data"]["staging_plan"]
        task = {
            **self._task(),
            "openlist_root_path": plan["openlist_job_root"],
            "raw_data": {"staging_plan": plan},
        }
        database = self.MismatchedTaskDatabase(job, task)
        service = self._service(database)

        result = service.enqueue_from_completed_directory(
            job=job,
            category_label="电视剧",
            root_path=plan["openlist_job_root"],
            payload={"staging_plan": plan},
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["task_id"], task["id"])
        self.assertEqual(database.task_updates, [])
        self.assertEqual(database.job_updates, [])


if __name__ == "__main__":
    unittest.main()
