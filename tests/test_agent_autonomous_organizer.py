from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from fnos_media_import.agent.auth import AgentTokenRecord
from fnos_media_import.agent.mcp.handlers import McpHandler
from fnos_media_import.agent.plans import organizer_plan_summary
from fnos_media_import.agent.registry import ToolRegistry
from fnos_media_import.agent.tools import organizer_write


ROOT = "/library/tv/Example (2026) [tmdb-123]"
IDENTITY = {"media_type": "tv", "ns": "tmdb", "value": "123", "title": "Example", "year": 2026}
TASK = {
    "id": 3, "category": "tv", "status": "waiting_review", "revision": 7,
    "openlist_root_path": "/staging/job-3",
    "mappings": [{"id": 11, "source_path": "/staging/job-3/01.mkv", "target_path": ROOT + "/Season 01/Example (2026) - S01E01.mkv",
        "status": "ready", "media_type": "tv", "season": 1, "episode": 1,
        "raw_data": {"organizer_identity": {"version": 1, "scope_root": "/library/tv", "canonical_dir": ROOT,
            "intent": "create_or_match", "manifest": {"version": 1, "identity": IDENTITY, "aliases": []}, "errors": []}}}],
    "operations": [{"id": 5, "type": "move_file", "source_path": "/staging/job-3/01.mkv",
        "target_path": ROOT + "/Season 01/Example (2026) - S01E01.mkv", "status": "pending"}],
}


class AgentAutonomousOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.task = copy.deepcopy(TASK)
        self.calls = []
        self.reads = []
        self.registry = ToolRegistry()
        organizer_write.register(self.registry)
        self.writes = SimpleNamespace(
            apply_organizer_task=self.record("apply"),
            retry_organizer_task=self.record("retry"),
            rollback_organizer_run=self.record("rollback"),
            update_organizer_mappings=self.record("update"),
        )
        self.handler = McpHandler(registry=self.registry,
            services=SimpleNamespace(writes=self.writes, organizer_task_detail=self.detail),
            token_store=SimpleNamespace(touch=lambda _id: None), audit=None, enabled=True)

    def record(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True, "task": self.task, "task_id": 3}, 200
        return call

    def detail(self, task_id):
        self.reads.append(task_id)
        return {"success": True, "task": self.task}, 200

    def call(self, name, args, scopes=("organize.write", "organize.apply")):
        token = AgentTokenRecord(id=1, name="rules-agent", scopes=frozenset(scopes), rate_limit=0,
            expires_at="", revoked_at="", last_used_at="")
        response = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}, token)
        self.assertIsNotNone(response)
        return response["result"]

    def ok(self, name, args, **kwargs):
        result = self.call(name, args, **kwargs)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def error(self, name, args, code, **kwargs):
        result = self.call(name, args, **kwargs)
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["code"], code)
        return result["structuredContent"]

    def test_apply_execute_true_runs_once_with_current_snapshot_hash_without_prior_preview(self):
        expected = organizer_plan_summary(self.task)
        result = self.ok("apply_organizer_task", {"task_id": 3, "execute": True})
        self.assertTrue(result["success"])
        self.assertEqual(self.reads, [3])
        self.assertEqual(self.calls, [("apply", (3, 7, expected["plan_hash"]), {})])
        self.assertNotIn("confirmation_required", result)

    def test_explicit_stale_hash_still_blocks_autonomous_apply(self):
        previous = organizer_plan_summary(self.task)["plan_hash"]
        self.task["revision"] += 1
        result = self.error("apply_organizer_task", {"task_id": 3, "execute": True, "plan_hash": previous}, "PLAN_CHANGED")
        self.assertFalse(result["retryable"])
        self.assertEqual(self.calls, [])

    def test_explicit_execute_false_never_writes_even_with_a_valid_hash(self):
        current = organizer_plan_summary(self.task)["plan_hash"]
        result = self.ok("apply_organizer_task", {"task_id": 3, "execute": False, "plan_hash": current})
        self.assertFalse(result["executed"])
        self.assertEqual(self.calls, [])

    def test_default_and_execute_false_remain_preview_without_human_gate(self):
        for extra in ({}, {"execute": False}):
            with self.subTest(extra=extra):
                preview = self.ok("apply_organizer_task", {"task_id": 3, **extra})
                self.assertFalse(preview["executed"])
                self.assertFalse(preview["human_confirmation_required"])
                self.assertIn("execute=true", preview["instruction"])
        self.assertEqual(self.calls, [])

    def test_legacy_hash_only_execution_is_preserved(self):
        plan = organizer_plan_summary(self.task)
        self.ok("apply_organizer_task", {"task_id": 3, "plan_hash": plan["plan_hash"]})
        self.assertEqual(self.calls, [("apply", (3, 7, plan["plan_hash"]), {})])

    def test_operator_without_apply_scope_cannot_execute_apply_or_retry(self):
        for name in ("apply_organizer_task", "retry_organizer_task"):
            with self.subTest(name=name):
                self.error(name, {"task_id": 3, "execute": True}, "SCOPE_DENIED", scopes=("organize.write",))
        self.assertEqual(self.calls, [])

    def test_retry_defaults_to_one_argument_rescan_and_execute_requires_explicit_second_argument(self):
        default = self.ok("retry_organizer_task", {"task_id": 3}, scopes=("organize.write",))
        explicit_preview = self.ok("retry_organizer_task", {"task_id": 3, "execute": False}, scopes=("organize.write",))
        execute = self.ok("retry_organizer_task", {"task_id": 3, "execute": True})
        self.assertEqual(default["mode"], "rescan_only")
        self.assertEqual(explicit_preview["mode"], "rescan_only")
        self.assertEqual(execute["mode"], "autonomous_retry")
        self.assertEqual(self.calls, [("retry", (3,), {}), ("retry", (3,), {}), ("retry", (3, True), {})])

    def test_autonomous_apply_does_not_promote_unknown_mapping_to_ready(self):
        self.task["mappings"][0]["status"] = "need_edit"
        self.error("apply_organizer_task", {"task_id": 3, "execute": True}, "STATE_CONFLICT")
        self.assertEqual(self.task["mappings"][0]["status"], "need_edit")
        self.assertEqual(self.calls, [])

    def test_execute_is_strictly_boolean_to_avoid_accidental_execution(self):
        for tool in ("apply_organizer_task", "retry_organizer_task"):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(tool=tool, value=value):
                    self.error(tool, {"task_id": 3, "execute": value}, "INVALID_PARAMS")
        self.assertEqual(self.calls, [])

    def test_rollback_intent_may_be_provided_by_authorized_agent(self):
        self.ok("rollback_organizer_run", {"run_id": 5, "confirm": True})
        self.assertEqual(self.calls, [("rollback", (5,), {})])
        description = self.registry.get("rollback_organizer_run").description
        self.assertIn("调用方", description)
        self.assertIn("AI", description)
        self.assertIn("不要求用户逐次", description)

    def test_execution_docs_allow_autonomy_without_disabling_plan_or_scope_guards(self):
        for tool in ("apply_organizer_task", "retry_organizer_task"):
            spec = self.registry.get(tool)
            execute = spec.input_schema["properties"]["execute"]
            self.assertEqual(execute, {"type": "boolean", "description": execute["description"], "default": False})
            self.assertIn("用户已授权规则", spec.description)
            self.assertNotIn("把摘要交给用户确认", spec.description)
        self.assertEqual(self.registry.get("apply_organizer_task").scope, "organize.apply")
        self.assertEqual(self.registry.get("retry_organizer_task").scope, "organize.write")
        self.assertTrue(self.registry.get("retry_organizer_task").annotations()["destructiveHint"])

    def test_backend_snapshot_race_remains_a_single_refusal_not_an_automatic_retry(self):
        plan = organizer_plan_summary(self.task)
        def reject(*args):
            self.calls.append(("apply", args, {}))
            return {"success": False, "code": "PLAN_CHANGED", "message": "Atomic snapshot changed", "retryable": False}, 409
        self.writes.apply_organizer_task = reject
        self.error("apply_organizer_task", {"task_id": 3, "execute": True}, "PLAN_CHANGED")
        self.assertEqual(self.calls, [("apply", (3, 7, plan["plan_hash"]), {})])


if __name__ == "__main__":
    unittest.main()
