from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.public_web import (
    _adapter_placeholders,
    _public_adapter_capabilities,
)
from fnos_media_import.providers.active import (
    ActiveProviderImporter,
    ActiveProviderUnavailable,
)


class _ConfiguredQuarkImporter:
    def check_configuration(self):
        return SimpleNamespace(ok=True, message="夸克账号凭证已配置")


class QuarkCapabilityTests(unittest.TestCase):
    def test_active_quark_account_is_reported_as_configured(self) -> None:
        items = _adapter_placeholders(
            {},
            {},
            quark_importer=_ConfiguredQuarkImporter(),
        )

        quark = next(item for item in items if item["key"] == "quark")
        self.assertTrue(quark["configured"])
        self.assertEqual(quark["status"], "implemented")
        self.assertEqual(quark["message"], "夸克账号凭证已配置")

    def test_public_capability_uses_the_active_quark_account(self) -> None:
        items = _public_adapter_capabilities(
            {},
            quark_importer=_ConfiguredQuarkImporter(),
            generic_importers={},
        )

        quark = next(item for item in items if item["key"] == "quark")
        self.assertTrue(quark["configured"])
        self.assertTrue(quark["enabled"])
        self.assertEqual(quark["status"], "implemented")

    def test_public_config_fails_closed_when_sixpan_account_is_inactive(self) -> None:
        def unavailable():
            raise ActiveProviderUnavailable("当前六盘账号未激活")

        items = _public_adapter_capabilities(
            {},
            generic_importers={
                "sixpan": ActiveProviderImporter("六盘", unavailable),
            },
        )

        sixpan = next(item for item in items if item["key"] == "sixpan")
        self.assertFalse(sixpan["configured"])
        self.assertFalse(sixpan["enabled"])
        self.assertEqual(sixpan["status"], "unavailable")

    def test_public_config_fails_closed_when_mobile_account_is_inactive(self) -> None:
        """An unactivated 移动云 account must not break guest config.

        ``ActiveProviderImporter`` defines no ``__bool__`` and proxies every
        attribute through ``resolve()``, so a bare ``cloud139_importer.describe()``
        used to raise and turn ``GET /api/public/config`` into an HTTP 500 for
        every visitor, taking the whole homepage down with it.
        """

        def unavailable():
            raise ActiveProviderUnavailable(
                "当前 mobile 账号未配置或未激活"
            )

        proxy = ActiveProviderImporter("移动云", unavailable)

        placeholders = _adapter_placeholders({}, {}, cloud139_importer=proxy)
        cloud139 = next(item for item in placeholders if item["key"] == "cloud139")
        self.assertFalse(cloud139["configured"])
        self.assertFalse(cloud139["enabled"])
        self.assertEqual(cloud139["status"], "unconfigured")

        items = _public_adapter_capabilities({}, cloud139_importer=proxy)
        public_cloud139 = next(item for item in items if item["key"] == "cloud139")
        self.assertFalse(public_cloud139["configured"])
        self.assertFalse(public_cloud139["enabled"])
        self.assertEqual(public_cloud139["status"], "unavailable")

    def test_public_config_still_reports_a_configured_mobile_account(self) -> None:
        """Failing closed must not mask a genuinely activated account."""

        class _ConfiguredCloud139:
            def describe(self):
                return {
                    "configured": True,
                    "capabilities": {"submit": True, "task_poll": True},
                    "config": {},
                }

        items = _public_adapter_capabilities(
            {}, cloud139_importer=_ConfiguredCloud139()
        )
        cloud139 = next(item for item in items if item["key"] == "cloud139")
        self.assertTrue(cloud139["configured"])
        self.assertTrue(cloud139["enabled"])


class QuarkCapabilityCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "fnos_media_import" / "app.py"
        ).read_text(encoding="utf-8")
        cls.tree = ast.parse(source)

    def _function(self, name: str) -> ast.FunctionDef:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"未找到函数：{name}")

    @staticmethod
    def _call_keywords(function: ast.FunctionDef, callee: str) -> list[set[str]]:
        calls: list[set[str]] = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == callee:
                calls.append({keyword.arg for keyword in node.keywords if keyword.arg})
        return calls

    def test_admin_adapter_routes_forward_live_quark_importer(self) -> None:
        for function_name in ("api_admin_adapters", "api_admin_adapter_probe"):
            calls = self._call_keywords(
                self._function(function_name), "_adapter_placeholders"
            )
            self.assertTrue(calls, function_name)
            self.assertTrue(
                all("quark_importer" in keywords for keywords in calls),
                function_name,
            )

    def test_public_config_forwards_all_live_importers(self) -> None:
        calls = self._call_keywords(
            self._function("api_public_config"), "_public_adapter_capabilities"
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(
            {"quark_importer", "cloud139_importer", "generic_importers"}
            <= calls[0]
        )

    def test_quark_adapter_is_registered_as_formal_provider_adapter(self) -> None:
        calls = []
        for node in ast.walk(self._function("create_app")):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "QuarkProviderAdapter":
                continue
            calls.append(node)

        self.assertEqual(len(calls), 1)
        keywords = {item.arg: item.value for item in calls[0].keywords if item.arg}
        factory = keywords.get("account_importer_factory")
        self.assertIsInstance(factory, ast.Name)
        self.assertEqual(factory.id, "QuarkImporter")

    def test_inactive_provider_is_mapped_to_503_not_500(self) -> None:
        """An unactivated account is a configuration state, not a server fault.

        Pins that the global error handler classifies
        ``ActiveProviderUnavailable`` before the generic 500 fallback, so the
        actionable "which Provider needs activating" message reaches the caller.
        """

        handler = self._function("handle_error")
        checked: list[str] = []
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                if len(node.args) == 2 and isinstance(node.args[1], ast.Name):
                    checked.append(node.args[1].id)

        self.assertIn("ActiveProviderUnavailable", checked)

        # The branch must actually return 503 and the provider_unavailable code,
        # otherwise it falls through to the opaque internal-error tail.
        branch = next(
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.If)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "isinstance"
                and len(call.args) == 2
                and isinstance(call.args[1], ast.Name)
                and call.args[1].id == "ActiveProviderUnavailable"
                for call in ast.walk(node.test)
            )
        )
        constants = {
            node.value
            for node in ast.walk(branch)
            if isinstance(node, ast.Constant)
        }
        self.assertIn(503, constants)
        self.assertIn("provider_unavailable", constants)

    def test_quark_qrcode_uses_registry_account_tester(self) -> None:
        calls = [
            node
            for node in ast.walk(self._function("create_app"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "QuarkQrAuthHandler"
        ]

        self.assertEqual(len(calls), 1)
        tester = calls[0].args[0]
        self.assertIsInstance(tester, ast.Attribute)
        self.assertEqual(tester.attr, "account_tester")
        self.assertIsInstance(tester.value, ast.Name)
        self.assertEqual(tester.value.id, "v1_adapter_registry")


if __name__ == "__main__":
    unittest.main()
