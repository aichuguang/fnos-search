from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from fnos_media_import.database import Database
from fnos_media_import.repositories.database_domain_mixin import (
    HISTORY_CLEANUP_TABLES,
    HISTORY_PRESERVED_TABLES,
    DatabaseDomainMixin,
    OrganizerRepository,
    RcloneRepository,
    UpdateRepository,
    _normalize_match_text,
    utc_minutes_from_now,
    utc_now,
    utc_seconds_from_now,
)
from fnos_media_import.schema import BASE_SCHEMA_SQL


class DatabaseDomainMixinCompatibilityTests(unittest.TestCase):
    def test_historical_helpers_and_repository_exports_remain_importable(self) -> None:
        self.assertIn("job_events", HISTORY_CLEANUP_TABLES)
        self.assertIn("app_settings", HISTORY_PRESERVED_TABLES)
        self.assertTrue(callable(RcloneRepository))
        self.assertTrue(callable(OrganizerRepository))
        self.assertTrue(callable(UpdateRepository))
        self.assertEqual(_normalize_match_text("电视剧：示例"), "电视剧示例")
        for value in (utc_now(), utc_minutes_from_now(1), utc_seconds_from_now(1)):
            self.assertRegex(value, re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"))

    def test_legacy_mixin_method_forwards_to_split_repository(self) -> None:
        database = DatabaseDomainMixin()
        database.rclone = SimpleNamespace(count_rclone_runs=lambda: 7)

        self.assertEqual(database.count_rclone_runs(), 7)

    def test_old_style_connect_only_subclass_lazily_builds_repositories(self) -> None:
        class LegacyDatabase(DatabaseDomainMixin):
            def __init__(self, path: Path) -> None:
                self.path = path

            @contextmanager
            def connect(self) -> Iterator[sqlite3.Connection]:
                connection = sqlite3.connect(self.path)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                try:
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = LegacyDatabase(Path(temp_dir) / "legacy.db")
            with legacy.connect() as connection:
                connection.executescript(BASE_SCHEMA_SQL)

            run_id = legacy.create_rclone_run("compat-test")

            self.assertGreater(run_id, 0)
            self.assertEqual(legacy.count_rclone_runs(), 1)
            self.assertEqual(legacy.count_organizer_tasks(), 0)
            self.assertEqual(legacy.count_update_subscriptions(), 0)

    def test_current_database_keeps_legacy_mixin_mro_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "app.db")
            self.assertIsInstance(database, DatabaseDomainMixin)


if __name__ == "__main__":
    unittest.main()
