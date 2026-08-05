from __future__ import annotations

import unittest

from fnos_media_import.services.rclone_job_feasibility import RcloneJobFeasibilityEvaluator
from tests.test_rclone_persisted_staging_plan import _persisted_plan


class RcloneFeasibilitySafetyTests(unittest.TestCase):
    @staticmethod
    def _staging_job(*, manifest_paths: list[str] | None = None) -> dict:
        raw_data = {
            "staging_plan": _persisted_plan(42),
            # 这些旧字段可能只是分享根层条目数，不能当作文件 manifest。
            "path_rule": {"file_count": 1},
            "check": {"data": {"share": {"file_num": 1}}},
        }
        if manifest_paths is not None:
            raw_data["rclone_staging_manifest"] = {
                "version": 1,
                "source_paths": manifest_paths,
                "expected_file_count": len(manifest_paths),
            }
        return {
            "id": 42,
            "target_route": "quark_to_mobile",
            "raw_data": raw_data,
        }

    def test_failed_run_with_unknown_expected_count_is_not_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            {"raw_data": {}},
            [{"status": "done", "target_path": "/target/E01.mkv"}],
            exit_code=1,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "transferring")
        self.assertEqual(result["completed_file_count"], 1)
        self.assertEqual(result["expected_file_count"], 0)

    def test_failed_run_can_recover_when_known_count_is_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            {"raw_data": {"expected_file_count": 1}},
            [{"status": "done", "target_path": "/target/E01.mkv"}],
            exit_code=1,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["completed_file_count"], 1)

    def test_successful_staging_run_without_manifest_is_not_complete(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            self._staging_job(),
            [
                {
                    "status": "done",
                    "source_path": "/旧夸克/电视剧/job-42/E01.mkv",
                    "target_path": "/目标/E01.mkv",
                }
            ],
            exit_code=0,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "transferring")
        self.assertEqual(result["expected_file_count"], 0)
        self.assertTrue(result["manifest_required"])

    def test_staging_manifest_requires_every_source_path_not_only_same_count(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            self._staging_job(
                manifest_paths=[
                    "/旧夸克/电视剧/job-42/E01.mkv",
                    "/旧夸克/电视剧/job-42/E02.mkv",
                ]
            ),
            [
                {"status": "done", "source_path": "/旧夸克/电视剧/job-42/E01.mkv"},
                {"status": "done", "source_path": "/旧夸克/电视剧/job-42/other.mkv"},
            ],
            exit_code=0,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["expected_file_count"], 2)
        self.assertEqual(result["completed_file_count"], 2)
        self.assertEqual(result["missing_manifest_file_count"], 1)

    def test_staging_manifest_completes_after_all_manifest_paths_are_done(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            self._staging_job(
                manifest_paths=[
                    "/旧夸克/电视剧/job-42/E01.mkv",
                    "/旧夸克/电视剧/job-42/E02.mkv",
                ]
            ),
            [
                {"status": "done", "source_path": "/旧夸克/电视剧/job-42/E01.mkv"},
                {"status": "skipped_existing", "source_path": "旧夸克/电视剧/job-42/E02.mkv"},
            ],
            exit_code=0,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["expected_file_count_source"], "staging_manifest")

    def test_legacy_job_keeps_legacy_recursive_count_compatibility(self) -> None:
        result = RcloneJobFeasibilityEvaluator.evaluate(
            {"raw_data": {"path_rule": {"file_count": 1}}},
            [{"status": "done", "target_path": "/target/E01.mkv"}],
            exit_code=0,
        )

        self.assertTrue(result["ready"])
        self.assertFalse(result["manifest_required"])


if __name__ == "__main__":
    unittest.main()
