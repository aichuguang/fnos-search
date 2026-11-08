from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flask import Flask

from fnos_media_import.agent.audit import AgentAuditLog
from fnos_media_import.agent.auth import ALL_SCOPES, SCOPE_PROFILES, AgentTokenStore
from fnos_media_import.blueprints.agent_admin import AgentAdminRouteContext, create_agent_admin_blueprint
from fnos_media_import.database import Database


def _allow(func):
    return func


class AgentAdminTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "admin.db")
        self.database.init_schema()
        self.token_store = AgentTokenStore(self.database)
        app = Flask(__name__)
        app.register_blueprint(
            create_agent_admin_blueprint(
                AgentAdminRouteContext(
                    admin_required=_allow,
                    token_store=lambda: self.token_store,
                    audit=lambda: AgentAuditLog(self.database),
                    token_profiles=SCOPE_PROFILES,
                    all_scopes=ALL_SCOPES,
                )
            )
        )
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_token_lifecycle(self) -> None:
        empty = self.client.get("/api/admin/agent/tokens").get_json()
        self.assertEqual(empty["items"], [])
        self.assertIn("read", empty["profiles"])

        created = self.client.post(
            "/api/admin/agent/tokens",
            json={"name": "hermes", "profile": "operator"},
        ).get_json()
        self.assertTrue(created["success"])
        self.assertTrue(created["token"].startswith("fnos_agent_"))
        token_id = created["item"]["id"]
        self.assertIn("import.write", created["item"]["scopes"])
        self.assertNotIn("organize.apply", created["item"]["scopes"])

        listed = self.client.get("/api/admin/agent/tokens").get_json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token_hash", listed[0])

        revoked = self.client.delete(f"/api/admin/agent/tokens/{token_id}").get_json()
        self.assertTrue(revoked["success"])

    def test_create_token_rejects_unknown_scope(self) -> None:
        response = self.client.post(
            "/api/admin/agent/tokens",
            json={"name": "x", "scopes": ["search", "bogus"]},
        )
        self.assertEqual(response.status_code, 400)

    def test_create_token_requires_name(self) -> None:
        self.assertEqual(self.client.post("/api/admin/agent/tokens", json={}).status_code, 400)

    def test_full_profile_includes_apply(self) -> None:
        created = self.client.post(
            "/api/admin/agent/tokens",
            json={"name": "mover", "profile": "full"},
        ).get_json()
        self.assertIn("organize.apply", created["item"]["scopes"])

    def test_audit_endpoint(self) -> None:
        AgentAuditLog(self.database).record(
            token_id=1,
            token_name="hermes",
            tool="search_resources",
            scope="search",
            risk="read",
            arguments={"keyword": "x"},
            outcome="ok",
        )
        body = self.client.get("/api/admin/agent/audit").get_json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["tool"], "search_resources")

    def test_disabled_surface_reports_409(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(
            create_agent_admin_blueprint(
                AgentAdminRouteContext(
                    admin_required=_allow,
                    token_store=lambda: None,
                    audit=lambda: None,
                    token_profiles=SCOPE_PROFILES,
                    all_scopes=ALL_SCOPES,
                )
            )
        )
        app.config.update(TESTING=True)
        response = app.test_client().get("/api/admin/agent/tokens")
        self.assertEqual(response.status_code, 409)
        self.assertIn("未启用", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
