from __future__ import annotations

import threading
import unittest

from fnos_media_import.services.rclone_category_finalizer import RcloneCategoryFinalizer
from fnos_media_import.services.rclone_run_import_finalizer import RcloneRunImportFinalizer
from fnos_media_import.services.rclone_service import RcloneService


class RcloneCategorySkippedExistingFallbackTests(unittest.TestCase):
    def test_zero_moved_count_with_skipped_existing_event_still_finishes_job(self) -> None:
        class FakeDatabase:
            @staticmethod
            def list_rclone_file_events(**_kwargs) -> list[dict]:
                return [
                    {
                        "job_id": 7,
                        "status": "skipped_existing",
                        "category": "电视剧",
                        "filename": "E01.mkv",
                        "source_path": "离线下载/电视剧/job-7/E01.mkv",
                        "target_path": "webdav/_入库暂存/电视剧/job-7/E01.mkv",
                    }
                ]

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict[int, dict]:
                return {
                    7: {
                        "id": 7,
                        "status": "transferring",
                        "category": "tv",
                    }
                }

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def update_job(*_args, **_kwargs) -> None:
                return None

        finished: list[dict] = []

        def finish_ready(_run_id, _category_key, _category, items, _events, **_kwargs):
            finished.extend({"job_id": item[0]["id"]} for item in items)
            return {"success": True, "completed_items": list(finished)}

        service = RcloneCategoryFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {"label": "电视剧"}},
            category_key=lambda *_args: "tv",
            event_matches=lambda *_args, **_kwargs: True,
            feasibility=lambda *_args, **_kwargs: {"ready": True, "status": "ready", "message": "可完成"},
            finish_ready=finish_ready,
        )

        result = service.finalize(
            11,
            "电视剧",
            {
                "status": "category_done",
                "moved_count": 0,
                "failed_count": 0,
                "target_path": "webdav/_入库暂存/电视剧",
            },
        )

        self.assertEqual(finished, [{"job_id": 7}])
        self.assertEqual(result["matched_job_count"], 1)


class RcloneRunOrganizerDispatchFallbackTests(unittest.TestCase):
    def test_run_end_fallback_dispatches_completed_items_to_organizer(self) -> None:
        class FakeDatabase:
            @staticmethod
            def list_rclone_file_events(**_kwargs) -> list[dict]:
                return [{"job_id": 8, "status": "done", "target_path": "目标/E01.mkv"}]

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict[int, dict]:
                return {8: {"id": 8, "status": "transferring", "category": "tv"}}

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def update_job(*_args, **_kwargs) -> None:
                return None

        dispatched: list[tuple[dict, dict]] = []
        finalizer = RcloneRunImportFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {"label": "电视剧"}},
            log=lambda _message: None,
            feasibility=lambda *_args: {"ready": True, "status": "ready", "message": "可完成"},
            finish_ready=lambda *_args, **_kwargs: {
                "success": True,
                "completed_items": [{"job_id": 8, "job": {"id": 8, "category": "tv"}}],
            },
            dispatch_ready=lambda result, payload: dispatched.append((result, payload)),
        )

        finalizer.finalize(12, 0)

        self.assertEqual(dispatched[0][0]["completed_items"][0]["job_id"], 8)
        self.assertEqual(dispatched[0][1]["trigger"], "rclone_run_finished")

    def test_service_queues_run_fallback_until_app_dispatcher_is_ready(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service._run_ready_handler = None
        service._pending_run_ready_results = []
        service._append_log = lambda _message: None
        service.db = None
        category_refresh = {"success": True, "completed_items": [{"job_id": 9}]}
        payload = {"run_id": 13, "trigger": "rclone_run_finished"}

        queued = service._dispatch_run_ready_to_organizer(category_refresh, payload)
        received: list[tuple[dict, dict]] = []
        service.set_run_ready_handler(lambda result, context: received.append((result, context)))

        self.assertTrue(queued["queued"])
        self.assertEqual(received, [(category_refresh, payload)])
        self.assertEqual(service._pending_run_ready_results, [])


class RcloneSingleFileRetryStateTests(unittest.TestCase):
    def test_failed_job_is_restored_to_waiting_transfer_before_retry_start(self) -> None:
        from tests.test_rclone_persisted_staging_plan import _persisted_plan

        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 42,
                    "status": "failed",
                    "error_message": "上次上传失败",
                    "category": "tv",
                    "target_route": "quark_to_mobile",
                    "raw_data": {"staging_plan": _persisted_plan()},
                }
                self.updates: list[dict] = []

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.updates.append(values)
                self.job.update(values)

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "电视剧"}}
        service.status = lambda: {"running": False}
        captured: dict = {}
        service.start = lambda **kwargs: captured.update(kwargs) or {"success": True}

        result = service.start_file_retry(
            {
                "id": 19,
                "job_id": 42,
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/旧夸克/电视剧/job-42/版本A/E01.mkv",
            }
        )

        self.assertTrue(result["success"])
        self.assertEqual(service.db.job["status"], "waiting_transfer")
        self.assertEqual(captured["staging_run"]["job_id"], 42)


if __name__ == "__main__":
    unittest.main()
