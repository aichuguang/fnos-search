from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.web_input import _video_file_paths
from fnos_media_import.services.callback_service import (
    CallbackDependencies,
    RcloneCallbackService,
)
from fnos_media_import.services.rclone_job_feasibility import (
    RcloneJobFeasibilityEvaluator,
)
from tests._rclone_v1_fixtures import (
    rclone_v1_job,
    rclone_v1_manifest,
    rclone_v1_plan,
)


JOB_ID = 42
SOURCE_JOB_ROOT = f"/quark/tv/job-{JOB_ID}"
TARGET_JOB_ROOT = f"/openlist/staging/tv/job-{JOB_ID}"


def _safe_int(value, default, minimum, maximum) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _plan(*, expected_file_count: int = 2) -> dict:
    return rclone_v1_plan(
        JOB_ID,
        category="tv",
        source_root="/quark/tv",
        staging_root="/openlist/staging/tv",
        final_root="/openlist/final/tv",
        expected_file_count=expected_file_count,
    )


def _manifest_job(*names: str) -> dict:
    names = names or ("E01.mkv", "E02.mkv")
    return rclone_v1_job(
        _plan(expected_file_count=len(names)),
        raw_data={"rclone_staging_manifest": rclone_v1_manifest(JOB_ID, *names)},
    )


class _CallbackDatabase(RepositoryFakeMixin):
    def __init__(self, job: dict) -> None:
        self.job = copy.deepcopy(job)
        self.file_events: list[tuple] = []

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **changes) -> None:
        self.job.update(copy.deepcopy(changes))

    def update_job_if_status(self, _job_id: int, expected_statuses, **changes) -> bool:
        if str(self.job.get("status") or "") not in {
            str(value) for value in expected_statuses
        }:
            return False
        self.job.update(copy.deepcopy(changes))
        return True

    @staticmethod
    def add_event(*_args, **_kwargs) -> None:
        return None

    @staticmethod
    def add_rclone_event(*_args, **_kwargs) -> None:
        return None

    def add_rclone_file_event(self, *args, **kwargs) -> None:
        self.file_events.append((args, kwargs))


def _callback_service(database: _CallbackDatabase) -> RcloneCallbackService:
    return RcloneCallbackService(
        CallbackDependencies(
            db=database,
            rclone=SimpleNamespace(),
            safe_int=_safe_int,
            callback_level=lambda _status: "info",
            cancelled_status="cancelled",
        )
    )


def _callback_service_with_managed_rclone(database: _CallbackDatabase) -> RcloneCallbackService:
    return RcloneCallbackService(
        CallbackDependencies(
            db=database,
            rclone=SimpleNamespace(
                build_v1_copy_run=lambda _job, _plan: {
                    "job_dir_name": f"job-{JOB_ID}",
                    "source_root": "/mounted/source/quark/tv",
                    "target_root": "/mounted/destination/tv",
                }
            ),
            safe_int=_safe_int,
            callback_level=lambda _status: "info",
            cancelled_status="cancelled",
        )
    )


class RcloneStagingManifestCallbackTests(unittest.TestCase):
    def test_manifest_uses_managed_rclone_coordinates_from_frozen_plan(self) -> None:
        database = _CallbackDatabase(rclone_v1_job(_plan()))
        service = _callback_service_with_managed_rclone(database)

        response, status_code = service.handle(
            {
                "run_id": 1,
                "job_id": JOB_ID,
                "status": "staging_manifest",
                "category": "tv",
                "source_path": "/mounted/source/quark/tv",
                "target_path": "/mounted/destination/tv",
                "manifest_paths": [
                    "/mounted/source/quark/tv/job-42/E01.mkv",
                ],
            }
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(response["success"])
        self.assertEqual(
            response["manifest"]["job_root"],
            "/mounted/source/quark/tv/job-42",
        )

    def test_manifest_callback_merges_paths_across_runs(self) -> None:
        database = _CallbackDatabase(rclone_v1_job(_plan(expected_file_count=3)))
        service = _callback_service(database)

        for run_id, paths in (
            (1, [f"{SOURCE_JOB_ROOT}/E01.mkv", f"{SOURCE_JOB_ROOT}/E02.mkv"]),
            (2, [f"{SOURCE_JOB_ROOT}/E02.mkv", f"{SOURCE_JOB_ROOT}/E03.mkv"]),
        ):
            response, status_code = service.handle(
                {
                    "run_id": run_id,
                    "job_id": JOB_ID,
                    "status": "staging_manifest",
                    "category": "tv",
                    "source_path": "/quark/tv",
                    "target_path": "/openlist/staging/tv",
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
        database = _CallbackDatabase(rclone_v1_job(_plan()))
        response, status_code = _callback_service(database).handle(
            {
                "run_id": 1,
                "job_id": JOB_ID,
                "status": "staging_manifest",
                "manifest_paths": ["/quark/tv/job-99/E01.mkv"],
            }
        )

        self.assertEqual(status_code, 409)
        self.assertFalse(response["success"])

    def test_file_callback_rejects_another_job_source_path(self) -> None:
        database = _CallbackDatabase(rclone_v1_job(_plan()))
        response, status_code = _callback_service(database).handle(
            {
                "run_id": 3,
                "job_id": JOB_ID,
                "require_job_match": True,
                "status": "done",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/quark/tv/job-99/E01.mkv",
                "target_path": f"{TARGET_JOB_ROOT}/E01.mkv",
            }
        )

        self.assertEqual(status_code, 409)
        self.assertEqual(response["error_code"], "rclone_staging_path_mismatch")
        self.assertEqual(database.file_events, [])

    def test_file_callback_rejects_target_outside_or_equal_to_job_root(self) -> None:
        for target_path in (
            "/openlist/other/job-42/E01.mkv",
            TARGET_JOB_ROOT,
        ):
            with self.subTest(target_path=target_path):
                database = _CallbackDatabase(rclone_v1_job(_plan()))
                response, status_code = _callback_service(database).handle(
                    {
                        "run_id": 3,
                        "job_id": JOB_ID,
                        "require_job_match": True,
                        "status": "done",
                        "category": "tv",
                        "filename": "E01.mkv",
                        "source_path": f"{SOURCE_JOB_ROOT}/E01.mkv",
                        "target_path": target_path,
                    }
                )

                self.assertEqual(status_code, 409)
                self.assertEqual(
                    response["error_code"], "rclone_staging_path_mismatch"
                )
                self.assertEqual(database.file_events, [])


class RcloneManifestFeasibilityTests(unittest.TestCase):
    def test_transport_manifest_keeps_sidecars_for_completion(self) -> None:
        names = ("E01.mp4", "E01.srt", "tvshow.nfo")
        source_paths = [f"{SOURCE_JOB_ROOT}/{name}" for name in names]
        job = _manifest_job(*names)
        events = [
            {"id": index, "status": "done", "source_path": path}
            for index, path in enumerate(source_paths, 1)
        ]

        verdict = RcloneJobFeasibilityEvaluator.evaluate(job, events, 0)

        self.assertTrue(verdict["ready"])
        self.assertEqual(verdict["expected_file_count"], 3)
        self.assertEqual(_video_file_paths(source_paths), [source_paths[0]])



class RcloneWorkerManifestOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")

    def test_source_listing_is_job_scoped_before_stability_check(self) -> None:
        self.assertIn('--include "/${RCLONE_ONLY_JOB_DIR}/**"', self.content)

    def test_manifest_ack_happens_before_first_file_processing(self) -> None:
        manifest_ack = self.content.index(
            'if ! persist_staging_manifest "$copy_file_list"'
        )
        file_loop = self.content.index("while IFS= read -r file; do", manifest_ack)
        self.assertLess(manifest_ack, file_loop)

    def test_all_job_scoped_callbacks_require_job_match(self) -> None:
        self.assertIn('payload["require_job_match"] = True', self.content)
        self.assertIn('payload["job_id"] = int(job_dir_match.group(1))', self.content)

    def test_unstable_listing_is_rejected_before_manifest(self) -> None:
        failed_assignment = self.content.index(
            'if copy_file_list="$(stable_file_list'
        )
        manifest_ack = self.content.index(
            'if ! persist_staging_manifest "$copy_file_list"'
        )
        self.assertLess(failed_assignment, manifest_ack)


if __name__ == "__main__":
    unittest.main()
