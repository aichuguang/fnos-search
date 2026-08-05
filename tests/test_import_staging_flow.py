from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.constants import (
    JOB_PROVIDER_SUBMITTING,
    JOB_SUBMITTED,
    ROUTE_CLOUD139_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
    SOURCE_MAGNET,
)
from fnos_media_import.database import Database
from fnos_media_import.importers.base import ImportResult
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.import_job_service import ImportJobCreationService, ImportJobRetryService
from fnos_media_import.services.import_staging_service import (
    ImportStagingService,
    map_staging_path_to_openlist,
    staging_category_root,
    validated_staging_plan_from_job,
)
from fnos_media_import.services.import_service import ImportService
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.runtime_builder import rclone_runtime_config
from tests.test_rclone_persisted_staging_plan import _persisted_plan


def _provider_submitting_raw(raw_data: dict | None = None) -> dict:
    return {
        **(raw_data or {}),
        "provider_submission_fence": {
            "version": 1,
            "state": "submitting",
            "attempt": 1,
        },
    }


class ImportStagingPathTests(unittest.TestCase):
    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            raw={
                "organizer": {
                    "enabled": True,
                    "staging_enabled": True,
                    "staging_dir_name": "_入库暂存",
                    "strm_refresh_prefix_tv": "/飞牛NAS/电视剧",
                },
                "openlist": {"base_url": "http://openlist.test"},
                "rclone": {"upload_backend": "cmcc_api"},
                "cmcc_upload": {"enabled": True, "backend": "cmcc_api"},
                "cloud139": {"target_root_path": "移动云盘A", "fnos_mount_name": "移动云"},
                "sixpan": {"fnos_mount_name": "清云"},
            }
        )

    @staticmethod
    def _mobile_category() -> dict:
        return {
            "label": "电视剧",
            "quark_save_path": "/离线下载/电视剧",
            "mobile_target_path": "移动云盘A/电视剧",
            "cloud139_target_path": "移动云盘A/电视剧",
            "cloud139_fnos_target_path": "/移动云/电视剧",
        }

    def test_staging_category_is_a_sibling_of_the_final_category(self) -> None:
        self.assertEqual(
            staging_category_root("/移动云/电视剧", category_label="电视剧"),
            "/移动云/_入库暂存/电视剧",
        )
        self.assertEqual(
            staging_category_root("移动云盘A/电视剧", category_label="电视剧"),
            "移动云盘A/_入库暂存/电视剧",
        )
        self.assertEqual(
            staging_category_root("/电视剧", category_label="电视剧"),
            "/_入库暂存/电视剧",
        )

    def test_quark_job_uses_job_source_and_mobile_staging_roots(self) -> None:
        plan = ImportStagingService(self._config()).build(
            job_id=42,
            route=ROUTE_QUARK_TO_MOBILE,
            category_key="tv",
            category=self._mobile_category(),
        )

        self.assertEqual(plan["provider_target_path"], "/离线下载/电视剧/job-42")
        self.assertEqual(plan["storage_staging_category_root"], "移动云盘A/_入库暂存/电视剧")
        self.assertEqual(plan["storage_job_root"], "移动云盘A/_入库暂存/电视剧/job-42")
        self.assertEqual(plan["openlist_final_category_root"], "/移动云/电视剧")
        self.assertEqual(plan["openlist_job_root"], "/移动云/_入库暂存/电视剧/job-42")
        self.assertEqual(plan["openlist_refresh_prefix"], "/飞牛NAS/电视剧")

    def test_cloud139_direct_job_uses_cmcc_staging_job_root(self) -> None:
        plan = ImportStagingService(self._config()).build(
            job_id=43,
            route=ROUTE_CLOUD139_DIRECT,
            category_key="tv",
            category=self._mobile_category(),
        )

        self.assertEqual(plan["provider_target_path"], "移动云盘A/_入库暂存/电视剧/job-43")
        self.assertEqual(plan["openlist_job_root"], "/移动云/_入库暂存/电视剧/job-43")

    def test_sixpan_job_uses_native_and_openlist_staging_roots(self) -> None:
        category = {
            "label": "电视剧",
            "sixpan_save_path": "/电视剧",
            "sixpan_fnos_target_path": "/清云/电视剧",
        }
        plan = ImportStagingService(self._config()).build(
            job_id=44,
            route=ROUTE_SIXPAN_OFFLINE,
            category_key="tv",
            category=category,
        )

        self.assertEqual(plan["provider_target_path"], "/_入库暂存/电视剧/job-44")
        self.assertEqual(plan["openlist_job_root"], "/清云/_入库暂存/电视剧/job-44")
        self.assertEqual(plan["openlist_final_category_root"], "/清云/电视剧")

    def test_staging_upload_path_maps_to_the_exact_openlist_job(self) -> None:
        plan = ImportStagingService(self._config()).build(
            job_id=45,
            route=ROUTE_QUARK_TO_MOBILE,
            category_key="tv",
            category=self._mobile_category(),
        )

        mapped = map_staging_path_to_openlist(
            "移动云盘A/_入库暂存/电视剧/job-45/原始目录/E01.mkv",
            plan,
        )

        self.assertEqual(mapped, "/移动云/_入库暂存/电视剧/job-45/原始目录/E01.mkv")

    def test_staging_is_not_applied_when_organizer_is_disabled(self) -> None:
        config = self._config()
        config.raw["organizer"]["enabled"] = False

        plan = ImportStagingService(config).build(
            job_id=46,
            route=ROUTE_QUARK_TO_MOBILE,
            category_key="tv",
            category=self._mobile_category(),
        )

        self.assertEqual(plan, {})

    def test_staging_fails_before_submit_when_openlist_is_not_configured(self) -> None:
        config = self._config()
        config.raw["openlist"] = {}

        with self.assertRaisesRegex(ValueError, "OpenList 未配置"):
            ImportStagingService(config).build(
                job_id=47,
                route=ROUTE_QUARK_TO_MOBILE,
                category_key="tv",
                category=self._mobile_category(),
            )

    def test_staging_segment_cannot_turn_into_a_child_of_the_final_category(self) -> None:
        self.assertEqual(
            staging_category_root(
                "/移动云/电视剧",
                category_label="电视剧",
                staging_dir_name="电视剧",
            ),
            "/移动云/_入库暂存/电视剧",
        )

    def test_sixpan_legacy_save_path_is_normalized_consistently(self) -> None:
        config = self._config()
        category = {
            "label": "电视剧",
            "sixpan_save_path": "/离线下载/电视剧",
        }

        plan = ImportStagingService(config).build(
            job_id=48,
            route=ROUTE_SIXPAN_OFFLINE,
            category_key="tv",
            category=category,
        )

        self.assertEqual(plan["storage_final_category_root"], "/电视剧")
        self.assertEqual(plan["provider_target_path"], "/_入库暂存/电视剧/job-48")
        self.assertEqual(plan["openlist_final_category_root"], "/清云/电视剧")

    def test_staging_plan_rejects_dot_dot_in_provider_source_root(self) -> None:
        category = self._mobile_category()
        category["quark_save_path"] = "/离线下载/../电视剧"

        with self.assertRaisesRegex(ValueError, "不安全"):
            ImportStagingService(self._config()).build(
                job_id=49,
                route=ROUTE_QUARK_TO_MOBILE,
                category_key="tv",
                category=category,
            )

    def test_persisted_staging_plan_rejects_dot_segments_in_final_roots(self) -> None:
        plan = _persisted_plan(50)
        plan["storage_final_category_root"] = "旧移动云/../电视剧"
        job = {
            "id": 50,
            "category": "tv",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "raw_data": {"staging_plan": plan},
        }

        with self.assertRaisesRegex(ValueError, "storage_final_category_root_path_segments"):
            validated_staging_plan_from_job(job)

    def test_staging_mapper_refuses_traversal_segments(self) -> None:
        plan = ImportStagingService(self._config()).build(
            job_id=51,
            route=ROUTE_QUARK_TO_MOBILE,
            category_key="tv",
            category=self._mobile_category(),
        )

        mapped = map_staging_path_to_openlist(
            "移动云盘A/_入库暂存/电视剧/job-51/../job-50/E01.mkv",
            plan,
        )

        self.assertEqual(mapped, "")


class ImportJobStagingLifecycleTests(unittest.TestCase):
    def test_new_job_persists_staging_plan_before_provider_submission(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job: dict = {}
                self.events: list[tuple] = []

            def create_job(self, values: dict) -> tuple[int, bool]:
                self.job = {"id": 7, **values}
                return 7, True

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def update_job_if_status(self, _job_id: int, expected_statuses, **values) -> bool:
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            def add_event(self, *values) -> None:
                self.events.append(values)

        class Link:
            route = ROUTE_QUARK_TO_MOBILE
            source_type = "quark"
            url = "https://pan.quark.cn/s/example"
            password = ""
            supported = True

            @staticmethod
            def to_dict() -> dict:
                return {"route": ROUTE_QUARK_TO_MOBILE, "source_type": "quark"}

        database = FakeDatabase()
        submitted: dict = {}

        def submit_quark(_job_id, _title, _url, target_path, *_args, **_kwargs):
            submitted["target_path"] = target_path
            submitted["job"] = database.get_job(7)
            return {"success": True, "job": submitted["job"]}

        service = ImportJobCreationService(
            database=database,
            config=SimpleNamespace(
                raw={"routes": {}},
                category=lambda _key: {"label": "电视剧"},
            ),
            detect_link=lambda *_args, **_kwargs: Link(),
            job_source_url=lambda url, _payload: url,
            target_path=lambda *_args, **_kwargs: "/离线下载/电视剧",
            staging_plan=lambda **_kwargs: {
                "enabled": True,
                "provider_target_path": "/离线下载/电视剧/job-7",
                "openlist_job_root": "/移动云/_入库暂存/电视剧/job-7",
            },
            submit_quark=submit_quark,
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

        service.create({"title": "测试剧", "url": Link.url, "category": "tv"})

        self.assertEqual(submitted["target_path"], "/离线下载/电视剧/job-7")
        self.assertTrue(submitted["job"]["raw_data"]["staging_plan"]["enabled"])

    def test_retry_uses_persisted_staging_root_but_history_keeps_original_target(self) -> None:
        staged = {
            "target_path": "/离线下载/电视剧/job-7/测试剧",
            "raw_data": {
                "staging_plan": {
                    "enabled": True,
                    "provider_target_path": "/离线下载/电视剧/job-7",
                }
            },
        }
        historical = {"target_path": "/离线下载/电视剧/旧资源", "raw_data": {}}

        self.assertEqual(
            ImportJobRetryService._provider_target_path(staged),
            "/离线下载/电视剧/job-7",
        )
        self.assertEqual(
            ImportJobRetryService._provider_target_path(historical),
            "/离线下载/电视剧/旧资源",
        )

    def test_provider_cannot_inject_a_staging_plan_into_a_legacy_job(self) -> None:
        class FakeDatabase:
            @staticmethod
            def get_job(_job_id: int) -> dict:
                return {"id": 8, "raw_data": {"request": {"url": "https://example.test"}}}

        service = ImportService.__new__(ImportService)
        service.db = FakeDatabase()

        merged = service._merge_provider_raw_data(
            8,
            {
                "provider": "legacy",
                "staging_plan": {"enabled": True, "job_id": 999},
                "staging_plan_required": True,
            },
        )

        self.assertEqual(merged["provider"], "legacy")
        self.assertNotIn("staging_plan", merged)
        self.assertNotIn("staging_plan_required", merged)


class RcloneStagingEnvironmentTests(unittest.TestCase):
    def test_rclone_destinations_point_to_staging_only_when_enabled(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {"staging_enabled": True, "staging_dir_name": "_入库暂存"}
        service.categories = {
            "tv": {
                "label": "电视剧",
                "quark_save_path": "/离线下载/电视剧",
                "mobile_target_path": "移动云盘A/电视剧",
                "cloud139_target_path": "移动云盘A/电视剧",
            }
        }
        service.cloud139_config = {"target_root_path": "移动云盘A"}

        mapping = service._category_dir_env({})

        self.assertEqual(mapping["RCLONE_SRC_TV_DIR"], "离线下载/电视剧")
        self.assertEqual(mapping["RCLONE_DST_TV_DIR"], "移动云盘A/_入库暂存/电视剧")
        self.assertEqual(
            service._cmcc_parent_path_for_category("tv", service.categories["tv"]),
            "移动云盘A/_入库暂存/电视剧",
        )

    def test_runtime_disables_worker_fnos_refresh_when_organizer_owns_completion(self) -> None:
        config = SimpleNamespace(
            raw={
                "rclone": {"refresh_in_worker": "true", "auto_interval_minutes": 15},
                "organizer": {"enabled": True, "staging_enabled": True},
                "openlist": {"base_url": "http://openlist.test"},
            }
        )

        runtime = rclone_runtime_config(config)

        self.assertTrue(runtime["staging_enabled"])
        self.assertEqual(runtime["refresh_in_worker"], "false")
        self.assertEqual(runtime["auto_interval_minutes"], 0)


class RcloneStagingJobMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"staging-match-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_job_directory_id_beats_fuzzy_title_matching(self) -> None:
        first_id, _ = self.database.create_job(
            {
                "title": "相似剧集 第一版",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/first",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧/job-1",
                "status": "waiting_transfer",
                "raw_data": {"staging_plan": {"enabled": True, "job_id": 1}},
            }
        )
        second_id, _ = self.database.create_job(
            {
                "title": "相似剧集 第二版",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/second",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧/job-2",
                "status": "waiting_transfer",
                "raw_data": {"staging_plan": {"enabled": True, "job_id": 2}},
            }
        )
        self.assertEqual((first_id, second_id), (1, 2))

        matched = self.database.find_job_for_rclone_callback(
            category="电视剧",
            filename="E01.mkv",
            source_path="离线下载/电视剧/job-1/相似剧集/E01.mkv",
            target_path="webdav/_入库暂存/电视剧/job-1/相似剧集/E01.mkv",
        )

        self.assertEqual(matched["id"], first_id)

    def test_conflicting_job_ids_do_not_fall_back_to_another_task(self) -> None:
        self.database.create_job(
            {
                "title": "测试剧",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/example",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧/job-1",
                "status": "waiting_transfer",
                "raw_data": {"staging_plan": {"enabled": True, "job_id": 1}},
            }
        )

        matched = self.database.find_job_for_rclone_callback(
            category="电视剧",
            filename="E01.mkv",
            source_path="离线下载/电视剧/job-1/E01.mkv",
            target_path="webdav/_入库暂存/电视剧/job-99/E01.mkv",
        )

        self.assertIsNone(matched)

    def test_nested_job_like_resource_name_does_not_override_the_staging_job(self) -> None:
        self.database.create_job(
            {
                "title": "资源目录名像任务号",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/example",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧/job-1",
                "status": "waiting_transfer",
                "raw_data": {"staging_plan": {"enabled": True, "job_id": 1}},
            }
        )

        matched = self.database.find_job_for_rclone_callback(
            category="电视剧",
            filename="E01.mkv",
            source_path="离线下载/电视剧/job-1/job-99/E01.mkv",
            target_path="webdav/_入库暂存/电视剧/job-1/job-99/E01.mkv",
        )

        self.assertEqual(matched["id"], 1)

    def test_invalid_job_directory_id_does_not_use_fuzzy_fallback(self) -> None:
        self.database.create_job(
            {
                "title": "测试剧",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/example",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧/job-1",
                "status": "waiting_transfer",
                "raw_data": {"staging_plan": {"enabled": True, "job_id": 1}},
            }
        )

        matched = self.database.find_job_for_rclone_callback(
            category="电视剧",
            filename="测试剧 E01.mkv",
            source_path="离线下载/电视剧/job-0/测试剧 E01.mkv",
            target_path="webdav/_入库暂存/电视剧/job-0/测试剧 E01.mkv",
        )

        self.assertIsNone(matched)


class SixPanStagingSubmissionTests(unittest.TestCase):
    def test_sixpan_submission_receives_the_persisted_job_save_path(self) -> None:
        captured: dict = {}

        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 9,
                    "status": JOB_PROVIDER_SUBMITTING,
                    "raw_data": _provider_submitting_raw({
                        "staging_plan": {
                            "enabled": True,
                            "provider_target_path": "/_入库暂存/电视剧/job-9",
                        },
                        "staging_plan_required": True,
                    }),
                }

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def update_job_if_status(self, _job_id: int, expected_statuses, **values) -> bool:
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

        class FakeImporter:
            refresh_after_submit = False

            @staticmethod
            def import_resource(**kwargs) -> ImportResult:
                captured.update(kwargs)
                return ImportResult(
                    True,
                    JOB_SUBMITTED,
                    "已提交",
                    external_task_id="sixpan-task",
                    target_path=kwargs["save_path"],
                    raw_data={
                        "provider": "sixpan",
                        # Provider evidence is untrusted with regard to the
                        # immutable per-job staging contract.
                        "staging_plan": {"enabled": False},
                        "staging_plan_required": False,
                    },
                )

        service = ImportService.__new__(ImportService)
        service.db = FakeDatabase()
        service._generic_importer = lambda _source_type: FakeImporter()
        service._extract_ignore_files = lambda _payload: ["sample.txt"]

        result = service._submit_generic_job(
            9,
            "测试剧",
            "magnet:?xt=urn:btih:test",
            "/_入库暂存/电视剧/job-9",
            {"label": "电视剧"},
            SOURCE_MAGNET,
            request_payload={
                "update_context": {
                    "canonical_resource_root": "/清云/电视剧/测试剧 (2026)",
                }
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(captured["save_path"], "/_入库暂存/电视剧/job-9")
        self.assertEqual(captured["ignore_files"], ["sample.txt"])
        self.assertTrue(result["job"]["raw_data"]["staging_plan"]["enabled"])
        self.assertEqual(
            result["job"]["raw_data"]["staging_plan"]["provider_target_path"],
            "/_入库暂存/电视剧/job-9",
        )
        self.assertTrue(result["job"]["raw_data"]["staging_plan_required"])
        self.assertEqual(
            result["job"]["raw_data"]["request"]["update_context"]["canonical_resource_root"],
            "/清云/电视剧/测试剧 (2026)",
        )

    def test_nested_sixpan_ignore_files_are_frozen_for_organizer_scan(self) -> None:
        captured: dict = {}

        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 10,
                    "status": JOB_PROVIDER_SUBMITTING,
                    "raw_data": _provider_submitting_raw({"staging_plan": {"enabled": True}}),
                }

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def update_job_if_status(self, _job_id: int, expected_statuses, **values) -> bool:
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

        class FakeImporter:
            refresh_after_submit = False

            @staticmethod
            def import_resource(**kwargs) -> ImportResult:
                captured.update(kwargs)
                return ImportResult(
                    True,
                    JOB_SUBMITTED,
                    "已提交",
                    external_task_id="sixpan-task-10",
                    target_path=kwargs["save_path"],
                    raw_data={"provider": "sixpan"},
                )

        service = ImportService.__new__(ImportService)
        service.db = FakeDatabase()
        service._generic_importer = lambda _source_type: FakeImporter()

        result = service._submit_generic_job(
            10,
            "测试剧",
            "magnet:?xt=urn:btih:test",
            "/_入库暂存/电视剧/job-10",
            {"label": "电视剧"},
            SOURCE_MAGNET,
            request_payload={"sixpan_selection": {"ignore_files": ["video-2", "sample"]}},
        )

        self.assertEqual(captured["ignore_files"], ["video-2", "sample"])
        self.assertEqual(
            result["job"]["raw_data"]["request"]["ignore_files"],
            ["video-2", "sample"],
        )

    def test_completed_staged_sixpan_never_refreshes_fnos_after_runtime_disable(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 11,
                    "status": JOB_PROVIDER_SUBMITTING,
                    "raw_data": _provider_submitting_raw({"staging_plan": {"enabled": True}}),
                }

            def get_job(self, _job_id: int) -> dict:
                return dict(self.job)

            def update_job(self, _job_id: int, **values) -> None:
                self.job.update(values)

            def update_job_if_status(self, _job_id: int, expected_statuses, **values) -> bool:
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

        class FakeImporter:
            refresh_after_submit = False

            @staticmethod
            def import_resource(**kwargs) -> ImportResult:
                return ImportResult(
                    True,
                    "done",
                    "已完成",
                    external_task_id="sixpan-task-11",
                    target_path=kwargs["save_path"],
                    raw_data={"provider": "sixpan"},
                )

        service = ImportService.__new__(ImportService)
        service.db = FakeDatabase()
        service._generic_importer = lambda _source_type: FakeImporter()
        service._extract_ignore_files = lambda _payload: []
        service._defer_media_refresh_to_organizer = lambda: False
        service._sixpan_refresh_hints = lambda *_args, **_kwargs: []
        service._record_media_refresh = lambda *_args, **_kwargs: self.fail(
            "已固化暂存计划的六盘任务不应刷新飞牛"
        )

        result = service._submit_generic_job(
            11,
            "测试剧",
            "magnet:?xt=urn:btih:test",
            "/_入库暂存/电视剧/job-11",
            {"label": "电视剧"},
            SOURCE_MAGNET,
            request_payload={},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["job"]["status"], "waiting_organizer")


class OrganizerRefreshOrderTests(unittest.TestCase):
    def test_staging_source_cleanup_stops_at_staging_category_root(self) -> None:
        category = {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}
        task = {
            "category": "tv",
            "openlist_root_path": "/移动云/_入库暂存/电视剧/job-1/原始剧名",
            "raw_data": {"target_root_path": "/移动云/电视剧"},
        }
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"tv": category}

        target_category = service._target_category_for_task(task, category)
        operations = service._operations_for_mappings(
            [
                {
                    "source_path": "/移动云/_入库暂存/电视剧/job-1/原始剧名/E01.mkv",
                    "target_path": "/移动云/电视剧/标准剧名 (2026)/Season 01/标准剧名 (2026) - S01E01.mkv",
                    "status": "ready",
                    "reason": [],
                }
            ],
            target_category,
        )

        cleanup_paths = [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"]
        self.assertEqual(target_category["source_category_root_path"], "/移动云/_入库暂存/电视剧")
        self.assertIn("/移动云/_入库暂存/电视剧/job-1/原始剧名", cleanup_paths)
        self.assertIn("/移动云/_入库暂存/电视剧/job-1", cleanup_paths)
        self.assertNotIn("/移动云/_入库暂存/电视剧", cleanup_paths)
        self.assertNotIn("/移动云/电视剧", cleanup_paths)

    def test_openlist_folder_refresh_runs_only_after_final_target_confirmation(self) -> None:
        trace: list[str] = []

        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 1,
                    "category": "tv",
                    "openlist_root_path": "/移动云/_入库暂存/电视剧/job-1",
                    "mappings": [],
                    "operations": [],
                    "evidence": {},
                }

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = operations

            @staticmethod
            def create_organizer_run(_task_id: int) -> int:
                return 3

            @staticmethod
            def update_organizer_task(_task_id: int, **values) -> None:
                if values.get("status") == "done":
                    trace.append("finalize_task")
                return None

            @staticmethod
            def update_organizer_run(_run_id: int, status: str, **_values) -> None:
                if status == "done":
                    trace.append("finalize_run")
                return None

            @staticmethod
            def release_organizer_locks(**_values) -> None:
                return None

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []

        def sync_linked_job(*_args, **kwargs) -> None:
            if kwargs.get("stage") == "done":
                trace.append("sync_done")

        service._sync_linked_job = sync_linked_job
        service._cleanup_source_empty_dirs_after_apply = lambda _task: {}

        def confirm(_task: dict) -> dict:
            trace.append("confirm")
            return {"success": True, "organized_target_path": "/移动云/电视剧/测试剧", "target_dirs": []}

        def refresh(_task_id: int, _task: dict) -> dict:
            trace.append("refresh_openlist")
            return {"enabled": True, "count": 1}

        service._confirm_standardized_targets = confirm
        service._refresh_openlist_strm_for_task = refresh
        service._refresh_fnos_if_needed = lambda _task_id: trace.append("schedule_fnos") or True

        result = service.apply_task(1)

        self.assertTrue(result["success"])
        self.assertTrue(result["fnos_refresh_scheduled"])
        self.assertEqual(
            trace,
            ["confirm", "refresh_openlist", "finalize_run", "finalize_task", "sync_done", "schedule_fnos"],
        )

    def test_fnos_refresh_switch_off_does_not_schedule(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"refresh_fnos_after_apply": False}
        service.fnos = SimpleNamespace(refresh=lambda *_args, **_kwargs: self.fail("开关关闭时不应刷新飞牛"))
        service.db = SimpleNamespace(
            get_organizer_task=lambda *_args, **_kwargs: self.fail("开关关闭时不应读取任务")
        )

        self.assertFalse(service._refresh_fnos_if_needed(1))

    def test_fnos_refresh_switch_on_schedules_async_refresh(self) -> None:
        refresh_calls: list[tuple[str, list[str]]] = []
        threads: list[object] = []

        class CapturingThread:
            def __init__(self, *, target, name: str, daemon: bool) -> None:
                self.target = target
                self.name = name
                self.daemon = daemon
                self.started = False
                threads.append(self)

            def start(self) -> None:
                self.started = True

        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {
            "refresh_fnos_after_apply": True,
            "refresh_delay_seconds": 7,
        }
        service.db = SimpleNamespace(
            get_organizer_task=lambda _task_id, include_children=False: {"id": 1, "category": "movie"}
        )
        service.categories = {
            "movie": {
                "label": "电影",
                "fnos_lib": "电影库",
                "strm_fnos_dir_list": ["/vol1/1000/电影"],
            }
        }
        service.fnos = SimpleNamespace(
            refresh=lambda library, dir_list=None: refresh_calls.append((library, list(dir_list or [])))
            or {"success": True}
        )

        with patch("fnos_media_import.organizer.service.threading.Thread", CapturingThread):
            scheduled = service._refresh_fnos_if_needed(1)

        self.assertTrue(scheduled)
        self.assertEqual(refresh_calls, [])
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].started)
        self.assertTrue(threads[0].daemon)
        with patch("fnos_media_import.organizer.service.time.sleep") as sleep_mock:
            threads[0].target()
        sleep_mock.assert_called_once_with(7)
        self.assertEqual(refresh_calls, [("电影库", ["/vol1/1000/电影"])])

    def test_fnos_refresh_schedule_failure_does_not_change_done_result(self) -> None:
        trace: list[str] = []

        class FakeLease:
            @staticmethod
            def ensure_owned() -> None:
                return None

        service = OrganizerService.__new__(OrganizerService)
        service._finalize_organizer_run_and_task = lambda *_args, **_kwargs: trace.append("finalize_done")
        service._sync_linked_job = lambda *_args, **_kwargs: trace.append("sync_done")

        def fail_schedule(_task_id: int) -> bool:
            trace.append("schedule_fnos")
            raise RuntimeError("thread start failed")

        service._refresh_fnos_if_needed = fail_schedule

        with patch("fnos_media_import.organizer.service.logger.warning") as warning:
            result = service._finish_strm_completion(
                1,
                2,
                {"id": 1, "raw_data": {}},
                FakeLease(),
                state={},
                summary={"task_id": 1},
                undo=[],
                task_evidence={},
                confirmation={"success": True},
                cleanup_enabled=False,
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["fnos_refresh_scheduled"])
        self.assertEqual(trace, ["finalize_done", "sync_done", "schedule_fnos"])
        warning.assert_called_once()

    def test_status_reports_fnos_refresh_switch(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {
            "enabled": True,
            "staging_enabled": True,
            "refresh_fnos_after_apply": True,
        }
        service.openlist = SimpleNamespace(configured=True)
        service.tmdb = SimpleNamespace(configured=True)
        service.ai = SimpleNamespace(configured=False)

        self.assertTrue(service.status()["fnos_refresh_after_apply"])

    def test_openlist_refresh_failure_keeps_the_task_in_review(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 2,
                    "category": "tv",
                    "openlist_root_path": "/移动云/_入库暂存/电视剧/job-2",
                    "mappings": [],
                    "operations": [],
                    "evidence": {},
                }
                self.run_updates: list[tuple[int, str, dict]] = []
                self.task_updates: list[dict] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = operations

            @staticmethod
            def create_organizer_run(_task_id: int) -> int:
                return 4

            def update_organizer_task(self, _task_id: int, **values) -> None:
                self.task_updates.append(values)

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

            @staticmethod
            def release_organizer_locks(**_values) -> None:
                return None

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        service._cleanup_source_empty_dirs_after_apply = lambda _task: {}
        service._confirm_standardized_targets = lambda _task: {
            "success": True,
            "organized_target_path": "/移动云/电视剧/测试剧",
            "target_dirs": ["/移动云/电视剧/测试剧"],
        }
        service._refresh_openlist_strm_for_task = lambda _task_id, _task: {
            "enabled": True,
            "failed": 1,
            "cleanup": {"failed": False},
            "items": [{"success": False, "message": "connection reset"}],
        }

        result = service.apply_task(2)

        self.assertFalse(result["success"])
        self.assertTrue(result["retryable"])
        self.assertEqual(service.db.run_updates[-1][1], "failed")
        self.assertEqual(service.db.task_updates[-1]["status"], "waiting_review")
        self.assertFalse(any(status == "done" for _run_id, status, _values in service.db.run_updates))

    def test_staged_task_forces_openlist_refresh_even_if_legacy_toggle_is_false(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {
            "strm_refresh_after_apply": False,
            "strm_refresh_prefix_tv": "/当前配置/电视剧",
            "strm_cleanup_old_before_refresh": False,
        }
        service.categories = {"tv": {"label": "电视剧"}}
        refreshed: list[str] = []
        service.openlist = SimpleNamespace(
            refresh_strm=lambda path, **_kwargs: refreshed.append(path) or {"success": True}
        )
        service._capture_strm_targets = lambda *_args, **_kwargs: self.fail(
            "触发 OpenList 文件夹刷新前后都不应读取 STRM 快照"
        )
        task = {
            "job_id": 42,
            "category": "tv",
            "raw_data": {
                "staging_plan": {
                    **_persisted_plan(42),
                    "openlist_refresh_prefix": "/固化挂载/电视剧",
                },
            },
            "mappings": [
                {
                    "status": "ready",
                    "target_path": "/移动云/电视剧/测试剧 (2026)/Season 01/测试剧 (2026) - S01E01.mkv",
                }
            ],
        }

        result = service._refresh_openlist_strm_for_task(1, task)

        self.assertEqual(refreshed, ["/固化挂载/电视剧/测试剧 (2026)"])
        self.assertTrue(result["forced_by_staging"])
        self.assertEqual(result["failed"], 0)

    def test_missing_refresh_prefix_fails_safely_without_scanning_openlist_root(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {
            "strm_refresh_after_apply": True,
            "strm_cleanup_old_before_refresh": False,
        }
        service.categories = {"tv": {"label": "电视剧"}}
        service.openlist = SimpleNamespace(
            refresh_strm=lambda *_args, **_kwargs: self.fail("缺少分类前缀时不应调用 OpenList 刷新")
        )
        task = {
            "job_id": 42,
            "category": "tv",
            "raw_data": {
                "staging_plan": {
                    **_persisted_plan(42),
                    "openlist_refresh_prefix": "",
                }
            },
            "mappings": [
                {
                    "status": "ready",
                    "target_path": "/移动云/电视剧/测试剧 (2026)/Season 01/测试剧 (2026) - S01E01.mkv",
                }
            ],
        }

        result = service._refresh_openlist_strm_for_task(1, task)

        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["configuration_error"])
        self.assertIn("拒绝错误刷新", result["message"])


if __name__ == "__main__":
    unittest.main()
