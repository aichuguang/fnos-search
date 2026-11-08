from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FnosSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        cls.media_script = (ROOT / "static" / "admin-media.js").read_text(encoding="utf-8")
        cls.admin_script = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")

    def fnos_card(self) -> str:
        match = re.search(r"<h3>飞牛媒体刷新</h3>(.*?)</section>", self.template, re.DOTALL)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_default_login_exposes_only_address_username_and_password(self) -> None:
        card = self.fnos_card()
        visible, _, _ = card.partition("<details")
        self.assertEqual(
            re.findall(r'id="(advFnos\w+)"', visible),
            ["advFnosServerUrl", "advFnosUsername", "advFnosPassword"],
        )
        self.assertIn("自动登录并缓存 Token", visible)
        self.assertIn("无需手动填写签名参数", visible)

    def test_legacy_overrides_are_optional_and_collapsed_by_default(self) -> None:
        card = self.fnos_card()
        match = re.search(r"<details\b([^>]*)>(.*?)</details>", card, re.DOTALL)
        self.assertIsNotNone(match)
        attributes, content = match.groups()
        self.assertNotRegex(attributes, r"\bopen\b")
        self.assertIn('class="advanced-details"', attributes)
        self.assertIn("高级兼容设置（普通登录无需填写）", content)
        self.assertIn("Token 默认自动获取", content)
        self.assertIn("已保存的敏感值留空会保留", content)
        self.assertIn("凭据管理", content)
        self.assertEqual(
            re.findall(r'id="(advFnos\w+)"', content),
            ["advFnosApiKey", "advFnosSecret", "advFnosToken"],
        )

    def test_secret_field_ids_and_existing_save_contract_are_preserved(self) -> None:
        card = self.fnos_card()
        for suffix, key in (
            ("Password", "password"),
            ("ApiKey", "api_key"),
            ("Secret", "secret"),
            ("Token", "token"),
        ):
            field_id = f"advFnos{suffix}"
            with self.subTest(field_id=field_id):
                self.assertEqual(self.template.count(f'id="{field_id}"'), 1)
                self.assertIn(f'id="{field_id}" type="password" autocomplete="new-password"', card)
                self.assertIn(f'{field_id}: "fnos.{key}"', self.admin_script)
                self.assertIn(f'setSecret("{field_id}", fnos.{key})', self.admin_script)
                if key == "password":
                    self.assertIn('password: $("advFnosPassword")?.value || ""', self.admin_script)
                else:
                    self.assertIn(f'{key}: value("{field_id}")', self.admin_script)

    def test_media_failure_keeps_backend_error_and_uses_three_field_fallback(self) -> None:
        self.assertIn(
            ': data.message || "飞牛媒体库加载失败，请检查飞牛地址、账号、密码和连接状态。"',
            self.media_script,
        )
        self.assertIn("<p>${escapeHtml(message)}</p>", self.media_script)
        self.assertIn("statusBox.textContent = error.message;", self.media_script)
        self.assertIn("${renderMediaDiagnosticHint(data)}", self.media_script)
        self.assertNotIn("FNOS_SERVER_URL / API Key / Secret", self.media_script)

    def test_media_asset_uses_login_release_cache_version(self) -> None:
        self.assertIn(
            '/static/admin-media.js?v=20261103-fnos-login-v014',
            self.template,
        )


if __name__ == "__main__":
    unittest.main()
