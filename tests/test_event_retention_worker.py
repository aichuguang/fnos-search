from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.event_retention_worker import EventRetentionWorker
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository

from tests._rclone_v1_fixtures import rclone_v1_job, rclone_v1_plan


OLD_TIMESTAMP = "2000-01-01T00:00:00Z"


class EventRetentionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = (
            Path(__file__).resolve().parents[1]
            / "codex_tmp"
            / "event-retention-tests"
            / uuid4().hex
        )
        self.temp_dir.mkdir(parents=True, exist_ok=False)
        self.db_path = self.temp_dir / "events.db"
        self._database_infrastructure = Database(self.db_path)
        self._database_infrastructure.init_schema()
        self.database = RepositorySet.from_database(self._database_infrastructure)

    def tearDown(self) -> None:
        for suffix in ("", "-journal", "-shm", "-wal"):
            (Path(f"{self.db_path}{suffix}")).unlink(missing_ok=True)
        self.temp_dir.rmdir()

    def _insert_job(self, status: str, suffix: str) -> int:
        job_id, created = self.database.job_commands.create(
            {
                "title": f"job-{suffix}",
                "category": "tv",
                "category_label": "TV",
                "source_provider_kind": "quark",
                "source_form": "share",
                "source_url": f"source-record:event-retention-{suffix}",
                "target_path": f"/openlist/tv/{suffix}",
                "status": status,
                "destination_id": "native-main",
                "plan_version": 1,
                "idempotency_key": f"event-retention:{suffix}",
            }
        )
        self.assertTrue(created)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE import_jobs SET created_at = ?, updated_at = ? WHERE id = ?",
                (OLD_TIMESTAMP, OLD_TIMESTAMP, job_id),
            )
        return job_id

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

    def _insert_update_subscription(self, suffix: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO update_subscriptions (
                    title, category, category_label, media_type, schedule_kind,
                    timezone, source_strategy, auto_import_policy, status,
                    created_at, updated_at
                ) VALUES (?, 'tv', 'TV', 'tv', 'manual', 'Asia/Shanghai',
                          'search', 'review', 'enabled', ?, ?)
                """,
                (f"subscription-{suffix}", OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            return int(cursor.lastrowid)

    def _insert_rclone_run(
        self,
        suffix: str,
        *,
        job_id: int | None = None,
        status: str = "success",
    ) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rclone_runs (
                    trigger_reason, operation_key, job_id, copy_descriptor_json,
                    status, started_at, finished_at, updated_at
                ) VALUES ('retention-test', ?, ?, '{}', ?, ?, ?, ?)
                """,
                (
                    f"event-retention-run:{suffix}",
                    job_id,
                    status,
                    OLD_TIMESTAMP,
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
        plan = rclone_v1_plan(
            active_job_id,
            category="tv",
            staging_root="/媒体/电视剧",
        )
        job = rclone_v1_job(plan, status="waiting_transfer")
        TransferExecutionRepository(self.database).create(
            active_job_id,
            plan,
            phase="transferring",
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE import_jobs
                SET target_path = ?, raw_data = ?, plan_version = 1, destination_id = ?
                WHERE id = ?
                """,
                (
                    job_root,
                    json.dumps(job["raw_data"], ensure_ascii=False),
                    job["destination_id"],
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

        before = self.database.rclone.find_job_for_rclone_callback(
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

        original_acquire = self.database.scheduler_leases.acquire
        acquire_count = 0

        def acquire_once(name: str, owner_id: str, ttl_seconds: int) -> bool:
            nonlocal acquire_count
            acquire_count += 1
            if acquire_count > 1:
                return False
            return original_acquire(name, owner_id, ttl_seconds)

        self.database.scheduler_leases.acquire = acquire_once  # type: ignore[method-assign]
        result = self._worker(batch_size=1, max_delete_per_run=2).run_once()

        self.assertFalse(result["success"])
        self.assertTrue(result["lease_lost"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(acquire_count, 2)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM guest_request_events").fetchone()[0], 1)

    def test_prunes_only_disposable_rclone_runs(self) -> None:
        disposable_job_id = self._insert_job("done", "run-disposable")
        active_job_id = self._insert_job("waiting_transfer", "run-active")
        failed_job_id = self._insert_job("failed", "run-failed")
        review_job_id = self._insert_job("failed", "run-review")
        organizer_job_id = self._insert_job("done", "run-organizer")

        disposable_run_id = self._insert_rclone_run("disposable", job_id=disposable_job_id)
        active_run_id = self._insert_rclone_run("active", job_id=active_job_id)
        failed_run_id = self._insert_rclone_run("failed", job_id=failed_job_id)
        review_run_id = self._insert_rclone_run("review", job_id=review_job_id)
        organizer_run_id = self._insert_rclone_run("organizer", job_id=organizer_job_id)

        execution_repository = TransferExecutionRepository(self.database)
        execution_repository.create(
            failed_job_id,
            rclone_v1_plan(failed_job_id),
            phase="failed",
        )
        execution_repository.create(
            review_job_id,
            rclone_v1_plan(review_job_id),
            phase="manual_review",
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO organizer_tasks (
                    job_id, rclone_run_id, trigger_type, category, category_label,
                    title, openlist_root_path, status, created_at, updated_at
                ) VALUES (?, ?, 'rclone', 'tv', 'TV', 'linked', '/linked',
                          'done', ?, ?)
                """,
                (organizer_job_id, organizer_run_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted_by_table"].get("rclone_runs"), 1)
        with self.database.connect() as connection:
            remaining = {
                int(row["id"])
                for row in connection.execute("SELECT id FROM rclone_runs").fetchall()
            }
        self.assertNotIn(disposable_run_id, remaining)
        self.assertEqual(
            remaining,
            {active_run_id, failed_run_id, review_run_id, organizer_run_id},
        )

    def test_prunes_terminal_update_history_and_expired_caches(self) -> None:
        subscription_id = self._insert_update_subscription("history")
        with self.database.connect() as connection:
            terminal_run = connection.execute(
                """
                INSERT INTO update_runs (
                    subscription_id, trigger_type, status, started_at, finished_at
                ) VALUES (?, 'manual', 'success', ?, ?)
                """,
                (subscription_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            preserved_run = connection.execute(
                """
                INSERT INTO update_runs (
                    subscription_id, trigger_type, status, started_at, finished_at
                ) VALUES (?, 'manual', 'success', ?, ?)
                """,
                (subscription_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            empty_run = connection.execute(
                """
                INSERT INTO update_runs (
                    subscription_id, trigger_type, status, started_at, finished_at
                ) VALUES (?, 'manual', 'failed', ?, ?)
                """,
                (subscription_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            terminal_run_id = int(terminal_run.lastrowid)
            preserved_run_id = int(preserved_run.lastrowid)
            empty_run_id = int(empty_run.lastrowid)
            connection.execute(
                """
                INSERT INTO update_candidates (
                    subscription_id, run_id, title, source_type, source_url,
                    source_url_hash, decision, created_at
                ) VALUES (?, ?, 'completed', 'quark', 'https://example.invalid/completed',
                          'completed-hash', 'completed', ?)
                """,
                (subscription_id, terminal_run_id, OLD_TIMESTAMP),
            )
            for decision in ("review", "submitted", "auto_import"):
                connection.execute(
                    """
                    INSERT INTO update_candidates (
                        subscription_id, run_id, title, source_type, source_url,
                        source_url_hash, decision, created_at
                    ) VALUES (?, ?, ?, 'quark', ?, ?, ?, ?)
                    """,
                    (
                        subscription_id,
                        preserved_run_id,
                        decision,
                        f"https://example.invalid/{decision}",
                        f"{decision}-hash",
                        decision,
                        OLD_TIMESTAMP,
                    ),
                )
            connection.execute(
                """
                INSERT INTO update_seen_items (
                    subscription_id, fingerprint, first_seen_at, last_seen_at
                ) VALUES (?, 'business-state', ?, ?)
                """,
                (subscription_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO update_preview_cache (
                    source_type, source_url_hash, expires_at, created_at, updated_at
                ) VALUES ('quark', 'expired-preview', ?, ?, ?)
                """,
                (OLD_TIMESTAMP, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )
            connection.execute(
                """
                INSERT INTO update_path_snapshots (
                    subscription_id, openlist_path, captured_at, expires_at
                ) VALUES (?, '/expired', ?, ?)
                """,
                (subscription_id, OLD_TIMESTAMP, OLD_TIMESTAMP),
            )

        result = self._worker().run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted_by_table"].get("update_candidates"), 1)
        self.assertEqual(result["deleted_by_table"].get("update_runs"), 2)
        self.assertEqual(result["deleted_by_table"].get("update_preview_cache"), 1)
        self.assertEqual(result["deleted_by_table"].get("update_path_snapshots"), 1)
        with self.database.connect() as connection:
            decisions = {
                str(row["decision"])
                for row in connection.execute(
                    "SELECT decision FROM update_candidates"
                ).fetchall()
            }
            run_ids = {
                int(row["id"])
                for row in connection.execute("SELECT id FROM update_runs").fetchall()
            }
            self.assertEqual(decisions, {"review", "submitted", "auto_import"})
            self.assertEqual(run_ids, {preserved_run_id})
            self.assertNotIn(terminal_run_id, run_ids)
            self.assertNotIn(empty_run_id, run_ids)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM update_seen_items").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM update_preview_cache").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM update_path_snapshots").fetchone()[0], 0)

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
