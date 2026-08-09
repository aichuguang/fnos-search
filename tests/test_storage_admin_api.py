from __future__ import annotations

import unittest
from pathlib import Path

from flask import Flask

from fnos_media_import.blueprints.settings import SettingsRouteContext, create_settings_blueprint


ROOT = Path(__file__).resolve().parents[1]


class StorageAdminApiTests(unittest.TestCase):
    """V1 routing is admin-owned; legacy target selection must stay removed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = Flask(__name__)
        ok = lambda: ({"success": True}, 200)
        cls.app.register_blueprint(
            create_settings_blueprint(
                SettingsRouteContext(
                    admin_required=lambda handler: handler,
                    config=ok,
                    history_summary=ok,
                    cleanup_history=ok,
                    advanced_config=ok,
                    advanced_config_update=ok,
                    advanced_export=ok,
                    settings=ok,
                    settings_update=ok,
                    settings_update_all=ok,
                )
            )
        )
        cls.client = cls.app.test_client()

    def test_legacy_storage_settings_routes_are_removed(self) -> None:
        self.assertEqual(self.client.get("/api/admin/settings/storage/status").status_code, 404)
        self.assertEqual(self.client.post("/api/admin/settings/storage/test", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/admin/settings/storage", json={}).status_code, 404)
        self.assertEqual(self.client.put("/api/admin/settings/storage", json={}).status_code, 404)

    def test_public_storage_target_selector_route_is_removed(self) -> None:
        self.assertEqual(self.client.get("/api/public/storage-targets").status_code, 404)

    def test_public_submit_does_not_send_destination_override(self) -> None:
        source = (ROOT / "static" / "public-submit.js").read_text(encoding="utf-8")
        self.assertNotIn("pendingDestinationId", source)
        self.assertNotIn("publicConfirmDestination", source)
        self.assertNotIn("submitPayload.destination_id", source)
        self.assertNotIn("/api/public/storage-targets", source)

    def test_admin_uses_only_v1_storage_workspace(self) -> None:
        html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        legacy_script = (ROOT / "static" / "admin-settings.js").read_text(encoding="utf-8")
        v1_script = (ROOT / "static" / "admin-storage-v1.js").read_text(encoding="utf-8")
        self.assertIn("admin-storage-v1.js", html)
        self.assertIn("refreshCustomSelects", v1_script)
        self.assertNotIn("storageTargetsRefreshBtn", html)
        self.assertNotIn("/api/admin/settings/storage", legacy_script)
        self.assertIn("/api/admin/storage-setup", v1_script)
        self.assertIn("/api/admin/storage-setup/routes", v1_script)
        self.assertNotIn("provider.runtime_schema", v1_script)
        self.assertNotIn("provider-runtime", v1_script)
        self.assertNotIn("data-runtime-action", v1_script)
        self.assertIn("authDefinition.input_schema", v1_script)
        self.assertIn("provider?.native_locator_schema", v1_script)
        self.assertIn("cause?.data", v1_script)
        self.assertIn("confirm_identity", v1_script)
        self.assertIn("assignment_revisions", v1_script)
        self.assertIn("ASSIGNMENT_CONFLICT", v1_script)
        self.assertIn("expected_revision", v1_script)
        self.assertIn("TARGET_CONFLICT", v1_script)
        self.assertNotIn("renderAccountsLegacy", v1_script)
        self.assertNotIn("connections[0]", v1_script)
        self.assertNotIn('normalizedProvider === "tianyi"', v1_script)
        self.assertNotIn("Provider 账号", v1_script)
        self.assertNotIn("存储目标与绑定", v1_script)
        self.assertNotIn("\ufffd", v1_script)


if __name__ == "__main__":
    unittest.main()
