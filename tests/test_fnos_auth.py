from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from fnos_media_import.config_persistence import sanitize_advanced_config

from fnos_media_import.media.fnos import (
    API_LOGIN, API_LOGIN_V2, API_MDB_LIST, API_MEDIADB_LIST, API_MEDIADB_SUM,
    API_SEARCH_LIST, DEFAULT_WEB_API_KEY, DEFAULT_WEB_SIGNING_SECRET, FnosMediaRefresher,
    SIGN_MODE_FNV, SIGN_MODE_PHP,
)


class Response:
    def __init__(self, payload=None, *, status=200):
        self.status_code = status
        self.payload = payload if payload is not None else {"code": 0, "data": {}}
        self.text = json.dumps(self.payload, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)


def login_ok(token="test-token"):
    return Response({"code": 0, "data": {"token": token}})


class FnosAuthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="fnos-auth-")
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name, "token.json")
        self.config = {"server_url": "https://nas.invalid", "username": "user", "password": "password",
                       "token_cache_path": str(self.cache), "log_enabled": False}

    def client(self, **overrides):
        client = FnosMediaRefresher({**self.config, **overrides})
        self.addCleanup(client.session.close)
        client.session.request = Mock(side_effect=AssertionError("Unexpected network request"))
        return client

    def test_address_username_password_are_sufficient_to_login_and_list_libraries(self):
        client = self.client()
        client.session.request.side_effect = [login_ok(), Response({"code": 0, "data": [{"guid": "tv", "name": "电视剧"}]})]
        self.assertTrue(client.describe()["configured"])
        result = client.media_libraries(include_summary=False)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["items"][0]["guid"], "tv")
        login, listing = client.session.request.call_args_list
        self.assertEqual(login.args, ("POST", "https://nas.invalid" + API_LOGIN_V2))
        payload = json.loads(login.kwargs["data"])
        self.assertEqual(payload, {"username": "user", "password": hashlib.sha256(b"password").hexdigest(), "app_name": "trimemedia-web"})
        self.assertEqual(listing.kwargs["headers"]["Authorization"], "test-token")
        self.assertFalse(login.kwargs["allow_redirects"])
        self.assertNotIn("Authorization", login.kwargs["headers"])

    def test_builtin_signature_matches_public_web_client_protocol(self):
        client = self.client()
        client.session.request.return_value = login_ok()
        client.session.request.side_effect = None
        with patch("fnos_media_import.media.fnos._random_digits", return_value="123456"), patch("fnos_media_import.media.fnos.time.time", return_value=1700000000):
            self.assertTrue(client.login())
        call = client.session.request.call_args
        body_hash = hashlib.md5(call.kwargs["data"]).hexdigest()
        expected = hashlib.md5("_".join([
            DEFAULT_WEB_SIGNING_SECRET, API_LOGIN_V2, "123456", "1700000000000", body_hash, DEFAULT_WEB_API_KEY,
        ]).encode()).hexdigest()
        self.assertEqual(call.kwargs["headers"]["Authx"], f"nonce=123456&timestamp=1700000000000&sign={expected}")
        self.assertEqual(client._active_sign_mode, SIGN_MODE_FNV)

    def test_get_authx_hashes_raw_query_string_with_chinese_params(self):
        client = self.client()
        params = {"q": "中文 测试", "page": 1}
        with patch("fnos_media_import.media.fnos._random_digits", return_value="123456"), patch("fnos_media_import.media.fnos.time.time", return_value=1700000000):
            authx = client._authx(
                "GET",
                API_SEARCH_LIST,
                params=params,
                body_text="",
                sign_mode=SIGN_MODE_FNV,
            )

        raw_query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        body_hash = hashlib.md5(raw_query.encode("utf-8")).hexdigest()
        expected = hashlib.md5("_".join([
            DEFAULT_WEB_SIGNING_SECRET, API_SEARCH_LIST, "123456", "1700000000000",
            body_hash, DEFAULT_WEB_API_KEY,
        ]).encode()).hexdigest()
        self.assertEqual(raw_query, "page=1&q=中文 测试")
        self.assertEqual(authx, f"nonce=123456&timestamp=1700000000000&sign={expected}")

    def test_settings_save_preserves_password_whitespace_and_blank_keeps_saved_value(self):
        saved = sanitize_advanced_config({"fnos": {"server_url": "https://nas.invalid", "username": "user", "password": " password "}})
        self.assertEqual(saved["fnos"]["password"], " password ")
        preserved = sanitize_advanced_config({"fnos": {"password": ""}}, current=saved, preserve_secret_placeholders=True)
        self.assertEqual(preserved["fnos"]["password"], " password ")
        client = self.client(**preserved["fnos"])
        client.session.request.side_effect = [login_ok()]
        self.assertTrue(client.login())
        self.assertEqual(json.loads(client.session.request.call_args.kwargs["data"])["password"], hashlib.sha256(b" password ").hexdigest())

    def test_legacy_login_fallback_only_when_v2_endpoint_is_unsupported(self):
        for status in (404, 405):
            with self.subTest(status=status):
                client = self.client(password=" pass with spaces ")
                client.session.request.side_effect = [Response({"message": "Not found"}, status=status), login_ok()]
                self.assertTrue(client.login(force=True))
                self.assertEqual(client.session.request.call_count, 2)
                call = client.session.request.call_args
                self.assertTrue(call.args[1].endswith(API_LOGIN))
                self.assertEqual(json.loads(call.kwargs["data"])["password"], " pass with spaces ")

    def test_wrong_credentials_challenges_and_rate_limits_do_not_downgrade(self):
        for status in (200, 401, 403, 429):
            with self.subTest(status=status):
                client = self.client()
                client.session.request.side_effect = [Response({"code": -1, "msg": "账号或密码错误"}, status=status)]
                result = client.media_summary()
                self.assertFalse(result["success"])
                self.assertIn("账号或密码错误", result["message"])
                self.assertEqual(client.session.request.call_count, 1)
                self.assertFalse(self.cache.exists())

    def test_network_failure_does_not_send_cleartext_login_or_silently_retry(self):
        client = self.client()
        client.session.request.side_effect = requests.Timeout("connection timed out")
        result = client.media_summary()
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["message"])
        self.assertEqual(client.session.request.call_count, 1)

    def test_successful_compatibility_signature_is_used_for_following_requests(self):
        client = self.client(api_key="custom-key", secret="custom-secret")
        client.session.request.side_effect = [Response({"code": -1, "msg": "signature invalid"}), login_ok(), Response()]
        self.assertTrue(client.media_summary()["success"])
        self.assertEqual(client._active_sign_mode, SIGN_MODE_PHP)
        self.assertEqual(client.session.request.call_count, 3)
        self.assertEqual(client._sign_modes(), [SIGN_MODE_PHP])
        request = client.session.request.call_args
        self.assertEqual(request.args[1], "https://nas.invalid" + API_MEDIADB_SUM)
        restored = self.client(api_key="custom-key", secret="custom-secret")
        self.assertEqual(restored._sign_modes(), [SIGN_MODE_PHP])

    def test_saved_token_is_reused_only_for_same_address_account_and_signing_config(self):
        client = self.client()
        client.session.request.side_effect = [login_ok()]
        self.assertTrue(client.login())
        same = self.client()
        self.assertEqual(same.token, "test-token")
        same.session.request.side_effect = [Response({"code": 0, "data": []})]
        self.assertTrue(same.media_libraries(include_summary=False)["success"])
        self.assertEqual(same.session.request.call_args.args[1], "https://nas.invalid" + API_MEDIADB_LIST)
        for changes in ({"username": "other"}, {"server_url": "https://other.invalid"}, {"secret": "other"}, {"app_name": "other-app"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.client(**changes).token)

    def test_legacy_or_malformed_token_cache_cannot_reuse_an_unknown_account(self):
        for payload in (["invalid"], {"server_url": "https://nas.invalid", "token": "unknown-account"}):
            with self.subTest(payload=payload):
                self.cache.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(self.client().token)

    def test_expired_token_automatically_logs_in_again_with_saved_credentials(self):
        client = self.client(token="expired")
        client.session.request.side_effect = [Response({"code": -2, "msg": "token expired"}), login_ok("renewed"), Response()]
        self.assertTrue(client.media_summary()["success"])
        self.assertEqual(client.token, "renewed")
        self.assertEqual(client.session.request.call_count, 3)
        self.assertEqual(client.session.request.call_args.kwargs["headers"]["Authorization"], "renewed")

    def test_manual_token_and_signature_overrides_remain_supported(self):
        client = self.client(username="", password="", token="manual", api_key="key", secret="secret", sign_mode="php")
        client.session.request.side_effect = [Response()]
        self.assertTrue(client.describe()["configured"])
        self.assertTrue(client.media_summary()["success"])
        self.assertEqual(client.api_key, "key")
        self.assertEqual(client.secret, "secret")
        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.session.request.call_args.kwargs["headers"]["Authorization"], "manual")

    def test_video_web_suffix_is_not_duplicated(self):
        client = self.client(server_url="https://nas.invalid/v/")
        client.session.request.side_effect = [login_ok()]
        self.assertTrue(client.login())
        self.assertEqual(client.session.request.call_args.args[1], "https://nas.invalid" + API_LOGIN_V2)

    def test_direct_video_service_route_is_discovered_and_cached(self):
        client = self.client()
        missing = Response({"msg": "Not found"}, status=404)
        client.session.request.side_effect = [missing, missing, login_ok(), Response()]
        self.assertTrue(client.media_summary()["success"])
        self.assertEqual(client.session.request.call_args_list[2].args[1], "https://nas.invalid/api/v2/user/loginByPassword")
        self.assertEqual(client.session.request.call_args.args[1], "https://nas.invalid/api/v1/mediadb/sum")
        self.assertTrue(self.client()._direct_api)

    def test_failed_response_with_token_does_not_count_as_logged_in(self):
        client = self.client()
        client.session.request.side_effect = [Response({"code": -1, "data": {"token": "invalid"}})]
        self.assertFalse(client.login())
        self.assertFalse(client.token)
        self.assertFalse(self.cache.exists())

    def test_missing_account_fields_do_not_ask_for_protocol_keys(self):
        for changes in ({"server_url": ""}, {"username": ""}, {"password": ""}):
            with self.subTest(changes=changes):
                client = self.client(**changes)
                result = client.media_libraries()
                self.assertFalse(result["success"])
                self.assertIn("用户名和密码", result["message"])
                self.assertNotIn("API_KEY", result["message"])
                self.assertNotIn("SECRET", result["message"])
                client.session.request.assert_not_called()

    def test_login_improvement_does_not_allow_missing_directory_full_scan(self):
        client = self.client()
        client.session.request.side_effect = [login_ok(), Response({"code": 0, "data": [{"name": "电视剧", "guid": "tv", "dir_list": []}]})]
        result = client.refresh("电视剧")
        self.assertFalse(result["success"])
        self.assertIn("dir_list", result["message"])
        self.assertEqual(client.session.request.call_count, 2)
        self.assertTrue(client.session.request.call_args.args[1].endswith(API_MDB_LIST))

    def test_login_errors_do_not_echo_password_or_hash(self):
        client = self.client()
        client.session.request.side_effect = [Response({"code": -1, "msg": "bad password " + hashlib.sha256(b"password").hexdigest()})]
        result = client.media_summary()
        self.assertNotIn("password", result["message"])
        self.assertNotIn(hashlib.sha256(b"password").hexdigest(), result["message"])


if __name__ == "__main__":
    unittest.main()
