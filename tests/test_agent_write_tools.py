from __future__ import annotations

import unittest
from dataclasses import replace

from fnos_media_import.agent.auth import AgentTokenRecord
from fnos_media_import.agent.mcp import protocol
from fnos_media_import.agent.mcp.handlers import McpHandler
from fnos_media_import.agent.plans import organizer_plan_hash, organizer_plan_summary
from fnos_media_import.agent.services import AgentServices, AgentWriteServices
from fnos_media_import.agent.tools import build_registry

WRITE_SCOPES = (
    "search",
    "import.read",
    "import.write",
    "organize.read",
    "organize.write",
    "schedule.read",
    "schedule.write",
    "ops.read",
)
APPLY_SCOPES = (*WRITE_SCOPES, "organize.apply")

TASK = {
    "id": 3,
    "title": "示例剧集",
    "status": "ready",
    "category": "tv",
    "revision": 4,
    "confidence": 92,
    "openlist_root_path": "/电视剧/示例剧集",
    "mappings": [
        {
            "id": 11,
            "source_path": "/剧集/原原名.E01.mkv",
            "source_name": "原原名.E01.mkv",
            "target_path": "/电视剧/示例剧集/Season 1/示例剧集 - S01E01.mkv",
            "target_name": "示例剧集 - S01E01.mkv",
            "status": "ready",
        }
    ],
}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __call__(self, name: str, result):
        def _inner(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result(args, kwargs) if callable(result) else result

        return _inner


def _write_services(recorder: _Recorder, *, task: dict | None = None) -> AgentWriteServices:
    read_task = task or TASK

    return AgentWriteServices(
        # Contract of the bridge: the visitor-safe submit response is enriched
        # with request_id / job_id / request_status before the tool sees it.
        submit_import=recorder("submit_import", lambda a, k: ({"success": True, "message": "已提交", "request_token": "rt1", "status": "处理中", "request_id": 5, "job_id": 9, "request_status": "submitted", "title": "示例", "category": "tv"}, 200)),
        preview_import=recorder("preview_import", lambda a, k: ({"success": True, "dry_run": True, "public_id": "p1", "title": "示例", "category": "tv", "provider_kind": "quark", "supported": True, "scoped_selection": False, "message": "预检通过"}, 200)),
        approve_request=recorder("approve_request", ({"success": True, "message": "已批准", "job": {"id": 9, "status": "running"}}, 200)),
        reject_request=recorder("reject_request", ({"success": True, "message": "已拒绝提交"}, 200)),
        retry_job=recorder("retry_job", ({"success": True, "message": "已重试"}, 200)),
        cancel_job=recorder("cancel_job", ({"success": True, "message": "已取消任务", "job": {"status": "cancelled"}}, 200)),
        review_job=recorder("review_job", ({"success": True, "message": "已重新提交", "queued": True}, 200)),
        update_organizer_mappings=recorder("update_mappings", ({"success": True, "message": "已更新", "task": read_task}, 200)),
        apply_organizer_task=recorder(
            "apply_organizer",
            lambda a, k: ({"success": True, "message": "整理已提交", "task": read_task}, 200),
        ),
        retry_organizer_task=recorder("retry_org", ({"success": True, "message": "已重试整理"}, 200)),
        rollback_organizer_run=recorder("rollback", ({"success": True, "message": "已回滚"}, 200)),
        create_subscription=recorder("create_sub", lambda a, k: ({"success": True, "item": {"id": 7, "title": "示例", "status": "enabled", "_created": True}}, 200)),
        preview_subscription=recorder("preview_sub", lambda a, k: ({"success": True, "title": "示例", "category": "tv", "media_type": "tv", "schedule_kind": "tmdb", "next_run_at": "2026-11-04T10:00:00+08:00"}, 200)),
        update_subscription=recorder("update_sub", lambda a, k: ({"success": True, "item": {"id": 7, "title": "示例", "status": "enabled"}}, 200)),
        set_subscription_status=recorder("set_status", lambda a, k: ({"success": True, "item": {"id": 7, "status": "paused"}, "message": "已暂停追更"}, 200)),
        run_subscription=recorder("run_sub", lambda a, k: ({"success": True, "run_id": 21, "status": "running"}, 200)),
        delete_subscription=recorder("delete_sub", lambda a, k: ({"success": True, "message": "已删除订阅"}, 200)),
        list_media_libraries=recorder("media_libs", {"success": True, "items": [{"row_key": "movie", "title": "电影", "resource_count": 12}], "running": [], "summary": {}}),
        search_media_library=recorder("media_search", {"success": True, "keyword": "繁花", "items": [{"title": "繁花", "type": "TV", "in_library": True, "local_episodes": 30}], "count": 1, "total": 1}),
        refresh_media_library=recorder("media_refresh", lambda a, k: ({"success": True, "message": "已触发刷新"}, 200)),
        list_trending_candidates=recorder("trending_list", {"items": [{"id": 31, "title": "热门剧", "status": "discovered"}], "total": 1}),
        # Real service shape: the created job is nested under "job".
        import_trending_candidate=recorder("trending_import", lambda a, k: ({"success": True, "created": True, "message": "已提交", "candidate": {"id": 31}, "job": {"id": 44, "status": "submitted"}}, 200)),
        # Real service shape: subscription id is top-level, item is the candidate.
        subscribe_trending_candidate=recorder("trending_sub", lambda a, k: ({"success": True, "created": True, "message": "已转追更", "subscription_id": 8}, 200)),
    )


def _services(recorder: _Recorder, *, task: dict | None = None) -> AgentServices:
    read_task = task or TASK
    return AgentServices(
        database=None,
        search=lambda keyword, *, sources, refresh: {"items": []},
        resource_detail=lambda public_id: ({"success": True, "detail": {"public_id": public_id}}, 200),
        resource_files=lambda public_id, fid: ({"success": True, "items": []}, 200),
        detect_link=lambda url, password: {"provider_kind": "quark", "supported": True, "url": url},
        tmdb_search=lambda keyword: {"status": "empty", "items": []},
        list_jobs=lambda **kwargs: {"items": [], "total": 0},
        job_detail=lambda job_id: ({"success": True, "job": {"id": job_id, "status": "review", "row_version": 4}}, 200),
        list_requests=lambda **kwargs: {"items": [], "total": 0},
        request_detail=lambda request_id: ({"success": True, "request": {"id": request_id}}, 200),
        list_organizer_tasks=lambda **kwargs: {"items": [], "total": 0, "status": {}},
        organizer_task_detail=lambda task_id: ({"success": True, "task": read_task, "status": {}}, 200),
        list_subscriptions=lambda **kwargs: {"items": [], "total": 0},
        subscription_detail=lambda subscription_id: ({"success": True, "item": {"id": subscription_id}}, 200),
        scheduler_status=lambda: {"enabled": True},
        system_status=lambda: {"ok": True},
        categories=lambda: {"tv": {"label": "剧集"}},
        writes=_write_services(recorder, task=task),
    )


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


def _call(handler: McpHandler, tool: str, arguments: dict, scopes: tuple[str, ...] = WRITE_SCOPES) -> dict:
    response = handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        _token(scopes),
    )
    assert response is not None
    return response


def _ok(handler: McpHandler, tool: str, arguments: dict, scopes: tuple[str, ...] = WRITE_SCOPES) -> dict:
    payload = _call(handler, tool, arguments, scopes)["result"]
    assert not payload["isError"], payload["structuredContent"]
    return payload["structuredContent"]


def _err(handler: McpHandler, tool: str, arguments: dict, scopes: tuple[str, ...] = WRITE_SCOPES) -> dict:
    payload = _call(handler, tool, arguments, scopes)["result"]
    assert payload["isError"], payload["structuredContent"]
    return payload["structuredContent"]


def _new_handler() -> tuple[McpHandler, _Recorder]:
    recorder = _Recorder()
    services = _services(recorder)
    handler = McpHandler(
        registry=build_registry(services),
        services=services,
        token_store=_NoopStore(),
        audit=None,
        enabled=True,
    )
    return handler, recorder


class AgentPlanHashTests(unittest.TestCase):
    def test_hash_changes_when_target_changes(self) -> None:
        import copy

        original = organizer_plan_hash(TASK)
        changed = copy.deepcopy(TASK)
        changed["mappings"][0]["target_path"] = "/电视剧/示例剧集/Season 1/别的名字.mkv"
        self.assertNotEqual(original, organizer_plan_hash(changed))

    def test_hash_is_stable_for_unrelated_fields(self) -> None:
        import copy

        original = organizer_plan_hash(TASK)
        changed = copy.deepcopy(TASK)
        changed["title"] = "改了任务标题"
        self.assertEqual(original, organizer_plan_hash(changed))

    def test_summary_distinguishes_move_rename_and_delete(self) -> None:
        summary = organizer_plan_summary(TASK)
        self.assertEqual(summary["mapping_count"], 1)
        # Different directory + different name is reported as a move.
        self.assertEqual(summary["move_count"], 1)
        self.assertEqual(summary["rename_count"], 0)
        self.assertEqual(summary["delete_count"], 0)
        self.assertEqual(summary["targets"][0]["action"], "move")
        self.assertEqual(summary["plan_hash"], organizer_plan_hash(TASK))

    def test_summary_counts_rename_in_place(self) -> None:
        task = {
            "id": 4,
            "status": "waiting_review",
            "category": "tv",
            "revision": 1,
            "openlist_root_path": "/剧集",
            "mappings": [
                {
                    "id": 1,
                    "status": "ready",
                    "source_path": "/剧集/旧名.E01.mkv",
                    "target_path": "/剧集/新名.S01E01.mkv",
                }
            ],
        }
        summary = organizer_plan_summary(task)
        self.assertEqual(summary["rename_count"], 1)
        self.assertEqual(summary["move_count"], 0)

    def test_summary_counts_delete_ad(self) -> None:
        task = {
            "id": 5,
            "status": "waiting_review",
            "category": "tv",
            "revision": 1,
            "openlist_root_path": "/剧集",
            "mappings": [
                {
                    "id": 1,
                    "status": "delete_ad",
                    "source_path": "/剧集/广告.mp4",
                    "target_path": "/剧集/广告.mp4",
                }
            ],
        }
        summary = organizer_plan_summary(task)
        self.assertEqual(summary["delete_count"], 0)
        self.assertEqual(summary["legacy_delete_count"], 1)
        self.assertEqual(summary["targets"][0]["action"], "invalid")
        self.assertEqual(summary["rename_count"], 0)
        self.assertEqual(summary["move_count"], 0)

    def test_summary_counts_blocked_mappings(self) -> None:
        task = {
            "id": 6,
            "status": "waiting_review",
            "category": "tv",
            "revision": 1,
            "mappings": [{"id": 1, "status": "need_edit", "source_path": "/a", "target_path": "/a"}],
        }
        self.assertEqual(organizer_plan_summary(task)["blocked_count"], 1)


class AgentWriteToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = _Recorder()
        self.services = _services(self.recorder)
        self.handler = McpHandler(
            registry=build_registry(self.services),
            services=self.services,
            token_store=_NoopStore(),
            audit=None,
            enabled=True,
        )

    def test_submit_import_dry_run_does_not_submit(self) -> None:
        body = _ok(self.handler, "submit_import", {"public_id": "p1", "category": "tv", "dry_run": True})
        self.assertTrue(body["dry_run"])
        names = [call[0] for call in self.recorder.calls]
        self.assertIn("preview_import", names)
        self.assertNotIn("submit_import", names)

    def test_submit_import_requires_resource_reference(self) -> None:
        error = _err(self.handler, "submit_import", {"category": "tv"})
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_submit_import_returns_job_and_approval_hint(self) -> None:
        body = _ok(self.handler, "submit_import", {"public_id": "p1", "category": "tv"})
        self.assertEqual(body["request_token"], "rt1")
        self.assertEqual(body["job_id"], 9)
        self.assertFalse(body["approve_required"])

    def test_reject_requires_reason(self) -> None:
        error = _err(self.handler, "reject_import_request", {"request_id": 5})
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_cancel_job_requires_confirm(self) -> None:
        error = _err(self.handler, "cancel_job", {"job_id": 9, "reason": "不要了"})
        self.assertEqual(error["code"], "CONFIRMATION_REQUIRED")
        self.assertFalse([call for call in self.recorder.calls if call[0] == "cancel_job"])

    def test_cancel_job_passes_cleanup_false_by_default(self) -> None:
        _ok(self.handler, "cancel_job", {"job_id": 9, "reason": "不要了", "confirm": True})
        calls = [call for call in self.recorder.calls if call[0] == "cancel_job"]
        self.assertEqual(len(calls), 1)
        payload = calls[0][1][1]
        self.assertFalse(payload["cleanup"])

    def test_confirm_not_submitted_requires_row_version_and_note(self) -> None:
        error = _err(self.handler, "confirm_job_not_submitted", {"job_id": 9, "confirm": True})
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_confirm_not_submitted_sends_expected_payload(self) -> None:
        _ok(
            self.handler,
            "confirm_job_not_submitted",
            {"job_id": 9, "row_version": 4, "note": "已核对网盘无本次文件", "confirm": True},
        )
        calls = [call for call in self.recorder.calls if call[0] == "review_job"]
        self.assertEqual(len(calls), 1)
        payload = calls[0][1][1]
        self.assertEqual(payload["action"], "confirm_not_submitted")
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["expected_row_version"], 4)

    def test_apply_requires_apply_scope(self) -> None:
        # Moving files is gated by the token profile (full), not by a per-task grant.
        result = _call(self.handler, "apply_organizer_task", {"task_id": 3}, scopes=WRITE_SCOPES)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "SCOPE_DENIED")

    def test_apply_first_call_returns_plan_without_executing(self) -> None:
        body = _ok(self.handler, "apply_organizer_task", {"task_id": 3}, scopes=APPLY_SCOPES)
        self.assertTrue(body["confirmation_required"])
        self.assertFalse(body["executed"])
        self.assertEqual(body["plan"]["plan_hash"], organizer_plan_hash(TASK))
        self.assertNotIn("apply_organizer_task", [call[0] for call in self.recorder.calls])

    def test_apply_rejects_stale_plan_hash(self) -> None:
        error = _err(
            self.handler,
            "apply_organizer_task",
            {"task_id": 3, "plan_hash": "stale"},
            scopes=APPLY_SCOPES,
        )
        self.assertEqual(error["code"], "PLAN_CHANGED")
        self.assertIn("current_plan_hash", error["details"])

    def test_apply_runs_when_plan_hash_matches(self) -> None:
        from tests.test_agent_identity_plans import task as identity_task

        current = {**identity_task(), "id": 3, "revision": TASK["revision"]}
        recorder = _Recorder()
        services = _services(recorder, task=current)
        handler = McpHandler(registry=build_registry(services), services=services, token_store=_NoopStore(), audit=None, enabled=True)
        plan_hash = organizer_plan_hash(current)
        body = _ok(
            handler,
            "apply_organizer_task",
            {"task_id": 3, "plan_hash": plan_hash},
            scopes=APPLY_SCOPES,
        )
        self.assertTrue(body["success"])
        self.assertEqual(body["plan_hash"], plan_hash)
        apply_calls = [call for call in recorder.calls if call[0] == "apply_organizer"]
        self.assertEqual(len(apply_calls), 1)
        # The previewed revision must reach the approval transaction.
        self.assertEqual(apply_calls[0][1], (3, current["revision"], plan_hash))

    def test_rollback_requires_confirm(self) -> None:
        error = _err(self.handler, "rollback_organizer_run", {"run_id": 2}, scopes=APPLY_SCOPES)
        self.assertEqual(error["code"], "CONFIRMATION_REQUIRED")

    def test_batch_mapping_only_accepts_title_and_season(self) -> None:
        error = _err(
            self.handler,
            "update_organizer_mappings",
            {"task_id": 3, "target_name": "x.mkv"},
        )
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_batch_mapping_returns_new_plan_hash(self) -> None:
        body = _ok(self.handler, "update_organizer_mappings", {"task_id": 3, "title": "新片名"})
        self.assertEqual(body["plan"]["plan_hash"], organizer_plan_hash(TASK))

    def test_create_subscription_requires_tmdb_id(self) -> None:
        error = _err(self.handler, "create_subscription", {"category": "tv"})
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_create_subscription_dry_run_does_not_create(self) -> None:
        body = _ok(self.handler, "create_subscription", {"tmdb_id": 550, "category": "tv", "dry_run": True})
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["next_run_at"], "2026-11-04T10:00:00+08:00")
        names = [call[0] for call in self.recorder.calls]
        self.assertIn("preview_sub", names)
        self.assertNotIn("create_sub", names)

    def test_create_subscription_rejects_unknown_schedule_kind(self) -> None:
        error = _err(
            self.handler,
            "create_subscription",
            {"tmdb_id": 550, "category": "tv", "schedule_kind": "hourly"},
        )
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_set_subscription_status_validates_enum(self) -> None:
        error = _err(
            self.handler,
            "set_subscription_status",
            {"subscription_id": 7, "status": "deleted"},
        )
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_run_subscription_now(self) -> None:
        body = _ok(self.handler, "run_subscription_now", {"subscription_id": 7})
        self.assertEqual(body["run_id"], 21)

    def test_write_tools_hidden_without_write_scopes(self) -> None:
        response = self.handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            _token(("search",)),
        )
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertNotIn("submit_import", names)
        self.assertNotIn("apply_organizer_task", names)

    def test_delete_subscription_requires_confirm(self) -> None:
        error = _err(self.handler, "delete_subscription", {"subscription_id": 7})
        self.assertEqual(error["code"], "CONFIRMATION_REQUIRED")
        self.assertNotIn("delete_sub", [call[0] for call in self.recorder.calls])

    def test_delete_subscription_after_confirm(self) -> None:
        body = _ok(self.handler, "delete_subscription", {"subscription_id": 7, "confirm": True})
        self.assertTrue(body["success"])
        self.assertEqual(body["subscription_id"], 7)

    def test_list_media_libraries_is_readable(self) -> None:
        body = _ok(self.handler, "list_media_libraries", {}, scopes=("ops.read",))
        self.assertEqual(body["libraries"][0]["title"], "电影")

    def test_search_library_reports_imported(self) -> None:
        body = _ok(self.handler, "search_library", {"keyword": "繁花"}, scopes=("ops.read",))
        self.assertTrue(body["in_library"])
        self.assertEqual(body["in_library_count"], 1)
        self.assertEqual(body["items"][0]["local_episodes"], 30)

    def test_search_library_maps_failure_reasons(self) -> None:
        cases = {
            "NOT_CONFIGURED": "LIBRARY_NOT_CONFIGURED",
            "LOGIN_FAILED": "LIBRARY_AUTH_FAILED",
            "UPSTREAM_ERROR": "LIBRARY_UNAVAILABLE",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                def fake_library(keyword, limit, value=reason):
                    return {
                        "success": False,
                        "code": value,
                        "message": f"失败原因-{value}",
                        "upstream_code": -2,
                        "items": [],
                    }

                services = replace(
                    _services(_Recorder()),
                    writes=replace(_write_services(_Recorder()), search_media_library=fake_library),
                )
                handler = McpHandler(
                    registry=build_registry(services),
                    services=services,
                    token_store=_NoopStore(),
                    audit=None,
                    enabled=True,
                )
                body = _call(handler, "search_library", {"keyword": "x"}, scopes=("ops.read",))
                error = body["result"]["structuredContent"]
                self.assertEqual(error["code"], expected)
                self.assertIn(f"失败原因-{reason}", error["message"])
                self.assertIn("hint", error)

    def test_refresh_media_library_rejects_unknown_category(self) -> None:
        error = _err(self.handler, "refresh_media_library", {"category": "nope"})
        self.assertEqual(error["code"], "INVALID_PARAMS")

    def test_refresh_media_library_triggers(self) -> None:
        body = _ok(self.handler, "refresh_media_library", {"category": "tv"})
        self.assertTrue(body["success"])

    def test_list_trending_candidates(self) -> None:
        body = _ok(self.handler, "list_trending_candidates", {}, scopes=("schedule.read",))
        self.assertEqual(body["items"][0]["id"], 31)
        self.assertIn("pagination", body)

    def test_import_trending_candidate(self) -> None:
        body = _ok(
            self.handler,
            "import_trending_candidate",
            {"candidate_id": 31, "public_id": "p9", "category": "tv"},
        )
        self.assertEqual(body["job_id"], 44)

    def test_subscribe_trending_candidate(self) -> None:
        body = _ok(self.handler, "subscribe_trending_candidate", {"candidate_id": 31})
        self.assertEqual(body["subscription_id"], 8)


class _NoopStore:
    def touch(self, token_id: int) -> None:
        return


if __name__ == "__main__":
    unittest.main()
