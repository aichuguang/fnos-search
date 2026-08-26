from __future__ import annotations

import re
import unittest
from pathlib import Path


class AdminAssetCacheVersionTests(unittest.TestCase):
    def test_changed_admin_assets_use_current_release_version(self) -> None:
        source = Path("templates/admin.html").read_text(encoding="utf-8")
        expected_version = "20260810-storage-flow-v006"
        for asset in (
            "admin-media.js",
            "admin-jobs.js",
            "admin-rclone.js",
            "admin-organizer.js",
            "admin-updates.js",
            "admin-trending.js",
            "admin-settings.js",
            "admin-storage-v1.js",
            "admin-adapters.js",
            "admin-bootstrap.js",
            "admin.js",
        ):
            match = re.search(rf'/static/{re.escape(asset)}\?v=([^"\s]+)', source)
            self.assertIsNotNone(match, asset)
            self.assertEqual(match.group(1), expected_version, f"{asset}: {match.group(1)}")

        css_match = re.search(r'/static/product\.css\?v=([^"\s]+)', source)
        self.assertIsNotNone(css_match, "product.css")
        self.assertEqual(css_match.group(1), expected_version)

        workbench_match = re.search(
            r'/static/resource-search-workbench\.js\?v=([^"\s]+)', source
        )
        self.assertIsNotNone(workbench_match, "resource-search-workbench.js")
        self.assertEqual(workbench_match.group(1), expected_version)


if __name__ == "__main__":
    unittest.main()
