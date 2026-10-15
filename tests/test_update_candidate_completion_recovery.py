from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.services.update_candidate_import_service import UpdateCandidateImportService
from fnos_media_import.services.update_completion_sync_service import UpdateCompletionSyncService
from tests.test_update_retry_and_rollback_sync import _RetryDb


class UpdateCandidateCompletionRecoveryTests(unittest.TestCase):
    def test_completed_job_seen_write_failure_is_recovered_by_normal_sync(self) -> None:
        candidate = {
            "id": 11, "subscription_id": 1, "season": 1, "episode": 4,
            "decision": "selected", "title": "Show S01E04", "source_url": "source-record:test",
            "job_id": None, "raw_data": {},
        }
        job = {"id": 101, "status": "done", "raw_data": {}}
        database = _RetryDb(
            {"id": 1, "title": "Show", "season": 1, "last_success_episode": 3,
             "next_episode": 4, "missing_episodes": [4], "raw_data": {}},
            [candidate], {101: job},
        )
        database.get_update_candidate = lambda _candidate_id: dict(database._candidates[0])
        imports = SimpleNamespace(create_internal_import_job=lambda _payload: {"success": True, "job": job})
        seen_calls = []

        def mark_seen(_subscription, row, *_args, **_kwargs):
            seen_calls.append(row["id"])
            if len(seen_calls) == 1:
                raise RuntimeError("transient seen write failure")
            database._seen_episodes.add((row["season"], row["episode"]))

        importer = UpdateCandidateImportService(
            database=database,
            import_service=lambda: imports,
            import_handler=lambda: None,
            mark_seen=mark_seen,
        )
        with self.assertRaisesRegex(RuntimeError, "transient seen write failure"):
            importer.import_candidate(11)
        self.assertEqual(database._candidates[0]["job_id"], 101)
        self.assertEqual(database._candidates[0]["decision"], "submitted")
        self.assertEqual(database._seen_episodes, set())

        synchronizer = UpdateCompletionSyncService(database=database, mark_seen=mark_seen)
        recovered = synchronizer.sync(1)

        self.assertEqual(recovered["completed_episodes"], [4])
        self.assertEqual(database._candidates[0]["decision"], "completed")
        self.assertEqual(database._seen_episodes, {(1, 4)})
        self.assertEqual(database._subscription["last_success_episode"], 4)
        self.assertEqual(database._subscription["next_episode"], 5)
        self.assertEqual(database._subscription["missing_episodes"], [])
        self.assertEqual(synchronizer.sync(1)["checked"], 0)
        self.assertEqual(seen_calls, [11, 11])


if __name__ == "__main__":
    unittest.main()
