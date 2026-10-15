from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist import OpenListValidationError
from fnos_media_import.openlist.locator_resolver import RcloneLocatorResolver
from fnos_media_import.openlist.runtime_sync import _kernel_lock
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError, RcloneWebdavConfigService
from fnos_media_import.services.rclone_worker_client import RcloneWorkerClient, RcloneWorkerRequestError
from fnos_media_import.storage.setup_service import StorageSetupError, StorageSetupService
from tests.test_rclone_webdav_config import FakeRcloneRunner


class _Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class OpenListRemoteSyncConcurrencyTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "codex_tmp"
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.db = _Database(Path(temporary.name) / "app.db")
        with self.db.connect() as connection:
            connection.executescript("""
                CREATE TABLE openlist_connections (
                    connection_id TEXT PRIMARY KEY, name TEXT, base_url TEXT, username TEXT,
                    password_ciphertext BLOB, credential_key_id TEXT, enabled INTEGER, revision INTEGER,
                    last_test_status TEXT, last_tested_at TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE openlist_connection_revisions (
                    connection_id TEXT, revision INTEGER, config_json TEXT, password_ciphertext BLOB,
                    credential_key_id TEXT, created_at TEXT, PRIMARY KEY(connection_id, revision)
                );
                CREATE TABLE openlist_mounts (mount_id TEXT PRIMARY KEY, connection_id TEXT);
                CREATE TABLE openlist_mount_revisions (mount_id TEXT, revision INTEGER, config_json TEXT, created_at TEXT);
            """)
        self.connections = self._connections()
        self.worker_connections = self._connections()
        self.runner = FakeRcloneRunner()
        self.settings = {"remote_name": "MP", "config_path": str(self.db.path.parent / "rclone.conf")}
        self.worker = RcloneWebdavConfigService(lambda: self.settings, runner=self.runner)
        self.worker.set_connection_service(self.worker_connections)
        self.connections.runtime_sync.tracks_remote = True
        self.service = self._setup(self.connections)

    def _connections(self):
        return OpenListConnectionService(
            _Database(self.db.path), lambda: b"k" * 32,
            probe_runner=lambda context: {"ok": True, "message": "isolated probe"},
        )

    def _setup(self, connections):
        connections.runtime_sync.tracks_remote = True
        connections.set_managed_remote_identity_provider(self.worker._managed_remote_identity)
        return StorageSetupService(
            accounts_service=object(), connection_service=connections,
            mount_service=SimpleNamespace(list_mounts=lambda: [], get_mount=lambda _id: None),
            target_service=object(), assignment_service=object(), connection_factory=self.db.connect,
            rclone_webdav_sync=self.worker.save,
        )

    def _save(self, url="http://old.invalid/dav", *, service=None):
        current = self.connections.list_connections()
        payload = {"name": "isolated", "base_url": url, "username": "user", "password": "isolated-secret"}
        if current:
            payload.update(connection_id=current[0]["connection_id"], expected_revision=current[0]["revision"])
        return (service or self.service).save_openlist(payload)

    def _current(self):
        return self.connections.list_connections()[0]

    def _assert_confirmed(self, url):
        current = self._current()
        self.assertEqual(current["base_url"], url)
        self.assertEqual(self.runner.configs[self.settings["remote_name"]]["url"], url)
        self.assertTrue(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))

    @staticmethod
    def _timeout():
        return RcloneWorkerRequestError("reply timed out", 504, {
            "success": False, "code": "RCLONE_SYNC_RESULT_UNKNOWN", "not_started": False,
        })

    def test_kernel_lock_excludes_an_independent_file_handle(self):
        sync = self.connections.runtime_sync
        with sync.mutation():
            handle = open(sync.change_lock.path, "r+b", buffering=0)
            try:
                with self.assertRaises(OSError):
                    _kernel_lock(handle)
            finally:
                handle.close()
        with open(sync.change_lock.path, "r+b", buffering=0) as handle:
            _kernel_lock(handle)
            _kernel_lock(handle, unlock=True)

    def test_sync_does_not_hold_sqlite_write_transaction_during_rpc(self):
        def bridge(payload):
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("SELECT COUNT(*) FROM openlist_connections")
            return self.worker.save(payload)

        self.service.rclone_webdav_sync = bridge
        self._save()
        self._assert_confirmed("http://old.invalid/dav")

    def test_late_rpc_is_rejected_before_any_remote_write(self):
        self._save()
        delayed = []

        def lose_request(payload):
            delayed.append(dict(payload))
            raise self._timeout()

        self.service.rclone_webdav_sync = lose_request
        with self.assertRaises(StorageSetupError) as failed:
            self._save("http://stale.invalid/dav")
        self.assertEqual(failed.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self.service.rclone_webdav_sync = self.worker.save
        self._save("http://new.invalid/dav")
        call_count = len(self.runner.calls)
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.save(delayed[0])
        self.assertEqual(len(self.runner.calls), call_count)
        self._assert_confirmed("http://new.invalid/dav")

    def test_running_old_worker_cannot_overtake_new_db_or_remote_after_http_timeout(self):
        self._save()
        entered = threading.Event()
        release = threading.Event()
        pending = []

        def gated_runner(command, **kwargs):
            if command[-1] == self.worker._SAVE_SCRIPT and "stale.invalid" in str(kwargs.get("input")):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("old writer was not released")
            return self.runner(command, **kwargs)

        self.worker._runner = gated_runner
        with ThreadPoolExecutor(max_workers=2) as pool:
            def timeout_bridge(payload):
                pending.append(pool.submit(self.worker.save, payload))
                self.assertTrue(entered.wait(2))
                raise self._timeout()

            self.service.rclone_webdav_sync = timeout_bridge
            with self.assertRaises(StorageSetupError):
                self._save("http://stale.invalid/dav")
            second = self._setup(self._connections())
            next_save = pool.submit(self._save, "http://new.invalid/dav", service=second)
            try:
                with self.assertRaises(FutureTimeout):
                    next_save.result(timeout=0.05)
                self.assertEqual(self._current()["base_url"], "http://stale.invalid/dav")
            finally:
                release.set()
            with self.assertRaises(RcloneWebdavConfigError) as old:
                pending[0].result(timeout=3)
            self.assertEqual(old.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
            self.assertTrue(next_save.result(timeout=3)["success"])
        self.assertFalse(any(call["command"][-1] == self.worker._RESTORE_SCRIPT for call in self.runner.calls))
        self._assert_confirmed("http://new.invalid/dav")

    def test_direct_connection_update_waits_for_full_save_and_marks_remote_unconfirmed(self):
        self._save()
        entered = threading.Event()
        release = threading.Event()
        original_bridge = self.service.rclone_webdav_sync

        def slow_bridge(payload):
            entered.set()
            if not release.wait(3):
                raise AssertionError("save was not released")
            return original_bridge(payload)

        self.service.rclone_webdav_sync = slow_bridge
        other = self._connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self._save, "http://first.invalid/dav")
            self.assertTrue(entered.wait(2))
            second = pool.submit(other.update_connection, "openlist_main", {"base_url": "http://direct.invalid/dav"})
            try:
                with self.assertRaises(FutureTimeout):
                    second.result(timeout=0.05)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3)["success"])
            second.result(timeout=3)
        current = self._current()
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        self.assertEqual(self.connections.resolve_credentials(current["connection_id"])["base_url"], "http://direct.invalid/dav")

    def test_raw_remote_update_cannot_bypass_database_credentials(self):
        self._save()
        before = len(self.runner.calls)
        with self.assertRaises(RcloneWebdavConfigError) as rejected:
            self.worker.save({"url": "http://other.invalid/dav", "username": "user", "password": "different"})
        self.assertEqual(rejected.exception.code, "RCLONE_SYNC_CONFLICT")
        self.assertEqual(len(self.runner.calls), before)
        self._assert_confirmed("http://old.invalid/dav")

    def test_known_remote_rejection_restores_both_database_and_remote(self):
        self._save()

        def reject_new(command, **kwargs):
            if command[-1] == self.worker._SAVE_SCRIPT and "rejected.invalid" in str(kwargs.get("input")):
                return subprocess.CompletedProcess(command, 1, "", "new config rejected")
            return self.runner(command, **kwargs)

        self.worker._runner = reject_new
        with self.assertRaises(StorageSetupError) as rejected:
            self._save("http://rejected.invalid/dav")
        self.assertEqual(rejected.exception.code, "RCLONE_WEBDAV_SYNC_FAILED")
        self._assert_confirmed("http://old.invalid/dav")

    def test_failed_remote_restore_is_unknown_and_never_rolls_back_database(self):
        self._save()

        def failed_restore(command, **kwargs):
            if command[-1] == self.worker._RESTORE_SCRIPT:
                return subprocess.CompletedProcess(command, 1, "", "restore failed")
            if "lsd" in command:
                return subprocess.CompletedProcess(command, 1, "", "probe failed")
            return self.runner(command, **kwargs)

        self.worker._runner = failed_restore
        with self.assertRaises(StorageSetupError) as failed:
            self._save("http://uncertain.invalid/dav")
        self.assertEqual(failed.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        current = self._current()
        self.assertEqual(current["base_url"], "http://uncertain.invalid/dav")
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))

    def test_read_timeout_does_not_masquerade_as_absent_worker(self):
        client = RcloneWorkerClient(base_url="http://unused.invalid", token="test", database=object(), session=Mock())
        client.session.request.side_effect = requests.ReadTimeout("unknown execution")
        self.service.rclone_webdav_sync = client.webdav_save
        with self.assertRaises(StorageSetupError) as failed:
            self._save()
        self.assertEqual(failed.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self.assertEqual(len(self.connections.list_connections()), 1)
        self.assertEqual(client.session.request.call_count, 1)

    def test_confirmed_connection_timeout_still_allows_native_openlist_configuration(self):
        client = RcloneWorkerClient(base_url="http://unused.invalid", token="test", database=object(), session=Mock())
        client.session.request.side_effect = requests.ConnectTimeout("connection never established")
        self.service.rclone_webdav_sync = client.webdav_save
        result = self._save()
        self.assertTrue(result["success"])
        self.assertFalse(result["rclone_available"])
        current = self._current()
        self.assertTrue(self.connections.resolve_credentials(current["connection_id"])["password"])
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))

    def test_remote_status_read_waits_for_writer(self):
        self._save()
        entered = threading.Event()
        release = threading.Event()

        def gated(command, **kwargs):
            if command[-1] == self.worker._SAVE_SCRIPT:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("writer not released")
            return self.runner(command, **kwargs)

        self.worker._runner = gated
        other_reader = RcloneWebdavConfigService(lambda: {"remote_name": "MP"}, runner=self.runner)
        other_reader.set_connection_service(self._connections())
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(self._save, "http://read-after.invalid/dav")
            self.assertTrue(entered.wait(2))
            read = pool.submit(other_reader.status)
            try:
                with self.assertRaises(FutureTimeout):
                    read.result(timeout=0.05)
            finally:
                release.set()
            write.result(timeout=3)
            self.assertEqual(read.result(timeout=3)["url"], "http://read-after.invalid/dav")

    def test_timeout_scripts_do_not_leave_a_shell_child_writing_config(self):
        self.assertIn("exec rclone config create", self.worker._SAVE_SCRIPT)
        self.assertIn("exec rclone config update", self.worker._SAVE_SCRIPT)
        self.assertIn('exec mv -f "$temporary" "$config"', self.worker._RESTORE_SCRIPT)
        self.assertIn(".tmp.$$", self.worker._RESTORE_SCRIPT)

    def test_missing_confirmation_after_upgrade_requires_explicit_save(self):
        self._save()
        self.connections.runtime_sync._path("remote-state.json").unlink()
        current = self._current()
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        self.assertEqual(self.worker.status()["connection_status"], "confirmation_required")
        with self.assertRaises(RcloneWebdavConfigError) as unconfirmed:
            self.worker.test()
        self.assertEqual(unconfirmed.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self._save()
        self._assert_confirmed("http://old.invalid/dav")

    def test_missing_remote_confirmation_does_not_block_native_openlist_credentials_or_probe(self):
        self._save()
        self.connections.runtime_sync._path("remote-state.json").unlink()
        current = self._current()
        credentials = self.connections.resolve_credentials(current["connection_id"], current["revision"])
        self.assertEqual(credentials["base_url"], current["base_url"])
        self.assertTrue(self.connections.test_connection(current["connection_id"])["ok"])
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))

    def test_confirmation_from_another_database_snapshot_is_not_reported_healthy(self):
        self._save()
        current = self._current()
        with self.db.connect() as connection:
            connection.execute("UPDATE openlist_connections SET base_url='http://restored.invalid/dav'")
            connection.execute(
                "UPDATE openlist_connection_revisions SET config_json=?",
                (json.dumps({"base_url": "http://restored.invalid/dav", "username": "user", "enabled": True}),),
            )
        self.assertEqual(self.connections.runtime_sync.state()["phase"], "confirmed")
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        self.assertEqual(self.worker.status()["connection_status"], "confirmation_required")
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.test()

    def _frozen_resolver(self):
        current = self._current()
        mount = {
            "mount_id": "test-mount", "revision": 1,
            "connection_id": current["connection_id"], "connection_revision": current["revision"],
            "namespace_id": f"openlist:{current['connection_id']}:test-mount",
            "storage_identity_id": "storage-test", "identity_root": "/", "webdav_root": "/cloud",
            "enabled": True, "identity_status": "verified", "readable": True, "writable": True,
        }
        resolver = RcloneLocatorResolver(dict(self.settings)).bind_revision_services(
            SimpleNamespace(get_revision=lambda _id, _revision: dict(mount)), self.worker_connections,
        )
        params = {
            "namespace_id": mount["namespace_id"], "mount_id": mount["mount_id"], "mount_revision": 1,
            "connection_id": current["connection_id"], "connection_revision": current["revision"],
            "storage_identity_id": mount["storage_identity_id"], "identity_root": "/", "path": "/job-1",
        }
        return resolver, params

    def test_remote_name_switch_invalidates_data_plane_and_explicit_save_restores_it(self):
        self._save()
        resolver, params = self._frozen_resolver()
        self.assertEqual(resolver.resolve_frozen_remote(**params), "MP:/cloud/job-1")
        self.runner.configs["OTHER"] = {"type": "webdav", "url": "http://other.invalid/dav", "user": "other", "pass": "set"}
        self.settings["remote_name"] = "OTHER"
        resolver.update_config(self.settings)
        current = self._current()
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        with self.assertRaises(OpenListValidationError):
            resolver.resolve_frozen_remote(**params)
        self.assertEqual(self.worker.status()["connection_status"], "confirmation_required")
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.test()
        self._save()
        self._assert_confirmed("http://old.invalid/dav")
        self.assertEqual(resolver.resolve_frozen_remote(**params), "OTHER:/cloud/job-1")

    def test_config_path_switch_invalidates_same_remote_until_resaved(self):
        self._save()
        resolver, params = self._frozen_resolver()
        self.settings["config_path"] = str(self.db.path.parent / "another.conf")
        resolver.update_config(self.settings)
        current = self._current()
        self.assertFalse(self.worker_connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        with self.assertRaises(OpenListValidationError):
            resolver.resolve_frozen_remote(**params)
        self.assertEqual(self.worker.status()["connection_status"], "confirmation_required")
        with self.assertRaises(RcloneWebdavConfigError):
            self.worker.test()
        self._save()
        self._assert_confirmed("http://old.invalid/dav")
        self.assertEqual(resolver.resolve_frozen_remote(**params), "MP:/cloud/job-1")

    def test_resolver_only_update_cannot_reuse_worker_scope_confirmation(self):
        self._save()
        resolver, params = self._frozen_resolver()
        resolver.update_config({"remote_name": "OTHER"})
        with self.assertRaises(OpenListValidationError):
            resolver.resolve_frozen_remote(**params)
        resolver.update_config({"remote_name": "MP", "config_path": str(self.db.path.parent / "other.conf")})
        with self.assertRaises(OpenListValidationError):
            resolver.resolve_frozen_remote(**params)

    def test_equivalent_normalized_config_path_keeps_confirmation(self):
        self._save()
        resolver, params = self._frozen_resolver()
        self.settings["config_path"] = str(self.db.path.parent) + "/./rclone.conf"
        resolver.update_config(self.settings)
        self._assert_confirmed("http://old.invalid/dav")
        self.assertEqual(resolver.resolve_frozen_remote(**params), "MP:/cloud/job-1")

    def test_status_and_test_reject_actual_remote_url_or_user_drift(self):
        self._save()
        original = dict(self.runner.configs["MP"])
        for field, value in (("url", "http://drift.invalid/dav"), ("user", "different-user")):
            with self.subTest(field=field):
                self.runner.configs["MP"] = {**original, field: value}
                self.assertEqual(self.worker.status()["connection_status"], "confirmation_required")
                with self.assertRaises(RcloneWebdavConfigError):
                    self.worker.test()

    def test_scope_change_during_remote_write_cannot_publish_confirmation(self):
        self._save()
        changed = False

        def switch_after_write(command, **kwargs):
            nonlocal changed
            result = self.runner(command, **kwargs)
            if command[-1] == self.worker._SAVE_SCRIPT and not changed:
                changed = True
                self.settings["remote_name"] = "OTHER"
            return result

        self.worker._runner = switch_after_write
        with self.assertRaises(StorageSetupError) as unknown:
            self._save("http://new.invalid/dav")
        self.assertEqual(unknown.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        current = self._current()
        self.assertFalse(self.connections.runtime_identity_matches(current["connection_id"], current["revision"]))
        self._save("http://new.invalid/dav")
        self._assert_confirmed("http://new.invalid/dav")

    def test_scope_change_during_remote_probe_cannot_report_healthy(self):
        self._save()

        def switch_during_probe(command, **kwargs):
            result = self.runner(command, **kwargs)
            if "lsd" in command:
                self.settings["remote_name"] = "OTHER"
            return result

        self.worker._runner = switch_during_probe
        with self.assertRaises(RcloneWebdavConfigError) as changed:
            self.worker.test()
        self.assertEqual(changed.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
