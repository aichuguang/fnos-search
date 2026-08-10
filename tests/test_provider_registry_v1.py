from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest

from fnos_media_import.accounts.auth_sessions import (
    ManagedAccountAuthHandler,
    SixpanDeviceCodeAuthHandler,
)
from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.domain.models import AccountTestResult, ProviderCapabilities
from fnos_media_import.planning.source_resolver import SourceResolver
from fnos_media_import.providers.base import manifest
from fnos_media_import.providers.registry import ProviderRegistry, ProviderRegistryError
from fnos_media_import.storage.assignment_service import SourceAssignmentService


class _Adapter(RepositoryFakeMixin):
    def __init__(self, provider_manifest):
        self.manifest = provider_manifest
        self.kind = provider_manifest.provider_kind

    def capabilities(self, account=None):
        del account
        return self.manifest.capabilities

    def validate_account_config(self, config, credentials):
        del config, credentials
        return []

    def test_account(self, account):
        del account
        return AccountTestResult(True, "user", "ok")

    def inspect_source(self, account, locator):
        del account, locator
        return {}

    def can_native_transfer(self, source, destination, account):
        del source, destination, account
        return False


class ProviderRegistryV1Tests(unittest.TestCase):
    def test_mobile_browser_authorization_stays_in_account_auth_flow(self) -> None:
        step = ManagedAccountAuthHandler("mobile", lambda: {}).start(
            {
                "name": "移动云主账号",
                "phone": "13800000000",
                "authorization": "Basic secret-value",
            }
        )

        self.assertEqual(step.status, "completed")
        self.assertEqual(step.account_data["config"]["phone"], "13800000000")
        self.assertEqual(
            step.account_data["credentials"]["authorization"],
            "Basic secret-value",
        )

    def test_sixpan_device_credentials_are_frozen_in_auth_session_state(self) -> None:
        configs = []

        class _Sixpan:
            def __init__(self, config):
                configs.append(dict(config))

            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "device-secret",
                    "user_code": "ABCD",
                    "verification_uri": "https://sixpan.example/authorize",
                    "expires_in": 600,
                }

            def check_device_authorization(self, **_kwargs):
                return {"access_token": "access-secret", "refresh_token": "refresh-secret"}

            @staticmethod
            def extract_tokens(value):
                return dict(value)

        handler = SixpanDeviceCodeAuthHandler(lambda: {}, _Sixpan)
        waiting = handler.start(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        )
        completed = handler.poll(waiting.private_state, {})

        self.assertEqual(waiting.private_state["auth_config"]["client_secret"], "client-secret")
        self.assertEqual(configs[1]["client_secret"], "client-secret")
        self.assertEqual(completed.account_data["credentials"]["access_token"], "access-secret")

    def test_external_provider_manifest_keeps_all_form_schemas(self) -> None:
        provider_manifest = manifest(
            "115",
            "115 网盘",
            [{"name": "cookie", "label": "Cookie", "secret": True, "required": True}],
            components={"search"},
            capabilities=ProviderCapabilities(supports_search=True),
            locator_fields=[{"name": "folder_id", "label": "目录 ID", "required": True}],
            runtime_fields=[{"name": "app_id", "label": "App ID", "required": True}],
            auth_methods=[
                {
                    "kind": "qrcode",
                    "label": "扫码登录",
                    "input_schema": {"fields": []},
                }
            ],
            search_sources=[
                {
                    "search_source_id": "115",
                    "label": "115 网盘",
                    "source_form": "share",
                }
            ],
        )
        registry = ProviderRegistry([_Adapter(provider_manifest)])

        payload = registry.manifests()[0]
        self.assertEqual(payload["provider_kind"], "115")
        self.assertEqual(payload["account_schema"]["fields"][0]["name"], "cookie")
        self.assertEqual(payload["native_locator_schema"]["fields"][0]["name"], "folder_id")
        self.assertEqual(payload["runtime_schema"]["fields"][0]["name"], "app_id")
        self.assertEqual(registry.search_sources()[0]["provider_kind"], "115")

    def test_builtin_manifests_expose_folder_browser_metadata(self) -> None:
        from fnos_media_import.providers.manifests import BUILTIN_MANIFESTS

        for kind, expected_root in (("mobile", "/"), ("quark", "0"), ("tianyi", "-11")):
            manifest_item = BUILTIN_MANIFESTS[kind]
            locator = manifest_item.native_locator_schema["fields"][0]
            self.assertTrue(locator.get("browseable"))
            self.assertEqual(locator.get("browse_root"), expected_root)

    def test_registry_is_the_account_probe_and_source_catalog_truth(self) -> None:
        provider_manifest = manifest(
            "115",
            "115 网盘",
            [{"name": "cookie", "label": "Cookie", "secret": True, "required": True}],
            components={"search"},
            capabilities=ProviderCapabilities(supports_search=True),
            search_sources=[
                {
                    "search_source_id": "115",
                    "source_form": "share",
                    "source_types": ["115", "115_share"],
                }
            ],
        )
        registry = ProviderRegistry([_Adapter(provider_manifest)])

        result = registry.account_tester("115", {"cookie": "secret"}, {})
        self.assertTrue(result.ok)
        self.assertEqual(registry.kinds(), ("115",))
        self.assertTrue(registry.has("115"))
        self.assertEqual(registry.search_sources()[0]["source_types"], ["115", "115_share"])

        accounts = ProviderAccountService(
            object(),
            lambda: b"x" * 32,
            catalog_provider=registry.manifests,
        )
        assignments = SourceAssignmentService(
            object(),
            object(),
            source_catalog_provider=registry.search_sources,
        )
        resolver = SourceResolver(
            lambda _url: {
                "provider_kind": "115",
                "source_form": "share",
                "search_source_id": "115",
            },
            source_catalog_provider=registry.search_sources,
        )
        descriptor = resolver.resolve({"url": "https://115.com/s/example"})

        self.assertEqual(accounts.catalog()[0]["provider_kind"], "115")
        self.assertEqual(assignments.sources()["115"]["provider_kind"], "115")
        self.assertEqual(descriptor.provider_kind, "115")
        self.assertEqual(descriptor.search_source_id, "115")

    def test_adapter_kind_must_match_manifest(self) -> None:
        provider_manifest = manifest(
            "115",
            "115 网盘",
            [{"name": "cookie", "label": "Cookie", "secret": True, "required": True}],
            components=set(),
            capabilities=ProviderCapabilities(),
        )
        adapter = _Adapter(provider_manifest)
        adapter.kind = "quark"
        with self.assertRaisesRegex(ProviderRegistryError, "不一致"):
            ProviderRegistry([adapter])

    def test_duplicate_source_type_is_rejected(self) -> None:
        first = manifest(
            "first",
            "First",
            [{"name": "cookie", "label": "Cookie", "secret": True, "required": True}],
            components={"search"},
            capabilities=ProviderCapabilities(supports_search=True),
            search_sources=[{"search_source_id": "first", "source_types": ["shared_type"]}],
        )
        second = manifest(
            "second",
            "Second",
            [{"name": "cookie", "label": "Cookie", "secret": True, "required": True}],
            components={"search"},
            capabilities=ProviderCapabilities(supports_search=True),
            search_sources=[{"search_source_id": "second", "source_types": ["shared_type"]}],
        )
        registry = ProviderRegistry([_Adapter(first), _Adapter(second)])
        with self.assertRaisesRegex(ProviderRegistryError, "来源类型重复注册"):
            registry.search_sources()

    def test_duplicate_manifest_field_is_rejected(self) -> None:
        provider_manifest = manifest(
            "broken",
            "Broken",
            [
                {"name": "cookie", "label": "Cookie", "required": True},
                {"name": "cookie", "label": "Cookie 2", "required": True},
            ],
            components=set(),
            capabilities=ProviderCapabilities(),
        )
        with self.assertRaisesRegex(ProviderRegistryError, "字段重复"):
            ProviderRegistry([_Adapter(provider_manifest)])


if __name__ == "__main__":
    unittest.main()
