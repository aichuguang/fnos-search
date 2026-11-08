from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from fnos_media_import.app import _register_agent_surface
from fnos_media_import.database import Database


def _builders(database: Database) -> dict:
    return {
        "database": database,
        "search": lambda keyword, *, sources, refresh: {
            "items": [{"public_id": "p1", "title": keyword}],
            "expires_in_minutes": 60,
        },
        "resource_detail": lambda public_id: ({"success": True, "detail": {"public_id": public_id}}, 200),
        "resource_files": lambda public_id, fid: ({"success": True, "items": []}, 200),
        "detect_link": lambda url, password: {"provider_kind": "quark", "supported": True, "url": url},
        "tmdb_search": lambda keyword: {"status": "empty", "items": []},
        "list_jobs": lambda **kwargs: {"items": [], "total": 0},
        "job_detail": lambda job_id: ({"success": True, "job": {"id": job_id}}, 200),
        "list_requests": lambda **kwargs: {"items": [], "total": 0},
        "request_detail": lambda request_id: ({"success": True, "request": {"id": request_id}}, 200),
        "list_organizer_tasks": lambda **kwargs: {"items": [], "total": 0, "status": {}},
        "organizer_task_detail": lambda task_id: ({"success": True, "task": {"id": task_id}, "status": {}}, 200),
        "list_subscriptions": lambda **kwargs: {"items": [], "total": 0},
        "subscription_detail": lambda subscription_id: ({"success": True, "item": {"id": subscription_id}}, 200),
        "scheduler_status": lambda: {"enabled": True},
        "system_status": lambda: {"ok": True},
        "categories": lambda: {"movie": {"label": "电影"}},
    }


class AgentSurfaceWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "agent.db")
        self.database.init_schema()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, *, enabled: bool = True) -> Flask:
        app = Flask(__name__)
        env = {"FNOS_AGENT_ENABLED": "true"} if enabled else {}
        with patch.dict("os.environ", env, clear=True):
            _register_agent_surface(app, _builders(self.database))
        return app

    def test_disabled_by_default(self) -> None:
        app = self._app(enabled=False)
        self.assertNotIn("agent_services", app.extensions)
        self.assertEqual(app.test_client().post("/mcp", json={}).status_code, 404)

    def test_enabled_registers_endpoint_and_token_store(self) -> None:
        app = self._app()
        self.assertIn("agent_token_store", app.extensions)
        info = app.test_client().get("/mcp").get_json()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["toolCount"], 35)

    def test_created_token_authorizes_initialize(self) -> None:
        app = self._app()
        plaintext, _ = app.extensions["agent_token_store"].create("hermes")
        response = app.test_client().post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["serverInfo"]["name"], "fnos-search-agent")

    def test_admin_routes_registered_with_admin_deps(self) -> None:
        app = Flask(__name__)
        with patch.dict("os.environ", {"FNOS_AGENT_ENABLED": "true"}, clear=True):
            _register_agent_surface(
                app,
                _builders(self.database),
                {
                    "admin_required": lambda func: func,
                    "database": self.database,
                },
            )
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertIn("/api/admin/agent/tokens", rules)
        self.assertIn("/api/admin/agent/tokens/<int:token_id>", rules)

    def test_admin_routes_explain_disabled_surface(self) -> None:
        app = Flask(__name__)
        with patch.dict("os.environ", {}, clear=True):
            _register_agent_surface(
                app,
                None,
                {
                    "admin_required": lambda func: func,
                    "database": self.database,
                },
            )
        client = app.test_client()
        # /mcp stays unmounted...
        self.assertEqual(client.post("/mcp", json={}).status_code, 404)
        # ...but the admin route explains the switch instead of a raw 404.
        response = client.get("/api/admin/agent/tokens")
        self.assertEqual(response.status_code, 409)
        self.assertIn("未启用", response.get_json()["message"])

    def test_tool_call_records_audit(self) -> None:
        app = self._app()
        plaintext, record = app.extensions["agent_token_store"].create("hermes")
        response = app.test_client().post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_resources", "arguments": {"keyword": "示例"}},
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        self.assertEqual(response.status_code, 200)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT tool, outcome, token_id FROM agent_tool_calls"
            ).fetchone()
        self.assertEqual(row["tool"], "search_resources")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["token_id"], record["id"])


if __name__ == "__main__":
    unittest.main()
