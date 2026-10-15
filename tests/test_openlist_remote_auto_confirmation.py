from __future__ import annotations

import copy
import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from unittest.mock import patch

from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist.runtime_sync import OpenListSyncConflict
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError, RcloneWebdavConfigService
from fnos_media_import.storage.setup_service import StorageSetupService
from tests.test_rclone_webdav_config import FakeRcloneRunner


class _MemoryDatabase:
    path = ":memory:"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE openlist_connections (
                connection_id TEXT PRIMARY KEY, name TEXT, base_url TEXT, username TEXT,
                password_ciphertext BLOB, credential_key_id TEXT, enabled INTEGER, revision INTEGER,
                last_test_status TEXT, last_tested_at TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE openlist_connection_revisions (
                connection_id TEXT, revision INTEGER, config_json TEXT, password_ciphertext BLOB,
                credential_key_id TEXT, created_at TEXT, PRIMARY KEY(connection_id, revision)
            );
            CREATE TABLE import_execution_plans (job_id INTEGER PRIMARY KEY, plan_json TEXT);
            INSERT INTO import_execution_plans VALUES (15, '{"version":1,"source_job":15}');
        """)

    @contextmanager
    def connect(self):
        with self.lock:
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise


class OpenListRemoteAutoConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _MemoryDatabase()
        self.addCleanup(self.db.connection.close)
        self.connections = self._connections()
        self.connections.runtime_sync.memory.clear()
        self.connections.create_connection({
            "connection_id": "openlist-main", "name": "Unit test",
            "base_url": "http://openlist.invalid/dav", "username": "test-user", "password": "test-password",
        })
        self.settings = {"remote_name": "MP", "config_path": "/config/rclone/rclone.conf"}
        self.runner = FakeRcloneRunner({"MP": {
            "type": "webdav", "url": "http://openlist.invalid/dav", "user": "test-user", "pass": "old-obscured",
        }})
        self.worker = RcloneWebdavConfigService(lambda: self.settings, runner=self.runner, timeout_seconds=5)
        self.worker.set_connection_service(self.connections)
        self.initial_rows = self._rows()

    def _connections(self):
        return OpenListConnectionService(self.db, lambda: b"k" * 32)

    def _rows(self):
        with self.db.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("openlist_connections", "openlist_connection_revisions", "import_execution_plans")
            }

    def _writes(self):
        return [call for call in self.runner.calls if call["command"][-1] == self.worker._SAVE_SCRIPT]

    def _probes(self):
        return [call for call in self.runner.calls if "lsd" in call["command"]]

    def _set_marker(self, phase="confirmed", **changes):
        marker = {
            "token": "finished-owner", "phase": phase,
            "fingerprint": self.connections.runtime_fingerprint("openlist-main", 1),
            "managed_remote": self.worker._managed_remote_identity(),
            **changes,
        }
        self.connections.runtime_sync.memory["remote-state.json"] = marker
        return marker

    def _advance_revision(self, *, base_url=None):
        with self.db.connect() as connection:
            frozen = connection.execute("SELECT * FROM openlist_connection_revisions WHERE revision=1").fetchone()
            config = json.loads(frozen["config_json"])
            if base_url is not None:
                config["base_url"] = base_url
                connection.execute("UPDATE openlist_connections SET base_url=?", (base_url,))
            connection.execute("UPDATE openlist_connections SET revision=2")
            connection.execute(
                "INSERT INTO openlist_connection_revisions VALUES (?,2,?,?,?,?)",
                (frozen["connection_id"], json.dumps(config), frozen["password_ciphertext"], frozen["credential_key_id"], frozen["created_at"]),
            )

    def test_missing_marker_with_existing_remote_is_probed_and_confirmed_without_db_mutation(self):
        self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))
        result = self.worker.ensure_confirmation()
        self.assertTrue(result["success"])
        self.assertTrue(result["auto_confirmed"])
        self.assertFalse(result["remote_rebuilt"])
        self.assertEqual(result["sync_status"], "confirmed")
        self.assertEqual(result["connection_status"], "success")
        self.assertEqual(len(self._writes()), 1)
        self.assertEqual(len(self._probes()), 1)
        self.assertEqual(self._rows(), self.initial_rows)
        self.assertTrue(self.connections.runtime_identity_matches("openlist-main", 1))
        marker = self.connections.runtime_sync.state()
        self.assertRegex(marker["remote_config_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotIn("test-password", json.dumps(marker))
        self.assertNotIn("obscured-value", json.dumps(marker))
        self.assertNotIn("_openlist_sync", result)
        self.assertNotIn("test-password", json.dumps(result))
        self.assertNotIn("old-obscured", json.dumps(result))

    def test_missing_remote_is_rebuilt_from_the_unique_existing_connection(self):
        self.runner.configs.clear()
        result = self.worker.ensure_confirmation()
        self.assertTrue(result["remote_rebuilt"])
        self.assertTrue(result["auto_confirmed"])
        self.assertEqual(self.runner.configs["MP"]["operation"], "create")
        self.assertEqual(self._rows(), self.initial_rows)
        self.assertTrue(self.connections.runtime_identity_matches("openlist-main", 1))

    def test_trusted_confirmation_is_a_noop_without_network_or_remote_writes(self):
        self.worker.ensure_confirmation()
        marker = self.connections.runtime_sync.state()
        self.runner.calls.clear()
        result = self.worker.ensure_confirmation()
        self.assertFalse(result["auto_confirmed"])
        self.assertFalse(result["remote_rebuilt"])
        self.assertEqual(self._writes(), [])
        self.assertEqual(self._probes(), [])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.connections.runtime_sync.state(), marker)
        self.assertEqual(self._rows(), self.initial_rows)

    def test_password_drift_or_removal_requires_actual_sync_and_probe(self):
        self.worker.ensure_confirmation()
        for changed_password in ("different-nonempty-obscured", "", None):
            with self.subTest(password_state=bool(changed_password)):
                self.runner.configs["MP"]["pass"] = changed_password
                self.runner.calls.clear()
                self.assertEqual(self.worker.status()["sync_status"], "confirmation_required")
                with self.assertRaises(RcloneWebdavConfigError):
                    self.worker.test()
                self.assertEqual(self._writes(), [])
                self.assertEqual(self._probes(), [])
                result = self.worker.ensure_confirmation()
                self.assertTrue(result["auto_confirmed"])
                self.assertEqual(len(self._writes()), 1)
                self.assertEqual(len(self._probes()), 1)
                self.assertEqual(self.worker.status()["sync_status"], "confirmed")
                self.runner.calls.clear()
                self.assertFalse(self.worker.ensure_confirmation()["auto_confirmed"])
                self.assertEqual(self._writes(), [])
                self.assertEqual(self._probes(), [])
        self.assertEqual(self._rows(), self.initial_rows)

    def test_legacy_marker_without_actual_config_digest_is_rebuilt_once(self):
        self.worker.ensure_confirmation()
        self.connections.runtime_sync.memory["remote-state.json"].pop("remote_config_fingerprint")
        self.runner.calls.clear()
        self.assertEqual(self.worker.status()["sync_status"], "confirmation_required")
        self.assertTrue(self.worker.ensure_confirmation()["auto_confirmed"])
        self.assertEqual(len(self._writes()), 1)
        self.assertEqual(len(self._probes()), 1)
        marker = self.connections.runtime_sync.state()
        self.runner.calls.clear()
        for _ in range(3):
            self.assertFalse(self.worker.ensure_confirmation()["auto_confirmed"])
        self.assertEqual(self._writes(), [])
        self.assertEqual(self._probes(), [])
        self.assertEqual(self.connections.runtime_sync.state(), marker)

    def test_unknown_and_pending_markers_do_not_carry_a_previous_config_digest(self):
        self.worker.ensure_confirmation()
        sync = self.connections.runtime_sync
        old_digest = sync.state()["remote_config_fingerprint"]
        for phase in ("pending", "unknown"):
            with self.subTest(phase=phase), sync.mutation() as token, sync.remote_lock.hold():
                self.assertTrue(sync.set_state(
                    token, phase,
                    fingerprint=self.connections.runtime_fingerprint("openlist-main", 1),
                    managed_remote=self.worker._managed_remote_identity(),
                    remote_config_fingerprint=old_digest,
                ))
                self.assertNotIn("remote_config_fingerprint", sync.state())
                self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))

    def test_password_drift_failed_probe_never_restores_confirmed_marker(self):
        self.worker.ensure_confirmation()
        self.runner.configs["MP"]["pass"] = "different-obscured"
        self.runner.fail_test = True
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.ensure_confirmation()
        marker = self.connections.runtime_sync.state()
        self.assertNotEqual(marker["phase"], "confirmed")
        self.assertNotIn("remote_config_fingerprint", marker)
        self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))

    def test_probe_rechecks_actual_password_digest_before_reporting_success(self):
        self.worker.ensure_confirmation()

        def mutate_after_probe(command, **kwargs):
            result = self.runner(command, **kwargs)
            if "lsd" in command:
                self.runner.configs["MP"]["pass"] = "changed-during-probe"
            return result

        self.worker._runner = mutate_after_probe
        with self.assertRaises(RcloneWebdavConfigError) as raised:
            self.worker.test()
        self.assertEqual(raised.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")

    def test_web_confirmation_writeback_preserves_worker_actual_config_digest(self):
        setup = StorageSetupService(
            accounts_service=object(), connection_service=self.connections,
            mount_service=object(), target_service=object(), assignment_service=object(),
            connection_factory=self.db.connect, rclone_webdav_sync=self.worker.save,
        )
        setup._sync_rclone_webdav("openlist-main", expected_revision=1)
        marker = self.connections.runtime_sync.state()
        self.assertRegex(marker["remote_config_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.worker.status()["sync_status"], "confirmed")
        self.runner.calls.clear()
        self.assertFalse(self.worker.ensure_confirmation()["auto_confirmed"])
        self.assertEqual(self._writes(), [])
        self.assertEqual(self._probes(), [])
        self.assertEqual(self._rows(), self.initial_rows)

    def test_two_workers_only_sync_once_and_second_observes_confirmation(self):
        entered = threading.Event()
        release = threading.Event()

        def gated(command, **kwargs):
            if command[-1] == self.worker._SAVE_SCRIPT:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test writer was not released")
            return self.runner(command, **kwargs)

        self.worker._runner = gated
        second = RcloneWebdavConfigService(lambda: dict(self.settings), runner=gated, timeout_seconds=5)
        second.set_connection_service(self._connections())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(self.worker.ensure_confirmation)
            self.assertTrue(entered.wait(1))
            second_result = pool.submit(second.ensure_confirmation)
            try:
                with self.assertRaises(FutureTimeout):
                    second_result.result(timeout=0.03)
            finally:
                release.set()
            self.assertTrue(first_result.result(timeout=2)["auto_confirmed"])
            self.assertFalse(second_result.result(timeout=2)["auto_confirmed"])
        self.assertEqual(len(self._writes()), 1)
        self.assertEqual(len(self._probes()), 1)

    def test_legacy_fingerprint_or_scope_less_marker_requires_real_revalidation(self):
        for scope in (self.worker._managed_remote_identity(), None):
            with self.subTest(scope=scope):
                marker = self._set_marker(fingerprint="old-fingerprint")
                if scope is None:
                    marker.pop("managed_remote")
                self.runner.calls.clear()
                self.assertTrue(self.worker.ensure_confirmation()["auto_confirmed"])
                self.assertEqual(len(self._writes()), 1)
                self.assertEqual(len(self._probes()), 1)
                self.assertTrue(self.connections.runtime_identity_matches("openlist-main", 1))
        self.assertEqual(self._rows(), self.initial_rows)

    def test_released_pending_and_unknown_markers_are_revalidated(self):
        for phase in ("pending", "unknown"):
            with self.subTest(phase=phase):
                self._set_marker(phase, fingerprint="")
                self.runner.calls.clear()
                self.assertTrue(self.worker.ensure_confirmation()["auto_confirmed"])
                self.assertEqual(len(self._probes()), 1)
                self.assertTrue(self.connections.runtime_identity_matches("openlist-main", 1))

    def test_known_scope_change_is_not_automatically_overwritten(self):
        self.worker.ensure_confirmation()
        marker = self.connections.runtime_sync.state()
        original = dict(self.settings)
        for change in ({"remote_name": "OTHER"}, {"config_path": "/another/rclone.conf"}):
            with self.subTest(change=change):
                self.settings.clear()
                self.settings.update({**original, **change})
                self.runner.calls.clear()
                with self.assertRaises(RcloneWebdavConfigError) as raised:
                    self.worker.ensure_confirmation()
                self.assertEqual(raised.exception.code, "RCLONE_SYNC_CONFLICT")
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(self.connections.runtime_sync.state(), marker)

    def test_actual_remote_drift_is_refused_even_with_a_trusted_marker(self):
        self.worker.ensure_confirmation()
        original = copy.deepcopy(self.runner.configs["MP"])
        for change in ({"type": "s3"}, {"url": "http://other.invalid/dav"}, {"user": "other-user"}):
            with self.subTest(change=change):
                self.runner.configs["MP"] = {**original, **change}
                self.runner.calls.clear()
                with self.assertRaises(RcloneWebdavConfigError) as raised:
                    self.worker.ensure_confirmation()
                self.assertEqual(raised.exception.code, "RCLONE_SYNC_CONFLICT")
                self.assertEqual(self._writes(), [])
                self.assertEqual(self._probes(), [])
                self.assertEqual(self.runner.configs["MP"], {**original, **change})

    def test_malformed_remote_is_not_treated_as_missing(self):
        self.runner.configs["MP"] = "malformed"
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.ensure_confirmation()
        self.assertEqual(self._writes(), [])

    def test_corrupt_marker_is_not_treated_as_an_interrupted_valid_sync(self):
        for marker in (
            {"phase": "unknown"}, {"phase": "confirmed", "token": "old"},
            {"phase": "unsupported", "token": "old", "fingerprint": ""},
            {"phase": "unknown", "token": "old", "fingerprint": "", "managed_remote": "invalid"},
        ):
            with self.subTest(marker=marker):
                self.connections.runtime_sync.memory["remote-state.json"] = marker
                self.runner.calls.clear()
                with self.assertRaises(RcloneWebdavConfigError) as raised:
                    self.worker.ensure_confirmation()
                self.assertEqual(raised.exception.code, "RCLONE_SYNC_CONFIRMATION_INVALID")
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(self.connections.runtime_sync.state(), marker)

    def test_read_failure_does_not_create_confirmation(self):
        self.runner.fail_dump_on = 1
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.ensure_confirmation()
        self.assertEqual(self._writes(), [])
        self.assertEqual(self.connections.runtime_sync.state(), {})

    def test_failed_probe_restores_config_and_does_not_confirm(self):
        original = copy.deepcopy(self.runner.configs)
        self.runner.fail_test = True
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.ensure_confirmation()
        self.assertEqual(self.runner.configs, original)
        self.assertNotEqual(self.connections.runtime_sync.state().get("phase"), "confirmed")
        self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))
        self.assertEqual(self._rows(), self.initial_rows)
        self.runner.fail_test = False
        self.assertTrue(self.worker.ensure_confirmation()["auto_confirmed"])

    def test_read_after_write_failure_does_not_confirm(self):
        self.runner.fail_dump_on = 3
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.ensure_confirmation()
        self.assertNotEqual(self.connections.runtime_sync.state().get("phase"), "confirmed")
        self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))

    def test_revision_change_during_initial_read_refuses_any_write(self):
        def mutate_revision(command, **kwargs):
            result = self.runner(command, **kwargs)
            if "dump" in command and self.runner.dump_calls == 1:
                self._advance_revision()
            return result
        self.worker._runner = mutate_revision
        with self.assertRaises(RcloneWebdavConfigError) as raised:
            self.worker.ensure_confirmation()
        self.assertEqual(raised.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self.assertEqual(self._writes(), [])
        self.assertNotEqual(self.connections.runtime_sync.state().get("phase"), "confirmed")

    def test_revision_change_during_write_refuses_stale_confirmation_and_rollback(self):
        def mutate_revision(command, **kwargs):
            result = self.runner(command, **kwargs)
            if command[-1] == self.worker._SAVE_SCRIPT:
                self._advance_revision()
            return result
        self.worker._runner = mutate_revision
        with self.assertRaises(RcloneWebdavConfigError) as raised:
            self.worker.ensure_confirmation()
        self.assertEqual(raised.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self.assertEqual(len(self._writes()), 1)
        self.assertFalse(any(call["command"][-1] == self.worker._RESTORE_SCRIPT for call in self.runner.calls))
        self.assertNotEqual(self.connections.runtime_sync.state().get("phase"), "confirmed")

    def test_confirming_current_connection_does_not_bless_a_different_frozen_identity(self):
        self._advance_revision(base_url="http://new-current.invalid/dav")
        self.runner.configs["MP"]["url"] = "http://new-current.invalid/dav"
        before = self._rows()
        self.assertTrue(self.worker.ensure_confirmation()["auto_confirmed"])
        self.assertTrue(self.connections.runtime_identity_matches("openlist-main", 2))
        self.assertFalse(self.connections.runtime_identity_matches("openlist-main", 1))
        self.assertEqual(self._rows(), before)

    def test_busy_change_lock_has_bounded_timeout_and_retryable_error(self):
        @contextmanager
        def busy(*, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 15)
            raise OpenListSyncConflict("isolated busy lock")
            yield
        with patch.object(self.connections.runtime_sync, "mutation", busy):
            with self.assertRaises(RcloneWebdavConfigError) as raised:
                self.worker.ensure_confirmation()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(self.runner.calls, [])

    def test_unbound_disabled_or_multiple_connections_are_not_automatically_configured(self):
        with self.assertRaises(RcloneWebdavConfigError):
            RcloneWebdavConfigService(lambda: self.settings, runner=self.runner).ensure_confirmation()
        current = self.connections.list_connections()[0]
        for rows in ([], [{**current, "enabled": False}], [current, current]):
            with self.subTest(rows=len(rows)), patch.object(self.connections, "list_connections", return_value=rows):
                with self.assertRaises(RcloneWebdavConfigError):
                    self.worker.ensure_confirmation()
        self.assertEqual(self.runner.calls, [])

    def test_status_and_test_do_not_require_manual_resaving_on_every_upgrade(self):
        status = self.worker.status()
        self.assertIn("自动核验", status["message"])
        self.assertNotIn("重新保存", status["message"])
        with self.assertRaises(RcloneWebdavConfigError) as raised:
            self.worker.test()
        self.assertIn("自动核验", str(raised.exception))
        self.assertNotIn("重新保存", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
