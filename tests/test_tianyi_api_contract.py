from __future__ import annotations

import unittest

from fnos_media_import.openapi import get_openapi_spec


class LegacyCloudRouteRemovalTests(unittest.TestCase):
    def test_legacy_cloud_paths_are_not_documented(self) -> None:
        paths = get_openapi_spec()["paths"]
        for path in (
            "/api/quark/check",
            "/api/quark/file-list",
            "/api/cloud139/check",
            "/api/cloud139/file-list",
            "/api/tianyi/check",
            "/api/tianyi/file-list",
        ):
            self.assertNotIn(path, paths)


if __name__ == "__main__":
    unittest.main()
