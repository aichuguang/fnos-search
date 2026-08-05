from __future__ import annotations

import unittest

from fnos_media_import.repositories.rclone_repository import (
    _job_matches_staging_callback_paths,
    _rclone_category_match_values,
    _rclone_job_id_from_paths,
)


class RcloneJobPathMatchingTests(unittest.TestCase):
    def test_job_directory_directly_under_category_is_authoritative(self) -> None:
        job_id, authoritative = _rclone_job_id_from_paths(
            "/离线下载/电视剧/job-17/剧名/E01.mkv",
            category_values=_rclone_category_match_values("tv"),
        )

        self.assertEqual(job_id, 17)
        self.assertTrue(authoritative)

    def test_nested_legacy_resource_name_does_not_disable_fuzzy_matching(self) -> None:
        job_id, authoritative = _rclone_job_id_from_paths(
            "/离线下载/电视剧/旧资源/job-2024/E01.mkv",
            category_values=_rclone_category_match_values("tv"),
        )

        self.assertEqual(job_id, 2024)
        self.assertFalse(authoritative)

    def test_invalid_category_child_job_id_is_rejected(self) -> None:
        job_id, authoritative = _rclone_job_id_from_paths(
            "/离线下载/电视剧/job-0/E01.mkv",
            category_values=_rclone_category_match_values("电视剧"),
        )

        self.assertEqual(job_id, -1)
        self.assertTrue(authoritative)

    def test_persisted_plan_root_must_match_the_callback_path(self) -> None:
        job = {
            "raw_data": {
                "staging_plan": {
                    "enabled": True,
                    "job_id": 17,
                    "quark_job_root": "/离线下载/电视剧/job-17",
                    "storage_job_root": "移动云盘A/_入库暂存/电视剧/job-17",
                }
            }
        }

        matched, configured = _job_matches_staging_callback_paths(
            job,
            "/离线下载/电视剧/job-17/剧名/E01.mkv",
            "移动云盘A/_入库暂存/电视剧/job-17/剧名/E01.mkv",
        )
        wrong_match, wrong_configured = _job_matches_staging_callback_paths(
            job,
            "/其它目录/job-17/E01.mkv",
        )

        self.assertTrue(configured)
        self.assertTrue(matched)
        self.assertTrue(wrong_configured)
        self.assertFalse(wrong_match)


if __name__ == "__main__":
    unittest.main()
