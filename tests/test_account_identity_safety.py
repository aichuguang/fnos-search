from __future__ import annotations

import sqlite3
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.accounts.repository import AccountConflictError
from fnos_media_import.accounts.service import (
    IDENTITY_CONFIG_KEY,
    AccountDisabledError,
    AccountIdentityChangedError,
    AccountIdentityUnverifiedError,
    InvalidConfigError,
)
from fnos_media_import.accounts.auth_sessions import AuthSessionError, ProviderAuthSessionService
from fnos_media_import.accounts.crypto import PURPOSE_PROVIDER_ACCOUNT, encrypt
from fnos_media_import.domain.models import AccountTestResult
from fnos_media_import.importers.base import AdapterCheckResult
from fnos_media_import.importers.cloud139 import Cloud139Importer
from fnos_media_import.importers.quark import QuarkImporter
from fnos_media_import.importers.sixpan import SixPanOfflineImporter
from fnos_media_import.importers.tianyi import TianyiImporter
from fnos_media_import.providers.adapters import QuarkProviderAdapter
from tests import test_storage_setup_v1 as storage_fixture


@contextmanager
def _workspace(*, known_identity=True):
    fixture = storage_fixture.StorageSetupV1Tests()
    fixture.setUp()
    fixture.accounts.set_tester(lambda _kind, credentials, _config: AccountTestResult(
        True, "editable nickname", "mock healthy",
        identity_id=("mobile:account:B" if credentials.get("authorization") == "Basic B" else "mobile:account:A")
        if known_identity else "",
    ))
    try:
        account_id = fixture._account("mobile", "Account A")
        fixture.webdav.dirs.add(fixture.mobile_uploader.webdav_root)
        fixture.service.save_openlist({
            "base_url": "http://openlist:5244/dav", "username": "admin", "password": "mock-only",
        })
        location = fixture.service.save_location({
            "name": "A storage", "kind": "provider", "account_id": account_id,
            "connection_id": "openlist_main", "webdav_root": fixture.mobile_uploader.webdav_root,
            "native_root": "root-folder-id", "confirm_identity": True,
        })
        location = {**location, "mount_id": next(
            item["mount_id"] for item in fixture.service.snapshot()["locations"]
            if item["destination_id"] == location["destination_id"]
        )}
        yield fixture, account_id, location
    finally:
        fixture.doCleanups()


def _snapshot(fixture):
    with fixture.db.connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "provider_accounts", "provider_account_revisions", "provider_account_selections",
                "provider_account_selection_revisions", "openlist_mounts", "openlist_mount_revisions",
                "storage_target_account_bindings", "storage_target_binding_revisions",
            )
        }


def _prepare(fixture, account_id, authorization="Basic A-renewed", *, tester=None):
    return fixture.accounts.prepare_authenticated_account(
        "mobile", {"account_id": account_id, "name": "Account A", "credentials": {"authorization": authorization}},
        tester=tester,
    )[0]


def _commit(fixture, prepared):
    with fixture.db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return fixture.accounts.commit_authenticated_account(prepared, conn=connection)


class AccountIdentitySafetyTests(unittest.TestCase):
    def test_different_provider_identity_is_rejected_before_any_mapping_change(self):
        with _workspace() as (fixture, account_id, _location):
            before = _snapshot(fixture)
            with self.assertRaises(AccountIdentityChangedError):
                _prepare(fixture, account_id, "Basic B")
            self.assertEqual(_snapshot(fixture), before)
            with self.assertRaises(AccountIdentityChangedError):
                fixture.accounts.revise_credentials_if_healthy(
                    account_id, config={}, credentials={"authorization": "Basic B"},
                )
            self.assertEqual(_snapshot(fixture), before)

    def test_same_identity_relogin_and_nickname_change_keep_proven_mappings(self):
        with _workspace() as (fixture, account_id, location):
            old_account = fixture.accounts.get_account(account_id)
            old_revision = old_account.current_revision
            old_credentials = fixture.accounts.resolve_credentials(account_id, old_revision)
            old_mount = fixture.mounts.get_mount(location["mount_id"])
            prepared = _prepare(fixture, account_id, tester=lambda *_: AccountTestResult(
                True, "a completely different nickname", "healthy", identity_id="mobile:account:A",
            ))
            updated = _commit(fixture, prepared)
            self.assertEqual(updated.identity_hint, "a completely different nickname")
            self.assertGreater(updated.current_revision, old_revision)
            self.assertEqual(fixture.mounts.get_mount(location["mount_id"]), old_mount)
            self.assertTrue(fixture.service.snapshot()["locations"][0]["ready"])
            self.assertEqual(fixture.accounts.resolve_credentials(account_id, old_revision), old_credentials)
            binding = fixture.targets.resolve_binding(location["destination_id"], 1)
            self.assertEqual(binding.account_revision, updated.current_revision)
            self.assertNotIn(IDENTITY_CONFIG_KEY, fixture.accounts.safe_account_config(account_id))
            self.assertNotIn(IDENTITY_CONFIG_KEY, fixture.accounts.resolve_account_context(account_id, updated.current_revision)["config"])

    def test_unproven_manual_revision_disables_mappings_but_keeps_old_frozen_proofs(self):
        for known_identity in (False, True):
            with self.subTest(known_identity=known_identity), _workspace(known_identity=known_identity) as (fixture, account_id, location):
                old_mount = fixture.mounts.get_mount(location["mount_id"])
                old_frozen = fixture.mounts.get_revision(old_mount["mount_id"], old_mount["revision"])
                old_account = fixture.accounts.get_account(account_id)
                old_credentials = fixture.accounts.resolve_credentials(account_id, old_account.current_revision)
                updated = fixture.accounts.revise_credentials(account_id, config={}, credentials={"authorization": "Basic unproven"})
                current = fixture.mounts.get_mount(location["mount_id"])
                self.assertEqual(current["identity_status"], "unverified")
                self.assertFalse(current["readable"])
                self.assertFalse(current["native_locator"]["upload_verified"])
                self.assertFalse(current["native_locator"]["organizer_mutation_verified"])
                self.assertNotIn("probe_fingerprint", current["native_locator"])
                self.assertGreater(current["revision"], old_mount["revision"])
                self.assertEqual(fixture.mounts.get_revision(old_mount["mount_id"], old_mount["revision"]), old_frozen)
                self.assertEqual(fixture.accounts.resolve_credentials(account_id, old_account.current_revision), old_credentials)
                with fixture.db.connect() as connection:
                    self.assertEqual(connection.execute("SELECT enabled FROM storage_target_account_bindings WHERE account_id=?", (account_id,)).fetchone()[0], 0)
                self.assertFalse(fixture.service.snapshot()["locations"][0]["ready"])
                self.assertIn("旧保存位置已停用", updated.last_test_message)

    def test_unchanged_legacy_credentials_do_not_invalidate_a_working_location(self):
        with _workspace(known_identity=False) as (fixture, account_id, location):
            old_mount = fixture.mounts.get_mount(location["mount_id"])
            prepared = _prepare(fixture, account_id, "Basic token")
            _commit(fixture, prepared)
            self.assertEqual(fixture.mounts.get_mount(location["mount_id"]), old_mount)
            self.assertTrue(fixture.service.snapshot()["locations"][0]["ready"])

    def test_unproven_mapping_can_be_reenabled_by_explicit_revalidation(self):
        with _workspace(known_identity=False) as (fixture, account_id, location):
            fixture.accounts.revise_credentials(account_id, config={}, credentials={"authorization": "Basic renewed"})
            fixture.accounts.test_account(account_id)
            probes_before = len(fixture.mobile_uploader.probe_calls)
            fixture.service.save_location({
                "name": "Revalidated A storage",
                "kind": "provider", "account_id": account_id, "connection_id": "openlist_main",
                "webdav_root": fixture.mobile_uploader.webdav_root, "native_root": "root-folder-id",
                "confirm_identity": True,
            }, destination_id=location["destination_id"])
            self.assertGreater(len(fixture.mobile_uploader.probe_calls), probes_before)
            self.assertTrue(fixture.service.snapshot()["locations"][0]["ready"])

    def test_interactive_unknown_identity_rejects_replacement_without_any_write(self):
        for known_identity in (False, True):
            with self.subTest(known_identity=known_identity), _workspace(known_identity=known_identity) as (fixture, account_id, _location):
                before = _snapshot(fixture)
                unknown = lambda *_: AccountTestResult(True, "unchanged nickname", "healthy")
                with self.assertRaises(AccountIdentityUnverifiedError):
                    _prepare(fixture, account_id, "Basic unproven", tester=unknown)
                with self.assertRaises(AccountIdentityUnverifiedError):
                    fixture.accounts.revise_credentials_if_healthy(
                        account_id, config={}, credentials={"authorization": "Basic unproven"}, tester=unknown,
                    )
                self.assertEqual(_snapshot(fixture), before)

    def test_final_commit_rejects_old_unproven_prepared_credentials(self):
        with _workspace(known_identity=False) as (fixture, account_id, _location):
            prepared = _prepare(fixture, account_id, "Basic token")
            ciphertext, key_id = encrypt(
                storage_fixture.KEY, json.dumps({"authorization": "Basic unproven"}), purpose=PURPOSE_PROVIDER_ACCOUNT,
            )
            prepared.update(credentials_ciphertext=ciphertext, credential_key_id=key_id)
            before = _snapshot(fixture)
            with self.assertRaises(AccountIdentityUnverifiedError):
                _commit(fixture, prepared)
            self.assertEqual(_snapshot(fixture), before)

    def test_legacy_identity_is_proved_by_old_and_new_read_only_probes(self):
        with _workspace(known_identity=False) as (fixture, account_id, location):
            old_mount = fixture.mounts.get_mount(location["mount_id"])
            probes = []

            def same_identity(_kind, credentials, _config):
                probes.append(credentials["authorization"])
                return AccountTestResult(True, "different nickname", "healthy", identity_id="mobile:account:A")

            prepared = _prepare(fixture, account_id, "Basic A-renewed", tester=same_identity)
            self.assertEqual(probes, ["Basic A-renewed", "Basic token"])
            _commit(fixture, prepared)
            self.assertEqual(fixture.mounts.get_mount(location["mount_id"]), old_mount)

    def test_expired_legacy_identity_does_not_authorize_replacement(self):
        with _workspace(known_identity=False) as (fixture, account_id, _location):
            before = _snapshot(fixture)

            def expired_original(_kind, credentials, _config):
                return AccountTestResult(
                    credentials["authorization"] != "Basic token", identity_id="mobile:account:A",
                )

            with self.assertRaises(AccountIdentityUnverifiedError):
                _prepare(fixture, account_id, tester=expired_original)
            self.assertEqual(_snapshot(fixture), before)

    def test_immediate_auth_helper_obeys_identity_policy_and_keeps_other_default(self):
        with _workspace() as (fixture, account_id, _location):
            other = fixture._account("mobile", "New default")
            sessions = ProviderAuthSessionService(fixture.db, lambda: storage_fixture.KEY, fixture.accounts, {})
            before = _snapshot(fixture)
            with self.assertRaises(AuthSessionError):
                sessions._complete_account("mobile", {"account_id": account_id, "credentials": {"authorization": "Basic B"}})
            self.assertEqual(_snapshot(fixture), before)
            sessions._complete_account("mobile", {"account_id": account_id, "credentials": {"authorization": "Basic A-renewed"}})
            self.assertEqual(fixture.accounts.get_active_account("mobile").account_id, other)

    def test_trusted_token_refresh_keeps_identity_and_mapping_for_future_work(self):
        with _workspace() as (fixture, account_id, location):
            before = fixture.accounts.get_account(account_id)
            old_mount = fixture.mounts.get_mount(location["mount_id"])
            updated = fixture.accounts.refresh_runtime_credentials(
                account_id, {"authorization": "Basic refreshed-A"}, expected_revision=before.current_revision,
            )
            self.assertEqual(fixture.mounts.get_mount(location["mount_id"]), old_mount)
            self.assertEqual(fixture.targets.resolve_binding(location["destination_id"], 1).account_revision, updated.current_revision)
            with fixture.db.connect() as connection:
                proof = fixture.accounts._repository.get_revision(account_id, updated.current_revision, conn=connection)["config"][IDENTITY_CONFIG_KEY]
            self.assertEqual(proof["identity_id"], "mobile:account:A")
            self.assertTrue(proof["verified"])
            self.assertTrue(fixture.service.snapshot()["locations"][0]["ready"])

    def test_runtime_refresh_without_revision_cannot_change_credentials_or_proofs(self):
        with _workspace() as (fixture, account_id, _location):
            current = fixture.accounts.get_account(account_id)
            before = _snapshot(fixture)
            with self.assertRaises(AccountConflictError):
                fixture.accounts.refresh_runtime_credentials(account_id, {"authorization": "Basic unbound-refresh"})
            self.assertEqual(_snapshot(fixture), before)
            unchanged = fixture.accounts.refresh_runtime_credentials(
                account_id, fixture.accounts.resolve_credentials(account_id, current.current_revision),
            )
            self.assertEqual(unchanged.current_revision, current.current_revision)
            self.assertEqual(_snapshot(fixture), before)

    def test_manual_revision_cannot_keep_mapping_proof_or_change_locked_identity(self):
        with _workspace() as (fixture, account_id, location):
            fixture.accounts.revise_credentials(account_id, config={}, credentials={"authorization": "Basic B"})
            self.assertFalse(fixture.mounts.get_mount(location["mount_id"])["native_locator"]["upload_verified"])
            for persist in (False, True):
                with self.subTest(persist=persist), self.assertRaises(AccountIdentityChangedError):
                    fixture.accounts.test_account(account_id, persist_result=persist)
            self.assertEqual(fixture.accounts.get_account(account_id).last_test_status, "untested")

    def test_identity_metadata_cannot_be_supplied_in_configuration(self):
        with _workspace() as (fixture, account_id, _location):
            override = {IDENTITY_CONFIG_KEY: {"provider_kind": "mobile", "identity_id": "mobile:account:B", "verified": True}}
            before = _snapshot(fixture)
            for action in (
                lambda: fixture.accounts.create_account(provider_kind="mobile", name="spoof", config=override, credentials={"authorization": "Basic B"}),
                lambda: fixture.accounts.revise_credentials(account_id, config=override, credentials={}),
                lambda: fixture.accounts.revise_credentials_if_healthy(account_id, config=override, credentials={}),
                lambda: fixture.accounts.prepare_authenticated_account("mobile", {"account_id": account_id, "config": override}),
            ):
                with self.assertRaises(InvalidConfigError):
                    action()
            self.assertEqual(_snapshot(fixture), before)

    def test_disabled_after_prepare_cannot_commit_or_replace_default_account(self):
        with _workspace() as (fixture, account_id, _location):
            prepared = _prepare(fixture, account_id)
            other_id = fixture._account("mobile", "Account B default")
            fixture.accounts.disable_account(account_id)
            before = _snapshot(fixture)
            with self.assertRaises(AccountDisabledError):
                _commit(fixture, prepared)
            self.assertEqual(_snapshot(fixture), before)
            self.assertEqual(fixture.accounts.get_active_account("mobile").account_id, other_id)

    def test_inactive_account_relogin_does_not_steal_new_default_selection(self):
        with _workspace() as (fixture, account_id, _location):
            prepared = _prepare(fixture, account_id)
            other_id = fixture._account("mobile", "New default")
            _commit(fixture, prepared)
            self.assertEqual(fixture.accounts.get_active_account("mobile").account_id, other_id)

    def test_existing_account_login_can_initialize_an_empty_default_selection(self):
        with _workspace() as (fixture, _account_id, _location):
            account = fixture.accounts.create_account(
                provider_kind="quark", name="Not yet activated", config={}, credentials={"cookie": "sid=A"},
            )
            prepared, _ = fixture.accounts.prepare_authenticated_account(
                "quark", {"account_id": account.account_id, "credentials": {"cookie": "sid=A-renewed"}},
                tester=lambda *_: AccountTestResult(True, identity_id="quark:uid:A"),
            )
            _commit(fixture, prepared)
            self.assertEqual(fixture.accounts.get_active_account("quark").account_id, account.account_id)

    def test_healthy_relogin_and_test_recheck_disabled_state_after_probe(self):
        for operation in ("relogin", "test"):
            with self.subTest(operation=operation), _workspace() as (fixture, account_id, _location):
                other_id = fixture._account("mobile", "Account B default")
                previous_revision = fixture.accounts.get_account(account_id).current_revision

                def disable_during_probe(*_args):
                    fixture.accounts.disable_account(account_id)
                    return AccountTestResult(True, "name", "healthy", identity_id="mobile:account:A")

                with self.assertRaises(AccountDisabledError):
                    if operation == "relogin":
                        fixture.accounts.revise_credentials_if_healthy(
                            account_id, config={}, credentials={"authorization": "Basic renewed"}, tester=disable_during_probe,
                        )
                    else:
                        fixture.accounts.test_account(account_id, tester=disable_during_probe)
                self.assertEqual(fixture.accounts.get_account(account_id).current_revision, previous_revision)
                self.assertEqual(fixture.accounts.get_active_account("mobile").account_id, other_id)

    def test_stale_prepared_revision_cannot_change_current_state(self):
        with _workspace() as (fixture, account_id, _location):
            prepared = _prepare(fixture, account_id)
            current = fixture.accounts.get_account(account_id)
            fixture.accounts.refresh_runtime_credentials(account_id, {"authorization": "Basic newest"}, expected_revision=current.current_revision)
            before = _snapshot(fixture)
            with self.assertRaises(AccountConflictError):
                _commit(fixture, prepared)
            self.assertEqual(_snapshot(fixture), before)

    def test_manual_revision_failure_rolls_back_mapping_invalidation_and_account_revision(self):
        with _workspace(known_identity=False) as (fixture, account_id, _location):
            before = _snapshot(fixture)
            with fixture.db.connect() as connection:
                connection.execute(
                    "CREATE TRIGGER reject_new_account_revision BEFORE INSERT ON provider_account_revisions "
                    "BEGIN SELECT RAISE(ABORT, 'test revision failure'); END"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "test revision failure"):
                fixture.accounts.revise_credentials(account_id, config={}, credentials={"authorization": "Basic unproven"})
            self.assertEqual(_snapshot(fixture), before)


class ProviderStableIdentityTests(unittest.TestCase):
    def test_quark_uses_provider_uid_not_nickname(self):
        importer = QuarkImporter({"cookie": "mock"})
        with patch.object(importer, "_get", return_value={"data": {"uid": "42", "nickname": "new nickname"}}):
            result = importer.probe()
        self.assertEqual(result.details["identity_id"], "quark:uid:42")
        with patch.object(importer, "_get", return_value={"data": {"nickname": "same nickname"}}):
            self.assertEqual(importer.probe().details["identity_id"], "")

    def test_tianyi_does_not_promote_configured_username_to_trusted_identity(self):
        importer = TianyiImporter({"session_key": "mock", "username": "editable", "identity": "editable"})
        with patch.object(importer, "_get", return_value={}):
            self.assertEqual(importer.probe().details["identity_id"], "")
            importer._auth_data["userId"] = "42"
            self.assertEqual(importer.probe().details["identity_id"], "tianyi:user:42")

    def test_mobile_identity_is_from_successful_account_scoped_probe(self):
        importer = Cloud139Importer({})
        client = SimpleNamespace(phone="13800000000", list_dir=Mock(return_value=[]))
        with patch.object(importer, "check_configuration", return_value=AdapterCheckResult(True, "ready", "")), patch.object(importer, "_cmcc", return_value=client):
            self.assertEqual(importer.probe().details["identity_id"], "mobile:account:13800000000")
            client.list_dir.side_effect = RuntimeError("unauthorized")
            self.assertFalse(importer.probe().ok)

    def test_sixpan_uses_user_identity_and_adapter_preserves_identity_contract(self):
        importer = SixPanOfflineImporter({})
        with patch.object(importer, "check_configuration", return_value=AdapterCheckResult(True, "ready", "")), patch.object(importer.client, "get_user", return_value={"identity": "user-42", "name": "nickname"}):
            self.assertEqual(importer.probe().details["identity_id"], "sixpan:user:user-42")
        fake = SimpleNamespace(probe=lambda: AdapterCheckResult(True, "ready", "", {"identity_id": "quark:uid:42", "identity_hint": "editable"}))
        adapter = QuarkProviderAdapter(importer_factory=lambda *_: fake, account_importer_factory=lambda _: fake, source_record_service=object())
        self.assertEqual(adapter.test_account({"config": {}, "credentials": {}}).identity_id, "quark:uid:42")


if __name__ == "__main__":
    unittest.main()
