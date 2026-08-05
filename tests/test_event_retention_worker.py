from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.services.event_retention_worker import EventRetentionWorker


OLD_TIMESTAMP = "2000-01-01T00:00:00Z"


class EventRetentionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "events.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_job(self, status: str, suffix: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO import_jobs (
                    title, category, category_label, source_type, source_url,
                    target_route, status, created_at, updated_at
                ) VALUES (?, 'tv', '电视剧', 'quark', ?, 'quark_to_mobile', ?, ?, ?)
                """,
                (f"任务-{suffix}", f"https://example.invalid/{suffix}", status, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            return int(cursor.lastrowid)

    def _insert_guest_request(self, suffix: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO guest_requests (
                    request_token, title, category, source_type, source_url,
                    source_url_hash, status, public_status, created_at, updated_at
                ) VALUES (?, ?, 'tv', 'quark', ?, ?, 'done', '已完成', ?, ?)
                """,
                (
                    f"token-{suffix}",
                    f"请求-{suffix}",
                    f"https://example.invalid/request/{suffix}",
                    f"hash-{suffix}",
                    OLD_TIMESTAMP,
                    OLD_TIMESTAMP,
                ),
            )
            return int(cursor.lastrowid)

    def _worker(self, **overrides: object) -> EventRetentionWorker:
        options = {
            "database": self.database,
            "owner_id": "retention-test",
            "log": lambda _message: None,
            "retention_days": 1,
            "batch_size": 100,
            "max_delete_per_run": 100,
        }
        options.update(overrides)
        return EventRetentionWorker(**options)

    def test_preserves_recovery_events_for_non_terminal_jobs(self) -> None:
        active_job_id = self._insert_job("waiting_transfer", "active")
        terminal_job_id = self._insert_job("done", "terminal")
        with self.database.connect() as connection:
            for job_id, label in ((active_job_id, "active"), (terminal_job_id, "terminal")):
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, level, message, created_at)
                    VALUES (?, 'info', ?, ?)
                    """,
                    (job_id, label, OLD_TIMESTAMP),
                )
                connection.execute(
                    """
                    INSERT INTO rclone_file_events(
                        job_id, status, level, filename, message, created_at
                    ) VALUES (?, 'done', 'info', ?, ?, ?)
                    """,
                    (job_id, f"{label}.mkv", label, OLD_TIMESTAMP),
                )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 2)
        with self.database.connect() as connection:
            job_event_ids = {
                int(row["job_id"])
                for row in connection.execute("SELECT job_id FROM job_events").fetchall()
            }
            file_event_ids = {
                int(row["job_id"])
                for row in connection.execute("SELECT job_id FROM rclone_file_events").fetchall()
            }
        self.assertEqual(job_event_ids, {active_job_id})
        self.assertEqual(file_event_ids, {active_job_id})

    def test_prunes_events_for_legacy_terminal_job_statuses(self) -> None:
        statuses = ("success", "skipped_existing", "skipped", "rejected", "canceled")
        jobs = [
            (self._insert_job(status, f"legacy-terminal-{index}"), status)
            for index, status in enumerate(statuses)
        ]
        with self.database.connect() as connection:
            for job_id, status in jobs:
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, level, message, created_at)
                    VALUES (?, 'info', ?, ?)
                    """,
                    (job_id, status, OLD_TIMESTAMP),
                )
                connection.execute(
                    """
                    INSERT INTO rclone_file_events(
                        job_id, status, level, filename, message, created_at
                    ) VALUES (?, 'done', 'info', ?, ?, ?)
                    """,
                    (job_id, f"{status}.mkv", status, OLD_TIMESTAMP),
                )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], len(statuses) * 2)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 0)

    def test_preserves_unmatched_file_event_that_can_recover_active_job(self) -> None:
        active_job_id = self._insert_job("waiting_transfer", "unmatched")
        job_root = f"/媒体/电视剧/job-{active_job_id}"
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE import_jobs SET target_path = ?, raw_data = ? WHERE id = ?",
                (
                    job_root,
                    json.dumps(
                        {
                            "staging_plan": {
                                "enabled": True,
                                "job_id": active_job_id,
                                "provider_target_path": job_root,
                                "openlist_job_root": job_root,
                            }
                        },
                        ensure_ascii=False,
                    ),
                    active_job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO rclone_file_events(
                    status, level, category, filename, source_path, target_path, message, created_at
                ) VALUES ('done', 'info', 'tv', 'episode.mkv', ?, ?, 'unmatched recovery evidence', ?)
                """,
                (f"{job_root}/episode.mkv", f"{job_root}/episode.mkv", OLD_TIMESTAMP),
            )

        before = self.database.find_job_for_rclone_callback(
            "tv",
            "episode.mkv",
            f"{job_root}/episode.mkv",
            f"{job_root}/episode.mkv",
        )
        result = self._worker().run_once()

        self.assertEqual(int((before or {}).get("id") or 0), active_job_id)
        self.assertTrue(result["success"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 1)

    def test_deletes_old_unmatched_event_that_recovery_never_consumes(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO rclone_file_events(
                    status, level, category, filename, source_path, target_path, message, created_at
                ) VALUES ('failed', 'error', 'tv', 'failed.mkv', '/old/failed.mkv',
                          '/old/failed.mkv', 'not recovery evidence', ?)
                """,
                (OLD_TIMESTAMP,),
            )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 1)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 0)

    def test_deletes_old_recoverable_unmatched_event_without_active_job(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO rclone_file_events(
                    status, level, category, filename, source_path, target_path, message, created_at
                ) VALUES ('done', 'info', 'tv', 'orphan.mkv', '/old/orphan.mkv',
                          '/old/orphan.mkv', 'orphan recovery evidence', ?)
                """,
                (OLD_TIMESTAMP,),
            )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 1)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 0)

    def test_deleted_parent_does_not_leave_old_file_event_forever(self) -> None:
        job_id = self._insert_job("done", "deleted-parent")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO rclone_file_events(
                    job_id, status, level, category, filename, source_path, target_path, message, created_at
                ) VALUES (?, 'done', 'info', 'tv', 'old.mkv', '/old/old.mkv',
                          '/old/old.mkv', 'deleted parent', ?)
                """,
                (job_id, OLD_TIMESTAMP),
            )
            connection.execute("DELETE FROM import_jobs WHERE id = ?", (job_id,))
            row = connection.execute("SELECT job_id FROM rclone_file_events").fetchone()
            self.assertIsNone(row["job_id"])

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 1)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 0)

    def test_round_robin_cursor_prevents_later_tables_from_starving(self) -> None:
        terminal_job_id = self._insert_job("done", "fairness")
        guest_request_id = self._insert_guest_request("fairness")
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO job_events(job_id, level, message, created_at)
                VALUES (?, 'info', ?, ?)
                """,
                [(terminal_job_id, f"job-{index}", OLD_TIMESTAMP) for index in range(10)],
            )
            connection.execute(
                """
                INSERT INTO guest_request_events(request_id, level, message, created_at)
                VALUES (?, 'info', 'guest', ?)
                """,
                (guest_request_id, OLD_TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO rclone_events(level, message, created_at)
                VALUES ('info', 'run', ?)
                """,
                (OLD_TIMESTAMP,),
            )
            connection.execute(
                """
                INSERT INTO rclone_file_events(job_id, status, level, filename, message, created_at)
                VALUES (?, 'done', 'info', 'file.mkv', 'file', ?)
                """,
                (terminal_job_id, OLD_TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO update_events(level, message, created_at)
                VALUES ('info', 'update', ?)
                """,
                (OLD_TIMESTAMP,),
            )

        worker = self._worker(batch_size=1, max_delete_per_run=1)
        results = [worker.run_once() for _ in range(5)]

        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual([result["deleted"] for result in results], [1, 1, 1, 1, 1])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 9)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM guest_request_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rclone_file_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM update_events").fetchone()[0], 0)

    def test_stops_before_next_batch_when_lease_renewal_fails(self) -> None:
        terminal_job_id = self._insert_job("done", "lease")
        guest_request_id = self._insert_guest_request("lease")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events(job_id, level, message, created_at)
                VALUES (?, 'info', 'job', ?)
                """,
                (terminal_job_id, OLD_TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO guest_request_events(request_id, level, message, created_at)
                VALUES (?, 'info', 'guest', ?)
                """,
                (guest_request_id, OLD_TIMESTAMP),
            )

        original_acquire = self.database.acquire_scheduler_lease
        acquire_count = 0

        def acquire_once(name: str, owner_id: str, ttl_seconds: int) -> bool:
            nonlocal acquire_count
            acquire_count += 1
            if acquire_count > 1:
                return False
            return original_acquire(name, owner_id, ttl_seconds)

        self.database.acquire_scheduler_lease = acquire_once  # type: ignore[method-assign]
        result = self._worker(batch_size=1, max_delete_per_run=2).run_once()

        self.assertFalse(result["success"])
        self.assertTrue(result["lease_lost"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(acquire_count, 2)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM guest_request_events").fetchone()[0], 1)

    def test_schema_contains_retention_indexes_and_migration_marker(self) -> None:
        expected = {
            "job_events": "idx_job_events_created_at_id",
            "guest_request_events": "idx_guest_request_events_created_at_id",
            "rclone_events": "idx_rclone_events_created_at_id",
            "rclone_file_events": "idx_rclone_file_events_created_at_id",
            "update_events": "idx_update_events_created_at_id",
        }
        with self.database.connect() as connection:
            for table, index_name in expected.items():
                indexes = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
                }
                self.assertIn(index_name, indexes)
            migration = connection.execute(
                "SELECT name FROM schema_migrations WHERE version = 15"
            ).fetchone()
        self.assertIsNotNone(migration)
        self.assertEqual(str(migration["name"]), "event_retention_indexes")


if __name__ == "__main__":
    unittest.main()
