from __future__ import annotations

import unittest
from unittest.mock import patch

from fnos_media_import.accounts.auth_sessions import SixpanDeviceCodeAuthHandler
from fnos_media_import.importers.sixpan import (
    DEFAULT_SIXPAN_VERIFICATION_URL,
    SixPanClient,
)


class SixPanApiContractTests(unittest.TestCase):
    @staticmethod
    def _client(**overrides: str) -> SixPanClient:
        return SixPanClient(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "oauth_client_id": "oauth-client-id",
                **overrides,
            }
        )

    def test_device_authorization_uses_protobuf_json_names_and_unwraps_response(self) -> None:
        client = self._client()
        with patch.object(
            client,
            "post",
            return_value={
                "data": {
                    "deviceCode": "device-secret",
                    "userCode": "ABCD-1234",
                    "expiresIn": 300,
                }
            },
        ) as post:
            result = client.request_device_code(device="fnos-admin")

        post.assert_called_once_with(
            "/v6/oauth/device_code",
            {"clientId": "oauth-client-id", "device": "fnos-admin"},
            allow_refresh=False,
        )
        self.assertEqual(result["device_code"], "device-secret")
        self.assertEqual(result["user_code"], "ABCD-1234")
        self.assertEqual(result["verification_uri"], DEFAULT_SIXPAN_VERIFICATION_URL)

    def test_device_poll_and_refresh_use_camel_case_and_extract_nested_tokens(self) -> None:
        updates: list[dict[str, object]] = []
        client = SixPanClient(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "oauth_client_id": "oauth-client-id",
                "refresh_token": "refresh-old",
            },
            token_update_callback=updates.append,
        )
        with patch.object(
            client,
            "post",
            side_effect=[
                {"result": {"login": True, "accessToken": "access-1", "refreshToken": "refresh-1"}},
                {"data": {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}},
            ],
        ) as post:
            polled = client.get_device_code_state(device_code="device-secret", user_code="ABCD")
            refreshed = client.refresh_access_token()

        self.assertTrue(polled["login"])
        self.assertEqual(polled["access_token"], "access-1")
        self.assertEqual(refreshed["access_token"], "access-2")
        self.assertEqual(
            post.call_args_list[0].args[1],
            {"deviceCode": "device-secret"},
        )
        self.assertEqual(
            post.call_args_list[1].args[1],
            {
                "refreshToken": "refresh-1",
                "grantType": "refresh_token",
                "clientId": "oauth-client-id",
            },
        )
        self.assertEqual(updates[-1]["access_token"], "access-2")

    def test_offline_operations_use_camel_case_request_fields(self) -> None:
        client = self._client(access_token="access-token")
        with patch.object(client, "post", return_value={}) as post:
            client.add_offline_task(
                source_url="magnet:?xt=urn:btih:example",
                title="Example",
                save_path="/All/",
                ignore_files=["sample.mkv"],
            )
            client.list_offline_tasks(limit=20, token="next-page")
            client.delete_offline_tasks(["task-1"], delete_files=True)

        self.assertEqual(
            post.call_args_list[0].args[1],
            {
                "url": "magnet:?xt=urn:btih:example",
                "name": "Example",
                "savePath": "/All/",
                "ignoreFiles": ["sample.mkv"],
            },
        )
        self.assertEqual(
            post.call_args_list[1].args[1],
            {"listInfo": {"limit": 20, "version": 0, "token": "next-page"}},
        )
        self.assertEqual(
            post.call_args_list[2].args[1],
            {"identity": ["task-1"], "deleteFiles": True},
        )

    def test_login_false_without_status_remains_pending(self) -> None:
        class Importer:
            @staticmethod
            def start_device_authorization(**_kwargs):
                return {
                    "deviceCode": "device-secret",
                    "userCode": "ABCD",
                    "verificationUri": "https://static.2dland.cn/user/landing/",
                }

            @staticmethod
            def check_device_authorization(**_kwargs):
                return {"login": False}

            @staticmethod
            def extract_tokens(_result):
                return {}

        handler = SixpanDeviceCodeAuthHandler(
            lambda: {"client_id": "client-id", "client_secret": "client-secret"},
            lambda _config: Importer(),
        )
        waiting = handler.start({})
        polled = handler.poll(waiting.private_state, {})

        self.assertEqual(polled.status, "waiting")
        self.assertIn("等待", polled.message)

    def test_provider_error_is_terminal_even_when_login_is_false(self) -> None:
        class Importer:
            @staticmethod
            def start_device_authorization(**_kwargs):
                return {
                    "deviceCode": "device-secret",
                    "verificationUri": "https://static.2dland.cn/user/landing/",
                }

            @staticmethod
            def check_device_authorization(**_kwargs):
                return {"login": False, "error": "access_denied"}

            @staticmethod
            def extract_tokens(_result):
                return {}

        handler = SixpanDeviceCodeAuthHandler(
            lambda: {"client_id": "client-id", "client_secret": "client-secret"},
            lambda _config: Importer(),
        )
        waiting = handler.start({})
        polled = handler.poll(waiting.private_state, {})

        self.assertEqual(polled.status, "failed")
        self.assertEqual(polled.message, "access_denied")


if __name__ == "__main__":
    unittest.main()
