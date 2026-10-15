from __future__ import annotations

import ast
import copy
import unittest
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flask import Flask, jsonify, request, session

from fnos_media_import.blueprints.auth import AuthRouteContext, create_auth_blueprint
from fnos_media_import.database import Database
from fnos_media_import.public_web import _hash_password, _verify_password_hash
from fnos_media_import.repositories.admin_profile_repository import AdminProfileRepository
from fnos_media_import.repositories.app_settings_repository import AppSettingsRepository
from fnos_media_import.services.admin_profile_service import AdminProfileService


ROOT = Path(__file__).resolve().parents[1]


class _Profiles:
    def __init__(self) -> None:
        self.value: dict[str, Any] = {}

    def get_profile(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)

    def save_profile(self, profile: dict[str, Any]) -> None:
        self.value = copy.deepcopy(profile)

    def update_profile_atomic(self, updater) -> dict[str, Any]:
        updated = updater(self.get_profile())
        self.save_profile(updated)
        return copy.deepcopy(updated)

    def get_branding(self) -> dict[str, Any]:
        return {}


def _service(repository: _Profiles | AdminProfileRepository, *, password: str = "admin") -> AdminProfileService:
    return AdminProfileService(
        repository,
        {"username": "admin", "password": password},
        ROOT / "codex_tmp" / "unused-auth-uploads",
        _hash_password,
        _verify_password_hash,
        session_key="isolated-auth-test-key",
    )


def _app(service: AdminProfileService, *, csrf_enabled: bool = False) -> Flask:
    # Run the actual nested app handlers without starting provider clients or
    # background workers. This exercises their wiring, not a copied guard.
    source = ast.parse((ROOT / "fnos_media_import" / "app.py").read_text(encoding="utf-8"))
    factory = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "create_app")
    names = {
        "admin_required", "_is_admin_logged_in", "_admin_profile",
        "_verify_admin_password", "api_admin_profile_update",
    }
    handlers = [node for node in factory.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(handlers) == len(names)
    namespace = {
        "Any": Any, "wraps": wraps, "session": session, "request": request,
        "jsonify": jsonify, "admin_profile_service": service,
    }
    exec(compile(ast.Module(body=handlers, type_ignores=[]), "<app-auth-handlers>", "exec"), namespace)
    app = Flask(__name__)
    app.config.update(SECRET_KEY="isolated-cookie-test-key", TESTING=True)
    app.register_blueprint(create_auth_blueprint(AuthRouteContext(
        csrf_enabled=lambda: csrf_enabled,
        rate_limit_login=lambda: None,
        verify_password=namespace["_verify_admin_password"],
        admin_profile=namespace["_admin_profile"],
        is_logged_in=namespace["_is_admin_logged_in"],
        security_status=lambda: {"success": True},
        admin_required=namespace["admin_required"],
        session_fingerprint=service.session_fingerprint,
    )))
    app.add_url_rule(
        "/api/admin/profile", "update_profile",
        namespace["admin_required"](namespace["api_admin_profile_update"]), methods=["POST"],
    )
    return app


class AdminAuthRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = _Profiles()
        self.service = _service(self.profiles)
        self.app = _app(self.service)

    def _login(self, client, username="admin", password="admin"):
        return client.post("/api/admin/login", json={"username": username, "password": password})

    def test_password_change_keeps_current_session_but_revokes_other_cookie(self) -> None:
        first, second = self.app.test_client(), self.app.test_client()
        self.assertEqual(self._login(first).status_code, 200)
        self.assertEqual(self._login(second).status_code, 200)
        response = first.post("/api/admin/profile", json={
            "username": "admin", "current_password": "admin", "new_password": "new-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_fingerprint", response.json)
        self.assertEqual(first.get("/api/admin/security/status").status_code, 200)
        self.assertEqual(second.get("/api/admin/security/status").status_code, 401)
        self.assertFalse(second.get("/api/admin/session").json["logged_in"])
        self.assertEqual(self._login(second).status_code, 401)
        self.assertEqual(self._login(second, password="new-password").status_code, 200)

    def test_chinese_username_can_login_and_be_changed_back(self) -> None:
        client, other = self.app.test_client(), self.app.test_client()
        self._login(client)
        self._login(other)
        original_cookie = other.get_cookie("session").value
        changed = client.post("/api/admin/profile", json={"username": "管理员", "current_password": "admin"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(client.get("/api/admin/security/status").status_code, 200)
        self.assertEqual(other.get("/api/admin/security/status").status_code, 401)
        self.assertEqual(self._login(other, username="管理员").status_code, 200)
        restored = client.post("/api/admin/profile", json={"username": "admin", "current_password": "admin"})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(other.get("/api/admin/security/status").status_code, 401)
        self.assertEqual(self._login(other).status_code, 200)
        replay = self.app.test_client()
        replay.set_cookie("session", original_cookie)
        self.assertEqual(replay.get("/api/admin/security/status").status_code, 401)

    def test_unicode_config_and_hashed_passwords_are_supported(self) -> None:
        service = _service(self.profiles, password="测试密码")
        self.assertTrue(service.verify_password("admin", "测试密码")[0])
        self.assertFalse(service.verify_password("admin", "错误密码")[0])
        result, status = service.update_profile({
            "username": "管理员", "current_password": "测试密码", "new_password": "安全的新密码",
        }, "admin")
        self.assertEqual(status, 200, result)
        self.assertTrue(service.verify_password("管理员", "安全的新密码")[0])

    def test_legacy_cookie_without_credential_fingerprint_must_relogin(self) -> None:
        client = self.app.test_client()
        with client.session_transaction() as cookie:
            cookie["admin_logged_in"] = True
            cookie["admin_username"] = "admin"
        self.assertEqual(client.get("/api/admin/security/status").status_code, 401)
        self.assertFalse(client.get("/api/admin/session").json["logged_in"])

    def test_fingerprint_is_stable_across_processes_and_changes_with_config_password(self) -> None:
        fingerprint = self.service.session_fingerprint()
        self.assertEqual(fingerprint, _service(self.profiles).session_fingerprint())
        self.assertNotEqual(fingerprint, _service(self.profiles, password="different").session_fingerprint())
        self.assertFalse(self.service.session_is_current("admin", "无效签名"))
        self.assertFalse(self.service.session_is_current("其他用户", fingerprint))

    def test_noncredential_save_does_not_revoke_sessions(self) -> None:
        first, second = self.app.test_client(), self.app.test_client()
        self._login(first)
        self._login(second)
        self.assertEqual(first.post("/api/admin/profile", json={"username": "admin"}).status_code, 200)
        self.assertEqual(second.get("/api/admin/security/status").status_code, 200)
        stored = self.profiles.get_profile()
        stored["avatar_url"] = "/static/avatar.png"
        self.profiles.save_profile(stored)
        self.assertEqual(second.get("/api/admin/security/status").status_code, 200)

    def test_password_change_between_verification_and_cookie_issue_cannot_mint_new_session(self) -> None:
        app = Flask("auth-race")
        app.config.update(SECRET_KEY="test", TESTING=True)
        current = {"fingerprint": "old"}

        def verify(_username, _password):
            current["fingerprint"] = "new"
            return True, "admin"

        app.register_blueprint(create_auth_blueprint(AuthRouteContext(
            csrf_enabled=lambda: False, rate_limit_login=lambda: None,
            verify_password=verify, admin_profile=lambda: {}, is_logged_in=lambda: False,
            security_status=lambda: {}, admin_required=lambda view: view,
            session_fingerprint=lambda: current["fingerprint"],
        )))
        client = app.test_client()
        self.assertEqual(self._login(client).status_code, 401)
        with client.session_transaction() as cookie:
            self.assertNotIn("admin_logged_in", cookie)

    def test_login_rejects_nonobject_json(self) -> None:
        self.assertEqual(self.app.test_client().post("/api/admin/login", json=["admin"]).status_code, 400)

    def test_profile_rejects_nonobject_json_and_invalid_unicode(self) -> None:
        client = self.app.test_client()
        self._login(client)
        self.assertEqual(client.post("/api/admin/profile", json=["admin"]).status_code, 400)
        for payload in (
            {"username": "\ud800", "current_password": "admin"},
            {"username": "admin", "current_password": "admin", "new_password": "invalid-\ud800"},
        ):
            self.assertEqual(client.post("/api/admin/profile", json=payload).status_code, 400)
        self.assertTrue(self.service.verify_password("admin", "admin")[0])

    def test_unicode_csrf_token_is_rejected_without_server_error(self) -> None:
        client = _app(self.service, csrf_enabled=True).test_client()
        self._login(client)
        token = client.get("/api/admin/session").json["csrf_token"]
        self.assertEqual(client.post(
            "/api/admin/profile", json={"username": "admin"}, headers={"X-CSRF-Token": "无效令牌"},
        ).status_code, 403)
        self.assertEqual(client.post(
            "/api/admin/profile", json={"username": "admin"}, headers={"X-CSRF-Token": token},
        ).status_code, 200)

    def test_profile_rechecks_revocation_inside_atomic_update(self) -> None:
        client = self.app.test_client()
        self._login(client)
        update = self.profiles.update_profile_atomic

        def update_after_revocation(updater):
            self.profiles.value["auth_revision"] = "another-session-changed-password"
            return update(updater)

        self.profiles.update_profile_atomic = update_after_revocation
        response = client.post("/api/admin/profile", json={"username": "admin"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.profiles.value, {"auth_revision": "another-session-changed-password"})
        with client.session_transaction() as cookie:
            self.assertNotIn("admin_logged_in", cookie)

    def test_change_after_profile_write_does_not_renew_revoked_session(self) -> None:
        client = self.app.test_client()
        self._login(client)
        update = self.profiles.update_profile_atomic

        def update_then_revoke(updater):
            updated = update(updater)
            self.profiles.value["auth_revision"] = "concurrent-later-change"
            return updated

        self.profiles.update_profile_atomic = update_then_revoke
        response = client.post("/api/admin/profile", json={
            "username": "admin", "current_password": "admin", "new_password": "my-new-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_fingerprint", response.json)
        self.assertEqual(client.get("/api/admin/security/status").status_code, 401)

    def test_atomic_repository_persists_revision_and_rolls_back_rejected_update(self) -> None:
        with TemporaryDirectory(prefix="fnos-auth-test-") as directory:
            database = Database(Path(directory) / "auth.sqlite3")
            database.init_schema()
            repository = AdminProfileRepository(AppSettingsRepository(database.connect))
            service = _service(repository)
            old_fingerprint = service.session_fingerprint()
            result, status = service.update_profile(
                {"username": "管理员", "current_password": "admin"}, "admin",
                current_fingerprint=old_fingerprint,
            )
            self.assertEqual(status, 200)
            reloaded = _service(AdminProfileRepository(AppSettingsRepository(database.connect)))
            self.assertTrue(reloaded.session_is_current("管理员", result["_auth_fingerprint"]))
            self.assertFalse(reloaded.session_is_current("admin", old_fingerprint))
            before = repository.get_profile()
            _, status = reloaded.update_profile(
                {"username": "admin", "current_password": "wrong"}, "管理员",
                current_fingerprint=result["_auth_fingerprint"],
            )
            self.assertEqual(status, 400)
            self.assertEqual(repository.get_profile(), before)


if __name__ == "__main__":
    unittest.main()
