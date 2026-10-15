from __future__ import annotations

import re
import unittest
from pathlib import Path


class AdminAssetCacheVersionTests(unittest.TestCase):
    def test_changed_admin_assets_use_current_release_version(self) -> None:
        source = Path("templates/admin.html").read_text(encoding="utf-8")
        expected_version = "20261009-recovery-v001"
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
            version = "20261011-bugfix-v002" if asset in {"admin-organizer.js", "admin.js"} else expected_version
            if asset == "admin-organizer.js":
                version = "20261011-review-guard-v003"
            if asset == "admin-storage-v1.js":
                version = "20261011-storage-cas-v004"
            if asset in {"admin-jobs.js", "admin-rclone.js"}:
                version = "20261011-manual-review-v005"
            if asset == "admin-jobs.js":
                version = "20261011-request-retry-v006"
            if asset == "admin-rclone.js":
                version = "20261011-live-progress-v007"
            if asset == "admin-trending.js":
                version = "20261012-quark-selection-v008"
            if asset in {"admin.js", "admin-bootstrap.js", "admin-rclone.js"}:
                version = "20261014-transfer-center-v009"
            if asset == "admin-rclone.js":
                version = "20261015-relay-prefetch-v013"
            self.assertEqual(match.group(1), version, f"{asset}: {match.group(1)}")

        css_match = re.search(r'/static/product\.css\?v=([^"\s]+)', source)
        self.assertIsNotNone(css_match, "product.css")
        self.assertEqual(css_match.group(1), expected_version)

        transfer_css_match = re.search(r'/static/admin-transfers\.css\?v=([^"\s]+)', source)
        self.assertIsNotNone(transfer_css_match, "admin-transfers.css")
        self.assertEqual(transfer_css_match.group(1), "20261015-relay-prefetch-v013")

        workbench_match = re.search(
            r'/static/resource-search-workbench\.js\?v=([^"\s]+)', source
        )
        self.assertIsNotNone(workbench_match, "resource-search-workbench.js")
        self.assertEqual(workbench_match.group(1), expected_version)


if __name__ == "__main__":
    unittest.main()
