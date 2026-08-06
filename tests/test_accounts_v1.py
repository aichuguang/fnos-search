"""V1.0 统一账号体系契约测试（unittest，不使用 pytest）。

覆盖：凭证加密往返与错误密钥失败、创建+修订（旧修订可读、新修订隔离）、
激活互斥与乐观并发冲突、当前账号禁用保护、删除引用保护、API 永不泄露凭证、
未检测/禁用账号不可激活。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from fnos_media_import.accounts import (
    AccountDisabledError,
    AccountInUseError,
    AccountNotTestedError,
    ActiveAccountDisableError,
    DecryptionError,
    MasterKeyMissingError,
    ProviderAccountService,
    SelectionChangedError,
    accounts_blueprint_for_service,
    decrypt,
    encrypt,
    master_key_from_env,
)
from fnos_media_import.database import Database
from fnos_media_import.domain.models import AccountTestResult

TEST_KEY_HEX = "a" * 64  # 32 字节 AES-256 主密钥
OTHER_KEY_HEX = "b" * 64


def _master_key() -> bytes:
    return bytes.fromhex(TEST_KEY_HEX)


def _tester(*, ok: bool = True, hint: str = "masked-user") -> Any:
    def tester(credentials: dict[str, Any], config: dict[str, Any]) -> AccountTestResult:
        return AccountTestResult(ok=ok, identity_hint=hint, message="通过" if ok else "网络失败")

    return tester


class AccountsTestCaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "accounts.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        self.service = ProviderAccountService(self.database, _master_key)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self, provider_kind: str = "quark", name: str = "家庭账号", **kw: Any) -> Any:
        credentials = kw.pop("credentials", {"cookie": "cookie-value-1"})
        config = kw.pop("config", {})
        account = self.service.create_account(
            provider_kind=provider_kind,
            name=name,
            config=config,
            credentials=credentials,
            **kw,
        )
        return account

    def _create_tested(self, provider_kind: str = "quark", name: str = "家庭账号", **kw: Any) -> Any:
        account = self._create(provider_kind=provider_kind, name=name, **kw)
        self.service.test_account(account.account_id, tester=_tester(ok=True))
        return account


class CryptoTests(unittest.TestCase):
    def test_round_trip_and_wrong_key_fails(self) -> None:
        ciphertext, key_id = encrypt(_master_key(), '{"cookie": "secret-1"}')
        self.assertEqual(key_id, "v1")
        self.assertEqual(
            decrypt(_master_key(), key_id, ciphertext).decode("utf-8"),
            '{"cookie": "secret-1"}',
        )
        with self.assertRaises(DecryptionError):
            decrypt(bytes.fromhex(OTHER_KEY_HEX), key_id, ciphertext)

    def test_binary_plaintext_round_trip(self) -> None:
        ciphertext, key_id = encrypt(_master_key(), b"\x00\x01raw")
        self.assertEqual(decrypt(_master_key(), key_id, ciphertext), b"\x00\x01raw")

    def test_unknown_key_id_rejected(self) -> None:
        ciphertext, _key_id = encrypt(_master_key(), b"data")
        with self.assertRaises(Exception):
            decrypt(_master_key(), "v9", ciphertext)

    def test_missing_master_key_raises_clear_error(self) -> None:
        with self.assertRaises(MasterKeyMissingError):
            master_key_from_env({})
        key = master_key_from_env({"FNOS_ACCOUNT_MASTER_KEY": TEST_KEY_HEX})
        self.assertEqual(len(key), 32)


class AccountLifecycleTests(AccountsTestCaseBase):
    def test_create_and_revise_keeps_old_revision_readable(self) -> None:
        account = self._create(credentials={"cookie": "cookie-v1"})
        self.assertEqual(account.current_revision, 1)
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 1), {"cookie": "cookie-v1"}
        )

        updated = self.service.revise_credentials(
            account.account_id, config={}, credentials={"cookie": "cookie-v2"}
        )
        self.assertEqual(updated.current_revision, 2)
        # 旧修订保持不变，新修订完全隔离。
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 1), {"cookie": "cookie-v1"}
        )
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 2), {"cookie": "cookie-v2"}
        )
        # 凭证更新后必须重新检测才能激活。
        self.assertNotEqual(
            self.service.get_account(account.account_id).last_test_status, "ok"
        )

    def test_revise_unknown_revision_raises(self) -> None:
        account = self._create()
        with self.assertRaises(Exception):
            self.service.resolve_credentials(account.account_id, 99)

    def test_activation_mutually_exclusive_and_optimistic_conflict(self) -> None:
        a = self._create_tested(provider_kind="quark", name="A")
        b = self._create_tested(provider_kind="quark", name="B")

        first = self.service.activate_account("default", a.account_id, None)
        self.assertEqual(first.selection_revision, 1)
        self.assertEqual(self.service.get_active_account("quark").account_id, a.account_id)

        # 过期 selection_revision 激活 B 必须冲突。
        with self.assertRaises(SelectionChangedError):
            self.service.activate_account("default", b.account_id, 0)
        # 已有选择却未携带 expected（无法校验）也必须冲突。
        with self.assertRaises(SelectionChangedError):
            self.service.activate_account("default", b.account_id, None)
        # 用最新 revision 激活 B 成功，同一 Provider 只有一个激活账号。
        second = self.service.activate_account("default", b.account_id, 1)
        self.assertEqual(second.selection_revision, 2)
        self.assertEqual(second.account_id, b.account_id)
        self.assertEqual(self.service.get_active_account("quark").account_id, b.account_id)

        # 不同 Provider 可以同时各自激活。
        c = self._create_tested(provider_kind="mobile", name="M")
        self.service.activate_account("default", c.account_id, None)
        self.assertEqual(self.service.get_active_account("mobile").account_id, c.account_id)
        self.assertEqual(self.service.get_active_account("quark").account_id, b.account_id)

    def test_untested_or_failed_cannot_activate(self) -> None:
        draft = self._create(provider_kind="quark", name="草稿")
        with self.assertRaises(AccountNotTestedError):
            self.service.activate_account("default", draft.account_id, None)

        self.service.test_account(draft.account_id, tester=_tester(ok=False))
        with self.assertRaises(AccountNotTestedError):
            self.service.activate_account("default", draft.account_id, None)

    def test_disabled_cannot_activate(self) -> None:
        account = self._create_tested(provider_kind="sixpan", name="A")
        self.service.disable_account(account.account_id)
        with self.assertRaises(AccountDisabledError):
            self.service.activate_account("default", account.account_id, None)

    def test_disable_current_blocked_until_switch(self) -> None:
        a = self._create_tested(provider_kind="tianyi", name="A")
        self.service.activate_account("default", a.account_id, None)
        with self.assertRaises(ActiveAccountDisableError):
            self.service.disable_account(a.account_id)

        b = self._create_tested(provider_kind="tianyi", name="B")
        self.service.activate_account("default", b.account_id, 1)
        self.service.disable_account(a.account_id)
        self.assertFalse(self.service.get_account(a.account_id).enabled)

    def test_delete_active_selection_blocked(self) -> None:
        account = self._create_tested(provider_kind="quark", name="A")
        self.service.activate_account("default", account.account_id, None)
        with self.assertRaises(AccountInUseError):
            self.service.delete_account(account.account_id)

    def test_delete_blocked_by_non_terminal_job_then_ok(self) -> None:
        account = self._create_tested(provider_kind="mobile", name="A")
        plan = {
            "version": 4,
            "job_id": 1,
            "source": {"account_id": account.account_id, "account_revision": 1},
            "destination": {},
        }
        job_id, created = self.database.create_job(
            {
                "title": "Referenced",
                "category": "movie",
                "category_label": "电影",
                "source_type": "mobile",
                "source_url": "https://example.com/referenced-job",
                "target_route": "mobile_direct",
                "target_path": "/电影/x",
                "status": "waiting_transfer",
                "plan_version": 4,
                "raw_data": {"execution_plan": plan},
                "idempotency_key": "account-ref-job-1",
            }
        )
        self.assertTrue(created)
        with self.assertRaises(AccountInUseError):
            self.service.delete_account(account.account_id)

        # 任务进入终态后不再阻挡删除。
        self.database.update_job_if_status(job_id, {"waiting_transfer"}, status="done")
        self.service.delete_account(account.account_id)
        self.assertIsNone(self.service.get_account(account.account_id))

    def test_delete_unreferenced_account(self) -> None:
        account = self._create(provider_kind="mobile", name="临时")
        self.service.delete_account(account.account_id)
        self.assertIsNone(self.service.get_account(account.account_id))

    def test_account_id_and_provider_kind_immutable(self) -> None:
        account = self._create_tested(provider_kind="quark", name="A")
        # service 层没有任何改 account_id / provider_kind 的方法，二次修订仍保留原值。
        updated = self.service.revise_credentials(
            account.account_id, config={}, credentials={"cookie": "v2"}
        )
        self.assertEqual(updated.account_id, account.account_id)
        self.assertEqual(updated.provider_kind, "quark")

    def test_master_key_missing_blocks_creation(self) -> None:
        service = ProviderAccountService(self.database, lambda: master_key_from_env({}))
        with self.assertRaises(MasterKeyMissingError):
            service.create_account(
                provider_kind="quark", name="x", config={}, credentials={"cookie": "c"}
            )


class AccountApiTests(AccountsTestCaseBase):
    def _app(self, *, deny_admin: bool = False) -> Any:
        from flask import Flask

        app = Flask(__name__)

        def protected(handler: Any) -> Any:
            if deny_admin:

                def denied(*_args: Any, **_kwargs: Any) -> Any:
                    from flask import jsonify

                    return jsonify({"success": False}), 401

                return denied

            return handler

        app.register_blueprint(accounts_blueprint_for_service(self.service, protected))
        return app

    def test_credentials_never_leak_in_api(self) -> None:
        app = self._app()
        client = app.test_client()
        self.service.create_account(
            provider_kind="quark",
            name="家庭账号",
            config={"remark": "some-note"},
            credentials={"cookie": "super-secret-cookie-value"},
        )

        raw = client.get("/api/admin/provider-accounts").get_data(as_text=True)
        self.assertNotIn("super-secret-cookie-value", raw)
        self.assertNotIn("cookie", raw)

        account = self.service.list_accounts("quark")[0]
        detail = client.get(f"/api/admin/provider-accounts/{account.account_id}")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_json()
        self.assertTrue(body["success"])
        self.assertNotIn("credentials", body["account"])
        self.assertNotIn("ciphertext", body["account"])
        self.assertTrue(body["account"]["configured"])
        self.assertEqual(
            set(body["account"].keys()),
            {
                "account_id",
                "provider_kind",
                "name",
                "enabled",
                "current_revision",
                "identity_hint",
                "configured",
                "last_test_status",
                "last_test_message",
                "last_tested_at",
                "created_at",
                "updated_at",
            },
        )
        detail_text = detail.get_data(as_text=True)
        self.assertNotIn("super-secret-cookie-value", detail_text)

    def test_catalog_endpoint(self) -> None:
        app = self._app()
        payload = app.test_client().get("/api/admin/providers/catalog").get_json()
        self.assertTrue(payload["success"])
        kinds = {item["provider_kind"] for item in payload["providers"]}
        self.assertEqual(kinds, {"mobile", "quark", "tianyi", "sixpan"})
        quark = next(item for item in payload["providers"] if item["provider_kind"] == "quark")
        self.assertTrue(quark["capabilities"]["supports_native_save"])
        self.assertFalse(quark["capabilities"]["supports_native_offline"])
        self.assertIn("cookie", [f["name"] for f in quark["schema"]["fields"]])

    def test_activate_conflict_via_api_returns_selection_changed(self) -> None:
        app = self._app()
        client = app.test_client()
        a = self._create_tested(provider_kind="quark", name="A")
        b = self._create_tested(provider_kind="quark", name="B")
        # a 激活 revision=1，再次激活 a（携带当前值）revision=2，使旧值 1 变成过期。
        client.post(f"/api/admin/provider-accounts/{a.account_id}/activate", json={"scope": "default"})
        client.post(
            f"/api/admin/provider-accounts/{a.account_id}/activate",
            json={"scope": "default", "expected_selection_revision": 1},
        )
        conflict = client.post(
            f"/api/admin/provider-accounts/{b.account_id}/activate",
            json={"scope": "default", "expected_selection_revision": 1},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "SELECTION_CHANGED")

        ok = client.post(
            f"/api/admin/provider-accounts/{b.account_id}/activate",
            json={"scope": "default", "expected_selection_revision": 2},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["selection"]["selection_revision"], 3)

    def test_disable_current_via_api_blocked(self) -> None:
        app = self._app()
        client = app.test_client()
        account = self._create_tested(provider_kind="tianyi", name="A")
        client.post(f"/api/admin/provider-accounts/{account.account_id}/activate", json={})
        response = client.post(f"/api/admin/provider-accounts/{account.account_id}/disable")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "ACCOUNT_ACTIVE")

    def test_patch_rejects_immutable_fields(self) -> None:
        app = self._app()
        client = app.test_client()
        account = self._create_tested(provider_kind="quark", name="A")
        response = client.patch(
            f"/api/admin/provider-accounts/{account.account_id}",
            json={"account_id": "hacked", "name": "改名"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "IMMUTABLE_FIELD")
        self.assertEqual(self.service.get_account(account.account_id).name, "A")

    def test_patch_updates_name_and_credentials(self) -> None:
        app = self._app()
        client = app.test_client()
        account = self._create(provider_kind="quark", name="原名")
        response = client.patch(
            f"/api/admin/provider-accounts/{account.account_id}",
            json={"name": "新名", "credentials": {"cookie": "new-cookie"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account"]["name"], "新名")
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 2), {"cookie": "new-cookie"}
        )
        self.assertEqual(
            self.service.resolve_credentials(account.account_id, 1), {"cookie": "cookie-value-1"}
        )

    def test_test_endpoint_returns_result_without_secret(self) -> None:
        app = self._app()
        client = app.test_client()
        account = self._create(provider_kind="quark", name="A", credentials={"cookie": "hush-hush"})
        response = client.post(f"/api/admin/provider-accounts/{account.account_id}/test")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("hush-hush", body)
        result = response.get_json()["result"]
        self.assertTrue(result["ok"])

    def test_admin_required_applies_to_all_account_routes(self) -> None:
        app = self._app(deny_admin=True)
        client = app.test_client()
        self.assertEqual(client.get("/api/admin/providers/catalog").status_code, 401)
        self.assertEqual(client.get("/api/admin/provider-accounts").status_code, 401)
        self.assertEqual(client.post("/api/admin/provider-accounts", json={}).status_code, 401)
        self.assertEqual(client.get("/api/admin/provider-accounts/acc_1").status_code, 401)
        self.assertEqual(client.patch("/api/admin/provider-accounts/acc_1", json={}).status_code, 401)
        self.assertEqual(
            client.post("/api/admin/provider-accounts/acc_1/test").status_code, 401
        )
        self.assertEqual(
            client.post("/api/admin/provider-accounts/acc_1/activate", json={}).status_code, 401
        )
        self.assertEqual(
            client.post("/api/admin/provider-accounts/acc_1/disable").status_code, 401
        )
        self.assertEqual(client.delete("/api/admin/provider-accounts/acc_1").status_code, 401)


if __name__ == "__main__":
    unittest.main()
