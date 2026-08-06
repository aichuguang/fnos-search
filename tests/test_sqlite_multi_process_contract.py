from __future__ import annotations

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fnos_media_import.database import Database


class SQLiteMultiProcessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "shared-app.db"
        self.web_db = Database(self.db_path)
        self.web_db.init_schema()
        self.worker_db = Database(self.db_path)
        self.worker_db.init_schema()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_two_database_instances_write_concurrently(self) -> None:
        def write_settings(database: Database, prefix: str) -> None:
            for index in range(30):
                database.set_app_settings({f"{prefix}.{index}": index})

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(write_settings, self.web_db, "web"),
                executor.submit(write_settings, self.worker_db, "worker"),
            ]
            for future in futures:
                future.result(timeout=10)

        settings = self.web_db.get_app_settings()
        self.assertEqual(settings["web.29"], 29)
        self.assertEqual(settings["worker.29"], 29)

    def test_scheduler_lease_is_mutually_exclusive_across_instances(self) -> None:
        self.assertTrue(self.web_db.acquire_scheduler_lease("shared", "web", 60))
        self.assertFalse(self.worker_db.acquire_scheduler_lease("shared", "worker", 60))
        self.assertFalse(self.worker_db.release_scheduler_lease("shared", "worker"))
        self.assertTrue(self.web_db.release_scheduler_lease("shared", "web"))
        self.assertTrue(self.worker_db.acquire_scheduler_lease("shared", "worker", 60))

    def test_wal_and_busy_timeout_allow_a_second_writer_to_wait(self) -> None:
        with self.web_db.connect() as first_connection:
            self.assertEqual(first_connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(first_connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            first_connection.execute("BEGIN IMMEDIATE")
            first_connection.execute(
                "INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                ("lock-holder", "true", "2026-08-06T00:00:00Z"),
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                waiting_write = executor.submit(
                    self.worker_db.set_app_settings,
                    {"waited-writer": True},
                )
                time.sleep(0.1)
                self.assertFalse(waiting_write.done())
                first_connection.commit()
                waiting_write.result(timeout=3)

        self.assertTrue(self.web_db.get_app_settings()["waited-writer"])


if __name__ == "__main__":
    unittest.main()
