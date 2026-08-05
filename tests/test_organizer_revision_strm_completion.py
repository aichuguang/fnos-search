from __future__ import annotations

import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fnos_media_import.database import Database
from fnos_media_import.organizer.run_lease import OrganizerScanLease
from fnos_media_import.organizer.service import OrganizerService


class _DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-revision-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()


class OrganizerCancellationCasTests(_DatabaseTestCase):
    def _create_job_and_task(self, *, status: str) -> tuple[int, int]:
        suffix = uuid.uuid4().hex
        job_id, created = self.database.create_job(
            {
                "title": f"Organizer cancellation {suffix}",
                "category": "tv",
                "category_label": "TV",
                "source_type": "quark",
                "source_url": f"https://pan.quark.cn/s/{suffix}",
                "target_route": "quark_to_mobile",
                "target_path": f"/mobile/staging/tv/job-{suffix}",
                "status": "waiting_organizer",
                "idempotency_key": f"organizer-cancel-cas:{suffix}",
            }
        )
        self.assertTrue(created)
        task_id = self.database.create_organizer_task(
            job_id=job_id,
            category="tv",
            openlist_root_path=f"/cloud/staging/tv/job-{job_id}",
            status=status,
        )
        return job_id, task_id

    @staticmethod
    def _service(database: Any) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        service._timers = {}
        return service

    def test_stale_cancel_snapshot_cannot_overwrite_concurrent_done(self) -> None:
        job_id, task_id = self._create_job_and_task(status="manual_confirmed")
        run_id, active = self.database.claim_organizer_run(task_id, owner_id="worker-a", lease_seconds=60)
        self.assertIsNone(active)
        self.assertIsNotNone(run_id)
        claimed = self.database.get_organizer_task(task_id, include_children=False)
        claimed_revision = int((claimed or {})["revision"])

        class FinalizeAfterListDatabase:
            def __init__(self, database: Database) -> None:
                self.database = database
                self.finalized = False

            def __getattr__(self, name: str) -> Any:
                return getattr(self.database, name)

            def list_organizer_tasks_by_job(self, linked_job_id: int, limit: int = 20) -> list[dict[str, Any]]:
                stale_tasks = self.database.list_organizer_tasks_by_job(linked_job_id, limit=limit)
                self.finalized = self.database.finalize_organizer_run_and_task(
                    int(run_id or 0),
                    task_id,
                    owner_id="worker-a",
                    run_status="done",
                    task_status="done",
                )
                return stale_tasks

        racing_database = FinalizeAfterListDatabase(self.database)
        result = self._service(racing_database).cancel_job_tasks(job_id, reason="linked job cancelled")

        self.assertTrue(racing_database.finalized)
        self.assertTrue(result["success"])
        self.assertEqual(result["cancelled_task_ids"], [])
        self.assertEqual(result["conflict_task_ids"], [])
        current = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual((current or {})["status"], "done")
        self.assertEqual(int((current or {})["revision"]), claimed_revision)
        run = next(item for item in self.database.list_organizer_runs() if item["id"] == run_id)
        self.assertEqual(run["status"], "done")

    def test_active_task_is_cancelled_and_scan_lease_is_invalidated(self) -> None:
        job_id, task_id = self._create_job_and_task(status="pending")
        self.assertTrue(
            self.database.claim_organizer_task_for_scan(
                task_id,
                allowed_statuses={"pending"},
                owner_id="scanner-a",
                lease_seconds=60,
            )
        )
        claimed = self.database.get_organizer_task(task_id, include_children=False)
        claimed_revision = int((claimed or {})["revision"])

        result = self._service(self.database).cancel_job_tasks(job_id, reason="linked job cancelled")

        self.assertTrue(result["success"])
        self.assertEqual(result["cancelled_task_ids"], [task_id])
        self.assertEqual(result["conflict_task_ids"], [])
        current = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual((current or {})["status"], "cancelled")
        self.assertEqual((current or {})["error_message"], "linked job cancelled")
        self.assertGreater(int((current or {})["revision"]), claimed_revision)
        self.assertIsNone((current or {})["scan_owner"])
        self.assertIsNone((current or {})["scan_lease_expires_at"])


class OrganizerScanRevisionTests(_DatabaseTestCase):
    def test_skip_during_scan_fences_the_old_plan_writer(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/cloud/staging/tv/job-1",
            status="pending",
        )
        self.assertTrue(
            self.database.claim_organizer_task_for_scan(
                task_id,
                allowed_statuses={"pending"},
                owner_id="scanner-a",
                lease_seconds=60,
            )
        )
        claimed = self.database.get_organizer_task(task_id, include_children=False)
        revision = int(claimed["revision"])

        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        skipped = service.skip_task(task_id)

        self.assertTrue(skipped["success"])
        current = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual(current["status"], "skipped")
        self.assertGreater(int(current["revision"]), revision)
        self.assertIsNone(current["scan_owner"])
        self.assertIsNone(current["scan_lease_expires_at"])
        self.assertFalse(
            self.database.replace_organizer_plan(
                task_id,
                files=[{"path": "/cloud/staging/tv/job-1/stale.mkv", "name": "stale.mkv"}],
                mappings=[],
                operations=[],
                expected_revision=revision,
                owner_id="scanner-a",
            )
        )
        self.assertEqual(self.database.get_organizer_task(task_id)["files"], [])

    def test_expired_scan_can_be_taken_over_but_old_revision_cannot_write(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/cloud/staging/tv/job-2",
            status="pending",
        )
        self.assertTrue(
            self.database.claim_organizer_task_for_scan(
                task_id,
                allowed_statuses={"pending"},
                owner_id="scanner-a",
                lease_seconds=60,
            )
        )
        first = self.database.get_organizer_task(task_id, include_children=False)
        first_revision = int(first["revision"])
        self.assertTrue(
            self.database.renew_organizer_scan(
                task_id,
                "scanner-a",
                lease_seconds=60,
                expected_revision=first_revision,
            )
        )
        self.assertTrue(
            self.database.owns_organizer_scan(
                task_id,
                "scanner-a",
                expected_revision=first_revision,
            )
        )

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE organizer_tasks SET scan_lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (task_id,),
            )

        self.assertFalse(
            self.database.owns_organizer_scan(
                task_id,
                "scanner-a",
                expected_revision=first_revision,
            )
        )
        self.assertTrue(
            Database(self.db_path).claim_organizer_task_for_scan(
                task_id,
                allowed_statuses={"pending"},
                owner_id="scanner-b",
                lease_seconds=60,
            )
        )
        second = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual(second["scan_owner"], "scanner-b")
        self.assertGreater(int(second["revision"]), first_revision)
        self.assertFalse(
            self.database.replace_organizer_plan(
                task_id,
                files=[],
                mappings=[],
                operations=[],
                expected_revision=first_revision,
                owner_id="scanner-a",
            )
        )

    def test_scan_heartbeat_renews_until_stopped(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.renewals = 0
                self.renewed_three_times = threading.Event()

            def renew_organizer_scan(self, *_args: Any, **_kwargs: Any) -> bool:
                self.renewals += 1
                if self.renewals >= 3:
                    self.renewed_three_times.set()
                return True

            @staticmethod
            def owns_organizer_scan(*_args: Any, **_kwargs: Any) -> bool:
                return True

        database = FakeDatabase()
        lease = OrganizerScanLease(
            database=database,
            task_id=1,
            owner_id="scanner",
            revision=2,
            lease_seconds=30,
            heartbeat_interval_seconds=0.05,
        )
        lease.start()
        self.assertTrue(database.renewed_three_times.wait(timeout=1))
        lease.ensure_owned()
        lease.stop()
        stopped_at = database.renewals
        time.sleep(0.10)

        self.assertGreaterEqual(stopped_at, 3)
        self.assertEqual(database.renewals, stopped_at)


class OrganizerRunRevisionTests(_DatabaseTestCase):
    def test_revision_change_prevents_old_run_from_finalizing_task(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/cloud/staging/tv/job-3",
            status="manual_confirmed",
        )
        run_id, active = self.database.claim_organizer_run(task_id, owner_id="worker-a", lease_seconds=60)
        self.assertIsNone(active)
        self.assertIsNotNone(run_id)
        task = self.database.get_organizer_task(task_id, include_children=False)
        old_revision = int(task["revision"])
        self.assertTrue(
            self.database.update_organizer_task(
                task_id,
                expected_revision=old_revision,
                expected_statuses={"executing"},
                bump_revision=True,
                error_message="superseded",
            )
        )

        finalized = self.database.finalize_organizer_run_and_task(
            int(run_id or 0),
            task_id,
            owner_id="worker-a",
            run_status="done",
            task_status="done",
        )

        self.assertFalse(finalized)
        current = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual(current["status"], "executing")
        self.assertEqual(current["error_message"], "superseded")
        run = next(item for item in self.database.list_organizer_runs() if item["id"] == run_id)
        self.assertEqual(run["status"], "running")
        self.assertFalse(self.database.owns_organizer_run(int(run_id or 0), "worker-a"))


class OrganizerPersistedOperationRecoveryTests(_DatabaseTestCase):
    def test_retry_after_crash_reuses_done_and_pending_operation_rows(self) -> None:
        task_id = self.database.create_organizer_task(
            category="tv",
            openlist_root_path="/cloud/staging/tv/job-4",
            status="manual_confirmed",
            evidence={},
            raw_data={},
        )
        self.assertTrue(
            self.database.replace_organizer_plan(
                task_id,
                files=[],
                mappings=[],
                operations=[
                    {
                        "type": "move",
                        "source_path": "/cloud/staging/tv/job-4/already.mkv",
                        "target_path": "/cloud/tv/show/already.mkv",
                        "status": "done",
                    },
                    {
                        "type": "move",
                        "source_path": "/cloud/staging/tv/job-4/pending.mkv",
                        "target_path": "/cloud/tv/show/pending.mkv",
                        "status": "pending",
                    },
                ],
            )
        )
        old_run_id, _active = self.database.claim_organizer_run(task_id, owner_id="crashed-worker", lease_seconds=60)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE organizer_runs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (old_run_id,),
            )
        recovered = self.database.recover_stale_organizer_runs()
        self.assertEqual(recovered["count"], 1)

        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.owner_id = "restarted-worker"
        service.categories = {"tv": {}}
        service.organizer_config = {"run_lease_seconds": 60, "run_lease_heartbeat_seconds": 10}
        service._worker_context = threading.local()
        service._sync_linked_job = lambda *_args, **_kwargs: None
        service._validate_staging_mapping_boundaries = lambda _task: None
        service._lock_keys = lambda _task: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._cleanup_source_empty_dirs_after_apply = lambda _task: {}
        service._confirm_standardized_targets = lambda _task: {
            "success": True,
            "organized_target_path": "/cloud/tv/show",
            "target_dirs": ["/cloud/tv/show"],
        }
        service._refresh_openlist_strm_for_task = lambda *_args, **_kwargs: {}
        service._operations_for_mappings = lambda *_args, **_kwargs: self.fail(
            "persisted operations must not be rebuilt after a crash"
        )
        executed: list[int] = []

        def execute(operation: dict[str, Any]) -> dict[str, Any]:
            executed.append(int(operation["id"]))
            return {"type": "move", "source_path": operation["target_path"], "target_path": operation["source_path"]}

        service._execute_operation = execute
        before = self.database.get_organizer_task(task_id)
        done_id = int(next(item for item in before["operations"] if item["status"] == "done")["id"])
        pending_id = int(next(item for item in before["operations"] if item["status"] == "pending")["id"])

        result = service.apply_task_from_worker(task_id)

        self.assertTrue(result["success"])
        self.assertEqual(executed, [pending_id])
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(after["status"], "done")
        statuses = {int(item["id"]): item["status"] for item in after["operations"]}
        self.assertEqual(statuses[done_id], "done")
        self.assertEqual(statuses[pending_id], "done")


class _Lease:
    def ensure_owned(self) -> None:
        return None


def _strm_service() -> tuple[OrganizerService, list[dict[str, Any]], list[tuple[Any, ...]]]:
    service = OrganizerService.__new__(OrganizerService)
    service.owner_id = "strm-worker"
    service.organizer_config = {
        "strm_confirm_retry_delays_seconds": [1, 2, 3],
        "strm_cleanup_old_before_refresh": True,
    }
    service._worker_context = SimpleNamespace(active=True)
    service._ensure_task_active = lambda *_args, **_kwargs: {}
    service._sync_linked_job = lambda *_args, **_kwargs: None
    finalized: list[dict[str, Any]] = []
    cleanup_calls: list[tuple[Any, ...]] = []

    def finalize(_run_id: int, _task_id: int, **values: Any) -> None:
        finalized.append(values)

    def cleanup(*args: Any, **kwargs: Any) -> dict[str, Any]:
        cleanup_calls.append((*args, kwargs))
        return {"enabled": True, "success": True}

    service._finalize_organizer_run_and_task = finalize
    service._cleanup_old_strm_dir_for_task = cleanup
    return service, finalized, cleanup_calls


class OrganizerStrmCompletionTests(unittest.TestCase):
    def test_refresh_ack_finishes_immediately_without_waiting_for_strm(self) -> None:
        service, finalized, cleanup_calls = _strm_service()
        path = "/strm/tv/New Show"
        service._refresh_openlist_strm_for_task = lambda *_args, **_kwargs: {
            "enabled": True,
            "failed": 0,
            "requested_at": "2026-01-01T00:00:00Z",
            "refresh_prefix": "/strm/tv",
            "resource_names": ["New Show"],
            "refresh_paths": [path],
        }
        service._capture_strm_targets = lambda *_args, **_kwargs: self.fail(
            "OpenList 接受刷新后不应再检查 STRM 文件"
        )
        task = {
            "id": 1,
            "status": "executing",
            "category": "tv",
            "raw_data": {},
            "evidence": {},
            "mappings": [],
        }

        result = service._start_strm_completion(
            1,
            11,
            task,
            _Lease(),
            summary={"task_id": 1},
            undo=[],
            task_evidence={},
            confirmation={"success": True},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(finalized[-1]["run_status"], "done")
        self.assertEqual(finalized[-1]["task_status"], "done")
        completion = finalized[-1]["raw_data"]["strm_completion"]
        self.assertEqual(completion["status"], "refresh_accepted")
        self.assertFalse(completion["confirmation_required"])
        self.assertEqual(completion["handled_by"], "openlist")
        self.assertEqual(cleanup_calls, [])

    def test_refresh_failure_exposes_openlist_error_detail(self) -> None:
        service, finalized, cleanup_calls = _strm_service()
        service._refresh_openlist_strm_for_task = lambda *_args, **_kwargs: {
            "enabled": True,
            "failed": 1,
            "items": [
                {
                    "success": False,
                    "path": "/strm/movie/Test Movie",
                    "message": 'unsupported protocol scheme ""',
                }
            ],
        }
        task = {
            "id": 1,
            "status": "executing",
            "category": "movie",
            "raw_data": {},
            "evidence": {},
            "mappings": [],
        }

        result = service._start_strm_completion(
            1,
            11,
            task,
            _Lease(),
            summary={"task_id": 1},
            undo=[],
            task_evidence={},
            confirmation={"success": True},
        )

        self.assertFalse(result["success"])
        self.assertIn('unsupported protocol scheme ""', result["message"])
        self.assertEqual(finalized[-1]["run_status"], "failed")
        self.assertEqual(finalized[-1]["task_status"], "waiting_review")
        self.assertEqual(cleanup_calls, [])

    def test_legacy_pending_task_is_reconciled_without_strm_probe_or_cleanup(self) -> None:
        service, finalized, cleanup_calls = _strm_service()
        path = "/strm/tv/New Show"
        service._capture_strm_targets = lambda *_args, **_kwargs: self.fail(
            "历史待确认任务也不应继续探测 STRM"
        )
        service._schedule_task_after = lambda *_args, **_kwargs: self.fail(
            "历史待确认任务不应再次安排等待"
        )
        state = {
            "version": 1,
            "status": "pending",
            "attempts": 2,
            "resource_names": ["New Show"],
            "refresh_paths": [path],
            "refresh_prefix": "/strm/tv",
            "summary": {"task_id": 1},
            "undo": [],
            "confirmation": {"success": True},
            "task_evidence": {},
        }
        task = {
            "id": 1,
            "status": "executing",
            "category": "tv",
            "raw_data": {"strm_completion": state},
            "evidence": {},
        }

        result = service._resume_strm_completion(1, 12, task, state, _Lease())

        self.assertEqual(result["status"], "done")
        self.assertEqual(finalized[-1]["task_status"], "done")
        completion = finalized[-1]["raw_data"]["strm_completion"]
        self.assertEqual(completion["status"], "refresh_accepted")
        self.assertTrue(completion["legacy_pending_reconciled"])
        self.assertEqual(cleanup_calls, [])

    def test_startup_requeues_legacy_strm_pending_state_for_immediate_reconciliation(self) -> None:
        task = {
            "id": 7,
            "status": "strm_pending",
            "raw_data": {
                "staging_plan": {"enabled": True},
                "strm_completion": {
                    "status": "pending",
                    "next_retry_at": "2000-01-01T00:00:00Z",
                },
            },
        }

        class FakeDatabase:
            @staticmethod
            def list_organizer_tasks(*, limit: int, status: str, offset: int = 0) -> list[dict[str, Any]]:
                del limit, offset
                return [task] if status == "strm_pending" else []

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"enabled": True, "staging_enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service._task_abort_reason = lambda *_args, **_kwargs: ""
        service._task_has_staging_plan = lambda _task: True
        service._sync_linked_job = lambda *_args, **_kwargs: None
        scheduled: list[tuple[int, int]] = []
        service._schedule_task_after = lambda task_id, delay: scheduled.append((task_id, delay)) or True

        service._recover_transient_scan_tasks_on_startup(include_scanning=False)

        self.assertEqual(scheduled, [(7, 0)])


if __name__ == "__main__":
    unittest.main()
