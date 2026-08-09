from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import base64
import hashlib
import json
import sqlite3
import unittest
from contextlib import contextmanager

from fnos_media_import.accounts.repository import ProviderAccountRepository
from fnos_media_import.domain.models import SelectionSnapshot, SourceDescriptor
from fnos_media_import.planning.source_resolver import SourceResolver
from fnos_media_import.repositories.job_command_repository import JobCommandRepository
from fnos_media_import.services.source_record_service import SourceRecordService


class _MemoryDatabase(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

    @contextmanager
    def connect(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


def _source_schema(db: _MemoryDatabase) -> None:
    db.connection.executescript(
        """
        CREATE TABLE source_records (
          source_record_id TEXT PRIMARY KEY,
          search_source_id TEXT NOT NULL,
          provider_kind TEXT NOT NULL,
          source_form TEXT NOT NULL,
          canonical_source_id TEXT NOT NULL,
          current_revision INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE source_record_revisions (
          source_record_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          access_payload_ciphertext BLOB NOT NULL,
          credential_key_id TEXT NOT NULL,
          selected_items_json TEXT NOT NULL,
          selected_items_hash TEXT NOT NULL,
          expected_file_count INTEGER,
          expected_total_bytes INTEGER,
          created_at TEXT NOT NULL,
          access_payload_hash TEXT NOT NULL,
          PRIMARY KEY (source_record_id, revision)
        );
        """
    )


class SourceSnapshotIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _MemoryDatabase()
        _source_schema(self.db)
        self.service = SourceRecordService(
            self.db, lambda: b"0123456789abcdef0123456789abcdef"
        )
        self.source = SourceDescriptor(
            provider_kind="quark",
            source_form="share",
            canonical_source_id="quark:share:abc",
            search_source_id="quark",
            expected_file_count=2,
            expected_total_bytes=123,
            selection=SelectionSnapshot("quark"),
        )

    def test_capture_uses_caller_transaction(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.service.capture(
                    self.source,
                    {"url": "https://pan.quark.cn/s/abc", "password": "p"},
                    connection=connection,
                )
                raise RuntimeError("rollback")
        self.assertIsNone(self.db.connection.execute("SELECT 1 FROM source_records").fetchone())

    def test_payload_hash_and_identity_are_verified_on_resolve(self) -> None:
        captured = self.service.capture(
            self.source,
            {"url": "https://pan.quark.cn/s/abc", "password": "p"},
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE source_record_revisions SET access_payload_hash=?",
                ("forged",),
            )
        with self.assertRaisesRegex(ValueError, "payload hash"):
            self.service.resolve(captured.source_record_id, captured.source_record_revision)


class SourceIdentityTests(unittest.TestCase):
    def test_generic_share_endpoints_do_not_collide(self) -> None:
        resolver = SourceResolver(lambda _url: {"provider_kind": "mobile", "source_form": "share"})
        first = resolver.resolve({"url": "https://example.test/share.html?code=one"})
        second = resolver.resolve({"url": "https://example.test/share.html?code=two"})
        reordered = resolver.resolve(
            {"url": "https://example.test/share.html?utm_source=x&code=one"}
        )
        self.assertNotEqual(first.canonical_source_id, second.canonical_source_id)
        self.assertEqual(first.canonical_source_id, reordered.canonical_source_id)

    def test_magnet_btih_is_canonical_across_query_order_and_encoding(self) -> None:
        resolver = SourceResolver(
            lambda _url: {
                "provider_kind": "sixpan",
                "source_form": "magnet",
                "search_source_id": "sixpan_magnet",
            }
        )
        infohash = "0123456789abcdef0123456789abcdef01234567"
        encoded = "urn%3Abtih%3A" + infohash.upper()
        first = resolver.resolve(
            {"url": f"magnet:?xt=urn:btih:{infohash}&dn=Example&tr=https%3A%2F%2Ftracker"}
        )
        reordered = resolver.resolve(
            {"url": f"magnet:?tr=https%3A%2F%2Fother&dn=Renamed&xt={encoded}"}
        )
        self.assertEqual(first.canonical_source_id, reordered.canonical_source_id)
        self.assertTrue(first.canonical_source_id.endswith(infohash))

    def test_magnet_base32_btih_matches_hex_identity(self) -> None:
        resolver = SourceResolver(
            lambda _url: {
                "provider_kind": "sixpan",
                "source_form": "magnet",
                "search_source_id": "sixpan_magnet",
            }
        )
        raw_hash = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        base32_hash = base64.b32encode(raw_hash).decode("ascii")
        from_hex = resolver.resolve(
            {"url": f"magnet:?xt=urn:btih:{raw_hash.hex()}"}
        )
        from_base32 = resolver.resolve(
            {"url": f"magnet:?dn=Example&xt=urn:btih:{base32_hash}"}
        )
        self.assertEqual(from_hex.canonical_source_id, from_base32.canonical_source_id)

    def test_invalid_magnet_xt_uses_stable_fallback(self) -> None:
        resolver = SourceResolver(
            lambda _url: {
                "provider_kind": "sixpan",
                "source_form": "magnet",
                "search_source_id": "sixpan_magnet",
            }
        )
        first = resolver.resolve({"url": "magnet:?dn=Example&xt=urn:sha1:not-btih"})
        reordered = resolver.resolve({"url": "magnet:?xt=urn:sha1:not-btih&dn=Example"})
        self.assertEqual(first.canonical_source_id, reordered.canonical_source_id)


class SelectionSnapshotTests(unittest.TestCase):
    def test_files_match_but_different_folders_produce_different_hashes(self) -> None:
        first = SelectionSnapshot.from_payload(
            "mobile",
            {
                "selection": {
                    "selected_files": [{"id": "file-1", "name": "episode.mkv"}],
                    "selected_folders": [{"id": "folder-a", "name": "Season A"}],
                }
            },
        )
        second = SelectionSnapshot.from_payload(
            "mobile",
            {
                "selection": {
                    "selected_files": [{"id": "file-1", "name": "episode.mkv"}],
                    "selected_folders": [{"id": "folder-b", "name": "Season B"}],
                }
            },
        )

        self.assertNotEqual(first.selection_hash, second.selection_hash)
        self.assertEqual(
            first.provider_selection["generic_selection"]["selected_files"],
            [{"id": "file-1", "name": "episode.mkv"}],
        )
        self.assertEqual(
            first.provider_selection["generic_selection"]["selected_folders"],
            [{"id": "folder-a", "name": "Season A"}],
        )

    def test_typed_refs_are_stable_across_field_and_item_order(self) -> None:
        first = SelectionSnapshot.from_payload(
            "tianyi",
            {
                "selection": {
                    "selected_files": [{"id": "file-b"}, {"id": "file-a"}],
                    "selected_folders": [{"id": "folder-a"}],
                }
            },
        )
        second = SelectionSnapshot.from_payload(
            "tianyi",
            {
                "selection": {
                    "selected_folders": [{"id": "folder-a"}],
                    "selected_files": [{"id": "file-a"}, {"id": "file-b"}],
                }
            },
        )

        self.assertEqual(first.selection_hash, second.selection_hash)
        restored = SelectionSnapshot.from_dict(first.to_dict())
        self.assertTrue(restored.is_valid)
        self.assertEqual(first.selection_hash, restored.selection_hash)


class JobIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _MemoryDatabase()
        self.db.connection.executescript(
            """
            CREATE TABLE import_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL, category TEXT NOT NULL, category_label TEXT NOT NULL,
              source_provider_kind TEXT NOT NULL, source_form TEXT NOT NULL,
              source_url TEXT NOT NULL, password TEXT,
              target_path TEXT, status TEXT NOT NULL,
              external_task_id TEXT, error_message TEXT, raw_data TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE, config_revision INTEGER NOT NULL,
              executor_id TEXT, destination_id TEXT, plan_version INTEGER
            );
            """
        )
        self.repo = JobCommandRepository(lambda: self.db.connect(), lambda *_args: False)

    @staticmethod
    def _job(key: str) -> dict:
        return {
            "title": "x", "category": "movie", "category_label": "电影",
            "source_provider_kind": "quark", "source_form": "share_link",
            "source_url": "source-record:abc@1", "status": "created",
            "raw_data": {"source": {"record_id": "abc", "revision": 1}},
            "idempotency_key": key, "config_revision": 1,
            "executor_id": "test", "destination_id": "target-a", "plan_version": 1,
        }

    def test_v1_does_not_use_broad_url_category_deduplication(self) -> None:
        with self.db.connect() as connection:
            first, created = self.repo.create_with_connection(connection, self._job("v1:a"))
            second, second_created = self.repo.create_with_connection(connection, self._job("v1:b"))
        self.assertTrue(created)
        self.assertTrue(second_created)
        self.assertNotEqual(first, second)
        with self.db.connect() as connection:
            duplicate, duplicate_created = self.repo.create_with_connection(connection, self._job("v1:a"))
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, first)

    def test_v1_repository_redacts_sensitive_job_fields(self) -> None:
        job = self._job("v1:secret")
        job["source_url"] = "https://example.test/share?token=secret"
        job["password"] = "extract-code"
        job["raw_data"] = {
            "request": {
                "url": job["source_url"],
                "password": job["password"],
                "selection": {"file_id": "safe"},
            }
        }
        with self.db.connect() as connection:
            job_id, _created = self.repo.create_with_connection(connection, job)
            row = connection.execute(
                "SELECT source_url, password, raw_data FROM import_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        self.assertTrue(str(row["source_url"]).startswith("source-ref:"))
        self.assertEqual(row["password"], "")
        persisted = json.loads(row["raw_data"])
        self.assertNotIn("url", persisted["request"])
        self.assertNotIn("password", persisted["request"])
        self.assertEqual(persisted["request"]["selection"], {"file_id": "safe"})


class AccountReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _MemoryDatabase()
        self.db.connection.executescript(
            """
            CREATE TABLE provider_accounts (
              account_id TEXT PRIMARY KEY, provider_kind TEXT, name TEXT, enabled INTEGER,
              current_revision INTEGER, identity_hint TEXT, last_test_status TEXT,
              last_test_message TEXT, last_tested_at TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE provider_account_revisions (
              account_id TEXT, revision INTEGER, config_json TEXT, credentials_ciphertext BLOB,
              credential_key_id TEXT, created_at TEXT, PRIMARY KEY(account_id, revision)
            );
            CREATE TABLE provider_account_selections (
              provider_kind TEXT, scope TEXT, account_id TEXT, account_revision INTEGER,
              selection_revision INTEGER, selected_at TEXT, PRIMARY KEY(provider_kind, scope)
            );
            CREATE TABLE provider_account_selection_revisions (
              provider_kind TEXT, scope TEXT, selection_revision INTEGER, account_id TEXT,
              account_revision INTEGER, selected_at TEXT,
              PRIMARY KEY(provider_kind, scope, selection_revision)
            );
            CREATE TABLE import_execution_plans (
              job_id INTEGER PRIMARY KEY, schema_version INTEGER, plan_json TEXT,
              plan_hash TEXT, created_at TEXT
            );
            CREATE TABLE transfer_executions (
              job_id INTEGER PRIMARY KEY, phase TEXT, snapshot_json TEXT
            );
            """
        )
        self.repo = ProviderAccountRepository(self.db)

    def test_live_canonical_execution_blocks_delete_reference(self) -> None:
        plan = json.dumps({"source": {"account_id": "a1"}}, separators=(",", ":"))
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (1,1,?,?,?)",
                (plan, hashlib.sha256(plan.encode()).hexdigest(), "now"),
            )
            connection.execute(
                "INSERT INTO transfer_executions VALUES (1,'transferring','{}')"
            )
        self.assertEqual(self.repo.count_non_terminal_jobs_referencing_account("a1"), 1)

    def test_completed_canonical_execution_preserves_history_reference(self) -> None:
        plan = json.dumps({"destination": {"account_id": "a1"}}, separators=(",", ":"))
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (1,1,?,?,?)",
                (plan, hashlib.sha256(plan.encode()).hexdigest(), "now"),
            )
            connection.execute(
                "INSERT INTO transfer_executions VALUES (1,'completed','{}')"
            )
        self.assertEqual(self.repo.count_non_terminal_jobs_referencing_account("a1"), 1)

    def test_failed_execution_still_preserves_frozen_account_revision(self) -> None:
        plan = json.dumps({"source": {"account_id": "a1"}}, separators=(",", ":"))
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (1,1,?,?,?)",
                (plan, hashlib.sha256(plan.encode()).hexdigest(), "now"),
            )
            connection.execute("INSERT INTO transfer_executions VALUES (1,'failed','{}')")
        self.assertEqual(self.repo.count_non_terminal_jobs_referencing_account("a1"), 1)

    def test_delete_removes_unreferenced_selection_history_before_revision(self) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO provider_accounts VALUES "
                "('a1','quark','A',0,1,'','ok','',NULL,'now','now')"
            )
            connection.execute(
                "INSERT INTO provider_account_revisions VALUES "
                "('a1',1,'{}',X'00','key','now')"
            )
            connection.execute(
                "INSERT INTO provider_account_selection_revisions VALUES "
                "('quark','default',1,'a1',1,'now')"
            )
        self.repo.delete_account("a1")
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM provider_accounts WHERE account_id='a1'"
            ).fetchone()
        )
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM provider_account_revisions WHERE account_id='a1'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
