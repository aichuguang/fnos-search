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
    "id": 3, "title": "Example", "category": "tv", "status": "waiting_review", "revision": 4,
    "openlist_root_path": "/staging/job-3",
    "mappings": [{
        "id": 11, "source_path": "/staging/job-3/Example.S01E01.mkv",
        "target_path": ROOT + "/Season 01/Example (2026) - S01E01.mkv",
        "target_name": "Example (2026) - S01E01.mkv", "status": "ready",
        "title": "Example", "year": "2026", "season": 1, "episode": 1, "media_type": "tv", "tmdb_id": 123,
        "raw_data": {"organizer_identity": {"version": 1, "scope_root": "/library/tv", "intent": "create_or_match",
            "canonical_dir": ROOT, "requested_identity": IDENTITY, "manifest": {"version": 1, "identity": IDENTITY, "aliases": []}, "errors": []}},
    }],
    "operations": [{"id": 1, "type": "move_file", "source_path": "/staging/job-3/Example.S01E01.mkv",
        "target_path": ROOT + "/Season 01/Example (2026) - S01E01.mkv", "status": "pending"}],
}


class AgentOrganizerWritesV2Tests(unittest.TestCase):
    def setUp(self):
        self.task = copy.deepcopy(TASK)
        self.calls = []
        self.registry = ToolRegistry()
        organizer_write.register(self.registry)
        self.writes = SimpleNamespace(
            update_organizer_mappings=self.record("update"),
            apply_organizer_task=self.record("apply"),
            retry_organizer_task=self.record("rescan"),
            rollback_organizer_run=self.record("rollback"),
        )
        services = SimpleNamespace(
            writes=self.writes,
            organizer_task_detail=lambda _id: ({"success": True, "task": self.task}, 200),
        )
        self.handler = McpHandler(registry=self.registry, services=services,
            token_store=SimpleNamespace(touch=lambda _id: None), audit=None, enabled=True)

    def record(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True, "task": self.task, "task_id": self.task["id"]}, 200
        return call

    def call(self, name, arguments, *, scopes=("organize.write", "organize.apply")):
        token = AgentTokenRecord(id=1, name="test", scopes=frozenset(scopes), rate_limit=0,
            expires_at="", revoked_at="", last_used_at="")
        response = self.handler.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}, token)
        self.assertIsNotNone(response)
        return response["result"]

    def ok(self, name, arguments, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def error(self, name, arguments, code="INVALID_PARAMS", **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["code"], code)
        return result["structuredContent"]

    def test_schema_removes_skip_and_delete_and_declares_strict_media_fields(self):
        self.assertIsNone(self.registry.get("skip_organizer_task"))
        self.assertFalse(hasattr(organizer_write, "_skip_task"))
        spec = self.registry.get("update_organizer_mappings")
        fields = spec.input_schema["properties"]
        self.assertNotIn("delete_ad", fields["status"]["enum"])
        self.assertEqual(fields["media_type"]["enum"], ["movie", "tv"])
        self.assertEqual((fields["season"]["minimum"], fields["season"]["maximum"]), (0, 99))
        self.assertEqual((fields["episode"]["minimum"], fields["episode"]["maximum"]), (1, 2000))
        self.assertEqual(fields["tmdb_id"]["type"], ["integer", "null"])
        self.assertIn("只改 ready 不代表", spec.description)

    def test_single_tmdb_set_clear_and_omission_have_distinct_payloads(self):
        for supplied, expected in (({"tmdb_id": 456}, {"tmdb_id": 456}), ({"tmdb_id": None}, {"tmdb_id": None}),
                                   ({"title": "Example"}, {"title": "Example"}),
                                   ({"title": "Example", "tmdb_id": " "}, {"title": "Example"})):
            with self.subTest(supplied=supplied):
                self.ok("update_organizer_mappings", {"task_id": 3, "mapping_id": 11, **supplied})
                self.assertEqual(self.calls[-1], ("update", (3, 11, expected), {}))

    def test_batch_tmdb_allows_set_but_empty_omits_and_null_is_rejected(self):
        self.ok("update_organizer_mappings", {"task_id": 3, "tmdb_id": 456})
        self.assertEqual(self.calls[-1][1], (3, 0, {"tmdb_id": 456}))
        self.ok("update_organizer_mappings", {"task_id": 3, "title": "Example", "tmdb_id": ""})
        self.assertEqual(self.calls[-1][1], (3, 0, {"title": "Example"}))
        before = list(self.calls)
        self.error("update_organizer_mappings", {"task_id": 3, "title": "Example", "tmdb_id": None})
        self.assertEqual(self.calls, before)

    def test_integer_bounds_and_types_are_checked_before_the_bridge(self):
        for name, values in {"season": [-1, 100, True, 1.1, "1"], "episode": [0, 2001, False, 1.1, "2"],
                             "tmdb_id": [0, -1, True, 1.1, "123", 2_147_483_648]}.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    self.error("update_organizer_mappings", {"task_id": 3, "mapping_id": 11, name: value})
        self.assertEqual(self.calls, [])
        self.ok("update_organizer_mappings", {"task_id": 3, "mapping_id": 11, "season": 0, "episode": 2000})
        self.assertEqual(self.calls[-1][1][2], {"season": 0, "episode": 2000})

    def test_media_type_and_delete_status_are_rejected_before_the_bridge(self):
        for arguments in ({"media_type": "anime"}, {"media_type": "variety"}, {"media_type": []}, {"status": "delete_ad"}):
            with self.subTest(arguments=arguments):
                self.error("update_organizer_mappings", {"task_id": 3, "mapping_id": 11, **arguments})
        self.assertEqual(self.calls, [])

    def test_target_name_is_a_basename_even_when_target_path_is_supplied(self):
        for name in ("../video.mkv", "folder/video.mkv", "folder\\video.mkv", "a..mkv", ".", "a\x00.mkv"):
            with self.subTest(name=name):
                self.error("update_organizer_mappings", {"task_id": 3, "mapping_id": 11,
                    "target_name": name, "target_path": ROOT + "/Season 01/safe.mkv"})
        self.assertEqual(self.calls, [])
        self.ok("update_organizer_mappings", {"task_id": 3, "mapping_id": 11, "target_name": "safe.mkv"})
        self.assertEqual(self.calls[-1][1][2]["target_path"], ROOT + "/Season 01/safe.mkv")

    def test_rescan_is_write_scope_and_never_calls_apply_or_rollback(self):
        result = self.ok("retry_organizer_task", {"task_id": 3}, scopes=("organize.write",))
        self.assertEqual(result["mode"], "rescan_only")
        self.assertEqual(self.calls, [("rescan", (3,), {})])
        self.assertIn("auto_apply=false", self.registry.get("retry_organizer_task").description)
        self.assertEqual(self.registry.get("retry_organizer_task").scope, "organize.write")

    def test_physical_apply_and_rollback_need_apply_scope(self):
        for name, arguments in (("apply_organizer_task", {"task_id": 3}),
                                ("rollback_organizer_run", {"run_id": 1, "confirm": True})):
            with self.subTest(name=name):
                self.error(name, arguments, "SCOPE_DENIED", scopes=("organize.write",))
        self.assertEqual(self.calls, [])

    def test_apply_requires_current_hash_and_uses_reviewed_revision(self):
        preview = self.ok("apply_organizer_task", {"task_id": 3})
        self.assertTrue(preview["confirmation_required"])
        self.assertFalse(preview["executed"])
        self.assertEqual(self.calls, [])
        supplied = preview["plan"]["plan_hash"]
        result = self.ok("apply_organizer_task", {"task_id": 3, "plan_hash": supplied})
        self.assertEqual(result["plan_hash"], supplied)
        self.assertEqual(self.calls, [("apply", (3, 4, supplied), {})])
        self.task["revision"] += 1
        self.error("apply_organizer_task", {"task_id": 3, "plan_hash": supplied}, "PLAN_CHANGED")
        self.assertEqual(len(self.calls), 1)

    def test_blocked_and_legacy_operations_cannot_reach_apply_even_with_current_hash(self):
        for mapping_status, operation_type in (("need_edit", "move_file"), ("conflict", "move_file"),
            ("delete_ad", "move_file"), ("skipped_existing", "move_file"),
            ("ready", "delete_file"), ("ready", "delete_duplicate_file"), ("ready", "unknown")):
            with self.subTest(mapping_status=mapping_status, operation_type=operation_type):
                self.task = copy.deepcopy(TASK)
                self.task["mappings"][0]["status"] = mapping_status
                self.task["operations"][0]["type"] = operation_type
                preview = self.ok("apply_organizer_task", {"task_id": 3})
                self.assertFalse(preview["can_apply"])
                self.assertIn("blocked_warning", preview)
                self.error("apply_organizer_task", {"task_id": 3, "plan_hash": organizer_plan_summary(self.task)["plan_hash"]}, "STATE_CONFLICT")
        self.assertEqual(self.calls, [])

    def test_missing_or_invalid_identity_cannot_reach_apply_with_current_hash(self):
        for kind in ("missing", "invalid", "review"):
            with self.subTest(kind=kind):
                self.task = copy.deepcopy(TASK)
                raw = self.task["mappings"][0]["raw_data"]
                if kind == "missing":
                    raw.clear()
                elif kind == "invalid":
                    raw["organizer_identity"]["manifest"] = {"invalid_manifest": True}
                else:
                    raw["organizer_identity"]["errors"] = ["Identity directory missing"]
                self.error("apply_organizer_task", {"task_id": 3, "plan_hash": organizer_plan_summary(self.task)["plan_hash"]}, "STATE_CONFLICT")
        self.assertEqual(self.calls, [])

    def test_actionable_bridge_errors_are_preserved_without_blind_retry(self):
        failure = ({"success": False, "code": "EXECUTION_CONFIRMATION_REQUIRED", "message": "Partial writes need explicit recovery",
            "retryable": True, "details": {"next_action": "apply_or_rollback", "internal_secret": "not projected"}}, 409)
        for tool, method, arguments in (
            ("update_organizer_mappings", "update_organizer_mappings", {"task_id": 3, "mapping_id": 11, "title": "Example"}),
            ("apply_organizer_task", "apply_organizer_task", {"task_id": 3, "plan_hash": organizer_plan_summary(self.task)["plan_hash"]}),
            ("retry_organizer_task", "retry_organizer_task", {"task_id": 3}),
            ("rollback_organizer_run", "rollback_organizer_run", {"run_id": 1, "confirm": True}),
        ):
            with self.subTest(tool=tool):
                setattr(self.writes, method, lambda *_args, **_kwargs: failure)
                result = self.error(tool, arguments, "EXECUTION_CONFIRMATION_REQUIRED")
                self.assertFalse(result["retryable"])
                self.assertEqual(result["details"], {"next_action": "apply_or_rollback"})
                self.assertIn("apply", result["hint"])


if __name__ == "__main__":
    unittest.main()
