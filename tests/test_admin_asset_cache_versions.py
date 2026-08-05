from __future__ import annotations

import re
import unittest
from pathlib import Path


class AdminAssetCacheVersionTests(unittest.TestCase):
    def test_changed_admin_assets_use_current_release_version(self) -> None:
        source = Path("templates/admin.html").read_text(encoding="utf-8")
        expected_version = "20260805-rclone-webdav-v003"
        for asset in (
            "admin-media.js",
            "admin-jobs.js",
            "admin-organizer.js",
            "admin-updates.js",
            "admin-trending.js",
            "admin-settings.js",
            "admin-adapters.js",
            "admin-bootstrap.js",
            "admin.js",
            "product.css",
        ):
            match = re.search(rf'/static/{re.escape(asset)}\?v=([^"\s]+)', source)
            self.assertIsNotNone(match, asset)
            self.assertEqual(match.group(1), expected_version, f"{asset}: {match.group(1)}")


if __name__ == "__main__":
    unittest.main()
