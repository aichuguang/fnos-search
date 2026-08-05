from __future__ import annotations

import copy
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.app import _cloud139_scan_filters_from_job, _sixpan_scan_filters_from_job
from fnos_media_import.constants import JOB_SUBMITTED, JOB_WAITING_OPENLIST, JOB_WAITING_ORGANIZER
from fnos_media_import.database import Database
from fnos_media_import.importers.base import ImportResult
from fnos_media_import.organizer.openlist_client import OpenListClient, OpenListItem
from fnos_media_import.organizer.service import OrganizerService, _remaining_retry_delay_seconds
from fnos_media_import.services.import_service import ImportService
from fnos_media_import.services.import_staging_service import validated_staging_plan_from_job
from fnos_media_import.services.organizer_dispatch_service import OrganizerDispatchService
from fnos_media_import.services.rclone_category_finalizer import RcloneCategoryFinalizer
from fnos_media_import.services.rclone_service import RcloneService
from tests.test_rclone_persisted_staging_plan import _persisted_plan


class PersistedPlanHardeningTests(unittest.TestCase):
    @staticmethod
    def _job(plan: dict) -> dict:
        return {
            "id": 42,
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }

    def test_future_plan_version_is_rejected(self) -> None:
        plan = {**_persisted_plan(42), "version": 3}

        with self.assertRaisesRegex(ValueError, "version=2"):
            validated_staging_plan_from_job(self._job(plan))

    def test_job_roots_must_be_exact_children_of_staging_category_roots(self) -> None:
        for key, value in (
            ("storage_job_root", "旧移动云/_入库暂存/其他/job-42"),
            ("openlist_job_root", "/旧挂载/_入库暂存/其他/job-42"),
        ):
            with self.subTest(key=key):
                plan = {**_persisted_plan(42), key: value}
                with self.assertRaisesRegex(ValueError, key):
                    validated_staging_plan_from_job(self._job(plan))


class WorkerCompletionBarrierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = (
            Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")

    def test_skipped_existing_ack_precedes_source_deletion(self) -> None:
        start = self.content.index('if ! ack_staging_file_completion "skipped_existing"')
        deletion = self.content.index('rclone_cmd deletefile "$source_remote_file"', start)
        failure_increment = self.content.index("failed_count=$((failed_count + 1))", start)

        self.assertLess(start, failure_increment)
        self.assertLess(failure_increment, deletion)

    def test_done_ack_precedes_cleanup_for_both_upload_backends(self) -> None:
        cmcc_ack = self.content.index(
            'if ! ack_staging_file_completion "done" "$category_key" "$current_filename"'
        )
        cmcc_cleanup = self.content.index(
            'finish_successful_file "$current_source_remote_file"', cmcc_ack
        )
        webdav_ack = self.content.index(
            'if ! ack_staging_file_completion "done" "$category_key" "$filename"',
            cmcc_cleanup,
        )
        webdav_cleanup = self.content.index(
            'finish_successful_file "$source_remote_file"', webdav_ack
        )

        self.assertLess(cmcc_ack, cmcc_cleanup)
        self.assertLess(webdav_ack, webdav_cleanup)
        self.assertIn('if [ -z "$APP_CALLBACK_URL" ]', self.content)


class StagedScanCompletenessTests(unittest.TestCase):
    @staticmethod
    def _task(expected_count: int = 2) -> dict:
        return {
            "id": 8,
            "job_id": 42,
            "category": "tv",
            "openlist_root_path": "/旧挂载/_入库暂存/电视剧/job-42",
            "raw_data": {
                "staging_plan": _persisted_plan(42),
                "scan_filters": {
                    "expected_names": ["01.mkv"],
                    "expected_paths": ["/分享源/Season 01/01.mkv"],
                    "expected_count": expected_count,
                },
            },
        }

    def test_staged_scan_is_unlimited_and_does_not_stop_on_duplicate_names(self) -> None:
        calls: list[dict] = []
        service = OrganizerService.__new__(OrganizerService)
        service.db = None
        service.organizer_config = {"max_scan_depth": 8, "max_files_per_task": 500}
        service.categories = {"tv": {"openlist_root_path": "/旧挂载/电视剧"}}
        service.openlist = SimpleNamespace(
            scan_videos=lambda _root, **kwargs: calls.append(kwargs) or []
        )

        service._scan_openlist_videos(
            self._task(), "/旧挂载/_入库暂存/电视剧/job-42"
        )

        self.assertEqual(calls[0]["max_files"], 0)
        self.assertEqual(calls[0]["expected_names"], [])
        self.assertEqual(calls[0]["expected_paths"], [])

    def test_expected_count_blocks_partial_visibility(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.db = None
        videos = [
            SimpleNamespace(path="/旧挂载/_入库暂存/电视剧/job-42/Season 01/01.mkv")
        ]

        message = service._scan_completeness_message(
            self._task(), videos, "/旧挂载/_入库暂存/电视剧/job-42"
        )

        self.assertIn("1/2", message)

    def test_openlist_client_zero_limit_scans_more_than_500_files(self) -> None:
        client = OpenListClient.__new__(OpenListClient)
        rows = [
            OpenListItem(
                name=f"E{index:04d}.mkv",
                path=f"/job/E{index:04d}.mkv",
                is_dir=False,
            )
            for index in range(600)
        ]
        client.list_dir = lambda _path, refresh=False: rows

        result = client.scan_videos("/job", max_files=0)

        self.assertEqual(len(result), 600)

    def test_relative_provider_paths_are_scoped_to_job_root(self) -> None:
        root = "/旧挂载/_入库暂存/电视剧/job-42"
        cloud_job = {
            "raw_data": {
                "selection": {
                    "selected_files": [
                        {"name": "01.mkv", "relative_path": "Season 01/01.mkv"},
                        {"name": "01.mkv", "relative_path": "Season 02/01.mkv"},
                    ]
                }
            }
        }
        sixpan_job = {
            "raw_data": {
                "parse": {
                    "files": [
                        {"name": "01.mkv", "path": "Season 01/01.mkv", "size": 30_000_000},
                        {"name": "01.mkv", "path": "Season 02/01.mkv", "size": 30_000_000},
                    ]
                }
            }
        }

        cloud_filters = _cloud139_scan_filters_from_job(cloud_job, root_path=root)
        sixpan_filters = _sixpan_scan_filters_from_job(sixpan_job, root_path=root)

        self.assertEqual(cloud_filters["expected_paths"], [])
        self.assertEqual(sixpan_filters["expected_paths"], [])
        self.assertEqual(cloud_filters["expected_count"], 2)
        self.assertEqual(sixpan_filters["expected_count"], 2)

    def test_explicit_openlist_path_remains_an_exact_visibility_check(self) -> None:
        root = "/旧挂载/_入库暂存/电视剧/job-42"
        filters = _cloud139_scan_filters_from_job(
            {
                "raw_data": {
                    "selection": {
                        "selected_files": [
                            {
                                "name": "E01.mkv",
                                "openlist_path": f"{root}/改名后/E01.mkv",
                            }
                        ]
                    }
                }
            },
            root_path=root,
        )

        self.assertEqual(filters["expected_paths"], [f"{root}/改名后/E01.mkv"])


class RcloneStartupRecoveryTests(unittest.TestCase):
    def test_waiting_organizer_without_task_is_redispatched(self) -> None:
        job = {
            "id": 42,
            "status": JOB_WAITING_ORGANIZER,
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {
                "staging_plan": _persisted_plan(42),
                "rclone_staging_manifest": {
                    "version": 1,
                    "source_paths": ["/旧夸克/电视剧/job-42/E01.mkv"],
                    "expected_file_count": 1,
                },
                "completion": {"rclone_run_id": 7},
            },
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            @staticmethod
            def list_jobs(**_kwargs) -> list[dict]:
                return [copy.deepcopy(job)]

            @staticmethod
            def list_organizer_tasks_by_job(*_args, **_kwargs) -> list[dict]:
                return []

            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return [
                    {
                        "id": 2,
                        "run_id": 7,
                        "job_id": 42,
                        "status": "done",
                        "filename": "E01.mkv",
                        "source_path": "/旧夸克/电视剧/job-42/E01.mkv",
                        "target_path": "旧移动云/_入库暂存/电视剧/job-42/E01.mkv",
                    }
                ]

            def add_event(self, *args) -> None:
                self.events.append(args)

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "电视剧"}}
        dispatched: list[tuple] = []
        service._dispatch_run_ready_to_organizer = (
            lambda result, payload: dispatched.append((result, payload)) or {"success": True}
        )

        result = service.recover_waiting_organizer_dispatches()

        self.assertEqual(result["recovered_job_ids"], [42])
        self.assertEqual(dispatched[0][0]["completed_items"][0]["job_id"], 42)

    def test_waiting_transfer_staged_job_is_requeued_after_restart(self) -> None:
        job = {
            "id": 42,
            "status": "waiting_transfer",
            "source_type": "quark",
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": _persisted_plan(42)},
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            @staticmethod
            def list_jobs(**_kwargs) -> list[dict]:
                return [copy.deepcopy(job)]

            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return []

            def add_event(self, *args) -> None:
                self.events.append(args)

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True, "queued": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertEqual(result["requeued_job_ids"], [42])
        self.assertEqual(starts[0]["staging_run"]["job_dir_name"], "job-42")

    def test_partially_completed_transferring_job_is_requeued(self) -> None:
        job = {
            "id": 42,
            "status": "transferring",
            "source_type": "quark",
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {
                "staging_plan": _persisted_plan(42),
                "expected_file_count": 2,
            },
        }

        class FakeDatabase:
            @staticmethod
            def list_jobs(*, status: str, **_kwargs) -> list[dict]:
                return [copy.deepcopy(job)] if status == "transferring" else []

            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return [
                    {
                        "id": 1,
                        "job_id": 42,
                        "status": "done",
                        "target_path": "/target/E01.mkv",
                    }
                ]

            @staticmethod
            def add_event(*_args) -> None:
                return None

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertEqual(result["requeued_job_ids"], [42])
        self.assertEqual(len(starts), 1)

    def test_unknown_expected_count_with_one_done_event_is_requeued(self) -> None:
        job = {
            "id": 42,
            "status": "transferring",
            "source_type": "quark",
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {
                "staging_plan": _persisted_plan(42),
                "expected_file_count": 0,
            },
        }

        class FakeDatabase:
            @staticmethod
            def list_jobs(*, status: str, **_kwargs) -> list[dict]:
                return [copy.deepcopy(job)] if status == "transferring" else []

            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return [
                    {
                        "id": 1,
                        "job_id": 42,
                        "status": "done",
                        "target_path": "/target/E01.mkv",
                    }
                ]

            @staticmethod
            def add_event(*_args) -> None:
                return None

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        result = service.recover_unstarted_staging_jobs()

        self.assertEqual(result["requeued_job_ids"], [42])
        self.assertEqual(result["completed_evidence_job_ids"], [])
        self.assertEqual(len(starts), 1)

    def test_direct_route_waiting_organizer_reuses_normal_dispatcher(self) -> None:
        plan = {
            **_persisted_plan(42),
            "route": "cloud139_direct",
            "provider_target_path": "旧移动云/_入库暂存/电视剧/job-42",
            "storage_backend": "cmcc_api",
            "quark_source_category_root": "",
            "quark_job_root": "",
        }
        job = {
            "id": 42,
            "status": JOB_WAITING_ORGANIZER,
            "source_type": "cloud139",
            "category": "tv",
            "target_route": "cloud139_direct",
            "raw_data": {"staging_plan": plan},
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            @staticmethod
            def list_jobs(**_kwargs) -> list[dict]:
                return [copy.deepcopy(job)]

            @staticmethod
            def list_organizer_tasks_by_job(*_args, **_kwargs) -> list[dict]:
                return []

            def add_event(self, *args) -> None:
                self.events.append(args)

        service = RcloneService.__new__(RcloneService)
        service.db = FakeDatabase()
        direct_calls: list[tuple[dict, str]] = []
        service._direct_ready_handler = lambda result, reason: (
            direct_calls.append((result, reason))
            or {"success": True, "queued": True, "task_id": 8}
        )
        service._dispatch_run_ready_to_organizer = lambda *_args, **_kwargs: self.fail(
            "直转任务恢复不应借用 rclone 文件事件计划"
        )

        result = service.recover_waiting_organizer_dispatches()

        self.assertEqual(result["recovered_job_ids"], [42])
        self.assertEqual(direct_calls[0][0]["job"]["raw_data"]["staging_plan"]["openlist_job_root"], "/旧挂载/_入库暂存/电视剧/job-42")
        self.assertEqual(direct_calls[0][1], "waiting_organizer_startup_recovery")


class RcloneFinalizerHardeningTests(unittest.TestCase):
    def test_waiting_openlist_is_not_rolled_back_by_duplicate_category_callback(self) -> None:
        class FakeDatabase:
            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return [{"job_id": 8, "status": "done", "target_path": "/target/E01.mkv"}]

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict:
                return {8: {"id": 8, "status": JOB_WAITING_OPENLIST}}

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

        finalizer = RcloneCategoryFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {"label": "电视剧"}},
            category_key=lambda *_args: "tv",
            event_matches=lambda *_args, **_kwargs: True,
            feasibility=lambda *_args: self.fail("等待 OpenList 的任务不应重新判定"),
            finish_ready=lambda *_args, **_kwargs: self.fail("等待 OpenList 的任务不应回退"),
        )

        result = finalizer.finalize(
            3,
            "电视剧",
            {"status": "category_done", "moved_count": 1, "failed_count": 0},
        )

        self.assertTrue(result["skipped"])

    def test_category_finalizer_uses_complete_event_reader(self) -> None:
        events = [
            {"id": index + 1, "job_id": 9, "status": "done", "target_path": f"/target/{index}.mkv"}
            for index in range(1201)
        ]

        class FakeDatabase:
            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return events

            @staticmethod
            def list_rclone_file_events(**_kwargs) -> list[dict]:
                raise AssertionError("不应再使用 1000 条上限读取")

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict:
                return {9: {"id": 9, "status": "transferring", "raw_data": {"expected_file_count": 1201}}}

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

        finished: list[list] = []
        finalizer = RcloneCategoryFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {}},
            category_key=lambda *_args: "tv",
            event_matches=lambda *_args, **_kwargs: True,
            feasibility=lambda _job, matched, _exit: {
                "ready": len(matched) == 1201,
                "status": "done",
                "message": "ok",
            },
            finish_ready=lambda _run, _key, _category, items, _events, **_kwargs: (
                finished.append(items) or {"success": True}
            ),
        )

        finalizer.finalize(
            4,
            "电视剧",
            {"status": "category_done", "moved_count": 1201, "failed_count": 0},
        )

        self.assertEqual(len(finished[0][0][1]), 1201)


class OrganizerRecoverySchedulingTests(unittest.TestCase):
    def test_persisted_retry_deadline_is_not_reset_to_five_seconds(self) -> None:
        delay = _remaining_retry_delay_seconds("2099-01-01T00:00:00Z", fallback=5)

        self.assertGreater(delay, 5)

    def test_database_does_not_recover_recent_run_owned_by_other_process(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = root / f"organizer-owner-{uuid.uuid4().hex}.db"
        try:
            database = Database(db_path)
            database.init_schema()
            task_id = database.create_organizer_task(
                category="tv",
                openlist_root_path="/job-42",
                status="executing",
            )
            database.create_organizer_run(task_id, owner_id="old-process")

            result = database.recover_stale_organizer_runs(
                older_than_minutes=30,
                owner_id="new-process",
            )

            self.assertEqual(result["count"], 0)
            self.assertEqual(database.get_organizer_task(task_id, include_children=False)["status"], "executing")
        finally:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()

    def test_database_does_not_recover_long_run_while_lease_is_live(self) -> None:
        root = Path(__file__).resolve().parents[1]
        db_path = root / f"organizer-stale-{uuid.uuid4().hex}.db"
        try:
            database = Database(db_path)
            database.init_schema()
            task_id = database.create_organizer_task(
                category="tv",
                openlist_root_path="/job-43",
                status="executing",
            )
            run_id = database.create_organizer_run(task_id, owner_id="old-process")
            with database.connect() as connection:
                connection.execute(
                    "UPDATE organizer_runs SET started_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                    (run_id,),
                )

            result = database.recover_stale_organizer_runs(
                older_than_minutes=30,
                owner_id="new-process",
            )

            self.assertEqual(result["count"], 0)
            self.assertEqual(database.get_organizer_task(task_id, include_children=False)["status"], "executing")
        finally:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()


class OrganizerRuntimeSwapTests(unittest.TestCase):
    def test_dispatcher_switches_to_new_organizer_after_reload(self) -> None:
        calls: list[str] = []

        class FakeOrganizer:
            enabled = True
            openlist = SimpleNamespace(configured=True)

            def __init__(self, name: str) -> None:
                self.name = name

            def enqueue_from_completed_directory(self, **_kwargs) -> dict:
                calls.append(self.name)
                return {"success": True, "queued": True, "message": self.name}

        database = SimpleNamespace(
            get_job=lambda _job_id: {"id": 42, "category": "tv", "raw_data": {}},
            add_event=lambda *_args, **_kwargs: None,
        )
        dispatcher = OrganizerDispatchService(
            database=database,
            organizer=FakeOrganizer("old"),
            resolve_plan=lambda _job: None,
            resolve_rclone_plan=lambda _item: {"root_path": "/job-42", "payload_extra": {}},
            set_completion_stage=lambda job, *_args, **_kwargs: job,
            invalid_virtual_path=lambda _path: False,
        )
        dispatcher.set_organizer(FakeOrganizer("new"))

        dispatcher.enqueue_rclone_completed_items(
            {
                "completed_items": [
                    {
                        "job_id": 42,
                        "job": {"id": 42, "category": "tv"},
                        "category_label": "电视剧",
                    }
                ]
            },
            {"run_id": 1},
        )

        self.assertEqual(calls, ["new"])


class StrmCleanupSafetyTests(unittest.TestCase):
    def test_generic_wrapper_directory_is_never_used_as_old_strm_name(self) -> None:
        for wrapper in ("4K", "1080P", "合集", "全集", "版本A", "字幕"):
            with self.subTest(wrapper=wrapper):
                task = {
                    "openlist_root_path": "/挂载/_入库暂存/电视剧/job-42",
                    "mappings": [
                        {
                            "status": "ready",
                            "source_path": f"/挂载/_入库暂存/电视剧/job-42/{wrapper}/E01.mkv",
                        }
                    ],
                }
                self.assertEqual(OrganizerService._old_strm_resource_name(task), "")

    def test_cleanup_uses_persisted_refresh_prefix(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {
            "strm_cleanup_old_before_refresh": True,
            "strm_refresh_prefix_tv": "/当前配置/电视剧",
        }
        removed: list[str] = []
        service.openlist = SimpleNamespace(
            exists=lambda _path: True,
            remove_path=lambda path: removed.append(path),
        )
        service._cleanup_old_local_strm_dir = lambda *_args, **_kwargs: {
            "enabled": False,
            "skipped": True,
        }
        task = {
            "openlist_root_path": "/挂载/_入库暂存/电视剧/job-42",
            "mappings": [
                {
                    "status": "ready",
                    "source_path": "/挂载/_入库暂存/电视剧/job-42/原始剧名/E01.mkv",
                }
            ],
        }

        service._cleanup_old_strm_dir_for_task(
            task,
            "tv",
            ["标准剧名 (2026)"],
            refresh_prefix="/固化挂载/电视剧",
        )

        self.assertEqual(removed, ["/固化挂载/电视剧/原始剧名"])


class Cloud139RefreshDeferralTests(unittest.TestCase):
    def test_staged_cloud139_job_never_refreshes_fnos_after_submit(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 42,
                    "category": "tv",
                    "target_route": "cloud139_direct",
                    "status": "provider_submitting",
                    "raw_data": {
                        "provider_submission_fence": {
                            "version": 1,
                            "state": "submitting",
                            "attempt": 1,
                        },
                        "staging_plan": {
                            **_persisted_plan(42),
                            "route": "cloud139_direct",
                            "provider_target_path": "旧移动云/_入库暂存/电视剧/job-42",
                            "storage_backend": "cmcc_api",
                            "quark_source_category_root": "",
                            "quark_job_root": "",
                        }
                    },
                }
                self.events: list[tuple] = []

            def get_job(self, _job_id: int) -> dict:
                return copy.deepcopy(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def update_job_if_status(self, _job_id: int, expected_statuses, **values) -> bool:
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            def add_event(self, *args) -> None:
                self.events.append(args)

        service = ImportService.__new__(ImportService)
        service.db = FakeDatabase()
        service.cloud139 = SimpleNamespace(
            refresh_after_submit=True,
            import_resource=lambda **kwargs: ImportResult(
                True,
                JOB_SUBMITTED,
                "已提交",
                target_path=kwargs["save_path"],
                raw_data={"directory_plan": {}},
            ),
        )
        service._extract_cloud139_native_selection = lambda _payload: None
        service._target_root_is_resource = lambda _payload: False
        service._defer_media_refresh_to_organizer = lambda: False
        service._record_media_refresh = lambda *_args, **_kwargs: self.fail("不应刷新飞牛")
        service._schedule_delayed_media_refresh = lambda *_args, **_kwargs: self.fail("不应安排延迟刷新")

        result = service._submit_cloud139_job(
            42,
            "测试剧",
            "https://example.invalid/share",
            "旧移动云/_入库暂存/电视剧/job-42",
            "",
            {"label": "电视剧"},
            "tv",
        )

        self.assertTrue(result["success"])
        self.assertTrue(any("跳过飞牛刷新" in event[2] for event in service.db.events))


if __name__ == "__main__":
    unittest.main()
