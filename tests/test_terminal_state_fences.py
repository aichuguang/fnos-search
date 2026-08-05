from __future__ import annotations

import copy
import unittest

from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.rclone_category_finalizer import RcloneCategoryFinalizer
from fnos_media_import.services.rclone_ready_items_completion_service import (
    RcloneReadyItemsCompletionService,
)
from fnos_media_import.services.rclone_run_import_finalizer import RcloneRunImportFinalizer
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.sixpan_offline_sync_service import SixPanOfflineSyncService


class _RacingJobDatabase:
    def __init__(self, status: str = "transferring", *, race_status: str = "cancelled") -> None:
        self.race_status = race_status
        self.job = {
            "id": 42,
            "status": status,
            "category": "tv",
            "category_label": "电视剧",
            "target_path": "/移动云/_入库暂存/电视剧/job-42",
            "raw_data": {},
        }
        self.events: list[tuple] = []
        self.rclone_events: list[tuple] = []
        self.update_attempts: list[tuple[set[str], dict]] = []
        self.file_events = [
            {
                "id": 1,
                "run_id": 9,
                "job_id": 42,
                "status": "done",
                "category": "电视剧",
                "filename": "E01.mkv",
                "source_path": "/夸克/_入库暂存/电视剧/job-42/E01.mkv",
                "target_path": "/移动云/_入库暂存/电视剧/job-42/E01.mkv",
                "raw_data": {},
            }
        ]

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def get_jobs_by_ids(self, _job_ids: list[int]) -> dict[int, dict]:
        return {42: copy.deepcopy(self.job)}

    def update_job_if_status(self, _job_id: int, expected_statuses, **updates) -> bool:
        self.update_attempts.append((set(expected_statuses), copy.deepcopy(updates)))
        self.job.update(
            status=self.race_status,
            error_message=f"{self.race_status} won the race",
            raw_data=(
                {"cancel": {"active": True, "generation": 3}}
                if self.race_status == "cancelled"
                else {"terminal": self.race_status}
            ),
        )
        return False

    def update_job(self, _job_id: int, **_updates) -> None:
        raise AssertionError("terminal-state race must use update_job_if_status")

    def add_event(self, *args) -> None:
        self.events.append(copy.deepcopy(args))

    def add_rclone_event(self, *args) -> None:
        self.rclone_events.append(copy.deepcopy(args))

    def list_all_rclone_file_events(self, *, run_id=None, job_id=None, **_kwargs) -> list[dict]:
        return [
            copy.deepcopy(item)
            for item in self.file_events
            if (run_id is None or item["run_id"] == run_id)
            and (job_id is None or item["job_id"] == job_id)
        ]


class TerminalStateFenceTests(unittest.TestCase):
    def test_organizer_linked_job_sync_cannot_revive_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase(status="waiting_organizer")
        service = OrganizerService.__new__(OrganizerService)
        service.db = database

        updated = service._sync_linked_job(
            {"id": 7, "job_id": 42, "openlist_root_path": "/移动云/电视剧"},
            status="organizing",
            stage="organizing",
            message="late organizer progress",
        )

        self.assertFalse(updated)
        self.assertEqual(database.job["status"], "cancelled")
        self.assertEqual(database.job["raw_data"]["cancel"]["generation"], 3)
        self.assertEqual(database.events, [])

    def test_ready_finalizer_cannot_promote_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase()
        service = RcloneReadyItemsCompletionService(
            database=database,
            config=lambda: {"defer_media_refresh_to_organizer": True},
            refresh_media=lambda *_args, **_kwargs: self.fail("refresh should be deferred"),
        )
        job = database.get_job(42)

        result = service.finish(
            9,
            "tv",
            {"label": "电视剧"},
            [(job, copy.deepcopy(database.file_events), {"ready": True})],
            copy.deepcopy(database.file_events),
            trigger="rclone_run_finished",
            success_message="done",
        )

        self.assertNotIn("completed_items", result)
        self.assertEqual(database.job["status"], "cancelled")
        self.assertEqual(database.events, [])

    def test_ready_finalizer_preserves_concurrent_success_terminals(self) -> None:
        for terminal_status in ("done", "success"):
            with self.subTest(terminal_status=terminal_status):
                database = _RacingJobDatabase(race_status=terminal_status)
                service = RcloneReadyItemsCompletionService(
                    database=database,
                    config=lambda: {"defer_media_refresh_to_organizer": True},
                    refresh_media=lambda *_args, **_kwargs: self.fail("refresh should be deferred"),
                )
                job = database.get_job(42)

                result = service.finish(
                    9,
                    "tv",
                    {"label": "电视剧"},
                    [(job, copy.deepcopy(database.file_events), {"ready": True})],
                    copy.deepcopy(database.file_events),
                    trigger="rclone_run_finished",
                    success_message="done",
                )

                self.assertNotIn("completed_items", result)
                self.assertEqual(database.job["status"], terminal_status)
                self.assertEqual(database.events, [])

    def test_category_finalizer_failure_cannot_overwrite_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase()
        service = RcloneCategoryFinalizer(
            database=database,
            categories=lambda: {"tv": {"label": "电视剧"}},
            category_key=lambda *_args: "tv",
            event_matches=lambda *_args, **_kwargs: True,
            feasibility=lambda *_args: {
                "ready": False,
                "status": "failed",
                "message": "incomplete transfer",
            },
            finish_ready=lambda *_args, **_kwargs: self.fail("cancelled job must not finish"),
        )

        result = service.finalize(
            9,
            "电视剧",
            {"status": "category_done", "moved_count": 1, "failed_count": 0},
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(database.job["status"], "cancelled")
        self.assertEqual(database.job["raw_data"]["cancel"]["generation"], 3)

    def test_run_finalizer_failure_cannot_overwrite_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase()
        dispatches: list[dict] = []
        service = RcloneRunImportFinalizer(
            database=database,
            categories=lambda: {"tv": {"label": "电视剧"}},
            log=lambda _message: None,
            feasibility=lambda *_args: {
                "ready": False,
                "status": "failed",
                "message": "incomplete transfer",
            },
            finish_ready=lambda *_args, **_kwargs: self.fail("cancelled job must not finish"),
            dispatch_ready=lambda *_args: dispatches.append({}),
        )

        service.finalize(9, 1)

        self.assertEqual(database.job["status"], "cancelled")
        self.assertEqual(dispatches, [])

    def test_single_file_retry_cannot_restart_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase(status="failed")
        service = RcloneService.__new__(RcloneService)
        service.db = database
        service.categories = {"tv": {"label": "电视剧"}}
        service.status = lambda: {"running": False}
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.start_file_retry(
            {
                "id": 1,
                "job_id": 42,
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/source/E01.mkv",
            }
        )

        self.assertFalse(result["success"])
        self.assertEqual(starts, [])
        self.assertEqual(database.job["status"], "cancelled")

    def test_sixpan_completion_cannot_promote_concurrent_cancel(self) -> None:
        database = _RacingJobDatabase(status="submitted")
        organizer_calls: list[tuple] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {},
            enqueue_organizer=lambda *args: organizer_calls.append(args) or {"queued": True},
            record_completed=lambda *_args, **_kwargs: {},
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        completed = service._complete_job(
            database.get_job(42),
            42,
            "sixpan-42",
            "test",
            {"state": "completed"},
        )

        self.assertFalse(completed)
        self.assertEqual(organizer_calls, [])
        self.assertEqual(database.job["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
