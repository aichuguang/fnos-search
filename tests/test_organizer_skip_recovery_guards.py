from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import test_organizer_review_recovery as fixtures


class OrganizerSkipRecoveryGuardTests(unittest.TestCase):
    setUp = fixtures.OrganizerReviewRecoveryTests.setUp
    _task = fixtures.OrganizerReviewRecoveryTests._task
    _queue = fixtures.OrganizerReviewRecoveryTests._queue
    _mapping = staticmethod(fixtures.OrganizerReviewRecoveryTests._mapping)

    def _assert_skip_authorizes_new_plan(self, approval_key: str) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.cloud.mkdir("/library", record=False)
        self.assertTrue(self.service.approve_task(task_id)["success"])
        before = self.db.organizer.get_organizer_task(task_id)
        self.db.organizer.update_organizer_task(
            task_id,
            status="failed" if approval_key == "execution_approval" else "manual_confirmed",
            evidence={approval_key: {"revision": before["revision"]}},
        )

        skipped, status = self.admin.skip(task_id)

        self.assertEqual(status, 200, skipped)
        self.assertTrue(skipped["success"], skipped)
        prepared = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(prepared["revision"], before["revision"] + 1)
        self.assertEqual(prepared["evidence"]["manual_approval"]["revision"], prepared["revision"])
        self.assertEqual(prepared["evidence"]["manual_approval"]["deletion_count"], 0)
        self.assertNotIn("execution_approval", prepared["evidence"])
        self.assertEqual(self.cloud.mutations, [])
        applied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertEqual(applied["status"], "done")
        self.assertTrue(self.cloud.exists("/library/S01E01.mkv"))

    def test_skip_replaces_prior_manual_approval_revision(self) -> None:
        self._assert_skip_authorizes_new_plan("manual_approval")

    def test_skip_replaces_prior_execution_approval_revision(self) -> None:
        self._assert_skip_authorizes_new_plan("execution_approval")

    def test_skip_cannot_discard_partially_completed_moves(self) -> None:
        mappings = [self._mapping("S01E01.mkv"), {**self._mapping("S01E02.mkv"), "episode": 2}]
        _job_id, task_id = self._task(mappings)
        self.service.organizer_config["bulk_operations_enabled"] = False
        self.admin.apply(task_id)
        execute = self.service._execute_operation

        def fail_second_move(operation):
            if operation["type"] == "move_file" and operation["source_path"].endswith("S01E02.mkv"):
                raise RuntimeError("provider unavailable for the second file")
            return execute(operation)

        with patch.object(self.service, "_execute_operation", side_effect=fail_second_move):
            applied = self.service.apply_task_from_worker(task_id)
        self.assertFalse(applied["success"], applied)
        before = self.db.organizer.get_organizer_task(task_id)
        self.assertTrue(self.cloud.exists(mappings[0]["target_path"]))
        self.assertTrue(self.cloud.exists(mappings[1]["source_path"]))
        mutations = list(self.cloud.mutations)
        queued = list(self.queued)

        with patch.object(self.service, "_list_openlist_tree_files", side_effect=AssertionError("must reject before scan")):
            rejected, status = self.admin.skip(task_id)

        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["code"], "MOVED_MAPPING_REQUIRES_ROLLBACK")
        self.assertIn(applied["run_id"], rejected["rollback_run_ids"])
        after = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["mappings"], before["mappings"])
        self.assertEqual(after["operations"], before["operations"])
        self.assertEqual(self.cloud.mutations, mutations)
        self.assertEqual(self.queued, queued)

    def test_skip_cannot_replace_an_unfinished_rollback_plan(self) -> None:
        _job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        self.admin.apply(task_id)
        applied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        with patch.object(self.service, "_execute_rollback_inverse", side_effect=RuntimeError("rollback interrupted")):
            rollback = self.service.rollback_run(applied["run_id"])
        self.assertFalse(rollback["success"], rollback)
        before = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(before["evidence"]["rollback"]["status"], "failed")
        mutations = list(self.cloud.mutations)

        with patch.object(self.service, "_list_openlist_tree_files", side_effect=AssertionError("must reject before scan")):
            rejected, status = self.admin.skip(task_id)

        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["code"], "ROLLBACK_INCOMPLETE")
        after = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["operations"], before["operations"])
        self.assertEqual(self.cloud.mutations, mutations)

    def test_skip_cannot_discard_a_failed_move_with_lost_provider_reply(self) -> None:
        mappings = [self._mapping("S01E01.mkv"), {**self._mapping("S01E02.mkv"), "episode": 2}]
        _job_id, task_id = self._task(mappings)
        self.service.organizer_config["bulk_operations_enabled"] = False
        self.admin.apply(task_id)
        execute = self.service._execute_operation

        def lose_move_reply(operation):
            if operation["type"] != "move_file":
                return execute(operation)
            if operation["source_path"].endswith("S01E01.mkv"):
                execute(operation)
                raise RuntimeError("provider reply lost after the first move")
            raise RuntimeError("provider unavailable for the second move")

        with patch.object(self.service, "_execute_operation", side_effect=lose_move_reply):
            applied = self.service.apply_task_from_worker(task_id)
        self.assertFalse(applied["success"], applied)
        before = self.db.organizer.get_organizer_task(task_id)
        self.assertTrue(self.cloud.exists(mappings[0]["target_path"]))
        self.assertTrue(self.cloud.exists(mappings[1]["source_path"]))
        self.assertTrue(all(op["status"] == "failed" for op in before["operations"] if op["type"] == "move_file"))
        mutations = list(self.cloud.mutations)

        with patch.object(self.service, "_list_openlist_tree_files", side_effect=AssertionError("must reconcile before scan")):
            rejected, status = self.admin.skip(task_id)

        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["code"], "MOVE_RESULT_UNCONFIRMED")
        self.assertIn(applied["run_id"], rejected["run_ids"])
        after = self.db.organizer.get_organizer_task(task_id)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["mappings"], before["mappings"])
        self.assertEqual(after["operations"], before["operations"])
        self.assertEqual(self.cloud.mutations, mutations)


if __name__ == "__main__":
    unittest.main()
