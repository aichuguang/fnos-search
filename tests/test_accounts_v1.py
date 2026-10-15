from __future__ import annotations

import json
import shutil
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from flask import Flask

from fnos_media_import.accounts.blueprint import accounts_blueprint_for_service
from fnos_media_import.accounts.auth_sessions import (
    AuthStep,
    ManagedAccountAuthHandler,
    ProviderAuthSessionService,
    QuarkQrAuthHandler,
    SixpanDeviceCodeAuthHandler,
    TianyiPasswordAuthHandler,
)
from fnos_media_import.accounts.crypto import (
    PURPOSE_APP_SESSION,
    PURPOSE_AUTH_SESSION,
    PURPOSE_PROVIDER_ACCOUNT,
    PURPOSE_SOURCE_RECORD,
    DecryptionError,
    MasterKeyMissingError,
    UnknownKeyIdError,
    decrypt,
    derive_key,
    encrypt,
    master_key_from_env,
)
from fnos_media_import.accounts.service import InvalidConfigError, ProviderAccountService
from fnos_media_import.accounts.repository import AccountConflictError
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.domain.models import AccountTestResult


KEY = bytes.fromhex("aa" * 32)
OTHER_KEY = bytes.fromhex("bb" * 32)
WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "accounts-v1-tests"


class AccountsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.db_path = WORK_TEST_ROOT / f"accounts-{uuid.uuid4().hex}.db"
        self._db_infrastructure = Database(self.db_path)
        self._db_infrastructure.init_schema()
        self.db = RepositorySet.from_database(self._db_infrastructure)
        self.service = ProviderAccountService(self.db, lambda: KEY)

    def tearDown(self) -> None:
        try:
            self.db.close_all_connections()
        except AttributeError:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.db_path}{suffix}").unlink()
            except FileNotFoundError:
                pass
        try:
            if WORK_TEST_ROOT.exists() and not any(WORK_TEST_ROOT.iterdir()):
                shutil.rmtree(WORK_TEST_ROOT)
        except OSError:
            pass

    def _quark(self, name: str = "夸克主账号"):
        return self.service.create_account(
            provider_kind="quark",
            name=name,
            config={"device_id": "device-a"},
            credentials={"cookie": "secret-cookie"},
        )

    def test_crypto_round_trip_and_wrong_key_fails(self) -> None:
        ciphertext, key_id = encrypt(KEY, "secret")
        self.assertEqual(key_id, "v2:provider-account")
        self.assertEqual(decrypt(KEY, key_id, ciphertext), b"secret")
        with self.assertRaises(DecryptionError):
            decrypt(OTHER_KEY, key_id, ciphertext)

    def test_master_key_missing_fails_closed(self) -> None:
        with self.assertRaises(MasterKeyMissingError):
            master_key_from_env({})

    def test_root_key_derives_isolated_purpose_keys(self) -> None:
        purposes = (
            PURPOSE_APP_SESSION,
            PURPOSE_PROVIDER_ACCOUNT,
            PURPOSE_AUTH_SESSION,
            PURPOSE_SOURCE_RECORD,
        )
        derived = {derive_key(KEY, purpose) for purpose in purposes}
        self.assertEqual(len(derived), len(purposes))
        ciphertext, key_id = encrypt(KEY, "secret", purpose=PURPOSE_AUTH_SESSION)
        with self.assertRaisesRegex(DecryptionError, "用途不匹配"):
            decrypt(KEY, key_id, ciphertext, purpose=PURPOSE_PROVIDER_ACCOUNT)

    def test_legacy_root_key_name_is_rejected(self) -> None:
        with self.assertRaises(MasterKeyMissingError):
            master_key_from_env({"FNOS_ACCOUNT_MASTER_KEY": KEY.hex()})
        self.assertEqual(
            master_key_from_env(
                {"FNOS_ROOT_KEY": OTHER_KEY.hex(), "FNOS_ACCOUNT_MASTER_KEY": KEY.hex()}
            ),
            OTHER_KEY,
        )

    def test_legacy_ciphertext_key_ids_are_rejected(self) -> None:
        with self.assertRaises(UnknownKeyIdError):
            decrypt(KEY, "v1", b"ignored")
        with self.assertRaises(UnknownKeyIdError):
            decrypt(KEY, "ol:v1", b"ignored")

    def test_account_revision_is_immutable_and_secret_is_not_serialized(self) -> None:
        account = self._quark()
        updated = self.service.revise_credentials(
            account.account_id,
            config={"device_id": "device-b"},
            credentials={},
        )
        old = self.service.resolve_account_context(account.account_id, 1)
        current = self.service.resolve_account_context(account.account_id, updated.current_revision)
        self.assertEqual(old["config"]["device_id"], "device-a")
        self.assertEqual(current["config"]["device_id"], "device-b")
        self.assertEqual(current["credentials"]["cookie"], "secret-cookie")
        payload = json.dumps(self.service.serialize_account(updated), ensure_ascii=False)
        self.assertNotIn("secret-cookie", payload)
        self.assertNotIn("ciphertext", payload)

    def test_account_test_requires_real_tester(self) -> None:
        account = self._quark()
        with self.assertRaises(InvalidConfigError):
            self.service.test_account(account.account_id)
        result = self.service.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "user***1", "检测通过"),
        )
        self.assertTrue(result.ok)

    def test_account_test_can_probe_without_persisting_health(self) -> None:
        account = self._quark()
        before = self.service.get_account(account.account_id)

        result = self.service.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "user***1", "ok"),
            persist_result=False,
        )

        after = self.service.get_account(account.account_id)
        self.assertTrue(result.ok)
        self.assertEqual(after.last_test_status, before.last_test_status)
        self.assertEqual(after.last_test_message, before.last_test_message)
        self.assertEqual(after.last_tested_at, before.last_tested_at)
        self.assertEqual(after.identity_hint, before.identity_hint)

    def test_tianyi_account_test_receives_account_scoped_cache_namespace(self) -> None:
        account = self.service.create_account(
            provider_kind="tianyi",
            name="Cloud189",
            config={"username": "same-user"},
            credentials={"password": "same-password"},
        )
        seen: list[str] = []

        self.service.test_account(
            account.account_id,
            tester=lambda _kind, _credentials, config: (
                seen.append(str(config.get("_cache_namespace") or ""))
                or AccountTestResult(True, "user***1", "ok")
            ),
            persist_result=False,
        )

        self.assertEqual(seen, [f"account:{account.account_id}"])

    def test_account_test_discards_result_when_credentials_change_mid_probe(self) -> None:
        account = self._quark()

        def tester(*_args):
            self.service.revise_credentials(
                account.account_id,
                config={},
                credentials={"cookie": "new-cookie"},
            )
            return AccountTestResult(True, "user-1", "旧凭证检测通过")

        with self.assertRaises(AccountConflictError):
            self.service.test_account(account.account_id, tester=tester)
        current = self.service.get_account(account.account_id)
        self.assertEqual(current.current_revision, 2)
        self.assertEqual(current.last_test_status, "untested")
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 2)["cookie"],
            "new-cookie",
        )

    def test_activation_freezes_account_revision(self) -> None:
        first = self._quark("A")
        second = self._quark("B")
        for account in (first, second):
            self.service.test_account(
                account.account_id,
                tester=lambda *_args: AccountTestResult(True, "user", "ok"),
            )
        selected_a = self.service.activate_account("default", first.account_id, None)
        selected_b = self.service.activate_account(
            "default", second.account_id, selected_a.selection_revision
        )
        self.assertEqual(selected_b.account_id, second.account_id)
        self.assertTrue(self.service.has_revision(first.account_id, selected_a.account_revision))

    def test_provider_token_refresh_advances_active_selection(self) -> None:
        account = self._quark()
        self.service.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "user", "ok"),
        )
        selected = self.service.activate_account("default", account.account_id, None)
        updated = self.service.refresh_runtime_credentials(
            account.account_id,
            {"cookie": "refreshed-cookie"},
            expected_revision=selected.account_revision,
        )
        current = self.service.get_active_account("quark")
        self.assertGreater(updated.current_revision, selected.account_revision)
        self.assertEqual(current.account_revision, updated.current_revision)
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, updated.current_revision)["cookie"],
            "refreshed-cookie",
        )

        unchanged = self.service.refresh_runtime_credentials(
            account.account_id,
            {"cookie": "refreshed-cookie"},
        )
        self.assertEqual(unchanged.current_revision, updated.current_revision)

    def test_stale_runtime_refresh_cannot_overwrite_newer_credentials(self) -> None:
        account = self._quark()
        old_revision = account.current_revision
        current = self.service.revise_credentials(
            account.account_id,
            config={},
            credentials={"cookie": "administrator-cookie"},
        )

        with self.assertRaises(AccountConflictError):
            self.service.refresh_runtime_credentials(
                account.account_id,
                {"cookie": "stale-worker-cookie"},
                expected_revision=old_revision,
            )

        self.assertEqual(
            self.service.resolve_credentials(account.account_id, current.current_revision)["cookie"],
            "administrator-cookie",
        )

    def test_one_runtime_can_advance_its_expected_revision_repeatedly(self) -> None:
        account = self._quark()
        first = self.service.refresh_runtime_credentials(
            account.account_id,
            {"cookie": "runtime-cookie-1"},
            expected_revision=account.current_revision,
        )
        second = self.service.refresh_runtime_credentials(
            account.account_id,
            {"cookie": "runtime-cookie-2"},
            expected_revision=first.current_revision,
        )
        self.assertEqual(second.current_revision, first.current_revision + 1)
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, second.current_revision)["cookie"],
            "runtime-cookie-2",
        )

    def test_manifest_driven_api_and_auth(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(
            accounts_blueprint_for_service(self.service, lambda view: view)
        )
        client = app.test_client()
        providers = client.get("/api/admin/providers").get_json()["providers"]
        kinds = {item["provider_kind"] for item in providers}
        self.assertEqual(kinds, {"mobile", "quark", "tianyi", "sixpan"})
        quark = next(item for item in providers if item["provider_kind"] == "quark")
        self.assertEqual(quark["account_schema"]["fields"][0]["name"], "cookie")
        self.assertEqual(quark["native_locator_schema"]["fields"][0]["name"], "path")
        self.assertEqual([item["kind"] for item in quark["auth_methods"]], ["qrcode"])
        tianyi = next(item for item in providers if item["provider_kind"] == "tianyi")
        self.assertEqual([item["kind"] for item in tianyi["auth_methods"]], ["password"])
        self.assertEqual(
            [item["name"] for item in tianyi["auth_methods"][0]["input_schema"]["fields"]],
            ["username", "password"],
        )
        sixpan = next(item for item in providers if item["provider_kind"] == "sixpan")
        sixpan_fields = {item["name"] for item in sixpan["account_schema"]["fields"]}
        self.assertIn("oauth_client_id", sixpan_fields)
        self.assertNotIn("access_token", sixpan_fields)
        self.assertNotIn("refresh_token", sixpan_fields)
        self.assertNotIn("fnos_mount_name", sixpan_fields)
        self.assertIsNone(sixpan["runtime_schema"])
        auth_fields = {
            item["name"]
            for item in sixpan["auth_methods"][0]["input_schema"]["fields"]
        }
        self.assertEqual(auth_fields, {"host", "client_id", "client_secret", "oauth_client_id"})
        self.assertIn("access_token", sixpan["secret_fields"])
        self.assertIn("refresh_token", sixpan["secret_fields"])

        mobile = next(item for item in providers if item["provider_kind"] == "mobile")
        self.assertIsNone(mobile["runtime_schema"])
        mobile_fields = {item["name"] for item in mobile["account_schema"]["fields"]}
        self.assertEqual(mobile_fields, {"phone", "host", "authorization"})

        response = client.post(
            "/api/admin/provider-accounts",
            json={
                "provider_kind": "quark",
                "name": "API 账号",
                "credentials": {"cookie": "api-secret"},
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("api-secret", response.get_data(as_text=True))

        protected = Flask(__name__ + "_protected")
        protected.register_blueprint(
            accounts_blueprint_for_service(
                self.service, lambda _view: (lambda **_kwargs: ({"success": False}, 401))
            )
        )
        self.assertEqual(
            protected.test_client().get("/api/admin/providers").status_code,
            401,
        )

    def test_auth_managed_account_update_is_name_only_and_atomic(self) -> None:
        account = self._quark()
        app = Flask(__name__ + "_rename")
        app.register_blueprint(accounts_blueprint_for_service(self.service, lambda view: view))
        client = app.test_client()

        renamed = client.patch(
            f"/api/admin/provider-accounts/{account.account_id}",
            json={"name": "家庭账号"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(self.service.get_account(account.account_id).name, "家庭账号")
        self.assertEqual(self.service.get_account(account.account_id).current_revision, 1)

        rejected = client.patch(
            f"/api/admin/provider-accounts/{account.account_id}",
            json={"name": "不应生效", "config": {"device_id": "forged"}},
        )
        self.assertEqual(rejected.status_code, 400)
        current = self.service.get_account(account.account_id)
        self.assertEqual(current.name, "家庭账号")
        self.assertEqual(current.current_revision, 1)
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 1)["cookie"],
            "secret-cookie",
        )

    def test_account_name_validation_is_enforced_by_service(self) -> None:
        account = self._quark()
        with self.assertRaisesRegex(InvalidConfigError, "不能为空"):
            self.service.update_account_name(account.account_id, "   ")
        with self.assertRaisesRegex(InvalidConfigError, "80"):
            self.service.update_account_name(account.account_id, "a" * 81)

    def test_managed_login_session_creates_tests_and_activates_account(self) -> None:
        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda *_args: AccountTestResult(True, "138****0000", "检测通过"),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {
                ("mobile", "managed_account"): ManagedAccountAuthHandler(
                    "mobile",
                    lambda: {
                        "name": "移动云主账号",
                        "config": {"phone": "13800000000"},
                        "credentials": {"authorization": "Basic hidden-secret"},
                    },
                )
            },
        )
        app = Flask(__name__ + "_auth")
        app.register_blueprint(accounts_blueprint_for_service(accounts, lambda view: view, sessions))
        response = app.test_client().post(
            "/api/admin/provider-auth-sessions",
            json={"provider_kind": "mobile", "auth_method": "managed_account"},
        )
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn("hidden-secret", text)
        session = response.get_json()["session"]
        self.assertEqual(session["status"], "completed")
        selection = accounts.get_active_account("mobile")
        self.assertEqual(selection.account_id, session["account_id"])
        relogin = app.test_client().post(
            "/api/admin/provider-auth-sessions",
            json={
                "provider_kind": "mobile",
                "auth_method": "managed_account",
                "payload": {"account_id": session["account_id"], "name": "移动云主账号"},
            },
        ).get_json()["session"]
        self.assertEqual(relogin["account_id"], session["account_id"])
        self.assertEqual(len(accounts.list_accounts("mobile")), 1)
        self.assertEqual(accounts.get_account(session["account_id"]).current_revision, 2)

    def test_failed_relogin_keeps_previous_revision_health_and_selection(self) -> None:
        def tester(_kind, credentials, _config):
            ok = credentials.get("authorization") == "Basic good"
            return AccountTestResult(ok, "138****0000", "检测通过" if ok else "凭证失效")

        accounts = ProviderAccountService(self.db, lambda: KEY, tester=tester)
        context = {
            "name": "移动云主账号",
            "config": {"phone": "13800000000"},
            "credentials": {"authorization": "Basic good"},
        }
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("mobile", "managed_account"): ManagedAccountAuthHandler("mobile", lambda: context)},
        )
        first = sessions.start("mobile", "managed_account")
        account_id = first["account_id"]
        selection = accounts.get_active_account("mobile")
        context.update(
            {
                "name": "不应保存的新名称",
                "credentials": {"authorization": "Basic bad"},
            }
        )

        with self.assertRaisesRegex(ValueError, "凭证失效"):
            sessions.start(
                "mobile",
                "managed_account",
                {"account_id": account_id, "name": context["name"]},
            )

        account = accounts.get_account(account_id)
        current = accounts.get_active_account("mobile")
        self.assertEqual(account.current_revision, 1)
        self.assertEqual(account.last_test_status, "ok")
        self.assertEqual(account.name, "移动云主账号")
        self.assertEqual(current.account_revision, selection.account_revision)
        self.assertEqual(
            accounts.resolve_credentials(account_id, 1)["authorization"],
            "Basic good",
        )

    def test_quark_qrcode_session_extracts_and_verifies_cookie(self) -> None:
        import requests

        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.cookies = requests.cookies.RequestsCookieJar()
                self.headers = {}

            def get(self, url, **kwargs):
                del kwargs
                if "getTokenForQrcodeLogin" in url:
                    return Response({"status": 2000000, "data": {"members": {"token": "qr-token"}}})
                if "getServiceTicketByQrcodeToken" in url:
                    self.cookies.set("__pus", "cookie-secret", domain="pan.quark.cn")
                    return Response({"status": 2000000, "data": {"members": {"service_ticket": "ticket"}}})
                if "drive-pc.quark.cn/1/clouddrive/file/sort" in url:
                    self.cookies.set("__puus", "refresh-secret", domain=".quark.cn")
                    return Response({"status": 200})
                # account/info is a cookie exchange endpoint. A successful
                # response does not consistently include a JSON status field.
                return Response({"data": {"uid": "user-1"}})

        session = Session()
        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda kind, credentials, config: AccountTestResult(
                kind == "quark" and "__pus=cookie-secret" in credentials.get("cookie", ""),
                "user-1",
                "ok",
            ),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("quark", "qrcode"): QuarkQrAuthHandler(
                lambda kind, credentials, config: AccountTestResult(
                    kind == "quark" and "__pus=cookie-secret" in credentials.get("cookie", ""),
                    "user-1",
                    "ok",
                ),
                session_factory=lambda: session,
            )},
        )
        waiting = sessions.start("quark", "qrcode", {"name": "夸克扫码账号"})
        self.assertEqual(waiting["status"], "waiting")
        self.assertTrue(waiting["public"]["qr_image"].startswith("data:image/svg+xml;base64,"))
        self.assertNotIn("qr-token", waiting["message"])
        completed = sessions.poll(waiting["session_id"])
        self.assertEqual(completed["status"], "completed")
        account_id = completed["account_id"]
        cookie = accounts.resolve_credentials(account_id, 1)["cookie"]
        self.assertIn("__pus=cookie-secret", cookie)
        self.assertIn("__puus=refresh-secret", cookie)
        self.assertEqual(accounts.get_active_account("quark").account_id, account_id)

    def test_auth_session_validation_error_is_not_reported_as_conflict(self) -> None:
        accounts = ProviderAccountService(self.db, lambda: KEY)
        sessions = ProviderAuthSessionService(self.db, lambda: KEY, accounts, {})
        app = Flask(__name__ + "_auth_error")
        app.register_blueprint(accounts_blueprint_for_service(accounts, lambda view: view, sessions))
        response = app.test_client().post(
            "/api/admin/provider-auth-sessions",
            json={"provider_kind": "tianyi", "auth_method": "missing"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "AUTH_METHOD_UNAVAILABLE")

    def test_auth_session_request_validation_returns_structured_400(self) -> None:
        accounts = ProviderAccountService(self.db, lambda: KEY)
        sessions = ProviderAuthSessionService(self.db, lambda: KEY, accounts, {})
        app = Flask(__name__ + "_auth_request_validation")
        app.register_blueprint(accounts_blueprint_for_service(accounts, lambda view: view, sessions))
        client = app.test_client()

        malformed = client.post(
            "/api/admin/provider-auth-sessions",
            data="{",
            content_type="application/json",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.get_json()["code"], "INVALID_JSON")
        self.assertEqual(malformed.get_json()["stage"], "request")

        missing = client.post("/api/admin/provider-auth-sessions", json={})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["code"], "AUTH_INPUT_REQUIRED")
        self.assertEqual(
            set(missing.get_json()["field_errors"]),
            {"provider_kind", "auth_method"},
        )

        invalid_payload = client.post(
            "/api/admin/provider-auth-sessions",
            json={
                "provider_kind": "tianyi",
                "auth_method": "password",
                "payload": [],
            },
        )
        self.assertEqual(invalid_payload.status_code, 400)
        self.assertEqual(invalid_payload.get_json()["code"], "AUTH_INPUT_INVALID")
        self.assertEqual(invalid_payload.get_json()["field_errors"], {"payload": "必须是 JSON 对象"})

    def test_tianyi_password_session_handles_captcha_and_encrypts_tokens(self) -> None:
        factory_configs: list[dict[str, object]] = []

        class FakeTianyiImporter:
            def begin_password_login(self, username, password):
                self.assertions = (username, password)
                return {
                    "status": "captcha_required",
                    "captcha_image": "data:image/png;base64,Y2FwdGNoYQ==",
                    "login_state": {"rsa_password": "private-encrypted-password"},
                }

            def complete_password_login(self, login_state, validate_code):
                if login_state.get("rsa_password") != "private-encrypted-password" or validate_code != "A1B2":
                    raise ValueError("验证码错误")
                return {
                    "status": "completed",
                    "identity_hint": "189****0000",
                    "credentials": {
                        "session_key": "session-secret",
                        "session_secret": "session-signing-secret",
                        "access_token": "access-secret",
                        "refresh_token": "refresh-secret",
                        "access_token_expires_at": 4102444800,
                    },
                }

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda kind, credentials, config: AccountTestResult(
                kind == "tianyi"
                and config.get("username") == "18900000000"
                and credentials.get("access_token") == "access-secret",
                "189****0000",
                "检测通过",
            ),
        )
        def factory(config):
            factory_configs.append(dict(config or {}))
            return FakeTianyiImporter()

        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("tianyi", "password"): TianyiPasswordAuthHandler(factory)},
        )
        waiting = sessions.start(
            "tianyi",
            "password",
            {"name": "天翼云主账号", "username": "18900000000", "password": "login-secret"},
        )
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["public"]["challenge"], "captcha")
        public_json = json.dumps(waiting, ensure_ascii=False)
        self.assertNotIn("login-secret", public_json)
        self.assertNotIn("private-encrypted-password", public_json)
        self.assertEqual(factory_configs[0]["username"], "18900000000")
        self.assertEqual(factory_configs[0]["password"], "login-secret")

        completed = sessions.poll(waiting["session_id"], {"validate_code": "A1B2"})
        self.assertEqual(completed["status"], "completed")
        account_id = completed["account_id"]
        credentials = accounts.resolve_credentials(account_id, 1)
        self.assertEqual(credentials["password"], "login-secret")
        self.assertEqual(credentials["session_key"], "session-secret")
        self.assertEqual(credentials["session_secret"], "session-signing-secret")
        self.assertEqual(credentials["refresh_token"], "refresh-secret")
        self.assertEqual(accounts.get_active_account("tianyi").account_id, account_id)
        self.assertEqual(factory_configs[1]["username"], "18900000000")
        self.assertEqual(factory_configs[1]["password"], "login-secret")

    def test_tianyi_captcha_refresh_and_error_retry_remain_waiting(self) -> None:
        class FakeTianyiImporter:
            def begin_password_login(self, _username, _password):
                return {
                    "status": "captcha_required",
                    "captcha_image": "data:image/png;base64,aW1hZ2UtMQ==",
                    "login_state": {"challenge": "one"},
                }

            def refresh_password_captcha(self, login_state):
                self.assert_state = dict(login_state)
                return {
                    "status": "captcha_required",
                    "captcha_image": "data:image/png;base64,aW1hZ2UtMg==",
                    "login_state": {"challenge": "two"},
                }

            def complete_password_login(self, login_state, validate_code):
                if validate_code == "BAD":
                    return {
                        "status": "captcha_required",
                        "captcha_image": "data:image/png;base64,aW1hZ2UtMw==",
                        "login_state": {"challenge": "three"},
                        "message": "验证码错误，请重试",
                        "error": "验证码错误",
                    }
                self.assert_state = dict(login_state)
                return {
                    "status": "completed",
                    "identity_hint": "189****0000",
                    "credentials": {
                        "session_key": "session-1",
                        "session_secret": "secret-1",
                    },
                }

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda kind, credentials, _config: AccountTestResult(
                kind == "tianyi"
                and credentials.get("session_key") == "session-1"
                and credentials.get("session_secret") == "secret-1",
                "189****0000",
                "检测通过",
            ),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {
                ("tianyi", "password"): TianyiPasswordAuthHandler(
                    lambda _config: FakeTianyiImporter()
                )
            },
        )
        waiting = sessions.start(
            "tianyi",
            "password",
            {"username": "18900000000", "password": "login-secret"},
        )

        unchanged = sessions.poll(waiting["session_id"], {})
        self.assertEqual(unchanged["status"], "waiting")
        self.assertEqual(
            unchanged["public"]["captcha_image"],
            "data:image/png;base64,aW1hZ2UtMQ==",
        )

        refreshed = sessions.poll(waiting["session_id"], {"refresh_captcha": True})
        self.assertEqual(refreshed["status"], "waiting")
        self.assertEqual(
            refreshed["public"]["captcha_image"],
            "data:image/png;base64,aW1hZ2UtMg==",
        )

        rejected = sessions.poll(waiting["session_id"], {"validate_code": "BAD"})
        self.assertEqual(rejected["status"], "waiting")
        self.assertEqual(
            rejected["public"]["captcha_image"],
            "data:image/png;base64,aW1hZ2UtMw==",
        )
        self.assertEqual(rejected["public"]["error"], "验证码错误")

        completed = sessions.poll(waiting["session_id"], {"validate_code": "GOOD"})
        self.assertEqual(completed["status"], "completed")

    def test_sixpan_device_code_session_encrypts_tokens_and_activates_account(self) -> None:
        class FakeSixpanImporter:
            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "private-device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.example.test/device",
                    "interval": 2,
                    "expires_in": 900,
                }

            def check_device_authorization(self, **_kwargs):
                return {"access_token": "access-secret", "refresh_token": "refresh-secret"}

            @staticmethod
            def extract_tokens(result):
                return {
                    key: result[key]
                    for key in ("access_token", "refresh_token")
                    if result.get(key)
                }

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda kind, credentials, config: AccountTestResult(
                kind == "sixpan"
                and credentials.get("access_token") == "access-secret"
                and config.get("client_id") == "client-id",
                "sixpan-user",
                "检测通过",
            ),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {
                ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
                    lambda: {
                        "host": "api.example.test",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                        "oauth_client_id": "oauth-client-id",
                    },
                    lambda _config: FakeSixpanImporter(),
                )
            },
        )
        waiting = sessions.start("sixpan", "device_code", {"name": "六盘主账号"})
        self.assertEqual(waiting["status"], "waiting")
        self.assertTrue(waiting["expires_at"])
        waiting_json = json.dumps(waiting, ensure_ascii=False)
        self.assertNotIn("private-device-code", waiting_json)
        self.assertNotIn("client-secret", waiting_json)

        completed = sessions.poll(waiting["session_id"])
        self.assertEqual(completed["status"], "completed")
        account_id = completed["account_id"]
        selection = accounts.get_active_account("sixpan")
        credentials = accounts.resolve_credentials(account_id, selection.account_revision)
        self.assertEqual(credentials["access_token"], "access-secret")
        self.assertEqual(credentials["refresh_token"], "refresh-secret")
        self.assertEqual(credentials["client_secret"], "client-secret")
        context = accounts.resolve_account_context(account_id, selection.account_revision)
        self.assertEqual(context["config"]["oauth_client_id"], "oauth-client-id")

    def test_sixpan_device_code_expiry_and_denial_are_terminal(self) -> None:
        class FakeSixpanImporter:
            def __init__(self, status):
                self.status = status

            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "private-device-code",
                    "verification_uri": "https://auth.example.test/device",
                    "expires_in": 600,
                }

            def check_device_authorization(self, **_kwargs):
                return {"status": self.status, "message": "用户拒绝了授权"}

            @staticmethod
            def extract_tokens(_result):
                return {}

        config = {
            "client_id": "client-id",
            "client_secret": "client-secret",
        }
        denied_handler = SixpanDeviceCodeAuthHandler(
            lambda: config,
            lambda _config: FakeSixpanImporter("AUTHORIZATION_DENIED"),
        )
        waiting = denied_handler.start({})
        denied = denied_handler.poll(waiting.private_state, {})
        self.assertEqual(denied.status, "failed")
        self.assertIn("用户拒绝", denied.message)

        expired_state = dict(waiting.private_state)
        expired_state["expires_at"] = "2000-01-01T00:00:00Z"
        expired = denied_handler.poll(expired_state, {})
        self.assertEqual(expired.status, "failed")
        self.assertIn("已过期", expired.message)

    def test_sixpan_device_code_start_provider_error_is_structured(self) -> None:
        class ProviderError(RuntimeError):
            status_code = 502

        class FailingSixpanImporter:
            def start_device_authorization(self, **_kwargs):
                raise ProviderError(
                    "upstream failed accessToken=private-token at https://api.example.test/device"
                )

        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            self.service,
            {
                ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
                    lambda: {},
                    lambda _config: FailingSixpanImporter(),
                )
            },
        )
        app = Flask(__name__ + "_sixpan_start_error")
        app.register_blueprint(accounts_blueprint_for_service(self.service, lambda view: view, sessions))

        response = app.test_client().post(
            "/api/admin/provider-auth-sessions",
            json={
                "provider_kind": "sixpan",
                "auth_method": "device_code",
                "payload": {"client_id": "client-id", "client_secret": "client-secret"},
            },
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["code"], "SIXPAN_DEVICE_CODE_START_FAILED")
        self.assertEqual(payload["stage"], "device_code_start")
        self.assertEqual(payload["http_status"], 502)
        self.assertEqual(payload["provider_kind"], "sixpan")
        self.assertEqual(payload["auth_method"], "device_code")
        self.assertNotIn("private-token", response.get_data(as_text=True))
        self.assertNotIn("https://api.example.test", response.get_data(as_text=True))

    def test_sixpan_device_code_poll_provider_error_is_structured(self) -> None:
        class ProviderError(RuntimeError):
            status_code = 503

        class FailingSixpanImporter:
            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "private-device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.example.test/device",
                    "expires_in": 600,
                }

            def check_device_authorization(self, **_kwargs):
                raise ProviderError("sixpan poll unavailable refreshToken=private-refresh")

        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            self.service,
            {
                ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
                    lambda: {},
                    lambda _config: FailingSixpanImporter(),
                )
            },
        )
        app = Flask(__name__ + "_sixpan_poll_error")
        app.register_blueprint(accounts_blueprint_for_service(self.service, lambda view: view, sessions))
        client = app.test_client()
        started = client.post(
            "/api/admin/provider-auth-sessions",
            json={
                "provider_kind": "sixpan",
                "auth_method": "device_code",
                "payload": {"client_id": "client-id", "client_secret": "client-secret"},
            },
        ).get_json()["session"]

        response = client.post(
            f"/api/admin/provider-auth-sessions/{started['session_id']}/verify",
            json={},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["code"], "SIXPAN_DEVICE_CODE_POLL_FAILED")
        self.assertEqual(payload["stage"], "device_code_poll")
        self.assertEqual(payload["http_status"], 503)
        self.assertNotIn("private-refresh", response.get_data(as_text=True))

    def test_sixpan_device_code_uses_encrypted_session_config_not_global_credentials(self) -> None:
        configs: list[dict[str, Any]] = []

        class FakeSixpanImporter:
            def __init__(self, config):
                configs.append(dict(config))

            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "private-device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.example.test/device",
                    "expires_in": 900,
                }

            def check_device_authorization(self, **_kwargs):
                return {"access_token": "access-secret", "refresh_token": "refresh-secret"}

            @staticmethod
            def extract_tokens(result):
                return {
                    key: result[key]
                    for key in ("access_token", "refresh_token")
                    if result.get(key)
                }

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda kind, credentials, config: AccountTestResult(
                kind == "sixpan"
                and config.get("client_id") == "client-id"
                and credentials.get("client_secret") == "client-secret",
                "sixpan-user",
                "检测通过",
            ),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {
                ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
                    lambda: {},
                    FakeSixpanImporter,
                )
            },
        )
        waiting = sessions.start(
            "sixpan",
            "device_code",
            {
                "name": "六盘主账号",
                "host": "api.example.test",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "oauth_client_id": "oauth-client-id",
            },
        )
        completed = sessions.poll(waiting["session_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(configs[0]["client_secret"], "client-secret")
        self.assertEqual(configs[1]["client_secret"], "client-secret")
        public_json = json.dumps(completed, ensure_ascii=False)
        self.assertNotIn("client-secret", public_json)
        self.assertNotIn("access-secret", public_json)

    def test_expired_auth_session_is_terminalized_when_restored(self) -> None:
        class PendingSixpanImporter:
            def start_device_authorization(self, **_kwargs):
                return {
                    "device_code": "private-device-code",
                    "verification_uri": "https://auth.example.test/device",
                    "expires_in": 600,
                }

            def check_device_authorization(self, **_kwargs):
                raise AssertionError("expired session must not poll Provider")

            @staticmethod
            def extract_tokens(_result):
                return {}

        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            self.service,
            {
                ("sixpan", "device_code"): SixpanDeviceCodeAuthHandler(
                    lambda: {"client_id": "client-id", "client_secret": "client-secret"},
                    lambda _config: PendingSixpanImporter(),
                )
            },
        )
        waiting = sessions.start("sixpan", "device_code")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE provider_auth_sessions SET expires_at=? WHERE session_id=?",
                ("2000-01-01T00:00:00Z", waiting["session_id"]),
            )

        restored = sessions.get(waiting["session_id"])
        self.assertEqual(restored["status"], "failed")
        self.assertIn("已过期", restored["message"])

    def test_concurrent_poll_claim_creates_only_one_account(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        class BlockingHandler:
            def start(self, _payload: dict[str, Any]) -> AuthStep:
                return AuthStep(status="waiting", message="等待授权", private_state={"code": "one-time"})

            def poll(self, _state: dict[str, Any], _payload: dict[str, Any]) -> AuthStep:
                nonlocal calls
                with calls_lock:
                    calls += 1
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("poll release timed out")
                return AuthStep(
                    status="completed",
                    message="授权完成",
                    account_data={
                        "name": "并发授权账号",
                        "config": {"device_id": "device-concurrent"},
                        "credentials": {"cookie": "concurrent-secret"},
                    },
                )

            def cancel(self, _state: dict[str, Any]) -> None:
                return None

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda *_args: AccountTestResult(True, "user", "ok"),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("quark", "test_concurrent"): BlockingHandler()},
        )
        waiting = sessions.start("quark", "test_concurrent")

        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(sessions.poll, waiting["session_id"])
            self.assertTrue(entered.wait(timeout=5))
            observer = executor.submit(sessions.poll, waiting["session_id"])
            observed = observer.result(timeout=5)
            self.assertEqual(observed["status"], "waiting")
            release.set()
            completed = owner.result(timeout=5)

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(calls, 1)
        with self.db.connect() as connection:
            account_count = connection.execute(
                "SELECT COUNT(*) FROM provider_accounts WHERE provider_kind='quark'"
            ).fetchone()[0]
            session_row = connection.execute(
                "SELECT status, row_version FROM provider_auth_sessions WHERE session_id=?",
                (waiting["session_id"],),
            ).fetchone()
        self.assertEqual(account_count, 1)
        self.assertEqual(session_row["status"], "completed")
        self.assertGreaterEqual(int(session_row["row_version"]), 3)

    def test_stale_poll_fails_closed_without_redeeming_one_time_code_again(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        class OneTimeHandler:
            def start(self, _payload: dict[str, Any]) -> AuthStep:
                return AuthStep(status="waiting", message="等待授权", private_state={"code": "device"})

            def poll(self, _state: dict[str, Any], _payload: dict[str, Any]) -> AuthStep:
                nonlocal calls
                with calls_lock:
                    calls += 1
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("stale poll release timed out")
                return AuthStep(
                    status="completed",
                    message="一次性凭证已兑换",
                    account_data={
                        "name": "不应落库的陈旧授权账号",
                        "config": {"device_id": "device-stale"},
                        "credentials": {"cookie": "stale-secret"},
                    },
                )

            def cancel(self, _state: dict[str, Any]) -> None:
                return None

        accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda *_args: AccountTestResult(True, "user", "ok"),
        )
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("quark", "test_one_time"): OneTimeHandler()},
        )
        waiting = sessions.start("quark", "test_one_time")

        with ThreadPoolExecutor(max_workers=1) as executor:
            stale_owner = executor.submit(sessions.poll, waiting["session_id"])
            self.assertTrue(entered.wait(timeout=5))
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE provider_auth_sessions SET updated_at=? WHERE session_id=?",
                    ("2000-01-01T00:00:00Z", waiting["session_id"]),
                )
            recovered = sessions.poll(waiting["session_id"])
            self.assertEqual(recovered["status"], "failed")
            self.assertIn("避免重复兑换", recovered["message"])
            release.set()
            stale_result = stale_owner.result(timeout=5)

        self.assertEqual(stale_result["status"], "failed")
        self.assertEqual(sessions.get(waiting["session_id"])["status"], "failed")
        self.assertEqual(calls, 1)
        with self.db.connect() as connection:
            account_count = connection.execute(
                "SELECT COUNT(*) FROM provider_accounts WHERE provider_kind='quark'"
            ).fetchone()[0]
        self.assertEqual(account_count, 0)

    def test_stale_account_persistence_recovers_without_repoll_or_duplicate_account(self) -> None:
        persist_entered = threading.Event()
        release_first_persist = threading.Event()
        provider_calls = 0
        test_calls = 0
        calls_lock = threading.Lock()

        class CompletedHandler:
            def start(self, _payload: dict[str, Any]) -> AuthStep:
                return AuthStep(status="waiting", message="等待授权", private_state={"code": "device"})

            def poll(self, _state: dict[str, Any], _payload: dict[str, Any]) -> AuthStep:
                nonlocal provider_calls
                with calls_lock:
                    provider_calls += 1
                return AuthStep(
                    status="completed",
                    message="授权完成",
                    account_data={
                        "name": "持久化恢复账号",
                        "config": {"device_id": "device-persist"},
                        "credentials": {"cookie": "persist-secret"},
                    },
                )

            def cancel(self, _state: dict[str, Any]) -> None:
                return None

        def tester(*_args: Any) -> AccountTestResult:
            nonlocal test_calls
            with calls_lock:
                test_calls += 1
                call_number = test_calls
            if call_number == 1:
                persist_entered.set()
                if not release_first_persist.wait(timeout=5):
                    raise AssertionError("first persistence probe release timed out")
            return AccountTestResult(True, "user", "ok")

        accounts = ProviderAccountService(self.db, lambda: KEY, tester=tester)
        sessions = ProviderAuthSessionService(
            self.db,
            lambda: KEY,
            accounts,
            {("quark", "test_persist_recovery"): CompletedHandler()},
        )
        waiting = sessions.start("quark", "test_persist_recovery")

        with ThreadPoolExecutor(max_workers=1) as executor:
            first_owner = executor.submit(sessions.poll, waiting["session_id"])
            self.assertTrue(persist_entered.wait(timeout=5))
            with self.db.connect() as connection:
                internal = connection.execute(
                    "SELECT status FROM provider_auth_sessions WHERE session_id=?",
                    (waiting["session_id"],),
                ).fetchone()
                self.assertEqual(internal["status"], "persisting_active")
                connection.execute(
                    "UPDATE provider_auth_sessions SET updated_at=? WHERE session_id=?",
                    ("2000-01-01T00:00:00Z", waiting["session_id"]),
                )

            recovered = sessions.poll(waiting["session_id"])
            self.assertEqual(recovered["status"], "completed")
            recovered_account_id = recovered["account_id"]
            release_first_persist.set()
            first_result = first_owner.result(timeout=5)

        self.assertEqual(first_result["status"], "completed")
        self.assertEqual(first_result["account_id"], recovered_account_id)
        self.assertEqual(provider_calls, 1)
        self.assertEqual(test_calls, 2)
        repeated = sessions.poll(waiting["session_id"])
        self.assertEqual(repeated["account_id"], recovered_account_id)
        self.assertEqual(provider_calls, 1)
        self.assertEqual(test_calls, 2)
        with self.db.connect() as connection:
            account_count = connection.execute(
                "SELECT COUNT(*) FROM provider_accounts WHERE provider_kind='quark'"
            ).fetchone()[0]
            session_row = connection.execute(
                "SELECT status, account_id FROM provider_auth_sessions WHERE session_id=?",
                (waiting["session_id"],),
            ).fetchone()
        self.assertEqual(account_count, 1)
        self.assertEqual(session_row["status"], "completed")
        self.assertEqual(session_row["account_id"], recovered_account_id)


if __name__ == "__main__":
    unittest.main()
