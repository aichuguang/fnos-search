from __future__ import annotations

import unittest

from fnos_media_import.organizer.service import _title_from_update_payload
from fnos_media_import.services.import_job_service import ImportJobCreationService


class TmdbTitleReferenceTests(unittest.TestCase):
    def test_job_title_prefers_tmdb_official_title(self) -> None:
        media_identity = {
            "tmdb_id": 12345,
            "media_type": "tv",
            "title": "重器",
            "year": "2026",
        }
        title = "重器[全33集][国语音轨+简繁英字幕].Forging.Justice.S01.2026.2160p"
        summary = ImportJobCreationService._safe_request_summary(
            {"title": title, "media_identity": media_identity},
            category_key="tv",
        )
        self.assertEqual(summary["raw_title"], title)
        self.assertEqual(summary["media_identity"]["title"], "重器")

    def test_request_summary_keeps_no_raw_title_without_media_identity(self) -> None:
        summary = ImportJobCreationService._safe_request_summary(
            {"title": "普通分享标题"},
            category_key="movie",
        )
        self.assertNotIn("raw_title", summary)

    def test_organizer_title_chain_uses_media_identity_as_reference(self) -> None:
        job = {
            "title": "重器 (2026)",
            "raw_data": {
                "request": {
                    "media_identity": {"tmdb_id": 1, "title": "重器", "year": "2026"},
                }
            },
        }
        self.assertEqual(
            _title_from_update_payload({"v1_transfer": True}, job, "/入库/job-1"),
            "重器",
        )

    def test_organizer_title_chain_still_falls_back_to_payload_title(self) -> None:
        job = {"title": "分享名 (2026)", "raw_data": {}}
        self.assertEqual(
            _title_from_update_payload({"title": "藏海传 4K"}, job, "/移动云/电视剧"),
            "藏海传",
        )

    def test_organizer_title_chain_prefers_explicit_update_context(self) -> None:
        job = {
            "raw_data": {
                "request": {
                    "media_identity": {"tmdb_id": 1, "title": "重器"},
                    "update_context": {"canonical_title": "运营指定的标题"},
                }
            },
        }
        self.assertEqual(
            _title_from_update_payload({}, job, "/入库/job-1"),
            "运营指定的标题",
        )


if __name__ == "__main__":
    unittest.main()
