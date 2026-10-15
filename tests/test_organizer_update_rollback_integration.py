from __future__ import annotations

import unittest

from fnos_media_import.services.update_completion_sync_service import UpdateCompletionSyncService
from fnos_media_import.services.update_service import UpdateService
from tests import test_organizer_review_recovery as fixtures


class OrganizerUpdateRollbackIntegrationTests(unittest.TestCase):
    setUp = fixtures.OrganizerReviewRecoveryTests.setUp
    _task = fixtures.OrganizerReviewRecoveryTests._task
    _queue = fixtures.OrganizerReviewRecoveryTests._queue
    _mapping = staticmethod(fixtures.OrganizerReviewRecoveryTests._mapping)

    def test_real_repository_rollback_and_corrected_reapply_update_subscription(self):
        job_id, task_id = self._task([self._mapping("S01E01.mkv")])
        subscription_id = self.db.update.create_update_subscription({
            "title": "Show", "category": "tv", "season": 1, "next_episode": 1,
        })
        candidate_id = self.db.update.create_update_candidate({
            "subscription_id": subscription_id, "job_id": job_id, "title": "Show E01",
            "source_type": "quark", "source_url": "https://example.invalid/share/episode-1",
            "season": 1, "episode": 1, "decision": "submitted", "raw_data": {},
        })
        updates = UpdateService.__new__(UpdateService)
        updates.db = self.db
        sync = UpdateCompletionSyncService(database=self.db, mark_seen=updates._mark_candidate_seen)
        self.admin.apply(task_id)
        applied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertEqual(sync.sync(subscription_id)["completed_episodes"], [1])
        mapping = self.db.organizer.get_organizer_task(task_id)["mappings"][0]
        self.db.update.upsert_update_seen_item({
            "subscription_id": subscription_id, "fingerprint": "openlist-seen-episode-1",
            "source_type": "openlist", "file_id": mapping["target_path"],
            "season": 1, "episode": 1, "raw_data": {"path": mapping["target_path"]},
        })

        rolled_back = self.service.rollback_run(applied["run_id"])
        self.assertTrue(rolled_back["success"], rolled_back)
        self.assertEqual(sync.sync(subscription_id)["review_episodes"], [1])
        self.assertEqual(self.db.update.get_update_candidate(candidate_id)["decision"], "review")
        self.assertEqual(self.db.update.list_update_seen_episodes(subscription_id), set())
        self.assertEqual(self.db.update.get_update_subscription(subscription_id)["missing_episodes"], [1])
        self.assertEqual(updates._inflight_episodes(subscription_id), {(1, 1)})

        changed = self.service.update_mapping(mapping["id"], {"title": "Corrected"}, task_id=task_id)
        self.assertTrue(changed["success"], changed)
        self.admin.apply(task_id)
        reapplied = self.service.apply_task_from_worker(task_id)
        self.assertTrue(reapplied["success"], reapplied)
        self.assertNotIn("rollback", self.db.job_queries.get(job_id)["raw_data"]["completion"])
        self.assertEqual(sync.sync(subscription_id)["completed_episodes"], [1])
        self.assertEqual(self.db.update.get_update_candidate(candidate_id)["decision"], "completed")
        self.assertEqual(self.db.update.list_update_seen_episodes(subscription_id), {(1, 1)})
        self.assertEqual(self.db.update.get_update_subscription(subscription_id)["missing_episodes"], [])
        self.assertTrue(self.cloud.exists(changed["target_path"]))
        self.assertEqual(sync.sync(subscription_id)["completed_count"], 0)


if __name__ == "__main__":
    unittest.main()
