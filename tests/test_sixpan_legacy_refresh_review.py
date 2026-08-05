from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fnos_media_import.constants import (
    COMPLETION_STAGE_DONE,
    COMPLETION_STAGE_REVIEW,
    JOB_DONE,
    JOB_REFRESHING,
    JOB_REVIEW,
)
from fnos_media_import.database import Database
from fnos_media_import.services.import_job_service import ImportJobRetryService
from fnos_media_import.services.sixpan_offline_sync_service import SixPanOfflineSyncService


class _Database:
    def __init__(self) -> None:
        self.job: dict[str, Any] = {
            "id": 21,
            "status": "submitted",
            "title": "测试剧",
            "category": "tv",
            "source_type": "sixpan",
            "source_url": "https://example.invalid/share",
            "target_route": "sixpan_offline",
            "target_path": "/电视剧/测试剧",
            "raw_data": {},
        }
        self.events: list[tuple[Any, ...]] = []

    def get_job(self, _job_id: int) -> dict[str, Any]:
        return dict(self.job)

    def update_job(self, _job_id: int, **updates: Any) -> None:
        self.job.update(updates)

    def update_job_if_status(self, _job_id: int, expected_statuses: set[str], **updates: Any) -> bool:
        if str(self.job.get("status") or "") not in expected_statuses:
            return False
        self.job.update(updates)
        return True

    def update_job_if_status_and_claim_token(
        self,
        _job_id: int,
        expected_statuses: set[str],
        expected_claim_token: str | None,
        **updates: Any,
    ) -> bool:
        if str(self.job.get("status") or "") not in expected_statuses:
            return False
        raw_data = self.job.get("raw_data") if isinstance(self.job.get("raw_data"), dict) else {}
        retry_state = (
            raw_data.get("sixpan_media_refresh_retry")
            if isinstance(raw_data.get("sixpan_media_refresh_retry"), dict)
            else {}
        )
        current_claim_token = str(retry_state.get("claim_token") or "").strip() or None
        if current_claim_token != expected_claim_token:
            return False
        self.job.update(updates)
        return True

    def add_event(self, *args: Any, **_kwargs: Any) -> None:
        self.events.append(args)


def _complete_with_refresh_failure(database: _Database) -> list[tuple[Any, ...]]:
    guest_updates: list[tuple[Any, ...]] = []
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
        record_completed=lambda *_args, **_kwargs: {
            "success": False,
            "message": "飞牛媒体库暂时不可用",
        },
        sync_guest_requests=lambda *args, **kwargs: guest_updates.append((args, kwargs)),
    )
    completed = service._complete_job(
        database.job,
        21,
        "sixpan-task-21",
        "poller",
        {"state": "completed", "progress": 100},
    )
    if not completed:
        raise AssertionError("六盘完成同步未更新任务")
    return guest_updates


class SixPanLegacyRefreshReviewTests(unittest.TestCase):
    def test_refresh_failure_enters_review_with_media_refresh_only_semantics(self) -> None:
        database = _Database()

        guest_updates = _complete_with_refresh_failure(database)

        completion = database.job["raw_data"]["completion"]
        refresh = database.job["raw_data"]["sixpan_legacy_refresh"]
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertEqual(completion["stage"], COMPLETION_STAGE_REVIEW)
        self.assertTrue(completion["provider_completed"])
        self.assertFalse(completion["retryable"])
        self.assertEqual(completion["retry_action"], "media_refresh_only")
        self.assertTrue(refresh["provider_completed"])
        self.assertEqual(refresh["retry_action"], "media_refresh_only")
        self.assertIn("不要重复提交六盘离线任务", database.job["error_message"])
        self.assertEqual(guest_updates[-1][0][1], JOB_REVIEW)
        self.assertEqual(guest_updates[-1][0][2]["retry_action"], "media_refresh_only")

    def test_generic_retry_refuses_to_resubmit_completed_sixpan_provider(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        provider_calls: list[tuple[Any, ...]] = []
        retry = ImportJobRetryService(
            database=database,
            config=SimpleNamespace(raw={}),
            submit_quark=lambda *args, **kwargs: provider_calls.append((args, kwargs)) or {},
            submit_cloud139=lambda *args, **kwargs: provider_calls.append((args, kwargs)) or {},
            submit_generic=lambda *args, **kwargs: provider_calls.append((args, kwargs)) or {},
        )

        result = retry.retry(21)

        self.assertFalse(result["success"])
        self.assertTrue(result["manual_review"])
        self.assertTrue(result["provider_completed"])
        self.assertTrue(result["media_refresh_only"])
        self.assertEqual(provider_calls, [])
        self.assertEqual(database.job["status"], JOB_REVIEW)

    def test_missing_category_enters_review_without_calling_refresh(self) -> None:
        database = _Database()
        refresh_calls: list[tuple[Any, ...]] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {},
            enqueue_organizer=lambda *_args, **_kwargs: {
                "success": False,
                "skipped": True,
                "message": "Organizer 未启用",
            },
            record_completed=lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or {"success": True},
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        completed = service._complete_job(
            database.job,
            21,
            "sixpan-task-21",
            "poller",
            {"state": "completed", "progress": 100},
        )

        self.assertTrue(completed)
        self.assertEqual(refresh_calls, [])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        refresh = database.job["raw_data"]["sixpan_legacy_refresh"]
        self.assertFalse(refresh["success"])
        self.assertIn("未找到分类配置", refresh["message"])

    def test_empty_refresh_result_is_not_treated_as_success(self) -> None:
        database = _Database()
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
            record_completed=lambda *_args, **_kwargs: {},
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        service._complete_job(
            database.job,
            21,
            "sixpan-task-21",
            "poller",
            {"state": "completed", "progress": 100},
        )

        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertFalse(database.job["raw_data"]["sixpan_legacy_refresh"]["success"])
        self.assertIn("未明确成功", database.job["error_message"])

    def test_media_refresh_only_retry_succeeds_without_touching_provider(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        importer_calls: list[str] = []
        refresh_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        guest_updates: list[tuple[Any, ...]] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: importer_calls.append("provider") or None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("刷新重试不应进入 Organizer")
            ),
            record_completed=lambda *args, **kwargs: (
                refresh_calls.append((args, kwargs))
                or {"success": True, "message": "刷新完成"}
            ),
            sync_guest_requests=lambda *args, **_kwargs: guest_updates.append(args),
        )

        result = service.retry_media_refresh(21, trigger="test")

        self.assertTrue(result["success"])
        self.assertEqual(importer_calls, [])
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0][1]["trigger"], "sixpan_refresh_retry:test")
        self.assertEqual(database.job["status"], JOB_DONE)
        self.assertEqual(database.job["error_message"], "")
        self.assertEqual(database.job["raw_data"]["completion"]["stage"], COMPLETION_STAGE_DONE)
        self.assertEqual(database.job["raw_data"]["completion"]["retry_action"], "none")
        self.assertEqual(guest_updates[-1][1], JOB_DONE)

    def test_media_refresh_only_retry_failure_stays_review(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: (_ for _ in ()).throw(AssertionError("不应访问六盘 Provider")),
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("不应进入 Organizer")
            ),
            record_completed=lambda *_args, **_kwargs: None,
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        result = service.retry_media_refresh(21, trigger="test")

        self.assertFalse(result["success"])
        self.assertEqual(database.job["status"], JOB_REVIEW)
        completion = database.job["raw_data"]["completion"]
        refresh = database.job["raw_data"]["sixpan_legacy_refresh"]
        self.assertEqual(completion["stage"], COMPLETION_STAGE_REVIEW)
        self.assertTrue(completion["provider_completed"])
        self.assertEqual(completion["retry_action"], "media_refresh_only")
        self.assertFalse(refresh["success"])
        self.assertIn("不要重复提交六盘离线任务", database.job["error_message"])

    def test_media_refresh_only_retry_claim_blocks_duplicate_refresh(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        nested_results: list[dict[str, Any]] = []
        refresh_calls: list[str] = []
        service: SixPanOfflineSyncService

        def record_completed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            refresh_calls.append(str(database.job.get("status") or ""))
            nested_results.append(service.retry_media_refresh(21, trigger="duplicate"))
            return {"success": True, "message": "刷新完成"}

        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {},
            record_completed=record_completed,
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        result = service.retry_media_refresh(21, trigger="first")

        self.assertTrue(result["success"])
        self.assertEqual(refresh_calls, [JOB_REFRESHING])
        self.assertEqual(len(nested_results), 1)
        self.assertFalse(nested_results[0]["success"])
        self.assertTrue(nested_results[0]["conflict"])
        self.assertGreater(nested_results[0]["retry_after_seconds"], 0)
        self.assertEqual(database.job["status"], JOB_DONE)

    def test_stale_media_refresh_claim_is_recovered_before_retry(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        raw_data = database.job["raw_data"]
        raw_data["completion"] = {
            **raw_data["completion"],
            "stage": JOB_REFRESHING,
            "retry_action": "media_refresh_only",
        }
        raw_data["sixpan_media_refresh_retry"] = {
            "status": "running",
            "started_at": "2026-01-01T00:00:00Z",
            "provider_completed": True,
            "retry_action": "media_refresh_only",
        }
        database.job["status"] = JOB_REFRESHING
        refresh_calls: list[int] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {},
            record_completed=lambda *_args, **_kwargs: refresh_calls.append(1) or {"success": True},
            sync_guest_requests=lambda *_args, **_kwargs: None,
            now=lambda: datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )

        result = service.retry_media_refresh(21, trigger="stale_recovery")

        self.assertTrue(result["success"])
        self.assertTrue(result["recovered_stale_claim"])
        self.assertEqual(refresh_calls, [1])
        self.assertEqual(database.job["status"], JOB_DONE)
        retry_state = database.job["raw_data"]["sixpan_media_refresh_retry"]
        self.assertTrue(retry_state["recovered_stale_claim"])
        self.assertEqual(retry_state["status"], "done")

    def test_old_claim_completion_cannot_overwrite_new_claim(self) -> None:
        database = _Database()
        _complete_with_refresh_failure(database)
        replacement_token = "newer-refresh-claim"

        def replace_claim_before_old_completion(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            retry_state = database.job["raw_data"]["sixpan_media_refresh_retry"]
            self.assertNotEqual(retry_state["claim_token"], replacement_token)
            database.job["raw_data"]["sixpan_media_refresh_retry"] = {
                **retry_state,
                "claim_token": replacement_token,
                "started_at": "2026-01-01T01:00:00Z",
                "status": "running",
            }
            database.job["raw_data"]["completion"]["stage"] = JOB_REFRESHING
            database.job["status"] = JOB_REFRESHING
            return {"success": True, "message": "旧请求刷新完成"}

        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {},
            record_completed=replace_claim_before_old_completion,
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        result = service.retry_media_refresh(21, trigger="old_claim")

        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(database.job["status"], JOB_REFRESHING)
        self.assertEqual(
            database.job["raw_data"]["sixpan_media_refresh_retry"]["claim_token"],
            replacement_token,
        )
        self.assertEqual(database.job["raw_data"]["completion"]["stage"], JOB_REFRESHING)

    def test_database_claim_token_update_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "sixpan-claim.db")
            database.init_schema()
            job_id, _created = database.create_job(
                {
                    "title": "测试剧",
                    "category": "tv",
                    "category_label": "电视剧",
                    "source_type": "magnet",
                    "source_url": "magnet:?xt=urn:btih:claim-token-test",
                    "target_route": "sixpan_offline",
                    "target_path": "/电视剧/测试剧",
                    "status": JOB_REFRESHING,
                    "external_task_id": "sixpan-claim-token-test",
                    "error_message": "",
                    "raw_data": {
                        "sixpan_media_refresh_retry": {
                            "claim_token": "new-claim",
                            "status": "running",
                        }
                    },
                }
            )

            stale_update = database.update_job_if_status_and_claim_token(
                job_id,
                {JOB_REFRESHING},
                "old-claim",
                status=JOB_DONE,
            )
            current_update = database.update_job_if_status_and_claim_token(
                job_id,
                {JOB_REFRESHING},
                "new-claim",
                status=JOB_DONE,
            )

            self.assertFalse(stale_update)
            self.assertTrue(current_update)
            self.assertEqual(database.get_job(job_id)["status"], JOB_DONE)

    def test_media_refresh_only_retry_rejects_unrelated_jobs(self) -> None:
        database = _Database()
        database.job["status"] = JOB_REVIEW
        refresh_calls: list[tuple[Any, ...]] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: None,
            poll_limit=lambda: 10,
            category=lambda _key: {"label": "电视剧"},
            enqueue_organizer=lambda *_args, **_kwargs: {},
            record_completed=lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or {"success": True},
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        missing_marker = service.retry_media_refresh(21)
        database.job["target_route"] = "quark_to_mobile"
        database.job["raw_data"] = {
            "completion": {
                "provider_completed": True,
                "retry_action": "media_refresh_only",
            }
        }
        wrong_route = service.retry_media_refresh(21)

        self.assertFalse(missing_marker["success"])
        self.assertTrue(missing_marker["rejected"])
        self.assertFalse(wrong_route["success"])
        self.assertTrue(wrong_route["rejected"])
        self.assertEqual(refresh_calls, [])


if __name__ == "__main__":
    unittest.main()
