from __future__ import annotations

import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.organizer.service import OrganizerService
from tests._repository_v1_fakes import RepositoryFakeMixin
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    bind_task_openlist_resolver,
    organizer_v1_job,
    organizer_v1_plan,
)


def _create_import_job(
    database: Database,
    suffix: str,
    *,
    organizer_root: str | None = None,
    strategy: str = "native_save",
) -> tuple[int, dict]:
    job_id, created = database.job_commands.create(
        {
            "title": f"Atomic organizer task {suffix}",
            "category": "tv",
            "category_label": "TV",
            "source_provider_kind": "quark",
            "source_form": "share",
            "source_url": f"source-record:organizer-task-{suffix}",
            "target_path": "/openlist/tv",
            "status": "waiting_organizer",
            "destination_id": "native-main",
            "plan_version": 1,
            "idempotency_key": f"organizer-task-idempotency:{suffix}",
        }
    )
    if not created:
        raise AssertionError("test import job was unexpectedly reused")
    plan = organizer_v1_plan(
        job_id,
        category="tv",
        organizer_root=organizer_root or f"/openlist/staging/tv/job-{job_id}",
        destination_final="/openlist/tv",
        strategy=strategy,
    )
    return job_id, plan


def _task_values(job_id: int, plan: dict) -> dict:
    return attach_organizer_v1_task({
        "job_id": job_id,
        "category": "tv",
        "category_label": "TV",
        "title": "Atomic organizer task",
        "source_keyword": "Atomic organizer task",
        "openlist_root_path": plan["paths"]["organizer"]["path"],
        "trigger_type": "direct_import_done",
        "status": "stabilizing",
        "evidence": {"job": {"id": job_id}},
        "raw_data": {"job_id": job_id},
    }, plan)


class OrganizerTaskDatabaseIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-task-idempotency-{uuid.uuid4().hex}.db"
        self._database_infrastructure = Database(self.db_path)
        self._database_infrastructure.init_schema()
        self.database = RepositorySet.from_database(self._database_infrastructure)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_two_database_instances_create_only_one_task_for_the_same_job(self) -> None:
        job_id, plan = _create_import_job(self.database, uuid.uuid4().hex)
        databases = [
            RepositorySet.from_database(Database(self.db_path)),
            RepositorySet.from_database(Database(self.db_path)),
        ]
        start = threading.Barrier(2)
        results: list[tuple[int, bool]] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def create_task(database: RepositorySet) -> None:
            try:
                start.wait(timeout=5)
                result = database.organizer.get_or_create_organizer_task_for_job(**_task_values(job_id, plan))
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
        self.assertEqual(len(self.database.organizer.list_organizer_tasks_by_job(job_id, limit=10)), 1)

    def test_historical_duplicate_rows_are_left_unchanged_and_the_newest_is_reused(self) -> None:
        job_id, plan = _create_import_job(self.database, uuid.uuid4().hex)
        values = _task_values(job_id, plan)
        first_id = self.database.organizer.create_organizer_task(**values)
        second_id = self.database.organizer.create_organizer_task(**values)

        task_id, created = RepositorySet.from_database(
            Database(self.db_path)
        ).organizer.get_or_create_organizer_task_for_job(**values)

        self.assertFalse(created)
        self.assertEqual(task_id, second_id)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(self.database.organizer.list_organizer_tasks_by_job(job_id, limit=10)), 2)

    def test_done_task_repairs_linked_job_after_completion_crash_window(self) -> None:
        job_id, plan = _create_import_job(self.database, uuid.uuid4().hex)
        self.database.job_commands.update(job_id, status="confirming")
        task_id = self.database.organizer.create_organizer_task(
            **{
                **_task_values(job_id, plan),
                "status": "done",
                "evidence": {
                    "completion_confirmation": {
                        "success": True,
                        "organized_target_path": "/媒体库/测试剧 (2026)",
                        "target_dirs": ["/媒体库/测试剧 (2026)"],
                    }
                },
                "raw_data": {
                    **_task_values(job_id, plan)["raw_data"],
                    "strm_completion": {
                        "status": "refresh_accepted",
                        "handled_by": "openlist",
                    }
                },
            }
        )
        cancelled_job_id, cancelled_plan = _create_import_job(self.database, uuid.uuid4().hex)
        self.database.job_commands.update(cancelled_job_id, status="cancelled")
        self.database.organizer.create_organizer_task(
            **{
                **_task_values(cancelled_job_id, cancelled_plan),
                "status": "done",
                "evidence": {"completion_confirmation": {"success": True}},
            }
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        bind_organizer_v1_execution_repository(service, plan, cancelled_plan)

        result = service._recover_completed_linked_jobs_on_startup()

        repaired = self.database.job_queries.get(job_id)
        self.assertIn(task_id, result["recovered_task_ids"])
        self.assertEqual(repaired["status"], "done")
        completion = repaired["raw_data"]["completion"]
        self.assertEqual(completion["organized_target_path"], "/媒体库/测试剧 (2026)")
        self.assertEqual(completion["target_dirs"], ["/媒体库/测试剧 (2026)"])
        self.assertEqual(completion["strm_completion"]["handled_by"], "openlist")
        self.assertEqual(self.database.job_queries.get(cancelled_job_id)["status"], "cancelled")


class OrganizerTaskServiceIdempotencyTests(unittest.TestCase):
    class AtomicReuseDatabase(RepositoryFakeMixin):
        def __init__(self, task_id: int, job: dict, task: dict) -> None:
            self.task_id = task_id
            self.job = job
            self.task = task
            self.job_lookups = 0
            self.atomic_calls: list[dict] = []

        def list_organizer_tasks_by_job(self, _job_id: int, limit: int = 1) -> list[dict]:
            self.job_lookups += 1
            if self.job_lookups == 1:
                return []
            return [dict(self.task)]

        def get_job(self, _job_id: int) -> dict:
            return dict(self.job)

        def get_organizer_task(self, _task_id: int, include_children: bool = False) -> dict:
            return dict(self.task)

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

    def _service(self, database: AtomicReuseDatabase, plan: dict) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "TV"}}
        bind_task_openlist_resolver(service, None, plan)
        service._mark_linked_job_waiting = lambda _task_id: self.fail("reused task must not mark the job again")
        service._schedule = lambda _task_id: self.fail("reused task must not be scheduled again")
        service._schedule_initial_openlist_visibility_wait = (
            lambda _task_id: self.fail("reused task must not schedule a visibility wait")
        )
        return service

    def test_rclone_callback_does_not_reschedule_an_atomically_reused_task(self) -> None:
        plan = organizer_v1_plan(
            81,
            category="tv",
            organizer_root="/openlist/staging/tv/job-81",
            destination_final="/openlist/tv",
            strategy="rclone_copy",
            source_landing="/quark/staging/tv/job-81",
            destination_staging="/openlist/staging/tv/job-81",
        )
        job = organizer_v1_job(plan, status="waiting_organizer", title="Atomic callback")
        task = attach_organizer_v1_task(
            {"id": 31, "status": "waiting_openlist"},
            plan,
        )
        database = self.AtomicReuseDatabase(task_id=31, job=job, task=task)
        service = self._service(database, plan)

        result = service.enqueue_from_rclone_callback(
            run_id=7,
            job=job,
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
        plan = organizer_v1_plan(
            82,
            category="tv",
            organizer_root="/openlist/staging/tv/job-82",
            destination_final="/openlist/tv",
        )
        job = organizer_v1_job(plan, status="waiting_organizer", title="Atomic direct import")
        task = attach_organizer_v1_task(
            {"id": 32, "status": "waiting_openlist"},
            plan,
        )
        database = self.AtomicReuseDatabase(task_id=32, job=job, task=task)
        service = self._service(database, plan)

        result = service.enqueue_from_completed_directory(
            job=job,
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
        self._database_infrastructure = Database(self.db_path)
        self._database_infrastructure.init_schema()
        self.database = RepositorySet.from_database(self._database_infrastructure)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_different_jobs_never_reuse_the_same_root_task_record(self) -> None:
        shared_root = "/openlist/legacy/shared-title"
        first_job_id, first_plan = _create_import_job(
            self.database,
            uuid.uuid4().hex,
            organizer_root=shared_root,
        )
        second_job_id, second_plan = _create_import_job(
            self.database,
            uuid.uuid4().hex,
            organizer_root=shared_root,
        )
        first_task_id = self.database.organizer.create_organizer_task(
            **{
                **_task_values(first_job_id, first_plan),
                "openlist_root_path": shared_root,
            }
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "TV"}}
        service._schedule_initial_openlist_visibility_wait = lambda _task_id: False
        service._schedule = lambda _task_id: None
        bind_task_openlist_resolver(service, None, first_plan, second_plan)

        result = service.enqueue_from_completed_directory(
            job=self.database.job_queries.get(second_job_id),
            category_label="TV",
            root_path=shared_root,
            payload={},
        )

        self.assertTrue(result["success"])
        self.assertNotEqual(result["task_id"], first_task_id)
        second_tasks = self.database.organizer.list_organizer_tasks_by_job(second_job_id, limit=10)
        self.assertEqual([item["id"] for item in second_tasks], [result["task_id"]])


class OrganizerRollbackPaginationTests(unittest.TestCase):
    def test_run_is_loaded_by_identity_for_rollback(self) -> None:
        plan = organizer_v1_plan(
            99,
            category="tv",
            organizer_root="/openlist/staging/tv/job-99",
            destination_final="/openlist/tv",
        )
        task = attach_organizer_v1_task(
            {"id": 99, "status": "done"},
            plan,
        )
        run = {
            "id": 7,
            "task_id": 99,
            "undo_data": [
                {"type": "move_file", "source_path": "/openlist/tv/Show/E01.mkv", "target_path": "/openlist/staging/tv/job-99/E01.mkv"}
            ],
        }

        class FakeDatabase(RepositoryFakeMixin):
            @staticmethod
            def get_organizer_run(run_id: int) -> dict | None:
                return dict(run) if run_id == 7 else None

            @staticmethod
            def get_organizer_task(_task_id: int, include_children: bool = True) -> dict:
                return task

            @staticmethod
            def get_job(_job_id: int) -> dict:
                return organizer_v1_job(plan)

            @staticmethod
            def claim_organizer_rollback(source_run_id: int, **_kwargs):
                assert source_run_id == 7
                task["status"] = "executing"
                return 8, {"success": True, "completed_count": 0}

            @staticmethod
            def renew_organizer_run(*_args, **_kwargs):
                return True

            owns_organizer_run = renew_organizer_run
            acquire_organizer_lock = renew_organizer_run
            update_organizer_rollback_progress = renew_organizer_run

            @staticmethod
            def release_organizer_locks(**_kwargs):
                return None

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {}
        bind_task_openlist_resolver(service, SimpleNamespace(configured=True), plan)
        inverses: list[dict] = []
        service._execute_rollback_inverse = lambda item, *_args: inverses.append(item)
        service._sync_linked_guest_requests = lambda *_args, **_kwargs: 0

        result = service.rollback_run(7)

        self.assertTrue(result["success"])
        self.assertEqual(result["done"], 1)
        self.assertEqual(inverses, run["undo_data"])


class OrganizerStagingTaskReuseSafetyTests(unittest.TestCase):
    class MismatchedTaskDatabase(RepositoryFakeMixin):
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
            self.job.update(updates)

        def update_job_if_status(
            self,
            job_id: int,
            _expected_statuses: set[str],
            **updates,
        ) -> bool:
            self.update_job(job_id, **updates)
            return True

        def add_event(self, *args) -> None:
            self.events.append(args)

    @staticmethod
    def _job(*, strategy: str = "native_save") -> tuple[dict, dict]:
        plan = organizer_v1_plan(
            42,
            category="tv",
            organizer_root="/openlist/staging/tv/job-42",
            destination_final="/openlist/tv",
            strategy=strategy,
            source_landing="/quark/staging/tv/job-42" if strategy == "rclone_copy" else None,
            destination_staging="/openlist/staging/tv/job-42" if strategy == "rclone_copy" else None,
        )
        return organizer_v1_job(plan, status="waiting_organizer", title="藏海传"), plan

    @staticmethod
    def _task(plan: dict, *, root: str | None = None) -> dict:
        task = attach_organizer_v1_task(
            {
                "id": 71,
                "status": "waiting_openlist",
            },
            plan,
        )
        if root is not None:
            task["openlist_root_path"] = root
        return task

    def _service(self, database: MismatchedTaskDatabase, plan: dict) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"tv": {"label": "电视剧"}}
        bind_task_openlist_resolver(service, None, plan)
        return service

    def test_completed_directory_rejects_mismatched_existing_staging_task(self) -> None:
        job, plan = self._job()
        database = self.MismatchedTaskDatabase(job, self._task(plan, root="/旧挂载/电视剧"))
        service = self._service(database, plan)

        result = service.enqueue_from_completed_directory(
            job=job,
            category_label="电视剧",
            root_path=plan["paths"]["organizer"]["path"],
            payload={},
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["queued"])
        self.assertIn("扫描路径与 V1 execution_plan 不一致", result["message"])
        self.assertEqual(database.task_updates[-1][1]["status"], "waiting_review")
        self.assertEqual(database.job_updates[-1][1]["status"], "review")

    def test_rclone_callback_rejects_mismatched_existing_staging_task(self) -> None:
        job, plan = self._job(strategy="rclone_copy")
        database = self.MismatchedTaskDatabase(job, self._task(plan, root="/旧挂载/电视剧"))
        service = self._service(database, plan)
        job_root = plan["paths"]["organizer"]["path"]

        result = service.enqueue_from_rclone_callback(
            run_id=9,
            job=job,
            category_label="电视剧",
            filename="E01.mkv",
            source_path="/旧夸克/电视剧/job-42/E01.mkv",
            target_path=f"{job_root}/E01.mkv",
            payload={},
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["queued"])
        self.assertIn("扫描路径与 V1 execution_plan 不一致", result["message"])
        self.assertEqual(database.task_updates[-1][1]["status"], "waiting_review")
        self.assertEqual(database.job_updates[-1][1]["status"], "review")

    def test_completed_directory_reuses_matching_staging_task(self) -> None:
        job, plan = self._job()
        task = self._task(plan)
        database = self.MismatchedTaskDatabase(job, task)
        service = self._service(database, plan)

        result = service.enqueue_from_completed_directory(
            job=job,
            category_label="电视剧",
            root_path=plan["paths"]["organizer"]["path"],
            payload={},
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["task_id"], task["id"])
        self.assertEqual(database.task_updates, [])
        self.assertEqual(database.job_updates, [])


if __name__ == "__main__":
    unittest.main()
