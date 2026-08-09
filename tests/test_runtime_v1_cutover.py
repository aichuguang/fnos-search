from __future__ import annotations

import unittest

from fnos_media_import.importers.cloud139 import Cloud139Importer
from fnos_media_import.importers.generic import GenericWebhookImporter
from fnos_media_import.importers.sixpan import SixPanOfflineImporter
from fnos_media_import.services.import_service import ImportService


class RuntimeV1CutoverTests(unittest.TestCase):
    def test_import_service_does_not_derive_target_from_removed_category_paths(self) -> None:
        method_names = set(vars(ImportService))
        self.assertNotIn("_target_path", method_names)
        self.assertFalse(any(name.startswith("_submit_") for name in method_names))

    def test_provider_importers_require_v1_locator_or_explicit_path(self) -> None:
        cloud139 = Cloud139Importer(
            {
                "target_root_path": "/legacy",
                "category_target_paths": {"movie": "/legacy/movie"},
            },
            {"access_token": "legacy"},
        )
        self.assertEqual(
            cloud139.target_path_for_category(
                category={"cloud139_target_path": "/legacy/movie", "label": "电影"}
            ),
            "",
        )

        sixpan = SixPanOfflineImporter(
            {"default_save_path": "/legacy", "access_token": "legacy"}
        )
        self.assertEqual(
            sixpan.target_path_for_category({"sixpan_save_path": "/legacy/movie", "label": "电影"}),
            "",
        )

        generic = GenericWebhookImporter(
            "旧接口", {"endpoint": "https://example.invalid/legacy"}
        )
        result = generic.import_resource(
            "电影",
            "https://example.invalid/share",
            {"label": "电影", "mobile_target_path": "/legacy"},
        )
        self.assertFalse(result.success)
        self.assertIn("V1", result.message)


if __name__ == "__main__":
    unittest.main()
