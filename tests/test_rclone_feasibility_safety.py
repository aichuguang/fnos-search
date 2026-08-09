from __future__ import annotations

import unittest

from fnos_media_import.services.rclone_job_feasibility import (
    RcloneJobFeasibilityEvaluator,
)
from tests._rclone_v1_fixtures import (
    rclone_v1_job,
    rclone_v1_manifest,
    rclone_v1_plan,
)


JOB_ID = 42
SOURCE_ROOT = f"/quark/tv/job-{JOB_ID}"


def _job(*, names: tuple[str, ...] = (), expected_file_count: int | None = None) -> dict:
    expected = len(names) if expected_file_count is None else expected_file_count
    raw_data = (
        {"rclone_staging_manifest": rclone_v1_manifest(JOB_ID, *names)}
        if names
        else {}
    )
    return rclone_v1_job(
        rclone_v1_plan(
            JOB_ID,
            category="tv",
            expected_file_count=expected,
        ),
        raw_data=raw_data,
    )


class RcloneFeasibilitySafetyTests(unittest.TestCase):
    def test_non_v1_job_is_manual_review_not_legacy_count_fallback(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            {"id": JOB_ID, "plan_version": 2, "raw_data": {"path_rule": {"file_count": 1}}},
            [{"status": "done", "target_path": "/target/E01.mkv"}],
            exit_code=0,
        )

        self.assertFalse(result["ready"])
        self.assertTrue(result["manual_review"])
        self.assertEqual(result["error_code"], "UNSUPPORTED_PLAN_VERSION")

    def test_failed_run_with_unknown_expected_count_is_not_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            _job(expected_file_count=0),
            [{"status": "done", "source_path": f"{SOURCE_ROOT}/E01.mkv"}],
            exit_code=1,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "transferring")
        self.assertEqual(result["completed_file_count"], 1)
        self.assertEqual(result["expected_file_count"], 0)

    def test_failed_run_can_recover_when_frozen_manifest_is_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            _job(names=("E01.mkv",)),
            [{"status": "done", "source_path": f"{SOURCE_ROOT}/E01.mkv"}],
            exit_code=1,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["completed_file_count"], 1)
        self.assertEqual(result["expected_file_count_source"], "staging_manifest")

    def test_successful_run_without_manifest_is_not_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            _job(expected_file_count=1),
            [{"status": "done", "source_path": f"{SOURCE_ROOT}/E01.mkv"}],
            exit_code=0,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "transferring")
        self.assertTrue(result["manifest_required"])
        self.assertEqual(result["expected_file_count_source"], "v1_verification")

    def test_manifest_requires_every_source_path_not_only_same_count(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            _job(names=("E01.mkv", "E02.mkv")),
            [
                {"status": "done", "source_path": f"{SOURCE_ROOT}/E01.mkv"},
                {"status": "done", "source_path": f"{SOURCE_ROOT}/other.mkv"},
            ],
            exit_code=0,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["expected_file_count"], 2)
        self.assertEqual(result["completed_file_count"], 2)
        self.assertEqual(result["missing_manifest_file_count"], 1)

    def test_manifest_completes_after_every_source_path_is_terminal(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            _job(names=("E01.mkv", "E02.mkv")),
            [
                {"status": "done", "source_path": f"{SOURCE_ROOT}/E01.mkv"},
                {
                    "status": "skipped_existing",
                    "source_path": f"{SOURCE_ROOT}/E02.mkv",
                },
            ],
            exit_code=0,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["expected_file_count_source"], "staging_manifest")


if __name__ == "__main__":
    unittest.main()
