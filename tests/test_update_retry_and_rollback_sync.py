from __future__ import annotations

import json
import sqlite3
import unittest

from fnos_media_import.repositories.update_repository import UpdateRepository
from fnos_media_import.services.update_completion_sync_service import UpdateCompletionSyncService
from fnos_media_import.services.update_service import UpdateService
from tests.test_update_completion_sync_out_of_order import _FakeDb


class _RetryDb(_FakeDb):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.revocations = []
        self.pages = []

    def list_update_candidates(self, subscription_id, limit=500, offset=0):
        self.pages.append((limit, offset))
        return [
            {**item, "job_status": (self._jobs.get(item.get("job_id")) or {}).get("status")}
            for item in super().list_update_candidates(subscription_id, limit, offset)
        ]

    def revoke_update_seen_for_job(self, subscription_id, job_id, *, expected_rollback):
        if self._jobs[job_id]["status"] in {"done", "success"}:
            return None
        self.revocations.append((subscription_id, job_id, list(expected_rollback.get("target_paths") or [])))
        for item in self._candidates:
            if item.get("job_id") == job_id:
                self._seen_episodes.discard((item.get("season"), item.get("episode")))
        return 1


class UpdateRetrySyncTests(unittest.TestCase):
    @staticmethod
    def database(*, decision="submitted", status="failed", seen=False):
        return _RetryDb(
            {"id": 1, "title": "Show", "season": 1, "last_success_episode": 3,
             "next_episode": 4, "missing_episodes": [], "raw_data": {}},
            [{"id": 11, "season": 1, "episode": 4, "decision": decision, "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": status, "raw_data": {}}},
            seen_episodes={(1, 4)} if seen else set(),
        )

    @staticmethod
    def inflight(db):
        service = UpdateService.__new__(UpdateService)
        service.db = db
        return service._inflight_episodes(1)

    @staticmethod
    def synchronizer(db):
        seen = []

        def mark_seen(_subscription, candidate, *_args, **_kwargs):
            seen.append(candidate["id"])
            db._seen_episodes.add((candidate.get("season"), candidate["episode"]))

        return UpdateCompletionSyncService(database=db, mark_seen=mark_seen), seen

    def test_terminal_job_retry_is_inflight_and_later_completes_once(self):
        for failed_status in ("failed", "cancelled", "unsupported"):
            with self.subTest(status=failed_status):
                db = self.database(status=failed_status)
                sync, seen = self.synchronizer(db)
                self.assertEqual(sync.sync(1)["failed_episodes"], [4])
                self.assertEqual(db._candidates[0]["decision"], "failed")
                self.assertEqual(db._subscription["missing_episodes"], [4])

                db._jobs[101]["status"] = "organizing"
                self.assertEqual(self.inflight(db), {(1, 4)})
                self.assertEqual(sync.sync(1)["pending_episodes"], [4])
                self.assertEqual(db._candidates[0]["decision"], "submitted")
                self.assertEqual(db._subscription["missing_episodes"], [])

                db._jobs[101]["status"] = "done"
                self.assertEqual(self.inflight(db), {(1, 4)})
                self.assertEqual(sync.sync(1)["completed_episodes"], [4])
                self.assertEqual(db._candidates[0]["decision"], "completed")
                self.assertEqual(db._subscription["last_success_episode"], 4)
                self.assertEqual(db._subscription["next_episode"], 5)
                self.assertEqual(sync.sync(1)["checked"], 0)
                self.assertEqual(seen, [11])

    def test_reapply_completion_racing_old_rollback_sync_keeps_new_seen(self):
        db = self.database(decision="completed", status="review", seen=True)
        db._jobs[101]["raw_data"] = {"completion": {"rollback": {
            "status": "completed", "run_id": 7, "rollback_run_id": 8, "target_paths": ["/library/E04.mkv"],
        }}}
        revoke = db.revoke_update_seen_for_job

        def finish_before_revoke(*args, **kwargs):
            db._jobs[101] = {"id": 101, "status": "done", "raw_data": {"completion": {"stage": "done"}}}
            return revoke(*args, **kwargs)

        db.revoke_update_seen_for_job = finish_before_revoke
        sync, seen = self.synchronizer(db)
        self.assertEqual(sync.sync(1)["completed_episodes"], [4])
        self.assertEqual(db.revocations, [])
        self.assertEqual(db._seen_episodes, {(1, 4)})
        self.assertEqual(db._candidates[0]["decision"], "completed")
        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(seen, [11])

    def test_retry_completed_before_next_sync_recovers_failed_candidate(self):
        db = self.database()
        sync, seen = self.synchronizer(db)
        sync.sync(1)
        db._jobs[101]["status"] = "done"
        self.assertEqual(sync.sync(1)["completed_episodes"], [4])
        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(seen, [11])

    def test_old_failed_candidate_gets_missing_state_once_without_repeated_writes(self):
        db = self.database(decision="failed")
        sync, _seen = self.synchronizer(db)
        self.assertEqual(sync.sync(1)["failed_episodes"], [4])
        updates = len(db.candidate_updates)
        events = len(db.events)
        self.assertEqual(sync.sync(1)["checked"], 0)
        self.assertEqual(len(db.candidate_updates), updates)
        self.assertEqual(len(db.events), events)

    def test_rejected_or_unbound_candidates_are_not_restored(self):
        db = self.database(decision="rejected", status="done")
        db._candidates.append({"id": 12, "decision": "failed", "season": 1, "episode": 5})
        sync, seen = self.synchronizer(db)
        self.assertEqual(sync.sync(1)["checked"], 0)
        self.assertEqual(self.inflight(db), set())
        self.assertEqual(seen, [])
        self.assertEqual(db._candidates[0]["decision"], "rejected")

    def test_failed_retry_beyond_first_candidate_page_is_inflight(self):
        db = self.database(decision="failed", status="transferring")
        db._candidates = [{"id": i, "decision": "rejected"} for i in range(1, 501)] + db._candidates
        db._candidates[-1]["id"] = 501
        self.assertEqual(self.inflight(db), {(1, 4)})
        self.assertEqual(db.pages, [(500, 0), (500, 500)])

    def test_rollback_revokes_completion_until_same_job_is_reapplied(self):
        for rollback_status in ("running", "failed", "completed"):
            with self.subTest(status=rollback_status):
                db = self.database(decision="completed", status="review", seen=True)
                db._jobs[101]["raw_data"] = {"completion": {
                    "provider_completed": True, "retry_action": "media_refresh_only",
                    "rollback": {"status": rollback_status, "run_id": 7, "rollback_run_id": 8,
                                 "target_paths": ["/library/Show/E04.mkv"]},
                }}
                sync, seen = self.synchronizer(db)
                self.assertEqual(sync.sync(1)["review_episodes"], [4])
                self.assertEqual(db._candidates[0]["decision"], "review")
                self.assertEqual(db._subscription["missing_episodes"], [4])
                self.assertEqual(db._seen_episodes, set())
                self.assertEqual(db.revocations[0], (1, 101, ["/library/Show/E04.mkv"]))
                self.assertEqual(self.inflight(db), {(1, 4)})
                self.assertEqual(seen, [])

                db._jobs[101]["status"] = "organizing"
                self.assertEqual(sync.sync(1)["pending_episodes"], [4])
                self.assertEqual(seen, [])
                db._jobs[101].update(status="organizing", raw_data={"completion": {"stage": "organizing"}})
                self.assertEqual(sync.sync(1)["pending_episodes"], [4])
                self.assertNotIn("organizer_rollback", db._candidates[0]["raw_data"])
                db._jobs[101]["status"] = "done"
                self.assertEqual(sync.sync(1)["completed_episodes"], [4])
                self.assertEqual(db._subscription["missing_episodes"], [])
                self.assertEqual(db._seen_episodes, {(1, 4)})
                self.assertEqual(seen, [11])


class UpdateRollbackEvidenceRepositoryTests(unittest.TestCase):
    def test_revoke_only_undone_job_and_exact_paths_preserves_other_copies(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE update_seen_items (
                id INTEGER PRIMARY KEY, subscription_id INTEGER, source_type TEXT,
                file_id TEXT, raw_data TEXT
            );
            CREATE TABLE update_path_snapshots (subscription_id INTEGER, openlist_path TEXT);
            CREATE TABLE import_jobs (id INTEGER PRIMARY KEY, status TEXT, raw_data TEXT);
        """)
        connection.executemany("INSERT INTO update_seen_items VALUES (?, ?, ?, ?, ?)", [
            (1, 1, "quark", "", json.dumps({"job_id": 101})),
            (2, 1, "openlist", "/library/Show/E04.mkv", "{}"),
            (3, 1, "openlist", "/other-copy/Show/E04.mkv", "{}"),
            (4, 1, "quark", "", json.dumps({"job_id": 102})),
            (5, 2, "quark", "", json.dumps({"job_id": 101})),
            (6, 1, "openlist", "/library/Show/E04.mkv.extra", "{}"),
        ])
        connection.executemany("INSERT INTO update_path_snapshots VALUES (?, ?)", [(1, "/library"), (2, "/other")])
        marker = {"status": "completed", "run_id": 7, "rollback_run_id": 8, "target_paths": ["/library/Show/E04.mkv"]}
        connection.execute("INSERT INTO import_jobs VALUES (?, ?, ?)", (101, "review", json.dumps({"completion": {"rollback": marker}})))
        connection.commit()
        repo = UpdateRepository(
            lambda: connection, subscription_queries=None, update_runs=None,
            decode_run=dict, decode_candidate=dict, backup=lambda **_kwargs: {},
        )

        self.assertIsNone(repo.revoke_update_seen_for_job(1, 101, expected_rollback={**marker, "rollback_run_id": 6}))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM update_seen_items").fetchone()[0], 6)
        self.assertEqual(repo.revoke_update_seen_for_job(1, 101, expected_rollback=marker), 2)
        self.assertEqual([row[0] for row in connection.execute("SELECT id FROM update_seen_items ORDER BY id")], [3, 4, 5, 6])
        self.assertEqual([row[0] for row in connection.execute("SELECT subscription_id FROM update_path_snapshots")], [2])
        self.assertEqual(repo.revoke_update_seen_for_job(1, 101, expected_rollback=marker), 0)
        connection.execute("UPDATE import_jobs SET status = 'done'")
        connection.execute("INSERT INTO update_seen_items VALUES (?, ?, ?, ?, ?)", (7, 1, "quark", "", json.dumps({"job_id": 101})))
        connection.commit()
        self.assertIsNone(repo.revoke_update_seen_for_job(1, 101, expected_rollback=marker))
        self.assertIsNotNone(connection.execute("SELECT id FROM update_seen_items WHERE id = 7").fetchone())


if __name__ == "__main__":
    unittest.main()
