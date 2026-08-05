from __future__ import annotations

import copy
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.app import _video_file_paths
from fnos_media_import.services.callback_service import CallbackDependencies, RcloneCallbackService
from fnos_media_import.services.rclone_category_finalizer import RcloneCategoryFinalizer
from fnos_media_import.services.rclone_job_feasibility import RcloneJobFeasibilityEvaluator
from fnos_media_import.services.rclone_run_import_finalizer import RcloneRunImportFinalizer
from fnos_media_import.services.rclone_service import RcloneService
from tests.test_rclone_persisted_staging_plan import _persisted_plan, _staging_run


def _safe_int(value, default, minimum, maximum) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _manifest_job() -> dict:
    return {
        "id": 42,
        "status": "transferring",
        "category": "tv",
        "target_route": "quark_to_mobile",
        "raw_data": {
            "staging_plan": _persisted_plan(42),
            "rclone_staging_manifest": {
                "version": 1,
                "source_paths": [
                    "/旧夸克/电视剧/job-42/E01.mkv",
                    "/旧夸克/电视剧/job-42/E02.mkv",
                ],
                "expected_file_count": 2,
            },
        },
    }


class RcloneStagingManifestCallbackTests(unittest.TestCase):
    def test_manifest_callback_merges_paths_across_runs(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.job = {
                    "id": 42,
                    "status": "transferring",
                    "target_route": "quark_to_mobile",
                    "raw_data": {"staging_plan": _persisted_plan(42)},
                }

            def get_job(self, _job_id: int) -> dict:
                return copy.deepcopy(self.job)

            def update_job(self, _job_id: int, **changes) -> None:
                self.job.update(copy.deepcopy(changes))

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

        database = FakeDatabase()
        service = RcloneCallbackService(
            CallbackDependencies(
                db=database,
                rclone=SimpleNamespace(),
                safe_int=_safe_int,
                callback_level=lambda _status: "info",
                enqueue_organizer=lambda *_args: None,
                cancelled_status="cancelled",
            )
        )

        for run_id, paths in (
            (1, ["/旧夸克/电视剧/job-42/E01.mkv", "/旧夸克/电视剧/job-42/E02.mkv"]),
            (2, ["/旧夸克/电视剧/job-42/E02.mkv", "/旧夸克/电视剧/job-42/E03.mkv"]),
        ):
            response, status_code = service.handle(
                {
                    "run_id": run_id,
                    "job_id": 42,
                    "status": "staging_manifest",
                    "category": "tv",
                    "source_path": "/旧夸克/电视剧",
                    "target_path": "旧移动云/_入库暂存/电视剧",
                    "manifest_paths": paths,
                }
            )
            self.assertEqual(status_code, 200)
            self.assertTrue(response["success"])

        manifest = database.job["raw_data"]["rclone_staging_manifest"]
        self.assertEqual(manifest["expected_file_count"], 3)
        self.assertEqual(manifest["run_ids"], [1, 2])
        self.assertEqual(len(manifest["source_paths"]), 3)

    def test_manifest_callback_rejects_path_from_another_job(self) -> None:
        class FakeDatabase:
            job = {
                "id": 42,
                "status": "transferring",
                "target_route": "quark_to_mobile",
                "raw_data": {"staging_plan": _persisted_plan(42)},
            }

            @classmethod
            def get_job(cls, _job_id: int) -> dict:
                return copy.deepcopy(cls.job)

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

        service = RcloneCallbackService(
            CallbackDependencies(
                db=FakeDatabase(),
                rclone=SimpleNamespace(),
                safe_int=_safe_int,
                callback_level=lambda _status: "info",
                enqueue_organizer=lambda *_args: None,
                cancelled_status="cancelled",
            )
        )

        response, status_code = service.handle(
            {
                "run_id": 1,
                "job_id": 42,
                "status": "staging_manifest",
                "manifest_paths": ["/旧夸克/电视剧/job-99/E01.mkv"],
            }
        )

        self.assertEqual(status_code, 409)
        self.assertFalse(response["success"])

    def test_file_callback_with_explicit_job_id_rejects_another_job_source_path(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.file_events: list[tuple] = []

            @staticmethod
            def get_job(_job_id: int) -> dict:
                return {
                    "id": 42,
                    "status": "transferring",
                    "target_route": "quark_to_mobile",
                    "raw_data": {"staging_plan": _persisted_plan(42)},
                }

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            def add_rclone_file_event(self, *args, **kwargs) -> None:
                self.file_events.append((args, kwargs))

        database = FakeDatabase()
        service = RcloneCallbackService(
            CallbackDependencies(
                db=database,
                rclone=SimpleNamespace(),
                safe_int=_safe_int,
                callback_level=lambda _status: "info",
                enqueue_organizer=lambda *_args: None,
                cancelled_status="cancelled",
            )
        )

        response, status_code = service.handle(
            {
                "run_id": 3,
                "job_id": 42,
                "require_job_match": True,
                "status": "done",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/旧夸克/电视剧/job-99/E01.mkv",
                "target_path": "/旧移动云/_入库暂存/电视剧/job-42/E01.mkv",
            }
        )

        self.assertEqual(status_code, 409)
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "rclone_staging_path_mismatch")
        self.assertEqual(database.file_events, [])

    def test_file_callback_rejects_target_outside_or_equal_to_storage_job_root(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.file_events: list[tuple] = []

            @staticmethod
            def get_job(_job_id: int) -> dict:
                return {
                    "id": 42,
                    "status": "transferring",
                    "category": "tv",
                    "target_route": "quark_to_mobile",
                    "raw_data": {"staging_plan": _persisted_plan(42)},
                }

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            def add_rclone_file_event(self, *args, **kwargs) -> None:
                self.file_events.append((args, kwargs))

        for target_path in (
            "/其它挂载/_入库暂存/电视剧/job-42/E01.mkv",
            "/旧移动云/_入库暂存/电视剧/job-42",
        ):
            with self.subTest(target_path=target_path):
                database = FakeDatabase()
                service = RcloneCallbackService(
                    CallbackDependencies(
                        db=database,
                        rclone=SimpleNamespace(),
                        safe_int=_safe_int,
                        callback_level=lambda _status: "info",
                        enqueue_organizer=lambda *_args: None,
                        cancelled_status="cancelled",
                    )
                )

                response, status_code = service.handle(
                    {
                        "run_id": 3,
                        "job_id": 42,
                        "require_job_match": True,
                        "status": "done",
                        "category": "tv",
                        "filename": "E01.mkv",
                        "source_path": "/旧夸克/电视剧/job-42/E01.mkv",
                        "target_path": target_path,
                    }
                )

                self.assertEqual(status_code, 409)
                self.assertFalse(response["success"])
                self.assertEqual(response["error_code"], "rclone_staging_path_mismatch")
                self.assertIn("目标目录", response["message"])
                self.assertEqual(database.file_events, [])


class RcloneCrossRunManifestCompletionTests(unittest.TestCase):
    @staticmethod
    def _events() -> list[dict]:
        return [
            {
                "id": 1,
                "run_id": 10,
                "job_id": 42,
                "status": "done",
                "category": "tv",
                "source_path": "/旧夸克/电视剧/job-42/E01.mkv",
                "target_path": "/目标/E01.mkv",
            },
            {
                "id": 2,
                "run_id": 10,
                "job_id": 42,
                "status": "failed",
                "category": "tv",
                "source_path": "/旧夸克/电视剧/job-42/E02.mkv",
                "target_path": "/目标/E02.mkv",
            },
            {
                "id": 3,
                "run_id": 11,
                "job_id": 42,
                "status": "done",
                "category": "tv",
                "source_path": "/旧夸克/电视剧/job-42/E02.mkv",
                "target_path": "/目标/E02.mkv",
            },
        ]

    def test_category_finalizer_uses_latest_terminal_events_across_runs(self) -> None:
        events = self._events()

        class FakeDatabase:
            @staticmethod
            def list_all_rclone_file_events(**filters) -> list[dict]:
                if filters.get("run_id"):
                    return [item for item in events if item["run_id"] == filters["run_id"]]
                if filters.get("job_id"):
                    return [item for item in events if item["job_id"] == filters["job_id"]]
                return list(events)

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict[int, dict]:
                return {42: _manifest_job()}

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def update_job(*_args, **_kwargs) -> None:
                return None

        completed: list = []
        finalizer = RcloneCategoryFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {}},
            category_key=lambda *_args: "tv",
            event_matches=lambda event, **_kwargs: event.get("job_id") == 42,
            feasibility=RcloneJobFeasibilityEvaluator.evaluate,
            finish_ready=lambda _run, _key, _category, items, _events, **_kwargs: (
                completed.extend(items) or {"success": True, "completed_items": items}
            ),
        )

        finalizer.finalize(
            11,
            "tv",
            {"status": "category_done", "moved_count": 1, "failed_count": 0},
        )

        self.assertEqual(len(completed), 1)
        self.assertEqual(len(completed[0][1]), 2)
        self.assertTrue(completed[0][2]["ready"])

    def test_transport_manifest_keeps_sidecars_while_organizer_count_uses_videos_only(self) -> None:
        source_paths = [
            "/旧夸克/电视剧/job-42/E01.mp4",
            "/旧夸克/电视剧/job-42/E01.srt",
            "/旧夸克/电视剧/job-42/tvshow.nfo",
        ]
        job = _manifest_job()
        job["raw_data"]["rclone_staging_manifest"] = {
            "version": 1,
            "source_paths": source_paths,
            "expected_file_count": 3,
        }
        events = [{"id": index, "status": "done", "source_path": path} for index, path in enumerate(source_paths, 1)]

        verdict = RcloneJobFeasibilityEvaluator.evaluate(job, events, 0)

        self.assertTrue(verdict["ready"])
        self.assertEqual(verdict["expected_file_count"], 3)
        self.assertEqual(_video_file_paths(source_paths), [source_paths[0]])

    def test_run_finalizer_uses_latest_terminal_events_across_runs(self) -> None:
        events = self._events()

        class FakeDatabase:
            @staticmethod
            def list_all_rclone_file_events(**filters) -> list[dict]:
                if filters.get("run_id"):
                    return [item for item in events if item["run_id"] == filters["run_id"]]
                if filters.get("job_id"):
                    return [item for item in events if item["job_id"] == filters["job_id"]]
                return list(events)

            @staticmethod
            def get_jobs_by_ids(_job_ids) -> dict[int, dict]:
                return {42: _manifest_job()}

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_rclone_event(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def update_job(*_args, **_kwargs) -> None:
                return None

        completed: list = []
        finalizer = RcloneRunImportFinalizer(
            database=FakeDatabase(),
            categories=lambda: {"tv": {}},
            log=lambda _message: None,
            feasibility=RcloneJobFeasibilityEvaluator.evaluate,
            finish_ready=lambda _run, _key, _category, items, _events, **_kwargs: (
                completed.extend(items) or {"success": True, "completed_items": items}
            ),
            dispatch_ready=lambda *_args: None,
        )

        finalizer.finalize(11, 0)

        self.assertEqual(len(completed), 1)
        self.assertEqual(len(completed[0][1]), 2)
        self.assertTrue(completed[0][2]["ready"])


class RcloneWorkerManifestOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = (
            Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")

    def test_source_listing_is_job_scoped_before_stability_check(self) -> None:
        self.assertIn('--include "/${RCLONE_ONLY_JOB_DIR}/**"', self.content)

    def test_manifest_ack_happens_before_first_file_processing(self) -> None:
        manifest_ack = self.content.index('if ! persist_staging_manifest "$file_list"')
        file_loop = self.content.index("while IFS= read -r file; do", manifest_ack)

        self.assertLess(manifest_ack, file_loop)

    def test_all_job_scoped_callbacks_carry_job_id_and_require_match(self) -> None:
        self.assertIn('if job_dir_match:\n    payload["job_id"]', self.content)
        self.assertIn('payload["require_job_match"] = True', self.content)
        self.assertNotIn('job_dir_match and payload.get("status") == "staging_manifest"', self.content)

    def test_unstable_staging_list_is_rejected_before_manifest(self) -> None:
        refusal = self.content.index("拒绝固化半批 manifest")
        failed_assignment = self.content.index('if ! file_list="$(stable_file_list')
        manifest_ack = self.content.index('if ! persist_staging_manifest "$file_list"')

        self.assertLess(refusal, failed_assignment)
        self.assertLess(failed_assignment, manifest_ack)


class RcloneStagingRetrySchedulingTests(unittest.TestCase):
    def test_empty_first_run_schedules_and_executes_delayed_retry(self) -> None:
        job = {
            "id": 42,
            "status": "waiting_transfer",
            "category": "tv",
            "target_route": "quark_to_mobile",
            "raw_data": {"staging_plan": _persisted_plan(42)},
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.job = copy.deepcopy(job)
                self.events: list[tuple] = []

            def get_job(self, _job_id: int) -> dict:
                return copy.deepcopy(self.job)

            def update_job(self, _job_id: int, **changes) -> None:
                self.job.update(copy.deepcopy(changes))

            @staticmethod
            def list_all_rclone_file_events(**_kwargs) -> list[dict]:
                return []

            def add_event(self, *args) -> None:
                self.events.append(args)

        class FakeTimer:
            created: list["FakeTimer"] = []

            def __init__(self, interval, function, args=()) -> None:
                self.interval = interval
                self.function = function
                self.args = args
                self.daemon = False
                self.started = False
                self.cancelled = False
                self.__class__.created.append(self)

            def start(self) -> None:
                self.started = True

            def cancel(self) -> None:
                self.cancelled = True

            def is_alive(self) -> bool:
                return self.started and not self.cancelled

        service = RcloneService.__new__(RcloneService)
        service.config = {"staging_retry_delay_seconds": 5, "staging_retry_max_delay_seconds": 20}
        service.db = FakeDatabase()
        service.lock = threading.Lock()
        service._staging_retry_timers = {}
        service._staging_retry_attempts = {}
        service._append_log = lambda _message: None
        starts: list[dict] = []
        service.start = lambda **kwargs: starts.append(kwargs) or {"success": True}

        with patch("fnos_media_import.services.rclone_service.threading.Timer", FakeTimer):
            scheduled = service._schedule_incomplete_staging_retry(
                _staging_run(42),
                run_id=7,
                exit_code=0,
            )
            self.assertTrue(scheduled["queued"])
            self.assertEqual(scheduled["delay_seconds"], 5)
            self.assertTrue(FakeTimer.created[-1].started)

            FakeTimer.created[-1].function(*FakeTimer.created[-1].args)

        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["staging_run"]["job_id"], 42)
        self.assertIn("staging_retry:42", starts[0]["reason"])


if __name__ == "__main__":
    unittest.main()
