from __future__ import annotations

import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.database import Database


class SchemaUpgradeCompatibilityTests(unittest.TestCase):
    def test_fresh_database_records_reserved_v20(self) -> None:
        root = Path(__file__).resolve().parents[1]
        database_path = root / f"schema-fresh-{uuid.uuid4().hex}.db"
        try:
            Database(database_path).init_schema()

            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT name FROM schema_migrations WHERE version = 20"
                ).fetchone()

            self.assertEqual(("reserved_organizer_multi_resource_movies",), row)
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{database_path}{suffix}")
                if path.exists():
                    path.unlink()

    def test_historical_multi_resource_v20_is_accepted(self) -> None:
        root = Path(__file__).resolve().parents[1]
        database_path = root / f"schema-historical-v20-{uuid.uuid4().hex}.db"
        try:
            database = Database(database_path)
            database.init_schema()
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    "UPDATE schema_migrations SET name = ? WHERE version = 20",
                    ("organizer_multi_resource_movies",),
                )
                connection.commit()

            database.init_schema()

            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    "SELECT version, name FROM schema_migrations WHERE version = 20"
                ).fetchall()

            self.assertEqual([(20, "organizer_multi_resource_movies")], rows)
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{database_path}{suffix}")
                if path.exists():
                    path.unlink()

    def test_database_newer_than_code_is_rejected_before_schema_changes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        database_path = root / f"schema-newer-version-{uuid.uuid4().hex}.db"
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, name, applied_at)
                    VALUES (21, 'future_schema', '2026-01-01T00:00:00Z');
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "v21.*v20"):
                Database(database_path).init_schema()

            with closing(sqlite3.connect(database_path)) as connection:
                import_jobs = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'import_jobs'"
                ).fetchone()
            self.assertIsNone(import_jobs)
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{database_path}{suffix}")
                if path.exists():
                    path.unlink()

    def test_init_schema_upgrades_legacy_organizer_tables_before_creating_lease_index(self) -> None:
        root = Path(__file__).resolve().parents[1]
        database_path = root / f"schema-upgrade-compatibility-{uuid.uuid4().hex}.db"
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );

                    CREATE TABLE organizer_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id INTEGER,
                        request_id INTEGER,
                        rclone_run_id INTEGER,
                        trigger_type TEXT,
                        category TEXT NOT NULL,
                        category_label TEXT,
                        title TEXT,
                        source_keyword TEXT,
                        openlist_root_path TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confidence REAL DEFAULT 0,
                        media_type TEXT,
                        tmdb_id INTEGER,
                        tmdb_title TEXT,
                        tmdb_year TEXT,
                        error_message TEXT,
                        evidence TEXT,
                        raw_data TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE organizer_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        summary TEXT,
                        undo_data TEXT,
                        error_message TEXT
                    );

                    CREATE TABLE organizer_locks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lock_key TEXT NOT NULL UNIQUE,
                        task_id INTEGER,
                        run_id INTEGER,
                        owner TEXT,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    [
                        (version, f"legacy_{version}", "2026-01-01T00:00:00Z")
                        for version in range(1, 12)
                    ],
                )
                connection.commit()

            database = Database(database_path)
            with patch.object(database, "_backup_before_pending_migrations"):
                database.init_schema()
                database.init_schema()

            with closing(sqlite3.connect(database_path)) as connection:
                organizer_run_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(organizer_runs)")
                }
                organizer_task_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(organizer_tasks)")
                }
                organizer_lock_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(organizer_locks)")
                }
                indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(organizer_runs)")
                }
                organizer_task_indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(organizer_tasks)")
                }
                versions = {
                    row[0] for row in connection.execute("SELECT version FROM schema_migrations")
                }

            self.assertTrue({"owner_id", "heartbeat_at", "lease_expires_at", "task_revision"} <= organizer_run_columns)
            self.assertTrue({"revision", "scan_owner", "scan_lease_expires_at"} <= organizer_task_columns)
            self.assertIn("expires_at", organizer_lock_columns)
            self.assertIn("idx_organizer_runs_owner_status", indexes)
            self.assertIn("idx_organizer_runs_active_lease", indexes)
            self.assertIn("idx_organizer_tasks_scan_lease", organizer_task_indexes)
            self.assertTrue({12, 13, 14} <= versions)
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{database_path}{suffix}")
                if path.exists():
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
