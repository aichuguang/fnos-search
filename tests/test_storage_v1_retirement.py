from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from fnos_media_import.providers.manifests import (
    MOBILE_MANIFEST,
    TIANYI_MANIFEST,
)
from fnos_media_import.storage_paths import resolve_local_target


ROOT = Path(__file__).resolve().parents[1]


class StorageV1RetirementTests(unittest.TestCase):
    def test_importing_storage_package_has_no_driver_registry_side_effects(self) -> None:
        script = (
            "import json,sys; import fnos_media_import.storage; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name.startswith('fnos_media_import.storage.'))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        self.assertEqual([], json.loads(result.stdout.strip()))

    def test_retired_storage_v3_modules_are_absent(self) -> None:
        for relative in (
            "fnos_media_import/storage/registry.py",
            "fnos_media_import/storage/types.py",
            "fnos_media_import/storage/_pathutil.py",
            "fnos_media_import/storage/drivers/__init__.py",
            "fnos_media_import/storage/drivers/rclone.py",
            "fnos_media_import/storage/drivers/cmcc_api.py",
            "fnos_media_import/services/storage_target_probe_service.py",
            "fnos_media_import/services/sixpan_polling_runtime.py",
            "fnos_media_import/services/sixpan_offline_sync_service.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_local_target_resolution_remains_in_current_path_module(self) -> None:
        self.assertEqual(
            "/storage-targets/library/movie",
            resolve_local_target("/storage-targets/library", "movie"),
        )
        for root, relative in (
            ("relative", "movie"),
            ("/data", "movie"),
            ("/storage-targets/library", "../movie"),
            ("/storage-targets/library", "/movie"),
        ):
            with self.subTest(root=root, relative=relative):
                with self.assertRaises(ValueError):
                    resolve_local_target(root, relative)

    def test_mobile_and_tianyi_advertise_provider_relay_upload(self) -> None:
        self.assertTrue(MOBILE_MANIFEST.capabilities.supports_relay_upload)
        self.assertTrue(TIANYI_MANIFEST.capabilities.supports_relay_upload)
        self.assertFalse(MOBILE_MANIFEST.capabilities.supports_rclone_dest)
        self.assertFalse(TIANYI_MANIFEST.capabilities.supports_rclone_dest)
        self.assertIn("relay", MOBILE_MANIFEST.components)
        self.assertIn("relay", TIANYI_MANIFEST.components)


if __name__ == "__main__":
    unittest.main()
