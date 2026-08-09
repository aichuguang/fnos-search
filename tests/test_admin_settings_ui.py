from __future__ import annotations

import unittest
from pathlib import Path

from fnos_media_import.services.security_status_service import SecurityStatusService


class AdminSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = Path("templates/admin.html").read_text(encoding="utf-8")
        cls.script = Path("static/admin.js").read_text(encoding="utf-8")

    def test_legacy_category_path_editor_is_removed(self) -> None:
        self.assertNotIn('data-settings-section-target="categories"', self.template)
        self.assertNotIn("upload_backend: cmccBackend", self.script)
        self.assertNotIn('id="categoryTemplateSixpanFnosRoot"', self.template)
        self.assertNotIn('id="advancedCategoryRows"', self.template)
        self.assertNotIn('id="advSixpanFnosMountName"', self.template)
        self.assertNotIn("cloud139_target_path", self.template)
        self.assertNotIn("cloud139_fnos_target_path", self.template)
        helper = Path("static/admin-settings.js").read_text(encoding="utf-8")
        self.assertNotIn("quark_save_path", helper)
        self.assertNotIn("sixpan_save_path", helper)

    def test_advanced_config_uses_v1_envelope_and_no_global_openlist_identity(self) -> None:
        self.assertNotIn('id="advOpenlistBaseUrl"', self.template)
        self.assertNotIn('id="advOpenlistToken"', self.template)
        self.assertNotIn("openlist.token", self.script)
        self.assertNotIn("advancedRouteChecks", self.template)
        self.assertNotIn("data-adv-route", self.script)
        self.assertIn('const ADVANCED_CONFIG_FORMAT = "fnos-media-import/advanced-config"', self.script)
        self.assertIn("advancedConfigUpdatePayload(config, { mode: \"merge\" })", self.script)
        self.assertIn("advanced: advancedConfigUpdatePayload", self.script)

    def test_secret_clear_actions_are_centralized(self) -> None:
        self.assertIn('id="manageAdvancedSecretsBtn"', self.template)
        self.assertNotIn("data-clear-advanced-secret", self.script)
        self.assertNotIn("config-secret-clear", self.script)

    def test_v1_workspace_is_the_only_storage_configuration_surface(self) -> None:
        self.assertIn('id="v1StorageWorkspace"', self.template)
        self.assertIn('admin-storage-v1.js', self.template)
        self.assertNotIn('id="sixpanStartAuthBtn"', self.template)
        self.assertNotIn('guest_selectable', self.template)
        self.assertNotIn('guest_selectable', Path("static/admin-settings.js").read_text(encoding="utf-8"))

    def test_notification_toggles_persist_immediately_with_partial_updates(self) -> None:
        source = Path("static/admin-notifications.js").read_text(encoding="utf-8")
        self.assertIn("function saveNotificationToggle", source)
        self.assertIn("notificationEnabled", source)
        self.assertIn("element.checked = previous", source)
        self.assertIn("silentLoading: true", source)

    def test_admin_settings_helpers_are_valid_and_exported(self) -> None:
        source = Path("static/admin-settings.js").read_text(encoding="utf-8")
        self.assertIn("window.FnosAdminSettings = Object.freeze", source)
        self.assertIn("collectSearchProviders", source)
        self.assertNotIn("\ufffd", source)

    def test_storage_setup_uses_three_responsive_product_workspaces(self) -> None:
        source = Path("static/admin-storage-v1.js").read_text(encoding="utf-8")
        styles = Path("static/product.css").read_text(encoding="utf-8")
        self.assertIn('viewButton("accounts", "云盘账号"', source)
        self.assertIn('viewButton("locations", "保存位置"', source)
        self.assertIn('viewButton("routes", "入库规则"', source)
        self.assertNotIn("来源 × 分类", source)
        self.assertNotIn("category_paths", source)
        self.assertNotIn("library_root", source)
        self.assertIn("provider-auth-sessions", source)
        self.assertNotIn('{ "Content-Type": "application/json" }', source)
        core = Path("static/ui-core.js").read_text(encoding="utf-8")
        self.assertIn('requestHeaders["Content-Type"] = "application/json"', core)
        self.assertIn("instanceof FormData", core)
        self.assertIn('data-provider-kind="${esc(provider.provider_kind)}"', source)
        self.assertIn("normalizedProvider", source)
        self.assertIn("normalizedMethod", source)
        self.assertIn("登录配置未加载完整，请刷新页面后重试", source)
        self.assertIn('id="providerLoginForm"', source)
        self.assertIn('id="providerChallengeForm"', source)
        self.assertIn("captcha_image", source)
        self.assertIn('data-auth-action="refresh-captcha"', source)
        self.assertIn("{ refresh_captcha: true }", source)
        self.assertIn("state.authSession.public?.error", source)
        self.assertNotIn('id="providerRuntimeForm"', source)
        self.assertNotIn('data-runtime-action="edit"', source)
        self.assertIn("function authInputSchema(provider, authDefinition)", source)
        self.assertIn('hasOwnProperty.call(authDefinition, "input_schema")', source)
        self.assertIn('=== "qrcode"', source)
        self.assertIn("schemaFields(authInputSchema(provider, authDefinition)).length", source)
        self.assertIn("关闭窗口，后台继续", source)
        self.assertIn("busyWasDisabled", source)
        self.assertIn("if (state.busy) return;", source)
        self.assertIn('data-account-action="test"', source)
        self.assertIn("/test`, { method: \"POST\" }", source)
        self.assertIn("登录凭证由授权流程维护", source)
        self.assertIn("JSON.stringify({ name: raw.name })", source)
        self.assertIn("AUTH_SESSION_STORAGE_KEY", source)
        self.assertIn("restoreAuthSession", source)
        self.assertIn(".storage-auth-captcha", styles)
        self.assertIn('id="storageAuthError"', source)
        self.assertIn('role="alert"', source)
        self.assertEqual(source.count("${renderAuthError()}"), 3)
        self.assertIn('ui.showToast?.("toast", message, "error", 6000)', source)
        self.assertIn('!inlineErrorPresent', source)
        self.assertIn('inlineError?.scrollIntoView?.({ block: "nearest" })', source)
        self.assertIn('["qrcode", "device_code"].includes(session.auth_method)', source)
        self.assertIn("系统仍在后台等待授权结果", source)
        self.assertNotIn("请继续扫码", source)
        self.assertIn(".storage-auth-error", styles)
        self.assertIn("function usesNativeFolderBrowser(provider)", source)
        self.assertIn('data-native-directory-action="enter"', source)
        self.assertIn("/storage-setup/accounts/${encodeURIComponent(accountId)}/folders", source)
        self.assertIn("native_root_name: raw.native_root_name || null", source)
        self.assertIn("location.native_upload_capable", source)
        self.assertIn(".storage-native-browser", styles)
        self.assertIn(".storage-native-folder-list", styles)
        self.assertIn(".storage-capability-list", styles)
        self.assertIn("@media (max-width: 700px)", styles)
        self.assertIn(".storage-route-row { grid-template-columns: 1fr;", styles)

    def test_provider_auth_script_cache_version_is_current(self) -> None:
        self.assertIn(
            '/static/admin-storage-v1.js?v=20260809-v1-cutover-v001',
            self.template,
        )

    def test_custom_select_labels_wrapped_dynamic_fields(self) -> None:
        source = Path("static/ui-select.js").read_text(encoding="utf-8")
        self.assertIn('const wrappingLabel = select.closest("label")', source)
        self.assertIn('wrappingLabel?.querySelector("span")', source)
        self.assertIn('/static/ui-select.js?v=20260808-custom-select-v017', self.template)

    def test_admin_layout_reserves_security_banner_row_and_product_theme(self) -> None:
        styles = Path("static/product.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr);", styles)
        self.assertIn("body[data-page=\"admin\"] .admin-tab-panel.active {\n    grid-row: 3;", styles)
        self.assertIn("Multi-link workspace visual consistency", styles)
        self.assertNotIn("#151719", styles)


    def test_security_status_does_not_emit_legacy_api_notice(self) -> None:
        status = SecurityStatusService(
            raw_config=lambda: {
                "admin": {"username": "admin", "password": "admin"},
            },
            settings=lambda: {"admin_profile": {"username": "owner", "password_hash": "hash"}},
            strict_enabled=lambda _config: True,
            docker_socket_mounted=lambda: False,
            admin_profile_key="admin_profile",
        ).build()

        self.assertEqual(status["issues"], [])
        self.assertNotIn("legacy_admin_api_protected", status["flags"])


if __name__ == "__main__":
    unittest.main()
