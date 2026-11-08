from __future__ import annotations

import unittest

from fnos_media_import.config_persistence import (
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    normalize_advanced_config_payload,
    trending_schedule_document,
)


class TrendingScheduleDocumentTests(unittest.TestCase):
    def test_builds_a_versioned_document(self) -> None:
        document = trending_schedule_document({}, enabled=True, run_at="08:30")
        self.assertEqual(document["format"], ADVANCED_CONFIG_EXPORT_FORMAT)
        self.assertEqual(document["version"], ADVANCED_CONFIG_EXPORT_VERSION)
        self.assertEqual(document["stored"]["hot_discovery"], {"enabled": True, "run_at": "08:30"})

    def test_preserves_other_sections_and_section_fields(self) -> None:
        stored = {
            "tmdb": {"token": "abc"},
            "hot_discovery": {"timezone": "Asia/Shanghai", "max_items_per_source": 20},
        }
        document = trending_schedule_document(stored, enabled=False, run_at="09:15")
        section = document["stored"]["hot_discovery"]
        self.assertEqual(section["timezone"], "Asia/Shanghai")
        self.assertEqual(section["max_items_per_source"], 20)
        self.assertFalse(section["enabled"])
        self.assertEqual(section["run_at"], "09:15")
        self.assertEqual(document["stored"]["tmdb"], {"token": "abc"})

    def test_document_passes_the_advanced_config_normalizer(self) -> None:
        document = trending_schedule_document({}, enabled=True, run_at="07:00")
        normalized = normalize_advanced_config_payload({"config": document, "mode": "merge"})
        self.assertEqual(normalized["hot_discovery"]["run_at"], "07:00")
        self.assertTrue(normalized["hot_discovery"]["enabled"])

    def test_rejects_unknown_section_fields(self) -> None:
        document = trending_schedule_document(
            {"hot_discovery": {"totally_unknown_field": 1}},
            enabled=True,
            run_at="08:30",
        )
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload({"config": document, "mode": "merge"})


if __name__ == "__main__":
    unittest.main()
