from __future__ import annotations

import json
import posixpath
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.database import Database
from fnos_media_import.organizer.openlist_client import OpenListItem
from fnos_media_import.organizer.service import OrganizerService, _mapping_problem_summary
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_task_openlist_resolver,
    organizer_v1_job,
    organizer_v1_plan,
)


class _MemoryOpenList:
    configured = True

    def __init__(self) -> None:
        self.files: dict[str, OpenListItem] = {}
        self.dirs: set[str] = {"/"}
        self.mutations: list[tuple[str, str]] = []

    def add_file(self, path: str, size: int = 100_000_000) -> None:
        self.files[path] = OpenListItem(name=posixpath.basename(path), path=path, is_dir=False, size=size)
        self.mkdir(posixpath.dirname(path), record=False)

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def get_item(self, path: str) -> OpenListItem | None:
        if path in self.dirs:
            return OpenListItem(name=posixpath.basename(path), path=path, is_dir=True, size=0)
        return self.files.get(path)

    def list_dir(self, path: str, **_kwargs) -> list[OpenListItem]:
        return [item for name, item in self.files.items() if posixpath.dirname(name) == path]

    def mkdir(self, path: str, *, record: bool = True) -> None:
        if record:
            self.mutations.append(("mkdir", path))
        while path and path != "/":
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def rename(self, source: str, name: str, **_kwargs) -> None:
        self.mutations.append(("rename", source))
        item = self.files.pop(source)
        self.add_file(posixpath.join(posixpath.dirname(source), name), item.size)

    def move(self, source: str, target_dir: str, **_kwargs) -> None:
        self.mutations.append(("move", source))
        item = self.files.pop(source)
        self.add_file(posixpath.join(target_dir, item.name), item.size)

    def remove_file(self, path: str) -> None:
        self.mutations.append(("delete", path))
        self.files.pop(path)

    def remove_empty_directory(self, path: str) -> None:
        self.dirs.discard(path)


class OrganizerReviewRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="organizer-review-")
        self.addCleanup(self.directory.cleanup)
        infrastructure = Database(Path(self.directory.name) / "test.db")
        infrastructure.init_schema()
        self.db = RepositorySet.from_database(infrastructure)
        self.cloud = _MemoryOpenList()
        self.service = OrganizerService.__new__(OrganizerService)
        self.service.db = self.db
        self.service.owner_id = "organizer-review-test"
        self.service.categories = {"movie": {"label": "Movie"}, "tv": {"label": "TV"}}
        self.service.openlist = self.cloud
        self.service.organizer_config = {"auto_apply_confidence": 85}
        self.service.ai = SimpleNamespace(configured=False)
        self.service.tmdb = SimpleNamespace(configured=False, search=lambda *_a, **_k: [])
        self.service._cleanup_source_empty_dirs_after_apply = lambda _task: {}
        self.service._refresh_openlist_strm_for_task = lambda *_args: {}
        self.service._refresh_fnos_if_needed = lambda *_args: False
        self.service._wait_for_openlist_path = lambda path, **_kwargs: self.cloud.exists(path)
        self.service._wait_for_openlist_absence = lambda path, **_kwargs: not self.cloud.exists(path)
        self.queued: list[int] = []
        dispatcher = SimpleNamespace(organizer_apply=self._queue)
        self.admin = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(organizer=self.service, worker_dispatcher=dispatcher)
        )

    def _queue(self, task_id: int) -> dict:
        self.queued.append(task_id)
        return {"success": True, "queued": True, "worker_task_id": len(self.queued)}

    def _task(self, mappings: list[dict], *, category: str = "tv") -> tuple[int, int]:
        job_id, _ = self.db.job_commands.create({
            "title": "Share alias", "category": category, "category_label": category, "status": "waiting_organizer",
            "source_provider_kind": "quark", "source_form": "share", "source_url": "source-record:test",
            "destination_id": "native-main", "plan_version": 1, "idempotency_key": "organizer-review-test",
        })
        plan = organizer_v1_plan(job_id, category=category, organizer_root="/staging/job-1", destination_final="/library")
        self.db.job_commands.update(job_id, raw_data=organizer_v1_job(plan)["raw_data"])
        bind_task_openlist_resolver(self.service, self.cloud, plan)
        task_id = self.db.organizer.create_organizer_task(
            job_id=job_id, category=category, title="Share alias", source_keyword="Share alias",
            openlist_root_path="/staging/job-1", status="waiting_review",
            raw_data=attach_organizer_v1_task({}, plan)["raw_data"],
        )
        self.db.organizer.update_organizer_task(task_id, confidence=60)
        for mapping in mappings:
            self.cloud.add_file(mapping["source_path"])
        task = self.db.organizer.get_organizer_task(task_id)
        category_config = self.service._target_category_for_task(task, self.service.categories[category])
        self.db.organizer.replace_organizer_plan(
            task_id, files=[], mappings=mappings,
            operations=self.service._operations_for_mappings(mappings, category_config),
        )
        return job_id, task_id

    @staticmethod
    def _mapping(name: str, status: str = "ready") -> dict:
        return {
            "source_path": f"/staging/job-1/{name}", "source_name": name,
            "target_path": f"/library/Show/Season 01/{name}", "target_name": name,
            "title": "Show", "media_type": "tv", "season": 1, "episode": 1,
            "confidence": 90, "status": status, "raw_data": {"staging_file": True},
        }

    def test_fallback_tmdb_match_keeps_accepted_score(self) -> None:
        _job_id, task_id = self._task([], category="movie")
        self.service.tmdb = SimpleNamespace(configured=True, search=lambda query, _kind: [
            {"id": 42, "title": "Canonical Title", "original_title": "Canonical Title", "year": "2026", "media_type": "movie"}
        ] if query == "Canonical Title" else [])
        video = OpenListItem(
            name="Canonical Title (2026).mkv", path="/staging/job-1/Canonical Title (2026).mkv",
            is_dir=False, size=100_000_000,
        )
        _files, mappings, _operations, summary = self.service._build_plan(
            self.db.organizer.get_organizer_task(task_id), "movie", {"label": "Movie"}, [video]
        )
        self.assertEqual(summary["tmdb_id"], 42)
        self.assertGreaterEqual(summary["confidence"], 85)
        self.assertTrue(summary["auto_applicable"])
        self.assertEqual(mappings[0]["status"], "ready")

    def test_unknown_title_and_unrecognized_episode_still_require_review(self) -> None:
        _job_id, task_id = self._task([])
        video = OpenListItem(name="Unknown.mkv", path="/staging/job-1/Unknown.mkv", is_dir=False, size=100_000_000)
        _files, mappings, _operations, summary = self.service._build_plan(
            self.db.organizer.get_organizer_task(task_id), "tv", {"label": "TV"}, [video]
        )
        self.assertFalse(summary["auto_applicable"])
        self.assertEqual(mappings[0]["status"], "need_edit")

    def test_accepted_tmdb_match_does_not_trigger_unnecessary_ai_review(self) -> None:
        _job_id, task_id = self._task([], category="movie")
        self.service.tmdb = SimpleNamespace(configured=True, search=lambda query, _kind: [
            {"id": 42, "title": "Canonical Title", "original_title": "Canonical Title", "year": "2026", "media_type": "movie"}
        ] if query == "Canonical Title" else [])
        calibrate = Mock(return_value={"requires_review": True})
        self.service.ai = SimpleNamespace(configured=True, calibrate=calibrate, model="test-model")
        video = OpenListItem(
            name="Canonical Title (2026).mkv", path="/staging/job-1/Canonical Title (2026).mkv",
            is_dir=False, size=100_000_000,
        )
        _files, mappings, _operations, summary = self.service._build_plan(
            self.db.organizer.get_organizer_task(task_id), "movie", {"label": "Movie"}, [video]
        )
        calibrate.assert_not_called()
        self.assertEqual(summary["tmdb_id"], 42)
        self.assertTrue(summary["auto_applicable"])
        self.assertEqual(mappings[0]["status"], "ready")

    def test_mapping_ad_delete_is_only_activated_by_explicit_apply(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv"), self._mapping("advert.mp4", "need_edit")])
        ad = self.db.organizer.get_organizer_task(task_id)["mappings"][1]
        saved = self.service.update_mapping(ad["id"], {"status": "delete_ad"}, task_id=task_id)
        self.assertTrue(saved["success"])
        self.assertEqual(self.cloud.mutations, [])
        self.assertNotIn("delete_file", [op["type"] for op in self.db.organizer.get_organizer_task(task_id)["operations"]])
        queued, status = self.admin.apply(task_id)
        self.assertEqual(status, 200)
        self.assertTrue(queued["queued"])
        approved = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(approved["status"], "manual_confirmed")
        self.assertEqual([op["type"] for op in approved["operations"]].count("delete_file"), 1)
        self.assertEqual(self.cloud.mutations, [])
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.cloud.mutations.count(("delete", ad["source_path"])), 1)

    def test_unresolved_mapping_blocks_dispatch_synchronously(self) -> None:
        _job_id, task_id = self._task([self._mapping("Unknown.mkv", "need_edit")])
        result, status = self.admin.apply(task_id)
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertEqual(self.queued, [])
        self.assertEqual(self.cloud.mutations, [])

    def test_queued_worker_does_not_apply_a_later_unconfirmed_ad_edit(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.service.update_mapping(mapping["id"], {"status": "delete_ad"}, task_id=task_id)
        result = self.service.apply_task_from_worker(task_id)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "waiting_review")
        self.assertEqual(self.cloud.mutations, [])

    def test_threaded_apply_cannot_bypass_plan_confirmation(self) -> None:
        _job_id, task_id = self._task([self._mapping("advert.mp4", "delete_ad")])
        result = self.service.start_apply_task(task_id)
        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["status"], "waiting_review")
        self.assertEqual(self.db.organizer.list_organizer_runs(), [])
        self.assertEqual(self.cloud.mutations, [])

    def test_mapping_edit_between_confirmation_check_and_claim_is_rejected(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.service._background_apply_lock = threading.Lock()
        self.service._background_apply_tasks = set()
        mapping_id = self.db.organizer.get_organizer_task(task_id)["mappings"][0]["id"]
        original_claim = self.db.organizer.claim_organizer_run

        def edit_then_claim(requested_task_id, **kwargs):
            saved = self.service.update_mapping(mapping_id, {"status": "delete_ad"}, task_id=task_id)
            self.assertTrue(saved["success"], saved)
            return original_claim(requested_task_id, **kwargs)

        for apply in (self.service.apply_task_from_worker, self.service.start_apply_task):
            with self.subTest(entrypoint=apply.__name__):
                approved = self.service.approve_task(task_id)
                self.assertTrue(approved["success"], approved)
                with patch.object(self.db.organizer, "claim_organizer_run", side_effect=edit_then_claim):
                    result = apply(task_id)
                self.assertFalse(result["success"], result)
                self.assertTrue(result["conflict"])
                self.assertEqual(result["status"], "waiting_review")
                self.assertEqual(self.db.organizer.list_organizer_runs(), [])
                self.assertEqual(self.service._background_apply_tasks, set())
                self.assertEqual(self.cloud.mutations, [])

    def test_confirmation_resume_preserves_completed_deletion_and_move(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv"), self._mapping("advert.mp4", "delete_ad")])
        actual_confirm = self.service._confirm_standardized_targets
        self.service._confirm_standardized_targets = lambda _task: {"success": False, "message": "not yet visible"}
        self.admin.apply(task_id)
        first = self.service.apply_task_from_worker(task_id)
        self.assertFalse(first["success"])
        before = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(before["status"], "waiting_review")
        mutation_count = len(self.cloud.mutations)
        completed = [op for op in before["operations"] if op["type"] in {"move_file", "delete_file"}]
        self.assertTrue(all(op["status"] == "done" for op in completed))
        self.service._confirm_standardized_targets = actual_confirm
        queued, status = self.admin.retry(task_id)
        self.assertEqual(status, 200)
        self.assertEqual(queued["retry_mode"], "resume_apply")
        after = self.db.organizer.get_organizer_task(task_id)
        restored = [op for op in after["operations"] if op["type"] in {"move_file", "delete_file"}]
        self.assertEqual([(op["status"], op["run_id"], op["undo_data"]) for op in restored],
                         [(op["status"], op["run_id"], op["undo_data"]) for op in completed])
        second = self.service.apply_task_from_worker(task_id)
        self.assertTrue(second["success"], second)
        self.assertEqual(second["status"], "done")
        self.assertEqual(len(self.cloud.mutations), mutation_count)

    def test_deletion_only_finishes_cleanup_without_import_or_refresh(self) -> None:
        job_id, task_id = self._task([self._mapping("advert.mp4", "delete_ad")])
        self.service._refresh_openlist_strm_for_task = lambda *_args: self.fail("cleanup-only must not refresh STRM")
        self.service._refresh_fnos_if_needed = lambda *_args: self.fail("cleanup-only must not refresh media")
        self.admin.apply(task_id)
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["cleanup_only"])
        task = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["evidence"]["cleanup_only"])
        self.assertTrue(task["evidence"]["completion_confirmation"]["cleanup_only"])
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "cancelled")
        self.admin.apply(task_id)
        self.assertEqual(self.cloud.mutations.count(("delete", "/staging/job-1/advert.mp4")), 1)

    def test_deletion_only_plan_is_not_automatically_executed(self) -> None:
        _job_id, task_id = self._task([])
        self.db.organizer.update_organizer_task(task_id, title="Show", source_keyword="Show")
        self.service.tmdb = SimpleNamespace(configured=True, search=lambda *_args: [
            {"id": 42, "title": "Show", "original_title": "Show", "media_type": "tv", "year": "2026"}
        ])
        video = OpenListItem(name="advert.mp4", path="/staging/job-1/advert.mp4", is_dir=False, size=1000)
        _files, mappings, _operations, summary = self.service._build_plan(
            self.db.organizer.get_organizer_task(task_id), "tv", {"label": "TV"}, [video]
        )
        self.assertGreaterEqual(summary["confidence"], 85)
        self.assertEqual(mappings[0]["status"], "delete_ad")
        self.assertFalse(summary["auto_applicable"])

    def test_failed_ad_delete_reply_resumes_without_replaying_completed_move(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv"), self._mapping("advert.mp4", "delete_ad")])
        original_remove = self.cloud.remove_file

        def delete_then_lose_reply(path: str) -> None:
            original_remove(path)
            raise RuntimeError("provider reply lost after deletion")

        self.cloud.remove_file = delete_then_lose_reply
        self.admin.apply(task_id)
        first = self.service.apply_task_from_worker(task_id)
        self.assertFalse(first["success"])
        self.assertEqual(self.db.organizer.get_organizer_task(task_id)["status"], "failed")
        self.cloud.remove_file = original_remove
        count = len(self.cloud.mutations)
        queued, status = self.admin.retry(task_id)
        self.assertEqual(status, 200)
        self.assertEqual(queued["retry_mode"], "resume_apply")
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.cloud.mutations), count)

    def test_legacy_deletion_only_waiting_review_can_complete_without_redelete(self) -> None:
        job_id, task_id = self._task([self._mapping("advert.mp4", "delete_ad")])
        task = self.db.organizer.get_organizer_task(task_id)
        for op in task["operations"]:
            self.db.organizer.update_organizer_operation(op["id"], status="done")
        self.cloud.files.clear()
        self.db.organizer.update_organizer_task(task_id, evidence={"completion_confirmation": {"success": False}})
        self.admin.apply(task_id)
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["cleanup_only"])
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "cancelled")

    def test_ad_deletion_outside_frozen_task_root_cannot_be_approved(self) -> None:
        mapping = {**self._mapping("advert.mp4", "delete_ad"), "source_path": "/other/advert.mp4"}
        _job_id, task_id = self._task([mapping])
        result, status = self.admin.apply(task_id)
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertEqual(self.queued, [])
        self.assertEqual(self.cloud.mutations, [])


@unittest.skipUnless(shutil.which("node"), "Node is required for the UI regression")
class OrganizerReviewUiTests(unittest.TestCase):
    def test_post_apply_review_has_visible_resume_action(self) -> None:
        module = Path(__file__).resolve().parents[1] / "static" / "admin-organizer.js"
        script = """
globalThis.window = globalThis;
require(process.argv[1]);
const ui = window.FnosAdminOrganizer.create({state: {}, escapeHtml: value => String(value ?? '')});
const task = {id: 7, status: 'waiting_review', mappings: [{status: 'delete_ad'}],
    evidence: {completion_confirmation: {success: false}}};
process.stdout.write(JSON.stringify({buttons: ui.organizerDrawerActionButtons(task),
    panel: ui.organizerDetailActionPanel(task), list: ui.organizerTaskButtons(task)}));
"""
        result = subprocess.run(["node", "-e", script, str(module)], check=True, capture_output=True, text=True, encoding="utf-8")
        rendered = json.loads(result.stdout)
        for html in rendered.values():
            self.assertIn("继续完成确认", html)
        self.assertIn('data-organizer-drawer-action="apply"', rendered["buttons"])


class OrganizerReviewReasonTests(unittest.TestCase):
    def test_review_reason_uses_configured_threshold(self) -> None:
        summary = _mapping_problem_summary([], 82, {}, auto_apply_confidence=90)
        issue = next(item for item in summary["issues"] if item["type"] == "low_confidence")
        self.assertIn("82", issue["message"])
        self.assertIn("90", issue["message"])
        self.assertEqual(_mapping_problem_summary([], 91, {}, auto_apply_confidence=90)["issues"], [])


if __name__ == "__main__":
    unittest.main()
