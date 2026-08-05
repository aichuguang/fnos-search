from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.constants import JOB_REVIEW
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.sixpan_offline_sync_service import SixPanOfflineSyncService
from tests.test_rclone_persisted_staging_plan import _persisted_plan


class SixPanOrganizerHandoffRegressionTests(unittest.TestCase):
    def test_enqueue_failure_enters_review_without_refreshing_fnos(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 9,
                    "category": "tv",
                    "target_path": "/_入库暂存/电视剧/job-9",
                    "raw_data": {"staging_plan": {"enabled": True, "job_id": 9}},
                }
                self.updates: list[dict] = []
                self.events: list[tuple] = []

            def update_job(self, _job_id: int, **values) -> None:
                self.updates.append(values)
                self.job.update(values)

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def add_event(self, *values) -> None:
                self.events.append(values)

        database = FakeDatabase()
        refresh_calls: list[tuple] = []
        guest_updates: list[tuple] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {
                "success": False,
                "message": "OpenList 暂时不可用",
            },
            record_completed=lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or {},
            sync_guest_requests=lambda *args, **kwargs: guest_updates.append((args, kwargs)),
        )

        service._complete_job(
            database.job,
            9,
            "sixpan-task-9",
            "poller",
            {"state": "completed", "progress": 100},
        )

        self.assertEqual(refresh_calls, [])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertTrue(database.job["raw_data"]["completion"]["retryable"])
        self.assertTrue(database.job["raw_data"]["sixpan_organizer_enqueue"]["retryable"])
        self.assertIn("Organizer 未成功接管", database.job["error_message"])
        self.assertEqual(database.events[-1][1], "warn")
        self.assertEqual(guest_updates[-1][0][1], JOB_REVIEW)

    def test_historical_job_without_staging_plan_keeps_legacy_refresh_fallback(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 10,
                    "category": "tv",
                    "target_path": "/电视剧/旧资源",
                    "raw_data": {},
                }
                self.events: list[tuple] = []

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def add_event(self, *values) -> None:
                self.events.append(values)

        database = FakeDatabase()
        refresh_calls: list[tuple] = []
        guest_updates: list[tuple] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {
                "success": False,
                "skipped": True,
                "message": "Organizer 未启用",
            },
            record_completed=lambda *args, **kwargs: (
                refresh_calls.append((args, kwargs))
                or {"success": True, "message": "已刷新"}
            ),
            sync_guest_requests=lambda *args, **kwargs: guest_updates.append((args, kwargs)),
        )

        service._complete_job(
            database.job,
            10,
            "sixpan-task-10",
            "poller",
            {"state": "completed", "progress": 100},
        )

        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0][0][2], "/电视剧/旧资源")
        self.assertNotIn("sixpan_organizer_enqueue", database.job["raw_data"])
        self.assertIn("旧流程", database.events[-1][2])
        self.assertTrue(guest_updates[-1][0][2]["legacy_refresh_fallback"])


class SixPanVisibilityRetryRegressionTests(unittest.TestCase):
    def test_staged_sixpan_task_starts_openlist_visibility_backoff(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 12,
                    "job_id": 9,
                    "openlist_root_path": "/清云/_入库暂存/电视剧/job-9",
                    "raw_data": {
                        "sixpan_openlist": {},
                        "staging_plan": {"enabled": True, "job_id": 9},
                    },
                    "evidence": {},
                }
                self.updates: list[dict] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = False) -> dict:
                return dict(self.task)

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.updates.append(values)
                self.task.update(values)

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"openlist_visible_retry_delays_seconds": [30, 60]}
        scheduled: list[tuple[int, int | float]] = []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        service._schedule_task_after = lambda task_id, delay: scheduled.append((task_id, delay))

        result = service._schedule_initial_openlist_visibility_wait(12)

        self.assertTrue(result)
        self.assertTrue(service._task_retries_openlist_visibility(service.db.task))
        self.assertEqual(service.db.task["status"], "waiting_openlist")
        retry_state = service.db.task["raw_data"]["openlist_visibility_retry"]
        self.assertIn("六盘离线任务已完成", retry_state["message"])
        self.assertEqual(scheduled, [(12, 30)])


class StagedStrmCleanupRegressionTests(unittest.TestCase):
    def test_job_root_cleanup_uses_original_resource_directory(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"strm_cleanup_old_before_refresh": True}
        cleaned: list[tuple[str, str]] = []
        service._cleanup_old_openlist_strm_dir = lambda old_name, category: (
            cleaned.append(("openlist", old_name))
            or {"success": True, "old_name": old_name, "category": category}
        )
        service._cleanup_old_local_strm_dir = lambda old_name, category: (
            cleaned.append(("local", old_name))
            or {"success": True, "old_name": old_name, "category": category}
        )
        task = {
            "openlist_root_path": "/移动云/_入库暂存/电视剧/job-42",
            "mappings": [
                {
                    "status": "ready",
                    "source_path": "/移动云/_入库暂存/电视剧/job-42/原始剧名/Season 01/E01.mkv",
                    "target_path": "/移动云/电视剧/标准剧名 (2026)/Season 01/标准剧名 (2026) - S01E01.mkv",
                }
            ],
        }

        result = service._cleanup_old_strm_dir_for_task(task, "tv", ["标准剧名 (2026)"])

        self.assertEqual(result["old_name"], "原始剧名")
        self.assertEqual(cleaned, [("openlist", "原始剧名"), ("local", "原始剧名")])
        self.assertNotIn(("openlist", "job-42"), cleaned)

    def test_historical_direct_files_do_not_delete_job_named_strm(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"strm_cleanup_old_before_refresh": True}
        service._cleanup_old_openlist_strm_dir = lambda *_args: self.fail("不应删除 job 名目录")
        service._cleanup_old_local_strm_dir = lambda *_args: self.fail("不应删除 job 名目录")
        task = {
            "openlist_root_path": "/清云/_入库暂存/电视剧/job-43",
            "mappings": [
                {
                    "status": "ready",
                    "source_path": "/清云/_入库暂存/电视剧/job-43/E01.mkv",
                    "target_path": "/清云/电视剧/标准剧名 (2026)/Season 01/E01.mkv",
                }
            ],
        }

        result = service._cleanup_old_strm_dir_for_task(task, "tv", ["标准剧名 (2026)"])

        self.assertTrue(result["skipped"])
        self.assertNotIn("job-43", result.get("old_name", ""))

    def test_verified_direct_file_staging_task_cleans_job_named_strm(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"strm_cleanup_old_before_refresh": True}
        cleaned: list[tuple[str, str]] = []
        service._cleanup_old_openlist_strm_dir = lambda old_name, category, **_kwargs: (
            cleaned.append(("openlist", old_name))
            or {"success": True, "old_name": old_name, "category": category}
        )
        service._cleanup_old_local_strm_dir = lambda old_name, category: (
            cleaned.append(("local", old_name))
            or {"success": True, "old_name": old_name, "category": category}
        )
        plan = _persisted_plan(43)
        task = {
            "job_id": 43,
            "category": "tv",
            "openlist_root_path": plan["openlist_job_root"],
            "raw_data": {"staging_plan": plan},
            "mappings": [
                {
                    "status": "ready",
                    "source_path": f'{plan["openlist_job_root"]}/E01.mkv',
                    "target_path": "/旧挂载/电视剧/标准剧名 (2026)/Season 01/E01.mkv",
                }
            ],
        }

        result = service._cleanup_old_strm_dir_for_task(
            task,
            "tv",
            ["标准剧名 (2026)"],
            refresh_prefix=plan["openlist_refresh_prefix"],
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["old_name"], "job-43")
        self.assertEqual(cleaned, [("openlist", "job-43"), ("local", "job-43")])

    def test_season_folder_under_job_root_is_not_treated_as_old_resource_name(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"strm_cleanup_old_before_refresh": True}
        service._cleanup_old_openlist_strm_dir = lambda *_args: self.fail("不应删除 Season 目录")
        service._cleanup_old_local_strm_dir = lambda *_args: self.fail("不应删除 Season 目录")
        plan = _persisted_plan(44)
        task = {
            "job_id": 44,
            "category": "tv",
            "openlist_root_path": plan["openlist_job_root"],
            "raw_data": {"staging_plan": plan},
            "mappings": [
                {
                    "status": "ready",
                    "source_path": f'{plan["openlist_job_root"]}/Season 01/E01.mkv',
                    "target_path": "/旧挂载/电视剧/标准剧名 (2026)/Season 01/E01.mkv",
                }
            ],
        }

        result = service._cleanup_old_strm_dir_for_task(task, "tv", ["标准剧名 (2026)"])

        self.assertTrue(result["skipped"])


class HistoricalOrganizerRecoveryRegressionTests(unittest.TestCase):
    def test_staging_mode_does_not_auto_recover_historical_failed_task(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            @staticmethod
            def list_organizer_tasks(**_kwargs) -> list[dict]:
                return [
                    {
                        "id": 7,
                        "trigger_type": "rclone_category_done",
                        "error_message": "connection reset by peer",
                        "raw_data": {},
                    }
                ]

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.updates.append(values)

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"enabled": True, "staging_enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service._sync_linked_job = lambda *_args, **_kwargs: self.fail("历史任务不应自动恢复")
        service._schedule_task_after = lambda *_args, **_kwargs: self.fail("历史任务不应重新调度")

        service._recover_transient_scan_tasks_on_startup()

        self.assertEqual(service.db.updates, [])

    def test_staged_waiting_openlist_task_is_rescheduled_after_restart(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            @staticmethod
            def list_organizer_tasks(*, status: str, **_kwargs) -> list[dict]:
                if status != "waiting_openlist":
                    return []
                return [
                    {
                        "id": 8,
                        "job_id": 18,
                        "status": "waiting_openlist",
                        "trigger_type": "rclone_category_done",
                        "error_message": "",
                        "category": "tv",
                        "raw_data": {
                            "staging_plan": _persisted_plan(18),
                            "openlist_visibility_retry": {"attempts": 1},
                        },
                    }
                ]

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.updates.append(values)

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"enabled": True, "staging_enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service._sync_linked_job = lambda *_args, **_kwargs: None
        scheduled: list[tuple[int, int]] = []
        service._schedule_task_after = lambda task_id, delay: scheduled.append((task_id, delay))

        service._recover_transient_scan_tasks_on_startup()

        self.assertEqual(service.db.updates[-1]["status"], "waiting_openlist")
        self.assertEqual(scheduled, [(8, 5)])


class ConfigExampleRegressionTests(unittest.TestCase):
    def test_organizer_example_matches_staging_openlist_flow(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
        content = path.read_text(encoding="utf-8")

        self.assertIn("staging_enabled: true", content)
        self.assertIn("staging_dir_name: \"_入库暂存\"", content)
        self.assertIn("/api/admin/scan/start", content)
        self.assertIn("strm_refresh_after_apply: true", content)
        self.assertNotIn("/api/admin/scan/star；", content)


if __name__ == "__main__":
    unittest.main()
