from __future__ import annotations

import unittest
from dataclasses import replace

from fnos_media_import.agent.auth import AgentTokenRecord
from fnos_media_import.agent.mcp.handlers import McpHandler
from fnos_media_import.agent.projection import compact_job
from fnos_media_import.agent.services import AgentServices
from fnos_media_import.agent.tools import build_registry
from fnos_media_import.agent.tools.scheduling_write import _subscription_payload
from fnos_media_import.app import _agent_submission_result
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)

REAL_EXECUTION = {
    "phase": "manual_review",
    "strategy": "native",
    "attempts": 2,
    "row_version": 7,
    "staging_verified": False,
    "final_verified": False,
    "app_acked": False,
    "organizer_task_id": 3,
    "last_error": {"code": "receipt_missing", "message": "缺少回执"},
    "receipt": None,
    "review": {
        "required": True,
        "reason": "receipt_missing",
        "resume_phase": "native_submitting",
        "can_resume": False,
        "can_confirm_not_submitted": True,
        "guidance": "请先在网盘中核对本任务的来源暂存目录",
        "paths": [
            {"label": "来源暂存目录", "namespace": "MP", "path": "/_入库暂存/demo"},
        ],
        "internal_debug": "should be dropped",
    },
    "internal_locator": "should be dropped",
}


def _token(scopes: tuple[str, ...]) -> AgentTokenRecord:
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
        search=lambda keyword, *, sources, refresh: {"items": []},
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
        categories=lambda: {"tv": {"label": "剧集"}},
    )


class SubmissionBridgeTests(unittest.TestCase):
    def test_enriches_ids_from_persisted_request(self) -> None:
        body = {"success": True, "request_token": "rt1", "status": "等待处理"}
        result = _agent_submission_result(
            body,
            {"id": 12, "job_id": 34, "status": "review", "title": "示例", "category": "tv"},
        )
        self.assertEqual(result["request_id"], 12)
        self.assertEqual(result["job_id"], 34)
        self.assertEqual(result["request_status"], "review")

    def test_missing_row_leaves_body_untouched(self) -> None:
        body = {"success": True, "request_token": "rt1"}
        result = _agent_submission_result(body, None)
        self.assertNotIn("request_id", result)
        self.assertNotIn("job_id", result)


class JobProjectionTests(unittest.TestCase):
    def test_keeps_transfer_execution_fields_needed_for_review(self) -> None:
        job = compact_job({"id": 1, "status": "review", "transfer_execution": REAL_EXECUTION})
        execution = job["transfer_execution"]
        self.assertEqual(execution["row_version"], 7)
        self.assertEqual(execution["phase"], "manual_review")
        self.assertTrue(execution["review"]["can_confirm_not_submitted"])
        self.assertEqual(execution["review"]["paths"][0]["path"], "/_入库暂存/demo")
        self.assertNotIn("internal_locator", execution)
        self.assertNotIn("internal_debug", execution["review"])

    def test_job_without_execution_is_unchanged(self) -> None:
        job = compact_job({"id": 2, "status": "done"})
        self.assertNotIn("transfer_execution", job)


class ResourceScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = _services()
        self.handler = McpHandler(
            registry=build_registry(self.services),
            services=self.services,
            token_store=_NoopStore(),
            audit=None,
            enabled=True,
        )

    def _read(self, uri: str, scopes: tuple[str, ...]) -> dict:
        response = self.handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
            _token(scopes),
        )
        return response

    def test_status_resource_requires_ops_read(self) -> None:
        response = self._read("fnos://status/system", ("search",))
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["data"]["code"], "SCOPE_DENIED")

    def test_status_resource_allowed_with_ops_read(self) -> None:
        response = self._read("fnos://status/system", ("ops.read",))
        self.assertIn("ok", response["result"]["contents"][0]["text"])

    def test_categories_requires_search(self) -> None:
        response = self._read("fnos://config/categories", ("ops.read",))
        self.assertEqual(response["error"]["data"]["code"], "SCOPE_DENIED")

    def test_resources_list_is_filtered_by_scope(self) -> None:
        response = self.handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            _token(("search",)),
        )
        uris = {item["uri"] for item in response["result"]["resources"]}
        self.assertEqual(uris, {"fnos://config/categories"})


class SubscriptionPayloadTests(unittest.TestCase):
    def test_explicit_empty_list_clears_filters(self) -> None:
        payload = _subscription_payload(
            {"subscription_id": 1, "include_keywords": [], "quality_profile": ""},
            include_identity=False,
        )
        self.assertEqual(payload["include_keywords"], [])
        self.assertEqual(payload["quality_profile"], "")

    def test_absent_keys_are_not_sent(self) -> None:
        payload = _subscription_payload({"subscription_id": 1}, include_identity=False)
        self.assertEqual(payload, {})

    def test_create_skips_empty_values(self) -> None:
        payload = _subscription_payload(
            {"tmdb_id": 550, "category": "tv", "include_keywords": []},
            include_identity=True,
        )
        self.assertNotIn("include_keywords", payload)
        self.assertEqual(payload["tmdb_id"], 550)


class SelectionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = build_registry(_services())
        self.tools = {tool.name: tool for tool in registry.all()}

    def test_submit_import_schema_documents_selection_structure(self) -> None:
        schema = self.tools["submit_import"].input_schema["properties"]
        quark = schema["quark_selection"]
        self.assertEqual(quark["type"], "object")
        self.assertIn("mode", quark["properties"])
        self.assertIn("selected_dirs", quark["properties"])
        entry = quark["properties"]["selected_dirs"]["items"]
        self.assertIn("fid", entry["properties"])
        self.assertIn("fid", entry["required"])

    def test_cloud139_and_tianyi_selection_have_items(self) -> None:
        schema = self.tools["submit_import"].input_schema["properties"]
        for name in ("cloud139_selection", "tianyi_selection"):
            self.assertIn("selected_files", schema[name]["properties"])
            self.assertIn("selected_folders", schema[name]["properties"])

    def test_subscription_sources_schema_is_structured(self) -> None:
        schema = self.tools["create_subscription"].input_schema["properties"]
        source = schema["sources"]["items"]
        self.assertIn("type", source["properties"])
        self.assertIn("type", source["required"])


class ApplyRevisionPassThroughTests(unittest.TestCase):
    def test_apply_forwards_expected_revision_to_approve(self) -> None:
        calls: list[tuple[int, int | None]] = []

        class FakeOrganizer:
            def get_task(self, task_id: int) -> dict:
                return {"id": task_id, "status": "waiting_review", "revision": 9}

            def approve_task(self, task_id: int, *, expected_revision: int | None = None) -> dict:
                calls.append((task_id, expected_revision))
                return {"success": True}

            def start_apply_task(self, task_id: int, *, expected_revision: int | None = None) -> dict:
                return {"success": True, "task_id": task_id}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(organizer=FakeOrganizer(), worker_dispatcher=None)
        )
        service.apply(5, expected_revision=9)
        self.assertEqual(calls, [(5, 9)])


class _NoopStore:
    def touch(self, token_id: int) -> None:
        return


class ResourceFilePaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        services = replace(
            _services(),
            resource_files=lambda public_id, fid: (
                {
                    "success": True,
                    "items": [{"fid": str(index), "name": f"f{index}.mp4"} for index in range(5)],
                    "message": "目录读取完成",
                },
                200,
            ),
        )
        self.handler = McpHandler(
            registry=build_registry(services),
            services=services,
            token_store=_NoopStore(),
            audit=None,
            enabled=True,
        )

    def test_offset_and_next_offset(self) -> None:
        response = self.handler.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "list_resource_files",
                    "arguments": {"public_id": "p1", "fid": "0", "limit": 2, "offset": 2},
                },
            },
            _token(("search",)),
        )
        body = response["result"]["structuredContent"]
        self.assertEqual([item["fid"] for item in body["items"]], ["2", "3"])
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 4)
        self.assertEqual(body["total"], 5)


if __name__ == "__main__":
    unittest.main()
