from __future__ import annotations

import unittest

from flask import Flask

from fnos_media_import.agent.auth import AgentTokenRecord
from fnos_media_import.agent.mcp import protocol
from fnos_media_import.agent.mcp.blueprint import create_agent_blueprint
from fnos_media_import.agent.services import AgentServices
from fnos_media_import.agent.tools import build_registry

GOOD_TOKEN = "fnos_agent_good"


class _FakeTokenStore:
    def __init__(self, *, scopes: tuple[str, ...] = ("search",), rate_limit: int = 0) -> None:
        self.scopes = frozenset(scopes)
        self.rate_limit = rate_limit
        self.touched: list[int] = []

    def verify(self, plaintext: str) -> AgentTokenRecord | None:
        if plaintext != GOOD_TOKEN:
            return None
        return AgentTokenRecord(
            id=7,
            name="hermes",
            scopes=self.scopes,
            rate_limit=self.rate_limit,
            expires_at="",
            revoked_at="",
            last_used_at="",
        )

    def touch(self, token_id: int) -> None:
        self.touched.append(int(token_id))


def _services() -> AgentServices:
    return AgentServices(
        database=None,
        search=lambda keyword, *, sources, refresh: {
            "items": [{"public_id": "p1", "title": keyword}],
            "expires_in_minutes": 60,
        },
        resource_detail=lambda public_id: ({"success": True, "detail": {"public_id": public_id}}, 200),
        resource_files=lambda public_id, fid: ({"success": True, "items": []}, 200),
        detect_link=lambda url, password: {"provider_kind": "quark", "supported": True, "url": url},
        tmdb_search=lambda keyword: {"status": "empty", "items": []},
        list_jobs=lambda **kwargs: {"items": [], "total": 0},
        job_detail=lambda job_id: ({"success": True, "job": {"id": job_id}}, 200),
        list_requests=lambda **kwargs: {"items": [], "total": 0},
        request_detail=lambda request_id: ({"success": True, "request": {"id": request_id}}, 200),
        list_organizer_tasks=lambda **kwargs: {"items": [], "total": 0, "status": {}},
        organizer_task_detail=lambda task_id: ({"success": True, "task": {"id": task_id}, "status": {}}, 200),
        list_subscriptions=lambda **kwargs: {"items": [], "total": 0},
        subscription_detail=lambda subscription_id: ({"success": True, "item": {"id": subscription_id}}, 200),
        scheduler_status=lambda: {"enabled": True},
        system_status=lambda: {"ok": True},
        categories=lambda: {"movie": {"label": "电影"}},
    )


def _client(store: _FakeTokenStore | None = None, *, max_concurrency: int = 2) -> tuple[Flask, _FakeTokenStore]:
    services = _services()
    fake_store = store or _FakeTokenStore()
    app = Flask(__name__)
    app.register_blueprint(
        create_agent_blueprint(
            registry=build_registry(services),
            services=services,
            token_store=fake_store,  # type: ignore[arg-type]
            audit=None,
            enabled=True,
            max_concurrency=max_concurrency,
        )
    )
    app.config.update(TESTING=True)
    return app, fake_store


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {GOOD_TOKEN}"}


class AgentMcpBlueprintTests(unittest.TestCase):
    def test_missing_token_is_unauthorized(self) -> None:
        app, _ = _client()
        response = app.test_client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], protocol.UNAUTHORIZED)

    def test_bad_token_is_unauthorized(self) -> None:
        app, _ = _client()
        response = app.test_client().post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer nope"},
        )
        self.assertEqual(response.status_code, 401)

    def test_ping_with_valid_token(self) -> None:
        app, _ = _client()
        response = app.test_client().post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"], {})

    def test_tools_call_marks_token_as_used(self) -> None:
        app, store = _client(_FakeTokenStore(scopes=("search",)))
        response = app.test_client().post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_resources", "arguments": {"keyword": "示例"}},
            },
            headers=_auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["result"]["isError"])
        self.assertEqual(store.touched, [7])

    def test_notification_returns_202_without_body(self) -> None:
        app, _ = _client()
        response = app.test_client().post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_auth(),
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, b"")

    def test_batch_request_returns_array(self) -> None:
        app, _ = _client()
        response = app.test_client().post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            ],
            headers=_auth(),
        )
        body = response.get_json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 2)

    def test_rate_limit_returns_429(self) -> None:
        app, _ = _client(_FakeTokenStore(scopes=("search",), rate_limit=1))
        client = app.test_client()
        first = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_auth())
        second = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers=_auth())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json()["error"]["code"], protocol.RATE_LIMITED)
        self.assertIn("Retry-After", second.headers)

    def test_invalid_json_body_returns_parse_error(self) -> None:
        app, _ = _client()
        response = app.test_client().post(
            "/mcp", data="{not json", headers={**_auth(), "Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], protocol.PARSE_ERROR)

    def test_get_reports_capabilities(self) -> None:
        app, _ = _client()
        body = app.test_client().get("/mcp").get_json()
        self.assertEqual(body["transport"], "http-post")
        self.assertEqual(body["toolCount"], 35)

    def test_delete_is_accepted_for_stateless_transport(self) -> None:
        app, _ = _client()
        self.assertEqual(app.test_client().delete("/mcp").status_code, 204)

    def test_scope_denied_returns_tool_error(self) -> None:
        app, _ = _client(_FakeTokenStore(scopes=("search",)))
        response = app.test_client().post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_jobs", "arguments": {}},
            },
            headers=_auth(),
        )
        payload = response.get_json()["result"]
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["structuredContent"]["code"], "SCOPE_DENIED")


if __name__ == "__main__":
    unittest.main()
