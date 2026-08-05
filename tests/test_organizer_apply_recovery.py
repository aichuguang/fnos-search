from __future__ import annotations

import threading
import sqlite3
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.organizer.run_lease import OrganizerRunLease


class _StartApplyDatabase:
    def __init__(self) -> None:
        self.task = {
            "id": 1,
            "status": "manual_confirmed",
            "category": "tv",
            "mappings": [],
        }
        self.events: list[tuple] = []
        self.run_updates: list[tuple[int, str, dict]] = []

    def get_organizer_task(self, _task_id: int, include_children: bool = True):
        return self.task

    def create_organizer_run(self, task_id: int, *, owner_id: str = "") -> int:
        self.events.append(("create_run", task_id, owner_id))
        return 71

    def update_organizer_task(self, task_id: int, **values) -> None:
        self.events.append(("update_task", task_id, values))
        self.task.update(values)

    def update_organizer_run(self, run_id: int, status: str, **values) -> None:
        self.run_updates.append((run_id, status, values))


def _start_apply_service(database: _StartApplyDatabase) -> OrganizerService:
    service = OrganizerService.__new__(OrganizerService)
    service.db = database
    service.owner_id = "test-owner"
    service._background_apply_lock = threading.Lock()
    service._background_apply_tasks = set()
    service._sync_linked_job = lambda *_args, **_kwargs: None
    return service


class OrganizerApplyStartupRecoveryTests(unittest.TestCase):
    def test_run_is_created_before_executing_and_passed_to_background_apply(self) -> None:
        database = _StartApplyDatabase()
        service = _start_apply_service(database)

        class CapturingThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                database.events.append(("thread_start", self.args))

        with patch("fnos_media_import.organizer.service.threading.Thread", CapturingThread):
            result = service.start_apply_task(1)

        self.assertTrue(result["success"])
        self.assertEqual(result["run_id"], 71)
        self.assertEqual(database.events[0], ("create_run", 1, "test-owner"))
        self.assertEqual(database.events[1][0:2], ("update_task", 1))
        self.assertEqual(database.events[1][2]["status"], "executing")
        self.assertEqual(database.events[2], ("thread_start", (1, 71)))

    def test_thread_start_failure_finishes_run_and_releases_in_memory_claim(self) -> None:
        database = _StartApplyDatabase()
        service = _start_apply_service(database)

        class FailingThread:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("cannot start thread")

        with (
            patch("fnos_media_import.organizer.service.threading.Thread", FailingThread),
            patch("fnos_media_import.organizer.service.logger.exception"),
        ):
            result = service.start_apply_task(1)

        self.assertFalse(result["success"])
        self.assertEqual(result["run_id"], 71)
        self.assertEqual(database.run_updates[-1][0:2], (71, "failed"))
        self.assertEqual(database.task["status"], "failed")
        self.assertNotIn(1, service._background_apply_tasks)


class OrganizerApplyRunReuseTests(unittest.TestCase):
    def test_apply_task_reuses_precreated_run(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 1,
                    "status": "executing",
                    "category": "tv",
                    "openlist_root_path": "/移动云/_入库暂存/电视剧/job-1",
                    "mappings": [],
                    "operations": [],
                    "evidence": {},
                }
                self.create_calls = 0
                self.run_updates: list[tuple[int, str, dict]] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = operations

            def create_organizer_run(self, _task_id: int, **_kwargs) -> int:
                self.create_calls += 1
                return 999

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.task.update(values)

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

            @staticmethod
            def release_organizer_locks(**_values) -> None:
                return None

        database = FakeDatabase()
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: []
        service._lock_keys = lambda _task: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        service._cleanup_source_empty_dirs_after_apply = lambda _task: {}
        service._confirm_standardized_targets = lambda _task: {
            "success": True,
            "organized_target_path": "/移动云/电视剧/测试剧",
            "target_dirs": [],
        }
        service._refresh_openlist_strm_for_task = lambda _task_id, _task: {}

        result = service.apply_task(1, run_id=71)

        self.assertTrue(result["success"])
        self.assertEqual(result["run_id"], 71)
        self.assertEqual(database.create_calls, 0)
        self.assertEqual(database.run_updates[-1][0:2], (71, "done"))

    def test_missing_task_finishes_precreated_run(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.run_updates: list[tuple[int, str, dict]] = []

            @staticmethod
            def get_organizer_task(_task_id: int, include_children: bool = True):
                return None

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

        database = FakeDatabase()
        service = OrganizerService.__new__(OrganizerService)
        service.db = database

        result = service.apply_task(1, run_id=71)

        self.assertFalse(result["success"])
        self.assertEqual(database.run_updates[-1][0:2], (71, "failed"))

    def test_review_early_return_finishes_precreated_run(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 1,
                    "status": "executing",
                    "category": "tv",
                    "openlist_root_path": "/移动云/_入库暂存/电视剧/job-1",
                    "mappings": [{"status": "conflict"}],
                    "operations": [],
                }
                self.run_updates: list[tuple[int, str, dict]] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = operations

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.task.update(values)

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

        database = FakeDatabase()
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.categories = {"tv": {"label": "电视剧"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None

        result = service.apply_task(1, run_id=71)

        self.assertFalse(result["success"])
        self.assertEqual(database.task["status"], "waiting_review")
        self.assertEqual(database.run_updates[-1][0:2], (71, "failed"))


class OrganizerApplyCrossProcessClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-claim-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_two_service_instances_submit_only_one_background_apply(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/移动云/_入库暂存/电视剧/job-88",
            status="manual_confirmed",
        )
        initial_read_barrier = threading.Barrier(2)

        class CoordinatedDatabase:
            def __init__(self, database: Database) -> None:
                self.database = database

            def get_organizer_task(self, requested_task_id: int, include_children: bool = True):
                task = self.database.get_organizer_task(requested_task_id, include_children=include_children)
                initial_read_barrier.wait(timeout=5)
                return task

            def __getattr__(self, name: str):
                return getattr(self.database, name)

        services = []
        background_started: list[tuple[int, int]] = []
        background_lock = threading.Lock()
        release_background = threading.Event()
        background_finished = threading.Event()
        for owner in ("process-a", "process-b"):
            service = _start_apply_service(CoordinatedDatabase(Database(self.db_path)))
            service.owner_id = owner

            def fake_apply(requested_task_id: int, *, run_id: int | None = None) -> dict:
                with background_lock:
                    background_started.append((requested_task_id, int(run_id or 0)))
                release_background.wait(timeout=5)
                background_finished.set()
                return {"success": True}

            service.apply_task = fake_apply
            services.append(service)

        results: list[dict] = []
        result_lock = threading.Lock()

        def submit(service: OrganizerService) -> None:
            result = service.start_apply_task(task_id)
            with result_lock:
                results.append(result)

        callers = [threading.Thread(target=submit, args=(service,)) for service in services]
        try:
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=10)

            self.assertTrue(all(not caller.is_alive() for caller in callers))
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.get("success") for result in results))
            self.assertEqual(len(background_started), 1)
            runs = self.database.list_organizer_runs(limit=10)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "running")
            self.assertEqual({result.get("run_id") for result in results}, {runs[0]["id"]})
            task = self.database.get_organizer_task(task_id, include_children=False)
            self.assertEqual(task["status"], "executing")
        finally:
            release_background.set()
            background_finished.wait(timeout=5)

    def test_finished_run_does_not_open_duplicate_completion_window(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/移动云/_入库暂存/电视剧/job-89",
            status="manual_confirmed",
        )
        run_id, active = self.database.claim_organizer_run(task_id, owner_id="process-a")
        self.assertIsNone(active)
        self.database.update_organizer_run(int(run_id or 0), "done")

        duplicate_run_id, claim_info = Database(self.db_path).claim_organizer_run(task_id, owner_id="process-b")

        self.assertIsNone(duplicate_run_id)
        self.assertEqual(claim_info["task_status"], "executing")
        self.assertEqual(len(self.database.list_organizer_runs(limit=10)), 1)

    def test_cancelled_and_skipped_tasks_cannot_be_claimed(self) -> None:
        for status in ("cancelled", "skipped"):
            with self.subTest(status=status):
                task_id = self.database.create_organizer_task(
                    category="tv",
                    openlist_root_path=f"/移动云/_入库暂存/电视剧/job-{status}",
                    status="manual_confirmed",
                )
                self.database.update_organizer_task(task_id, status=status)

                run_id, claim_info = Database(self.db_path).claim_organizer_run(
                    task_id,
                    owner_id="late-worker",
                )

                self.assertIsNone(run_id)
                self.assertEqual(claim_info["task_status"], status)
                task = self.database.get_organizer_task(task_id, include_children=False)
                self.assertEqual(task["status"], status)
                self.assertEqual(
                    [run for run in self.database.list_organizer_runs(limit=20) if run["task_id"] == task_id],
                    [],
                )

    def test_expired_run_is_atomically_replaced_by_only_one_concurrent_claim(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/移动云/_入库暂存/电视剧/job-90",
            status="manual_confirmed",
        )
        old_run_id, _active = self.database.claim_organizer_run(task_id, owner_id="old-process")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE organizer_runs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (old_run_id,),
            )
        barrier = threading.Barrier(2)
        claims: list[tuple[int | None, dict | None]] = []
        claim_lock = threading.Lock()

        def claim(owner: str) -> None:
            barrier.wait(timeout=5)
            result = Database(self.db_path).claim_organizer_run(task_id, owner_id=owner)
            with claim_lock:
                claims.append(result)

        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("new-a", "new-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(1 for run_id, _active in claims if run_id), 1)
        runs = self.database.list_organizer_runs(limit=10)
        self.assertEqual(sum(1 for run in runs if run["status"] == "running"), 1)
        self.assertEqual(next(run for run in runs if run["id"] == old_run_id)["status"], "failed")

    def test_expired_lock_from_other_task_is_recovered_before_acquire(self) -> None:
        old_task_id = self.database.create_organizer_task(category="tv", openlist_root_path="/job-91", status="manual_confirmed")
        old_run_id, _active = self.database.claim_organizer_run(old_task_id, owner_id="old-process")
        self.assertTrue(
            self.database.acquire_organizer_lock(
                "/移动云/电视剧",
                task_id=old_task_id,
                run_id=old_run_id,
                owner="old-process",
            )
        )
        new_task_id = self.database.create_organizer_task(category="tv", openlist_root_path="/job-92", status="manual_confirmed")
        new_run_id, _active = self.database.claim_organizer_run(new_task_id, owner_id="new-process")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE organizer_runs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (old_run_id,),
            )

        acquired = self.database.acquire_organizer_lock(
            "/移动云/电视剧",
            task_id=new_task_id,
            run_id=new_run_id,
            owner="new-process",
        )

        self.assertTrue(acquired)
        runs = self.database.list_organizer_runs(limit=10)
        self.assertEqual(next(run for run in runs if run["id"] == old_run_id)["status"], "failed")


class OrganizerRunLeaseTests(unittest.TestCase):
    def test_heartbeat_runs_during_work_and_stops_on_exit(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.renewals = 0

            def renew_organizer_run(self, _run_id: int, _owner_id: str, **_kwargs) -> bool:
                self.renewals += 1
                return True

            @staticmethod
            def owns_organizer_run(_run_id: int, _owner_id: str) -> bool:
                return True

        database = FakeDatabase()
        with OrganizerRunLease(
            database=database,
            run_id=1,
            owner_id="owner",
            lease_seconds=30,
            heartbeat_interval_seconds=0.05,
        ):
            time.sleep(0.18)
        stopped_at = database.renewals
        time.sleep(0.12)

        self.assertGreaterEqual(stopped_at, 3)
        self.assertEqual(database.renewals, stopped_at)

    def test_apply_stops_before_next_operation_when_lease_is_lost(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.owned = True
                self.operation_updates: list[int] = []
                self.task = {
                    "id": 1,
                    "status": "executing",
                    "category": "tv",
                    "openlist_root_path": "/移动云/_入库暂存/电视剧/job-1",
                    "mappings": [],
                    "operations": [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}],
                    "evidence": {},
                }

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            @staticmethod
            def renew_organizer_run(_run_id: int, _owner_id: str, **_kwargs) -> bool:
                return True

            def owns_organizer_run(self, _run_id: int, _owner_id: str) -> bool:
                return self.owned

            @staticmethod
            def replace_organizer_operations(_task_id: int, _operations: list[dict]) -> None:
                return None

            def update_organizer_operation(self, operation_id: int, **_values) -> None:
                self.operation_updates.append(operation_id)

            @staticmethod
            def update_organizer_task(_task_id: int, **_values) -> None:
                return None

            def finalize_organizer_run_and_task(self, _run_id: int, _task_id: int, **values) -> bool:
                self.task["status"] = values["task_status"]
                self.task["error_message"] = values.get("error_message") or ""
                return True

            @staticmethod
            def release_organizer_locks(**_values) -> None:
                return None

        database = FakeDatabase()
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service.owner_id = "owner"
        service.organizer_config = {}
        service.categories = {"tv": {"label": "电视剧"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: database.task["operations"]
        service._validate_staging_mapping_boundaries = lambda _task: None
        service._lock_keys = lambda _task: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        executed: list[int] = []

        def execute(operation: dict) -> dict:
            executed.append(int(operation["id"]))
            database.owned = False
            return {"type": "move"}

        service._execute_operation = execute

        result = service.apply_task(1, run_id=71)

        self.assertFalse(result["success"])
        self.assertEqual(executed, [1])
        self.assertEqual(database.operation_updates, [])
        self.assertEqual(database.task["status"], "waiting_review")

    def test_legacy_schema_migration_adds_run_and_lock_lease_columns(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = root / f"organizer-lease-migration-{uuid.uuid4().hex}.db"
        try:
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE organizer_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT,
                    started_at TEXT NOT NULL
                );
                CREATE TABLE organizer_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lock_key TEXT NOT NULL UNIQUE,
                    task_id INTEGER,
                    run_id INTEGER,
                    owner TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO organizer_runs(task_id, status, owner_id, started_at)
                VALUES (1, 'running', 'legacy', '2000-01-01T00:00:00Z');
                """
            )
            connection.commit()
            connection.close()
            database = Database(db_path)
            with database.connect() as migration_connection:
                database._apply_organizer_run_lease_migration(migration_connection)
            with database.connect() as check:
                run_columns = {row["name"] for row in check.execute("PRAGMA table_info(organizer_runs)")}
                lock_columns = {row["name"] for row in check.execute("PRAGMA table_info(organizer_locks)")}
                run = check.execute("SELECT heartbeat_at, lease_expires_at FROM organizer_runs").fetchone()

            self.assertIn("heartbeat_at", run_columns)
            self.assertIn("lease_expires_at", run_columns)
            self.assertIn("expires_at", lock_columns)
            self.assertTrue(run["heartbeat_at"])
            self.assertTrue(run["lease_expires_at"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()

if __name__ == "__main__":
    unittest.main()
