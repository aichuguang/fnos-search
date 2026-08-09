from __future__ import annotations

import unittest

from fnos_media_import.services.rclone_category_finalizer import RcloneCategoryFinalizer
from fnos_media_import.services.rclone_run_import_finalizer import RcloneRunImportFinalizer


class _ExecutionRepository:
    @staticmethod
    def get(job_id: int):
        return {"job_id": job_id} if job_id == 7 else None

    @staticmethod
    def plan(job_id: int):
        return {"version": 1, "job_id": job_id} if job_id == 7 else None


class _Database:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    @staticmethod
    def list_rclone_file_events(**_kwargs):
        return [
            {
                "job_id": 7,
                "status": "done",
                "category": "电影",
                "target_path": "/target/file.mkv",
            }
        ]

    @staticmethod
    def get_jobs_by_ids(_job_ids):
        return {7: {"id": 7, "status": "transferring", "category": "movie"}}

    def add_rclone_event(self, *args, **kwargs) -> None:
        self.events.append((args, kwargs))

    def add_event(self, *args, **kwargs) -> None:
        self.events.append((args, kwargs))


class V1FinalizerGuardTests(unittest.TestCase):
    def test_category_finalizer_does_not_complete_v1_execution(self) -> None:
        database = _Database()
        finished: list[object] = []
        service = RcloneCategoryFinalizer(
            database=database,
            categories=lambda: {"movie": {}},
            category_key=lambda *_args: "movie",
            event_matches=lambda *_args, **_kwargs: True,
            feasibility=lambda *_args: {"ready": True, "status": "ready", "message": "ok"},
            finish_ready=lambda *args, **kwargs: finished.append((args, kwargs)) or {},
            execution_repository=_ExecutionRepository(),
        )

        result = service.finalize(
            11,
            "电影",
            {
                "status": "category_done",
                "moved_count": 1,
                "failed_count": 0,
                "target_path": "/target",
            },
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(finished, [])
        self.assertTrue(
            any(
                event_args
                and "V1 durable transfer" in str(event_args[2])
                for event_args, _event_kwargs in database.events
            )
        )

    def test_run_finalizer_does_not_dispatch_v1_execution(self) -> None:
        database = _Database()
        finished: list[object] = []
        dispatched: list[object] = []
        service = RcloneRunImportFinalizer(
            database=database,
            categories=lambda: {"movie": {}},
            log=lambda _message: None,
            feasibility=lambda *_args: {"ready": True, "status": "ready", "message": "ok"},
            finish_ready=lambda *args, **kwargs: finished.append((args, kwargs)) or {},
            dispatch_ready=lambda *args, **kwargs: dispatched.append((args, kwargs)),
            execution_repository=_ExecutionRepository(),
        )

        service.finalize(12, 0)

        self.assertEqual(finished, [])
        self.assertEqual(dispatched, [])
        self.assertTrue(
            any(
                event_args
                and "V1 durable transfer" in str(event_args[2])
                for event_args, _event_kwargs in database.events
            )
        )


if __name__ == "__main__":
    unittest.main()
