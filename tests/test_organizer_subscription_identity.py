from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.database import Database
from fnos_media_import.media_identity_contract import normalize_media_identity, subscription_import_identity
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.repositories.repository_set import OrganizerRepositories
from fnos_media_import.services.import_job_service import ImportJobCreationService, V1PlanningError
from fnos_media_import.services.update_candidate_import_service import UpdateCandidateImportService
from fnos_media_import.v1_cutover import analyze_database, apply_database


IDENTITY = {"media_type": "tv", "ns": "tmdb", "value": "123", "title": "Example", "year": 2026}
ROOT = "/library/Example (2026) [tmdb-123]"


class IdentityPayloadTests(unittest.TestCase):
    def test_identity_validation_matches_portable_manifest_boundaries(self):
        for identity in (
            {**IDENTITY, "year": 1799}, {**IDENTITY, "year": 2200}, {**IDENTITY, "year": True},
            {**IDENTITY, "ns": "local", "value": "zzzzzzzzzz"},
            {**IDENTITY, "ns": "imdb", "value": "tt0"},
            {**IDENTITY, "value": "1" * 65}, {**IDENTITY, "value": True},
        ):
            with self.subTest(identity=identity):
                self.assertEqual(normalize_media_identity(identity), {})
        self.assertEqual(normalize_media_identity({**IDENTITY, "ns": "local", "value": "ABCDEF0123"})["value"], "abcdef0123")

    def test_aliases_are_path_free_typed_identity_keys(self):
        identity = normalize_media_identity({**IDENTITY, "token": "secret", "aliases": [
            ["tv", "imdb", "tt123"], {"media_type": "tv", "ns": "tvdb", "value": "0012", "path": "/bad"},
            ["movie", "tmdb", "456"], "Title alias", ["tv", "tmdb", "../../bad"],
        ]})
        self.assertEqual(identity["aliases"], [["tv", "imdb", "tt123"], ["tv", "tvdb", "12"]])
        self.assertNotIn("token", identity)

    def test_new_subscription_with_tmdb_identity_can_create_first_library_root(self):
        subscription = {"id": 1, "title": "Example", "category": "tv", "tmdb_id": 123,
                        "identity_ns": "tmdb", "identity_value": "123", "raw_data": {"canonical_openlist_root": ROOT}}
        identity, intent = subscription_import_identity(subscription)
        self.assertEqual(identity["value"], "123")
        self.assertEqual(intent, "create_or_match")
        subscription["canonical_dir"] = ROOT
        self.assertEqual(subscription_import_identity(subscription)[1], "update_existing")

    def test_request_summary_persists_identity_intent_and_subscription_not_routes(self):
        summary = ImportJobCreationService._safe_request_summary({
            "media_identity": {**IDENTITY, "aliases": [["tv", "imdb", "tt123"]], "root": "/bad"},
            "organizer_intent": "update_existing", "update_subscription_id": 12, "target_path": "/bad",
        }, category_key="tv")
        self.assertEqual(summary["organizer_intent"], "update_existing")
        self.assertEqual(summary["update_subscription_id"], 12)
        self.assertEqual(summary["media_identity"]["aliases"], [["tv", "imdb", "tt123"]])
        self.assertNotIn("target_path", summary)
        self.assertNotIn("root", summary["media_identity"])

    def test_public_creation_rejects_subscription_reference(self):
        service = ImportJobCreationService.__new__(ImportJobCreationService)
        with self.assertRaises(V1PlanningError):
            service.create({"update_subscription_id": 12})

    def test_candidate_cannot_inject_identity_intent_or_routes(self):
        captured = {}
        subscription = {"id": 7, "title": "Example", "category": "tv", "tmdb_id": 123, "canonical_dir": ROOT}
        repo = SimpleNamespace(
            get_update_candidate=lambda _id: {"id": 1, "subscription_id": 7, "source_url": "https://example.invalid/share",
                "raw_data": {"candidate": {"import_payload": {"media_identity": {**IDENTITY, "value": "999"},
                    "organizer_intent": "create_or_match", "update_subscription_id": 99,
                    "target_path": "/bad", "category": "movie"}}}},
            get_update_subscription=lambda _id, **_kwargs: subscription,
            update_update_candidate=lambda *_args, **_kwargs: None,
            add_update_event=lambda *_args, **_kwargs: None,
        )
        def create(payload):
            captured.update(payload)
            return {"success": True, "job": {"id": 1, "status": "created"}}
        service = UpdateCandidateImportService(database=SimpleNamespace(update=repo),
            import_service=lambda: SimpleNamespace(create_internal_import_job=create),
            import_handler=lambda: None, mark_seen=lambda *_args, **_kwargs: None)
        service.import_candidate(1)
        self.assertEqual(captured["media_identity"]["value"], "123")
        self.assertEqual(captured["organizer_intent"], "update_existing")
        self.assertEqual(captured["update_subscription_id"], 7)
        self.assertEqual(captured["category"], "tv")
        self.assertNotIn("target_path", captured)


class IdentityBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="organizer-identity-")
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "test.db")
        self.database.init_schema()
        self.repo = RepositorySet.from_database(self.database)
        self.subscription_id = self.repo.update.create_update_subscription({"title": "Example", "category": "tv", "media_type": "tv", "tmdb_id": 123})

    def job(self, *, subscription_id=None):
        raw = {"request": {"update_subscription_id": subscription_id}} if subscription_id else {}
        with self.database.connect() as conn:
            cursor = conn.execute("""INSERT INTO import_jobs
                (title,category,category_label,source_provider_kind,source_form,source_url,status,raw_data,created_at,updated_at,destination_id,plan_version,idempotency_key)
                VALUES ('Example','tv','TV','quark','share','safe-reference','created',?,'now','now','test',1,?)""",
                (json.dumps(raw), f"job-{subscription_id}-{id(raw)}"))
            return int(cursor.lastrowid)

    def test_success_binds_before_candidate_link_and_is_idempotent(self):
        job_id = self.job(subscription_id=self.subscription_id)
        for _ in range(2):
            self.assertTrue(self.repo.update.bind_organized_subscription(job_id, IDENTITY, ROOT))
        saved = self.repo.update.get_update_subscription(self.subscription_id)
        self.assertEqual(saved["canonical_dir"], ROOT)
        self.assertEqual(saved["identity_ns"], "tmdb")
        self.assertEqual(saved["identity_value"], "123")
        self.assertEqual(subscription_import_identity(saved)[1], "update_existing")
        self.assertIs(OrganizerRepositories.from_set(self.repo).update, self.repo.update)

    def test_no_relationship_cannot_be_bound_by_caller_supplied_id(self):
        self.assertFalse(self.repo.update.bind_organized_subscription(self.job(), IDENTITY, ROOT, self.subscription_id))

    def test_candidate_link_is_valid_relationship(self):
        job_id = self.job()
        self.repo.update.create_update_candidate({"subscription_id": self.subscription_id, "job_id": job_id,
            "title": "Example", "source_type": "quark", "source_url": "https://example.invalid/share"})
        self.assertTrue(self.repo.update.bind_organized_subscription(job_id, IDENTITY, ROOT))

    def test_conflicting_identity_is_rejected_but_same_identity_refreshes_directory_cache(self):
        job_id = self.job(subscription_id=self.subscription_id)
        with self.assertRaises(ValueError):
            self.repo.update.bind_organized_subscription(job_id, {**IDENTITY, "value": "999"}, ROOT)
        self.repo.update.bind_organized_subscription(job_id, IDENTITY, ROOT)
        renamed = "/library/Renamed Example (2026) [tmdb-123]"
        self.assertTrue(self.repo.update.bind_organized_subscription(job_id, {**IDENTITY, "title": "Renamed Example"}, renamed))
        saved = self.repo.update.get_update_subscription(self.subscription_id)
        self.assertEqual(saved["canonical_dir"], renamed)
        self.assertEqual(saved["raw_data"]["canonical_openlist_root"], renamed)
        with self.assertRaises(ValueError):
            self.repo.update.bind_organized_subscription(job_id, {**IDENTITY, "value": "999"}, ROOT)

    def test_v29_offline_upgrade_is_additive_and_dry_run_does_not_modify_source(self):
        job_id = self.job(subscription_id=self.subscription_id)
        with self.database.connect() as conn:
            for column in ("identity_ns", "identity_value", "canonical_dir"):
                conn.execute(f"ALTER TABLE update_subscriptions DROP COLUMN {column}")
            conn.execute("DELETE FROM schema_migrations WHERE version=30")
        dry = analyze_database(self.database.path)
        self.assertEqual(dry["post_cutover_migrations"], [30])
        with self.database.connect() as conn:
            self.assertNotIn("canonical_dir", {row["name"] for row in conn.execute("PRAGMA table_info(update_subscriptions)")})
        result = apply_database(self.database.path)
        self.assertTrue(result["additive_only"])
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 30)
            self.assertIsNotNone(conn.execute("SELECT id FROM import_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertIsNone(conn.execute("SELECT canonical_dir FROM update_subscriptions WHERE id=?", (self.subscription_id,)).fetchone()[0])
        self.assertTrue(apply_database(self.database.path)["already_applied"])
        self.database.init_schema()

    def test_pre_cutover_database_runs_existing_cutover_then_additive_upgrade(self):
        with self.database.connect() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version >= 29")
        dry = analyze_database(self.database.path)
        self.assertEqual(dry["target_schema_version"], 30)
        result = apply_database(self.database.path)
        self.assertEqual(result["schema_version"], 29)
        self.assertEqual(result["post_cutover_migrations"], [30])
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 30)
            self.assertIsNotNone(conn.execute("SELECT id FROM update_subscriptions WHERE id=?", (self.subscription_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
