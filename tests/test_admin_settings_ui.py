from __future__ import annotations

import unittest
from pathlib import Path

from fnos_media_import.services.security_status_service import SecurityStatusService


class AdminSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = Path("templates/admin.html").read_text(encoding="utf-8")
        cls.script = Path("static/admin.js").read_text(encoding="utf-8")

    def test_sixpan_mount_has_one_editable_source(self) -> None:
        self.assertEqual(self.template.count('id="categoryTemplateSixpanFnosRoot"'), 1)
        self.assertNotIn('id="advSixpanFnosMountName"', self.template)
        self.assertIn(
            'sixpan_fnos_target_path: template.sixpanFnosRoot ? joinTemplatePath(template.sixpanFnosRoot, dir) : ""',
            self.script,
        )
        settings_script = Path("static/admin-settings.js").read_text(encoding="utf-8")
        build_patch = settings_script.split("function buildCategoryPatch", 1)[1].split("window.FnosAdminSettings", 1)[0]
        self.assertNotIn("sixpan_fnos_target_path", build_patch)

    def test_secret_clear_actions_are_centralized(self) -> None:
        self.assertIn('id="manageAdvancedSecretsBtn"', self.template)
        self.assertNotIn("data-clear-advanced-secret", self.script)
        self.assertNotIn("config-secret-clear", self.script)

    def test_sixpan_authorization_is_not_presented_as_another_save_action(self) -> None:
        self.assertIn('id="sixpanStartAuthBtn">开始授权</button>', self.template)
        self.assertNotIn("保存并开始授权", self.template)

    def test_notification_toggles_persist_immediately_with_partial_updates(self) -> None:
        source = Path("static/admin-notifications.js").read_text(encoding="utf-8")
        self.assertIn('saveNotificationToggle("notificationEnabled", "通知")', source)
        self.assertIn("return { enabled: checked }", source)
        self.assertIn("element.checked = previous", source)
        self.assertIn("silentLoading: true", source)


    def test_security_status_does_not_emit_legacy_api_notice(self) -> None:
        status = SecurityStatusService(
            raw_config=lambda: {
                "admin": {"username": "admin", "password": "admin"},
                "app": {"secret_key": "runtime-secret"},
            },
            settings=lambda: {"admin_profile": {"username": "owner", "password_hash": "hash"}},
            strict_enabled=lambda _config: True,
            default_secret=lambda _secret: False,
            docker_socket_mounted=lambda: False,
            admin_profile_key="admin_profile",
        ).build()

        self.assertEqual(status["issues"], [])
        self.assertNotIn("legacy_admin_api_protected", status["flags"])


if __name__ == "__main__":
    unittest.main()
