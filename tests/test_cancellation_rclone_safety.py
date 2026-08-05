from __future__ import annotations

import copy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.cmcc.client import CmccApiError
from fnos_media_import.cmcc.uploader import CmccUploader
from fnos_media_import.services.job_cancellation_service import (
    JobCancellationDependencies,
    JobCancellationService,
)
from fnos_media_import.services.rclone_run_queue import RcloneRunQueue
from fnos_media_import.services.rclone_service import RcloneService
from tests.test_rclone_persisted_staging_plan import _persisted_plan


def _staging_run(job_id: int) -> dict:
    return {
        "job_id": job_id,
        "category": "tv",
        "job_dir_name": f"job-{job_id}",
        "source_category_root": "/quark/tv",
        "storage_staging_category_root": "/mobile/staging/tv",
        "storage_backend": "cmcc_api",
    }


class _ProcessController:
    def __init__(self) -> None:
        self.terminated: list[object] = []

    @staticmethod
    def is_active(process: object | None) -> bool:
        return process is not None

    def terminate(self, process: object) -> bool:
        self.terminated.append(process)
        return True


def _cancel_service(*, active_job_id: int, queued_job_ids: tuple[int, ...]) -> tuple[RcloneService, _ProcessController]:
    service = RcloneService.__new__(RcloneService)
    service.lock = threading.Lock()
    service.run_queue = RcloneRunQueue()
    for job_id in queued_job_ids:
        service.run_queue.enqueue(
            reason=f"job:{job_id}",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=_staging_run(job_id),
        )
    service._staging_retry_timers = {}
    service._active_staging_job_id = active_job_id
    service._active_run_reason = f"job:{active_job_id}" if active_job_id else ""
    service._stop_requested_job_ids = set()
    service.process = object() if active_job_id else None
    controller = _ProcessController()
    service.process_controller = controller
    service.status_locked = lambda: {
        "running": bool(service.process),
        "queue_count": service.run_queue.snapshot()["queue_count"],
    }
    return service, controller


class RcloneJobScopedCancellationTests(unittest.TestCase):
    def test_cancelling_queued_job_preserves_active_and_other_queued_runs(self) -> None:
        service, controller = _cancel_service(active_job_id=41, queued_job_ids=(42, 43))

        result = service.cancel_job(42, stop_running=True)

        self.assertTrue(result["success"])
        self.assertFalse(result["active_match"])
        self.assertFalse(result["stop_sent"])
        self.assertEqual(result["removed_queue_count"], 1)
        self.assertEqual(controller.terminated, [])
        snapshot = service.run_queue.snapshot()
        self.assertEqual(snapshot["queue_count"], 1)
        self.assertEqual(snapshot["queued_runs"][0]["job_id"], 43)

    def test_cancelling_active_job_terminates_only_it_and_keeps_queue(self) -> None:
        service, controller = _cancel_service(active_job_id=42, queued_job_ids=(43, 44))
        active_process = service.process

        result = service.cancel_job(42, stop_running=True)

        self.assertTrue(result["active_match"])
        self.assertTrue(result["stop_sent"])
        self.assertEqual(controller.terminated, [active_process])
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 2)

    def test_job_queue_removal_does_not_touch_legacy_unscoped_run(self) -> None:
        queue = RcloneRunQueue()
        queue.enqueue(
            reason="legacy_scan",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:00",
            staging_run=None,
        )
        queue.enqueue(
            reason="job:42",
            file_retry=None,
            category_filter="tv",
            queued_at="2026-07-30T12:00:01",
            staging_run=_staging_run(42),
        )

        removed = queue.remove_job(42)

        self.assertEqual(len(removed), 1)
        snapshot = queue.snapshot()
        self.assertEqual(snapshot["queue_count"], 1)
        self.assertIsNone(snapshot["queued_runs"][0]["job_id"])


class RcloneCleanupScopeTests(unittest.TestCase):
    def test_staging_title_fallback_scans_only_the_current_job_root(self) -> None:
        service = RcloneService.__new__(RcloneService)
        listed: list[str] = []
        service._rclone_lsf = lambda source_dir: (
            listed.append(source_dir)
            or {
                "ok": True,
                "files": ["完全不同的目录/E01.mkv", "完全不同的目录/E02.srt"],
                "message": "ok",
            }
        )
        service._category_dirs_for_key = lambda _category: {
            "source_dir": "/旧夸克/电视剧",
            "target_dir": "/旧移动云/电视剧",
        }
        plan = _persisted_plan(42)
        job = {
            "id": 42,
            "title": "同名电视剧",
            "category": "tv",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }

        specs = service._cleanup_specs_from_title(job=job, request_item=None, known_specs=[])

        self.assertEqual(listed, [plan["quark_job_root"].strip("/")])
        self.assertEqual(len(specs), 2)
        self.assertTrue(all(item["source_path"].startswith(plan["quark_job_root"].strip("/")) for item in specs))
        self.assertEqual({item["matched_by"] for item in specs}, {"staging_job_root"})

    def test_non_quark_job_never_scans_quark_category_for_title_cleanup(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service._rclone_lsf = lambda _source_dir: self.fail("非夸克任务不应扫描夸克分类目录")
        service._category_dirs_for_key = lambda _category: {
            "source_dir": "/旧夸克/电视剧",
            "target_dir": "/旧移动云/电视剧",
        }
        job = {
            "id": 42,
            "title": "同名电视剧",
            "category": "tv",
            "source_type": "magnet",
            "target_route": "sixpan_offline",
            "raw_data": {},
        }

        specs = service._cleanup_specs_from_title(job=job, request_item=None, known_specs=[])

        self.assertEqual(specs, [])

    def test_local_temp_cleanup_uses_exact_job_relative_path(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {
            "local_temp": "/temp/fnos-media-import",
            "container_name": "rclone-server",
        }
        service._category_dirs_for_key = lambda _category: {
            "source_dir": "/旧夸克/电视剧",
            "target_dir": "/旧移动云/电视剧",
        }
        captured: dict = {}

        def run_command(label, command, **kwargs):
            captured.update(label=label, command=command, kwargs=kwargs)
            return {"type": kwargs["item_type"], "ok": True, "path": kwargs["path"], "message": "ok"}

        service._run_cleanup_command = run_command
        plan = _persisted_plan(42)
        job = {
            "id": 42,
            "category": "tv",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }
        spec = {
            "filename": "E01.mkv",
            "source_path": f"{plan['quark_job_root']}/原始剧名/E01.mkv",
        }

        result = service._delete_local_temp_file("E01.mkv", job=job, spec=spec)

        exact = "/temp/fnos-media-import/离线剧集/job-42/原始剧名/E01.mkv"
        self.assertTrue(result["ok"])
        self.assertEqual(captured["kwargs"]["path"], exact)
        self.assertEqual(captured["command"][-4:], ["rm", "-f", "--", exact])
        self.assertNotIn("find", captured["command"])
        self.assertNotIn("-name", captured["command"])

    def test_local_temp_cleanup_treats_glob_characters_as_literal_path_text(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {
            "local_temp": "/temp/fnos-media-import",
            "container_name": "rclone-server",
        }
        captured: dict = {}
        service._run_cleanup_command = lambda label, command, **kwargs: (
            captured.update(label=label, command=command, kwargs=kwargs)
            or {"type": kwargs["item_type"], "ok": True, "path": kwargs["path"], "message": "ok"}
        )
        plan = _persisted_plan(42)
        job = {
            "id": 42,
            "category": "tv",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }
        source_path = f"{plan['quark_job_root']}/原始剧名/E[01]*.mkv"

        service._delete_local_temp_file(
            "E[01]*.mkv",
            job=job,
            spec={"filename": "E[01]*.mkv", "source_path": source_path},
        )

        exact = "/temp/fnos-media-import/离线剧集/job-42/原始剧名/E[01]*.mkv"
        self.assertEqual(captured["command"][-4:], ["rm", "-f", "--", exact])

    def test_other_category_uses_worker_local_root_name(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {"local_temp": "/temp/fnos-media-import"}
        plan = _persisted_plan(42)
        plan.update(
            {
                "category": "other",
                "quark_source_category_root": "/旧夸克/其他",
                "quark_job_root": "/旧夸克/其他/job-42",
                "provider_target_path": "/旧夸克/其他/job-42",
                "storage_final_category_root": "/旧移动云/其他",
                "storage_staging_category_root": "/旧移动云/_入库暂存/其他",
                "storage_job_root": "/旧移动云/_入库暂存/其他/job-42",
                "openlist_job_root": "/旧挂载/_入库暂存/其他/job-42",
                "openlist_final_category_root": "/旧挂载/其他",
                "openlist_staging_category_root": "/旧挂载/_入库暂存/其他",
                "openlist_refresh_prefix": "/飞牛NAS/其他",
            }
        )
        job = {
            "id": 42,
            "category": "other",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }

        path = service._local_temp_cleanup_path(
            job=job,
            spec={"source_path": "/旧夸克/其他/job-42/资料.bin"},
        )

        self.assertEqual(path, "/temp/fnos-media-import/离线其他/job-42/资料.bin")

    def test_empty_temp_directory_cleanup_is_scoped_to_confirmed_job_roots(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {
            "local_temp": "/temp/fnos-media-import",
            "container_name": "rclone-server",
        }
        calls: list[tuple[list[str], str]] = []
        service._run_cleanup_command = lambda _label, command, **kwargs: (
            calls.append((command, kwargs["path"]))
            or {"type": kwargs["item_type"], "ok": True, "path": kwargs["path"], "message": "ok"}
        )
        result = {
            "items": [
                {
                    "type": "local_temp",
                    "ok": True,
                    "path": "/temp/fnos-media-import/离线剧集/job-42/目录/E01.mkv",
                },
                {
                    "type": "local_temp",
                    "ok": True,
                    "path": "/temp/fnos-media-import/离线剧集/job-43/E02.mkv",
                },
                {
                    "type": "local_temp",
                    "ok": True,
                    "path": "/outside/untrusted.mkv",
                },
            ]
        }

        service._delete_empty_local_temp_dirs(result)

        self.assertEqual(
            {path for _command, path in calls},
            {
                "/temp/fnos-media-import/离线剧集/job-42",
                "/temp/fnos-media-import/离线剧集/job-43",
            },
        )
        self.assertTrue(all(command[3] == "find" for command, _path in calls))
        self.assertNotIn("/temp/fnos-media-import", {path for _command, path in calls})

    def test_unsafe_container_temp_root_is_never_used_for_deletion(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.config = {"local_temp": "/", "container_name": "rclone-server"}
        service._run_cleanup_command = lambda *_args, **_kwargs: self.fail(
            "容器根目录不能作为任务 temp 删除边界"
        )
        plan = _persisted_plan(42)
        job = {
            "id": 42,
            "category": "tv",
            "source_type": "quark",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": plan},
        }

        item = service._delete_local_temp_file(
            "E01.mkv",
            job=job,
            spec={"source_path": f"{plan['quark_job_root']}/E01.mkv"},
        )

        self.assertTrue(item["skipped"])
        self.assertEqual(item["path"], "")


class _Jobs:
    def __init__(self) -> None:
        self.job = {"id": 42, "status": "transferring", "raw_data": {}}
        self.events: list[tuple] = []

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **updates) -> None:
        self.job.update(copy.deepcopy(updates))

    def update_job_if_status(self, _job_id: int, expected_statuses, **updates) -> bool:
        if str(self.job.get("status") or "") not in {str(value) for value in expected_statuses}:
            return False
        self.job.update(copy.deepcopy(updates))
        return True

    def add_event(self, *args) -> int:
        self.events.append(copy.deepcopy(args))
        return len(self.events)


class _Cleaner:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[int, bool]] = []

    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict:
        self.cancel_calls.append((job_id, stop_running))
        return {
            "success": True,
            "job_id": job_id,
            "removed_queue_count": 1,
            "active_match": False,
            "stop_sent": False,
            "message": "cancelled",
        }

    def cleanup_cancelled_task(self, **_kwargs) -> dict:
        raise AssertionError("cleanup must not run when cleanup=false")


class JobCancellationFenceTests(unittest.TestCase):
    def test_cleanup_false_still_revokes_execution_and_persists_generation(self) -> None:
        jobs = _Jobs()
        cleaner = _Cleaner()
        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=jobs,
                cleaner=cleaner,
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
            )
        )

        result = service.cancel(
            copy.deepcopy(jobs.job),
            reason="admin cancel",
            payload={"cleanup": False, "stop_running": True, "delete_source": False},
            request_item=None,
            cleanup_default=True,
            stop_running_default=False,
            admin_username="admin",
        )

        self.assertEqual(cleaner.cancel_calls, [(42, True)])
        self.assertEqual(jobs.job["status"], "cancelled")
        cancel = jobs.job["raw_data"]["cancel"]
        self.assertTrue(cancel["active"])
        self.assertEqual(cancel["generation"], 1)
        self.assertFalse(cancel["cleanup_requested"])
        self.assertFalse(cancel["delete_source_requested"])
        self.assertEqual(result["cleanup"]["items"][0]["type"], "cancel_execution")


class _CmccClientWithLookupFailure:
    def check_exists(self, **_kwargs):
        raise ConnectionResetError("query reset")


class _CmccClientWithoutPostCompleteVisibility:
    def __init__(self) -> None:
        self.lookup_count = 0

    def check_exists(self, **_kwargs) -> dict:
        self.lookup_count += 1
        return {"exist": False, "file_id": "", "size": None}

    def create_file(self, **_kwargs) -> dict:
        return {
            "fileId": "file-1",
            "uploadId": "upload-1",
            "parentFileId": "parent-1",
            "partInfos": [{"partNumber": 1, "uploadUrl": "https://upload.invalid/1"}],
        }

    def complete_file(self, **_kwargs) -> dict:
        return {"success": True}


class CmccVerificationSafetyTests(unittest.TestCase):
    def test_lookup_exception_is_not_converted_to_missing_file(self) -> None:
        uploader = CmccUploader(client=_CmccClientWithLookupFailure())

        with self.assertRaises(CmccApiError):
            uploader._find_existing("parent-1", "E01.mkv")

    def test_completed_upload_without_visible_matching_file_is_not_verified(self) -> None:
        client = _CmccClientWithoutPostCompleteVisibility()
        uploader = CmccUploader(client=client)
        uploader._upload_parts = lambda **_kwargs: None
        local_file = Path(__file__)
        with patch("fnos_media_import.cmcc.uploader._atomic_write_json") as write_manifest:
            result = uploader.upload_file(
                local_file=local_file,
                target_parent_file_id="parent-1",
                target_name="E01.mkv",
            )

            self.assertFalse(result.success)
            self.assertEqual(result.status, "failed")
            self.assertIn("verification failed", result.message)
            payloads = [call.args[1] for call in write_manifest.call_args_list if len(call.args) > 1]
            self.assertTrue(payloads)
            self.assertNotIn("verified", {str(payload.get("status")) for payload in payloads})


if __name__ == "__main__":
    unittest.main()
