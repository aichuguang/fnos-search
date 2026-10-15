from __future__ import annotations

import json
import posixpath
import sqlite3
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from fnos_media_import.planning.job_plan import attach_plan_integrity
from fnos_media_import.transfer import (
    PHASE_ACK_PENDING,
    PHASE_FINAL_VERIFYING,
    PHASE_MANUAL_REVIEW,
    PHASE_ORGANIZING,
    PHASE_RETRY_WAIT,
    TransferOrchestrator,
    TransferPlan,
)
from fnos_media_import.transfer.dispatch import TransferDispatchService, _OrganizerAdapter
from fnos_media_import.transfer.verifier import TransferVerifier
from tests._organizer_v1_fixtures import attach_organizer_v1_task, organizer_v1_plan


class OrganizerFinalManifestTests(unittest.TestCase):
    parent = "/disk/tv/Show/Season 01"
    episode = parent + "/Show.S01E02.mkv"

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE organizer_tasks (
                id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT, revision INTEGER,
                category TEXT, raw_data TEXT, evidence TEXT, error_message TEXT
            );
            CREATE TABLE organizer_files (id INTEGER PRIMARY KEY, task_id INTEGER, path TEXT, size INTEGER);
            CREATE TABLE organizer_mappings (
                id INTEGER PRIMARY KEY, task_id INTEGER, file_id INTEGER, status TEXT,
                source_path TEXT, target_path TEXT
            );
            CREATE TABLE organizer_operations (id INTEGER PRIMARY KEY, task_id INTEGER, status TEXT);
        """)
        raw = organizer_v1_plan(1, category="tv", organizer_root="/disk/staging/job-1", destination_final="/disk/tv")
        raw["verification"].update(expected_file_count=1, expected_total_size=100)
        attach_plan_integrity(raw)
        self.plan = TransferPlan.from_dict(raw)
        self.task = attach_organizer_v1_task({"id": 7, "status": "done", "evidence": {}}, raw)
        self.connection.execute(
            "INSERT INTO organizer_tasks VALUES (7, 1, 'done', 1, ?, ?, '{}', '')",
            (self.task["category"], json.dumps(self.task["raw_data"])),
        )
        self.database = SimpleNamespace(connect=lambda: nullcontext(self.connection))
        self.adapter = _OrganizerAdapter(self.database, SimpleNamespace())
        self.actual: dict[str, dict] = {}
        self.listed: list[str] = []
        self.on_list = lambda: None
        frozen = {**self.plan.organizer_path, "enabled": True, "webdav_root": "/disk"}

        def client_for_mount(mount_id, *, revision):
            self.assertEqual(mount_id, frozen["mount_id"])
            self.assertEqual(revision, frozen["mount_revision"])
            return SimpleNamespace(exists=lambda path: path in self.actual, list_dir=self._list_dir), frozen

        mount_service = SimpleNamespace(
            get_revision=lambda mount_id, revision: frozen,
            client_for_mount=client_for_mount,
            resolve_mount_absolute_path=lambda mount, path: path,
        )
        dispatch = object.__new__(TransferDispatchService)
        dispatch.mount_service = mount_service
        self.verifier = TransferVerifier(dispatch._list_target)
        self._mappings([(self.episode, 100, "ready")])
        self._remote([(self.episode, 100)])

    def _list_dir(self, path):
        self.listed.append(path)
        self.on_list()
        return [{"path": path, "name": posixpath.basename(path), "is_dir": True}, *self.actual[path]]

    def _mappings(self, items):
        self.connection.execute("DELETE FROM organizer_mappings")
        self.connection.execute("DELETE FROM organizer_files")
        confirmed = []
        for index, (target, size, status) in enumerate(items, 1):
            source = f"/disk/staging/job-1/source-{index}.mkv"
            self.connection.execute("INSERT INTO organizer_files VALUES (?, 7, ?, ?)", (index, source, size))
            self.connection.execute(
                "INSERT INTO organizer_mappings VALUES (?, 7, ?, ?, ?, ?)",
                (index, index, status, source, target),
            )
            if status in {"ready", "skipped_existing"} and target.endswith(".mkv"):
                confirmed.append(target)
        self.connection.execute(
            "UPDATE organizer_tasks SET evidence=? WHERE id=7",
            (json.dumps({"completion_confirmation": {"success": True, "confirmed_paths": confirmed}}),),
        )

    def _remote(self, files):
        self.actual = {}
        for path, size in files:
            self.actual.setdefault(posixpath.dirname(path), []).append(
                {"path": path, "name": posixpath.basename(path), "size": size, "is_dir": False}
            )

    def _orchestrator(self):
        orchestrator = TransferOrchestrator(
            self.plan, {}, organizer_service=self.adapter, verifier=self.verifier,
        )
        orchestrator.state.phase = PHASE_ORGANIZING
        orchestrator.state.staging_verified = True
        self.assertEqual(orchestrator.step().phase, PHASE_FINAL_VERIFYING)
        self.assertEqual(orchestrator.state.organizer_task_id, 7)
        return orchestrator

    def test_new_episode_in_existing_season_only_verifies_current_mapping(self):
        self._remote([(self.parent + "/Show.S01E01.mkv", 900), (self.episode, 100)])
        orchestrator = self._orchestrator()
        self.assertEqual(orchestrator.step().phase, PHASE_ACK_PENDING)
        self.assertTrue(orchestrator.state.final_verified)
        self.assertEqual(self.listed, [self.parent])

    def test_deleted_ads_and_skipped_files_are_not_final_targets(self):
        self.plan.raw["verification"].update(expected_file_count=3, expected_total_size=130)
        self.plan = TransferPlan.from_dict(attach_plan_integrity(self.plan.raw))
        self.task = attach_organizer_v1_task(self.task, self.plan.raw)
        self.connection.execute("UPDATE organizer_tasks SET raw_data=?", (json.dumps(self.task["raw_data"]),))
        self._mappings([
            (self.episode, 100, "ready"),
            ("/disk/staging/job-1/ad.mkv", 10, "delete_ad"),
            ("/disk/staging/job-1/skip.mkv", 20, "skipped"),
        ])
        self.assertEqual(self._orchestrator().step().phase, PHASE_ACK_PENDING)
        self.assertEqual(self.listed, [self.parent])

    def test_retained_sidecar_is_checked_without_counting_unrelated_artwork(self):
        subtitle = self.parent + "/Show.S01E02.srt"
        self._mappings([(self.episode, 100, "ready"), (subtitle, 30, "ready")])
        self._remote([(self.episode, 100), (subtitle, 30), (self.parent + "/poster.jpg", 50)])
        self.assertEqual(self._orchestrator().step().phase, PHASE_ACK_PENDING)
        self.assertEqual(self.listed, [self.parent], "one listing per parent, not one request per file")

    def test_extra_file_cannot_substitute_for_missing_task_file(self):
        self._remote([(self.parent + "/unrelated.mkv", 100)])
        orchestrator = self._orchestrator()
        self.assertEqual(orchestrator.step().phase, PHASE_RETRY_WAIT)
        self.assertFalse(orchestrator.state.app_acked)
        self.assertIn("Show.S01E02.mkv", orchestrator.state.message)

    def test_matching_total_size_cannot_hide_wrong_individual_file_sizes(self):
        subtitle = self.parent + "/Show.S01E02.srt"
        self._mappings([(self.episode, 100, "ready"), (subtitle, 30, "ready")])
        self._remote([(self.episode, 99), (subtitle, 31)])
        self.assertEqual(self._orchestrator().step().phase, PHASE_RETRY_WAIT)

    def test_passthrough_unknown_source_size_checks_exact_file_existence(self):
        self._mappings([(self.episode, None, "ready")])
        self.assertEqual(self._orchestrator().step().phase, PHASE_ACK_PENDING)

    def test_skipped_existing_file_is_verified_and_directory_is_not_a_file(self):
        self._mappings([(self.episode, 100, "skipped_existing")])
        self.assertEqual(self._orchestrator().step().phase, PHASE_ACK_PENDING)
        self.actual[self.parent][0]["is_dir"] = True
        self.assertEqual(self._orchestrator().step().phase, PHASE_RETRY_WAIT)

    def test_known_zero_size_is_not_treated_as_unknown(self):
        subtitle = self.parent + "/empty.srt"
        self._mappings([(self.episode, 100, "ready"), (subtitle, 0, "ready")])
        self._remote([(self.episode, 100), (subtitle, 1)])
        self.assertEqual(self._orchestrator().step().phase, PHASE_RETRY_WAIT)

    def test_missing_or_unrelated_completion_evidence_fails_closed(self):
        for change in (
            "UPDATE organizer_tasks SET raw_data='{}'",
            "UPDATE organizer_tasks SET evidence='{}'",
            "UPDATE organizer_tasks SET status='cancelled'",
            "UPDATE organizer_tasks SET job_id=2",
            "UPDATE organizer_tasks SET category='movie'",
            "UPDATE organizer_mappings SET file_id=999",
            "INSERT INTO organizer_operations VALUES (1, 7, 'pending')",
        ):
            with self.subTest(change=change):
                self.connection.execute("SAVEPOINT invalid_evidence")
                self.connection.execute(change)
                result = self.adapter.verify_final(self.plan, organizer_task_id=7, verifier=self.verifier)
                self.assertEqual(result.status, "unknown")
                self.assertEqual(self.listed, [])
                self.connection.execute("ROLLBACK TO invalid_evidence")
                self.connection.execute("RELEASE invalid_evidence")

    def test_rollback_or_revision_change_during_remote_read_cannot_authorize_ack(self):
        orchestrator = self._orchestrator()
        self.on_list = lambda: self.connection.execute("UPDATE organizer_tasks SET status='executing', revision=2")
        self.assertEqual(orchestrator.step().phase, PHASE_MANUAL_REVIEW)
        self.assertFalse(orchestrator.state.final_verified)
        self.assertFalse(orchestrator.state.app_acked)

    def test_restored_final_verification_reloads_manifest_not_snapshot_directory(self):
        original = self._orchestrator()
        snapshot = original.snapshot()
        snapshot["organizer_final_path"] = "/wrong/shared/directory"
        restored = TransferOrchestrator(
            self.plan, {}, organizer_service=self.adapter, verifier=self.verifier,
        ).restore(snapshot)
        self.assertEqual(restored.step().phase, PHASE_ACK_PENDING)
        self.assertEqual(self.listed, [self.parent])

    def test_non_organizer_directory_verification_contract_is_unchanged(self):
        result = TransferVerifier(lambda _descriptor: {"exists": True, "file_count": 2, "total_size": 200}).verify(
            descriptor={"path": self.parent}, expected_file_count=1, expected_total_size=100,
        )
        self.assertEqual(result.status, "missing")


if __name__ == "__main__":
    unittest.main()
