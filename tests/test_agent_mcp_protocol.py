from __future__ import annotations

import unittest

from fnos_media_import.agent.auth import AgentTokenRecord
from fnos_media_import.agent.mcp import protocol
from fnos_media_import.agent.mcp.handlers import McpHandler
from fnos_media_import.agent.services import AgentServices
from fnos_media_import.agent.tools import build_registry

ALL_READ_SCOPES = ("search", "import.read", "organize.read", "schedule.read", "ops.read")


class _TouchStore:
    def __init__(self) -> None:
        self.touched: list[int] = []

    def touch(self, token_id: int) -> None:
        self.touched.append(int(token_id))


def _token(scopes: tuple[str, ...] = ALL_READ_SCOPES) -> AgentTokenRecord:
    return AgentTokenRecord(
        id=1,
        name="hermes",
        scopes=frozenset(scopes),
        rate_limit=0,
        expires_at="",
        revoked_at="",
        last_used_at="",
    )


def _services() -> AgentServices:
    return AgentServices(
        database=None,
        search=lambda keyword, *, sources, refresh: {
            "items": [
                {
                    "public_id": "p1",
                    "title": "示例影片",
                    "provider": "pansou",
                    "link": {"provider_kind": "quark", "supported": True},
                }
            ],
            "expires_in_minutes": 60,
        },
        resource_detail=lambda public_id: (
            {
                "success": True,
                "detail": {
                    "public_id": public_id,
                    "title": "示例影片",
                    "inspection": {"items": [{"fid": "7", "name": "第1集.mp4", "is_dir": False}]},
                },
            },
            200,
        ),
        resource_files=lambda public_id, fid: (
            {"success": True, "items": [{"fid": "8", "name": "a.mp4"}], "message": "目录读取完成"},
            200,
        ),
        detect_link=lambda url, password: {"provider_kind": "quark", "supported": True, "url": url},
        tmdb_search=lambda keyword: {
            "status": "ready",
            "items": [{"tmdb_id": 1, "title": keyword, "selection_token": "tok"}],
        },
        list_jobs=lambda **kwargs: {
            "items": [{"id": 1, "title": "示例影片", "status": "completed"}],
            "total": 1,
        },
        job_detail=lambda job_id: (
            {
                "success": True,
                "job": {"id": job_id, "title": "示例影片", "status": "failed", "error_message": "boom"},
                "events": [{"id": 9, "level": "error", "message": "boom", "created_at": "2026-01-01"}],
            },
            200,
        ),
        list_requests=lambda **kwargs: {"items": [], "total": 0},
        request_detail=lambda request_id: ({"success": True, "request": {"id": request_id, "title": "示例影片"}}, 200),
        list_organizer_tasks=lambda **kwargs: {
            "items": [{"id": 3, "title": "示例影片", "status": "awaiting_review"}],
            "total": 1,
            "status": {},
        },
        organizer_task_detail=lambda task_id: (
            {
                "success": True,
                "task": {"id": task_id, "title": "示例影片", "mappings": [{"id": 1, "target_name": "示例 (2024)"}]},
                "status": {},
            },
            200,
        ),
        list_subscriptions=lambda **kwargs: {
            "items": [{"id": 5, "title": "示例影片", "status": "enabled"}],
            "total": 1,
        },
        subscription_detail=lambda subscription_id: (
            {
                "success": True,
                "item": {"id": subscription_id, "title": "示例影片"},
                "runs": [],
                "candidates": [],
                "events": [],
            },
            200,
        ),
        scheduler_status=lambda: {"enabled": True},
        system_status=lambda: {"ok": True},
        categories=lambda: {"movie": {"label": "电影"}},
    )


def _handler(*, scopes: tuple[str, ...] = ALL_READ_SCOPES) -> McpHandler:
    services = _services()
    return McpHandler(
        registry=build_registry(services),
        services=services,
        token_store=_TouchStore(),
        audit=None,
        enabled=True,
    )


def _call(handler: McpHandler, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    response = handler.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        _token(),
    )
    assert response is not None
    return response


class AgentMcpProtocolTests(unittest.TestCase):
    def test_initialize_reports_capabilities(self) -> None:
        result = _call(_handler(), "initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "fnos-search-agent")
        self.assertIn("tools", result["capabilities"])

    def test_unsupported_protocol_version_falls_back(self) -> None:
        result = _call(_handler(), "initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(result["protocolVersion"], protocol.DEFAULT_PROTOCOL_VERSION)

    def test_tools_list_contains_only_read_only_tools_by_default(self) -> None:
        tools = _call(_handler(), "tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertIn("search_resources", names)
        self.assertIn("get_job", names)
        self.assertIn("list_subscriptions", names)
        self.assertEqual(len(tools), 17)
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertIn("inputSchema", tool)

    def test_tools_list_respects_scopes(self) -> None:
        handler = McpHandler(
            registry=build_registry(_services()),
            services=_services(),
            token_store=_TouchStore(),
            enabled=True,
        )
        response = handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            _token(("search",)),
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"search_resources", "get_resource", "list_resource_files", "detect_link", "resolve_tmdb"})

    def test_unknown_method_is_reported(self) -> None:
        response = _call(_handler(), "does/not/exist")
        self.assertEqual(response["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_invalid_envelope_is_reported(self) -> None:
        handler = _handler()
        response = handler.handle({"id": 1, "method": "ping"}, _token())
        self.assertEqual(response["error"]["code"], protocol.INVALID_REQUEST)

    def test_notification_returns_no_response(self) -> None:
        handler = _handler()
        self.assertIsNone(handler.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, _token()))

    def test_ping_round_trip(self) -> None:
        self.assertEqual(_call(_handler(), "ping")["result"], {})

    def test_search_tool_returns_projected_items(self) -> None:
        result = _call(_handler(), "tools/call", {"name": "search_resources", "arguments": {"keyword": "示例"}})
        payload = result["result"]
        self.assertFalse(payload["isError"])
        self.assertEqual(payload["structuredContent"]["items"][0]["public_id"], "p1")
        self.assertIn("text", payload["content"][0])

    def test_missing_argument_is_a_tool_error(self) -> None:
        result = _call(_handler(), "tools/call", {"name": "search_resources", "arguments": {}})
        payload = result["result"]
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["structuredContent"]["code"], "INVALID_PARAMS")

    def test_missing_scope_is_denied(self) -> None:
        handler = _handler()
        response = handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_jobs", "arguments": {}}},
            _token(("search",)),
        )
        payload = response["result"]
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["structuredContent"]["code"], "SCOPE_DENIED")

    def test_unknown_tool_is_invalid_params(self) -> None:
        response = _call(_handler(), "tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(response["error"]["code"], protocol.INVALID_PARAMS)

    def test_job_detail_includes_hint(self) -> None:
        result = _call(_handler(), "tools/call", {"name": "get_job", "arguments": {"job_id": 12}})
        body = result["result"]["structuredContent"]
        self.assertEqual(body["job"]["id"], 12)
        self.assertIn("next", body)

    def test_resources_read_categories(self) -> None:
        response = _call(_handler(), "resources/read", {"uri": "fnos://config/categories"})
        contents = response["result"]["contents"]
        self.assertEqual(contents[0]["uri"], "fnos://config/categories")
        self.assertIn("movie", contents[0]["text"])

    def test_prompt_requires_title(self) -> None:
        response = _call(_handler(), "prompts/get", {"name": "ingest_by_title", "arguments": {}})
        self.assertEqual(response["error"]["code"], protocol.INVALID_PARAMS)

    def test_prompt_ingest_contains_workflow(self) -> None:
        response = _call(_handler(), "prompts/get", {"name": "ingest_by_title", "arguments": {"title": "示例"}})
        text = response["result"]["messages"][0]["content"]["text"]
        self.assertIn("search_resources", text)
        self.assertIn("submit_import", text)


if __name__ == "__main__":
    unittest.main()
