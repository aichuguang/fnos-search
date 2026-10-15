from __future__ import annotations

import posixpath
import unittest
from unittest.mock import patch

from tests import test_organizer_review_recovery as fixtures


class OrganizerRollbackSafetyTests(unittest.TestCase):
    setUp = fixtures.OrganizerReviewRecoveryTests.setUp
    _task = fixtures.OrganizerReviewRecoveryTests._task
    _queue = fixtures.OrganizerReviewRecoveryTests._queue
    _mapping = staticmethod(fixtures.OrganizerReviewRecoveryTests._mapping)

    def _applied(self, *, confirmation_failure: bool = False):
        job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        if confirmation_failure:
            with patch.object(self.service, "_confirm_standardized_targets", return_value={"success": False, "message": "not visible"}):
                result = self.service.apply_task_from_worker(task_id)
            self.assertFalse(result["success"])
        else:
            result = self.service.apply_task_from_worker(task_id)
            self.assertTrue(result["success"], result)
        return job_id, task_id, result["run_id"]

    def test_real_scan_cannot_be_taken_over_by_stale_apply(self):
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.assertTrue(self.service.update_mapping(mapping["id"], {"status": "delete_ad"}, task_id=task_id)["success"])
        self.assertEqual(self.db.organizer.get_organizer_task(task_id)["operations"], [])
        visited = []

        def scan(_task, _root):
            current = self.db.organizer.get_organizer_task(task_id)
            self.assertEqual(current["status"], "scanning")
            result = self.service.apply_task_from_worker(task_id)
            self.assertTrue(result["conflict"], result)
            self.assertEqual(result["status"], "scanning")
            run_id, conflict = self.db.organizer.claim_organizer_run(task_id, owner_id="late-apply", expected_revision=current["revision"])
            self.assertIsNone(run_id)
            self.assertTrue(conflict["conflict"])
            self.assertEqual(self.db.organizer.get_organizer_task(task_id)["scan_owner"], self.service.owner_id)
            visited.append(True)
            return list(self.cloud.files.values())

        with patch.object(self.service, "_scan_openlist_videos", side_effect=scan), patch.object(self.service, "_scan_staging_companion_files", return_value=[]):
            self.service.process_task_from_worker(task_id, auto_apply=False, respect_schedule=False)
        self.assertEqual(visited, [True])
        self.assertEqual(self.cloud.mutations, [])
        self.assertTrue(self.cloud.exists(mapping["source_path"]))

    def test_scan_failure_cannot_reuse_pre_scan_approval(self):
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        before = self.db.organizer.get_organizer_task(task_id)
        self.assertTrue(before["evidence"]["manual_approval"])
        self.assertTrue(self.db.organizer.claim_organizer_task_for_scan(task_id, allowed_statuses={"manual_confirmed"}, owner_id="scanner"))
        self.db.organizer.update_organizer_task(task_id, status="failed", clear_scan_lease=True)
        current = self.db.organizer.get_organizer_task(task_id)
        self.assertNotIn("manual_approval", current["evidence"])
        run_id, result = self.db.organizer.claim_organizer_run(task_id, owner_id="stale-worker", expected_revision=current["revision"])
        self.assertIsNone(run_id)
        self.assertTrue(result["conflict"])
        self.assertFalse(self.service.apply_task_from_worker(task_id)["success"])
        self.assertEqual(self.cloud.mutations, [])

    def test_repository_rechecks_approval_version(self):
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        task = self.db.organizer.get_organizer_task(task_id)
        evidence = {**task["evidence"], "manual_approval": {"revision": task["revision"] - 1}}
        self.db.organizer.update_organizer_task(task_id, evidence=evidence)
        run_id, result = self.db.organizer.claim_organizer_run(task_id, owner_id="worker", expected_revision=task["revision"])
        self.assertIsNone(run_id)
        self.assertTrue(result["conflict"])

    def test_changed_title_is_blocked_until_existing_rollback_restores_staging(self):
        _job_id, task_id, run_id = self._applied(confirmation_failure=True)
        before = self.db.organizer.get_organizer_task(task_id)
        mapping = before["mappings"][0]
        for edit in (
            lambda: self.service.update_mapping(mapping["id"], {"title": "Corrected"}, task_id=task_id),
            lambda: self.service.batch_update_mappings(task_id, {"title": "Corrected"}),
            lambda: self.service.process_task(task_id, auto_apply=False),
        ):
            result = edit()
            self.assertTrue(result["conflict"], result)
            self.assertEqual(result["code"], "MOVED_MAPPING_REQUIRES_ROLLBACK")
            self.assertIn(f"run #{run_id}", result["message"])
            self.assertIn("执行记录", result["message"])
            self.assertEqual(self.db.organizer.get_organizer_task(task_id)["revision"], before["revision"])
        result = self.service.rollback_run(run_id)
        self.assertTrue(result["success"], result)
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        saved = self.service.update_mapping(mapping["id"], {"title": "Corrected"}, task_id=task_id)
        self.assertTrue(saved["success"], saved)
        self.admin.apply(task_id)
        applied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertTrue(self.cloud.exists(saved["target_path"]))
        self.assertEqual(self.db.organizer.get_organizer_task(task_id)["mappings"][0]["source_path"], mapping["source_path"])

    def test_rollback_is_idempotent_and_reopens_task_job_and_operations(self):
        job_id, task_id, run_id = self._applied()
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.cloud.dirs.discard(posixpath.dirname(mapping["source_path"]))
        original_move = self.cloud.move

        def move(source, target_dir, **kwargs):
            self.assertTrue(self.cloud.exists(target_dir), target_dir)
            original_move(source, target_dir, **kwargs)

        with patch.object(self.cloud, "move", side_effect=move):
            result = self.service.rollback_run(run_id)
        self.assertTrue(result["success"], result)
        task = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(task["status"], "waiting_review")
        self.assertNotIn("manual_approval", task["evidence"])
        self.assertEqual(next(op for op in task["operations"] if op["type"] == "move_file")["status"], "pending")
        job = self.db.job_queries.get(job_id)
        self.assertEqual(job["status"], "review")
        rollback = job["raw_data"]["completion"]["rollback"]
        self.assertEqual(rollback["status"], "completed")
        self.assertEqual(rollback["target_paths"], [mapping["target_path"]])
        self.assertEqual(self.db.organizer.get_organizer_run(run_id)["status"], "rolled_back")
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        self.assertFalse(self.cloud.exists(mapping["target_path"]))
        count = len(self.cloud.mutations)
        repeated = self.service.rollback_run(run_id)
        self.assertTrue(repeated["already_rolled_back"])
        self.assertEqual(len(self.cloud.mutations), count)
        self.admin.apply(task_id)
        applied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        job = self.db.job_queries.get(job_id)
        self.assertEqual(job["status"], "done")
        self.assertNotIn("rollback", job["raw_data"]["completion"])

    def test_lost_provider_reply_resumes_from_durable_holding_location(self):
        job_id, task_id, run_id = self._applied()
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        original_move = self.cloud.move
        lost_reply = []

        def move(source, target_dir, **kwargs):
            original_move(source, target_dir, **kwargs)
            if not lost_reply:
                lost_reply.append(True)
                raise RuntimeError("reply lost after real move")

        with patch.object(self.cloud, "move", side_effect=move):
            first = self.service.rollback_run(run_id)
        self.assertFalse(first["success"])
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "review")
        self.assertEqual(self.db.organizer.get_organizer_task(task_id)["evidence"]["rollback"]["status"], "failed")
        self.assertFalse(self.service.approve_task(task_id)["success"])
        saved = self.service.update_mapping(mapping["id"], {"title": "Other"}, task_id=task_id)
        self.assertEqual(saved["code"], "ROLLBACK_INCOMPLETE")
        second, status = self.admin.retry(task_id)
        self.assertEqual(status, 200, second)
        self.assertTrue(second["success"], second)
        self.assertEqual(second["retry_mode"], "resume_rollback")
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        self.assertEqual(self.cloud.mutations.count(("move", mapping["target_path"])), 1)

    def test_lost_progress_write_does_not_repeat_completed_move(self):
        _job_id, task_id, run_id = self._applied()
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        original = self.db.organizer.update_organizer_rollback_progress
        failed = []

        def checkpoint(*args, **kwargs):
            if kwargs.get("status", "running") == "running" and not failed:
                failed.append(True)
                raise RuntimeError("checkpoint unavailable")
            return original(*args, **kwargs)

        with patch.object(self.db.organizer, "update_organizer_rollback_progress", side_effect=checkpoint):
            first = self.service.rollback_run(run_id)
        self.assertFalse(first["success"])
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        second = self.service.rollback_run(run_id)
        self.assertTrue(second["success"], second)
        self.assertEqual(self.cloud.mutations.count(("move", mapping["target_path"])), 1)

    def test_concurrent_rollback_is_rejected_before_any_second_mutation(self):
        _job_id, task_id, run_id = self._applied()
        actual = self.service._execute_rollback_inverse
        competing = []

        def inverse(*args):
            if not competing:
                result = self.service.rollback_run(run_id)
                competing.append(result)
                self.assertTrue(result["conflict"], result)
                self.assertFalse(self.db.organizer.claim_organizer_task_for_scan(task_id, allowed_statuses={"executing"}, owner_id="scanner"))
            actual(*args)

        with patch.object(self.service, "_execute_rollback_inverse", side_effect=inverse):
            result = self.service.rollback_run(run_id)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(competing), 1)

    def test_conflicting_restore_target_is_never_overwritten(self):
        job_id, task_id, run_id = self._applied()
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.cloud.add_file(mapping["source_path"], size=42)
        count = len(self.cloud.mutations)
        result = self.service.rollback_run(run_id)
        self.assertFalse(result["success"])
        self.assertEqual(len(self.cloud.mutations), count)
        self.assertEqual(self.cloud.files[mapping["source_path"]].size, 42)
        self.assertTrue(self.cloud.exists(mapping["target_path"]))
        self.assertEqual(self.db.job_queries.get(job_id)["status"], "review")

    def test_expired_rollback_lease_resumes_without_replaying_moved_file(self):
        _job_id, task_id, run_id = self._applied()
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        with patch.object(self.db.organizer, "update_organizer_rollback_progress", side_effect=SystemExit("crash before checkpoint")):
            with self.assertRaises(SystemExit):
                self.service.rollback_run(run_id)
        task = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(task["status"], "executing")
        rollback_run_id = task["evidence"]["rollback"]["rollback_run_id"]
        with self.db.connect() as conn:
            conn.execute("UPDATE organizer_runs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (rollback_run_id,))
        result = self.service.rollback_run(run_id)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.db.organizer.get_organizer_run(rollback_run_id)["status"], "failed")
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        self.assertEqual(self.cloud.mutations.count(("move", mapping["target_path"])), 1)

    def test_batch_inverse_restores_same_original_names_in_separate_folders(self):
        # Only the rollback's batch record is under test. The small in-memory
        # client deliberately does not implement bulk-move/reconciliation APIs.
        self.service.organizer_config["bulk_operations_enabled"] = False
        self.service.organizer_config["bulk_reconcile_timeout_seconds"] = 0
        mappings = [
            {**self._mapping("S01E01.mkv"), "source_path": "/staging/job-1/a/episode.mkv"},
            {**self._mapping("S01E02.mkv"), "source_path": "/staging/job-1/b/episode.mkv", "episode": 2},
        ]
        _job_id, task_id = self._task(mappings)
        self.admin.apply(task_id)
        result = self.service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        run_id = result["run_id"]
        run = self.db.organizer.get_organizer_run(run_id)
        moves = [item for item in run["undo_data"] if item["type"] == "move_file"]
        self.assertEqual(len(moves), 2)
        inverse = [{"type": "move_file_batch", "items": moves}, *[item for item in run["undo_data"] if item["type"] != "move_file"]]
        self.db.organizer.update_organizer_run(run_id, "done", undo_data=inverse)
        rolled_back = self.service.rollback_run(run_id)
        self.assertTrue(rolled_back["success"], rolled_back)
        for mapping in mappings:
            self.assertTrue(self.cloud.exists(mapping["source_path"]))
            self.assertFalse(self.cloud.exists(mapping["target_path"]))

    def test_frozen_boundary_rejects_tampered_undo_paths(self):
        _job_id, task_id, run_id = self._applied()
        self.db.organizer.update_organizer_run(run_id, "done", undo_data=[{
            "type": "move_file", "source_path": "/other/private.mkv", "target_path": "/staging/job-1/private.mkv",
        }])
        count = len(self.cloud.mutations)
        result = self.service.rollback_run(run_id)
        self.assertTrue(result["conflict"], result)
        self.assertEqual(len(self.cloud.mutations), count)
        self.assertEqual(self.db.organizer.get_organizer_task(task_id)["status"], "done")

    def test_reconciled_skipped_move_still_has_a_working_ui_rollback(self):
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        actual_move = self.cloud.move

        def lose_reply(*args, **kwargs):
            actual_move(*args, **kwargs)
            raise RuntimeError("reply lost after apply move")

        with patch.object(self.cloud, "move", side_effect=lose_reply):
            first = self.service.apply_task_from_worker(task_id)
        self.assertFalse(first["success"])
        self.admin.retry(task_id)
        second = self.service.apply_task_from_worker(task_id)
        self.assertTrue(second["success"], second)
        run_id = second["run_id"]
        self.assertFalse(self.db.organizer.get_organizer_run(run_id)["undo_data"])
        listed = next(run for run in self.service.list_runs() if run["id"] == run_id)
        self.assertEqual(len(listed["undo_data"]), 1)  # Existing UI renders its rollback button.
        result = self.service.rollback_run(run_id)
        self.assertTrue(result["success"], result)
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.assertTrue(self.cloud.exists(mapping["source_path"]))
        edited = self.service.update_mapping(mapping["id"], {"title": "Corrected"}, task_id=task_id)
        self.assertTrue(edited["success"], edited)


if __name__ == "__main__":
    unittest.main()
