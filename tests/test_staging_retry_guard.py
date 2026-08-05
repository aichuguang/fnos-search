from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.constants import ROUTE_QUARK_TO_MOBILE
from fnos_media_import.services.import_job_service import ImportJobRetryService


class ImportJobRetryStagingGuardTests(unittest.TestCase):
    @staticmethod
    def _job(*, staging: bool) -> dict:
        raw_data = {
            "request": {"url": "https://pan.quark.cn/s/example"},
        }
        if staging:
            raw_data["staging_plan"] = {
                "version": 2,
                "enabled": True,
                "route": ROUTE_QUARK_TO_MOBILE,
                "category": "tv",
                "job_id": 7,
                "job_dir_name": "job-7",
                "provider_target_path": "/离线下载/电视剧/job-7",
                "quark_source_category_root": "/离线下载/电视剧",
                "quark_job_root": "/离线下载/电视剧/job-7",
                "storage_backend": "cmcc_api",
                "storage_final_category_root": "webdav/电视剧",
                "storage_staging_category_root": "webdav/_入库暂存/电视剧",
                "storage_job_root": "webdav/_入库暂存/电视剧/job-7",
                "openlist_final_category_root": "/移动云/电视剧",
                "openlist_staging_category_root": "/移动云/_入库暂存/电视剧",
                "openlist_job_root": "/移动云/_入库暂存/电视剧/job-7",
                "openlist_refresh_prefix": "/飞牛NAS/电视剧",
            }
        return {
            "id": 7,
            "title": "测试剧",
            "category": "tv",
            "source_type": "quark",
            "source_url": "https://pan.quark.cn/s/example",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "target_path": "/离线下载/电视剧/旧资源",
            "status": "failed",
            "raw_data": raw_data,
        }

    @staticmethod
    def _service(job: dict, *, staging_enabled: bool, submitted: dict) -> ImportJobRetryService:
        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            def get_job(self, _job_id: int) -> dict:
                return job

            def add_event(self, *values) -> None:
                self.events.append(values)

            @staticmethod
            def update_job(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def update_job_if_status(_job_id: int, expected_statuses, **values) -> bool:
                if job.get("status") not in set(expected_statuses):
                    return False
                job.update(values)
                return True

        def submit_quark(*args, **kwargs):
            submitted["args"] = args
            submitted["kwargs"] = kwargs
            return {"success": True, "job": job}

        config = SimpleNamespace(
            raw={
                "organizer": {
                    "enabled": staging_enabled,
                    "staging_enabled": True,
                },
                "openlist": {"base_url": "http://openlist.test"},
            },
            category=lambda _key: {"label": "电视剧"},
        )
        return ImportJobRetryService(
            database=FakeDatabase(),
            config=config,
            submit_quark=submit_quark,
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

    def test_history_without_staging_plan_is_not_resubmitted_into_ignored_old_path(self) -> None:
        submitted: dict = {}
        service = self._service(self._job(staging=False), staging_enabled=True, submitted=submitted)

        result = service.retry(7)

        self.assertFalse(result["success"])
        self.assertIn("没有固化 staging_plan", result["message"])
        self.assertNotIn("args", submitted)

    def test_persisted_staging_plan_is_reused(self) -> None:
        submitted: dict = {}
        service = self._service(self._job(staging=True), staging_enabled=True, submitted=submitted)

        result = service.retry(7)

        self.assertTrue(result["success"])
        self.assertEqual(submitted["args"][3], "/离线下载/电视剧/job-7")

    def test_enabled_but_incomplete_plan_is_rejected_before_provider_retry(self) -> None:
        submitted: dict = {}
        job = self._job(staging=True)
        job["raw_data"]["staging_plan"].pop("storage_backend")
        service = self._service(job, staging_enabled=True, submitted=submitted)

        result = service.retry(7)

        self.assertFalse(result["success"])
        self.assertIn("不完整", result["message"])
        self.assertNotIn("args", submitted)

    def test_legacy_retry_remains_available_when_staging_mode_is_disabled(self) -> None:
        submitted: dict = {}
        service = self._service(self._job(staging=False), staging_enabled=False, submitted=submitted)

        result = service.retry(7)

        self.assertTrue(result["success"])
        self.assertEqual(submitted["args"][3], "/离线下载/电视剧/旧资源")

    def test_failed_new_job_keeps_the_guard_even_if_openlist_is_temporarily_missing(self) -> None:
        submitted: dict = {}
        job = self._job(staging=False)
        job["raw_data"]["staging_plan_required"] = True
        service = self._service(job, staging_enabled=True, submitted=submitted)
        service.config.raw["openlist"] = {}

        result = service.retry(7)

        self.assertFalse(result["success"])
        self.assertNotIn("args", submitted)

    def test_organizer_handoff_failure_does_not_resubmit_the_provider(self) -> None:
        submitted: dict = {}
        job = self._job(staging=True)
        job["status"] = "review"
        job["raw_data"]["completion"] = {
            "stage": "review",
            "message": "Organizer 未成功创建标准化任务",
            "organizer_scan_path": "/移动云/_入库暂存/电视剧/job-7",
        }
        service = self._service(job, staging_enabled=True, submitted=submitted)

        result = service.retry(7)

        self.assertFalse(result["success"])
        self.assertIn("避免重复转存", result["message"])
        self.assertNotIn("args", submitted)

    def test_exhausted_rclone_staging_retry_does_not_resubmit_the_provider(self) -> None:
        submitted: dict = {}
        job = self._job(staging=True)
        job["status"] = "review"
        job["raw_data"]["completion"] = {
            "stage": "review",
            "message": "任务级 rclone 自动补跑已经耗尽",
            "staging_retry_attempts": 8,
            "staging_retry_exhausted": True,
        }
        service = self._service(job, staging_enabled=True, submitted=submitted)

        result = service.retry(7)

        self.assertFalse(result["success"])
        self.assertIn("rclone 自动补跑已经耗尽", result["message"])
        self.assertNotIn("args", submitted)


if __name__ == "__main__":
    unittest.main()
