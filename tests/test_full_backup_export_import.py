from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fnos_media_import.accounts.crypto import (
    PURPOSE_OPENLIST,
    PURPOSE_PROVIDER_ACCOUNT,
    encrypt,
)
from fnos_media_import.database import Database
from fnos_media_import.accounts.repository import ProviderAccountRepository
from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.planning.plan_validator import PlanValidationDeps, PlanValidator
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.backup_service import (
    FULL_BACKUP_FORMAT,
    FULL_BACKUP_VERSION,
    FullBackupService,
)

WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "full-backup-tests"

ROOT_KEY_A = bytes.fromhex("11" * 32)
ROOT_KEY_B = bytes.fromhex("22" * 32)

CONFIG_TABLES = (
    "provider_accounts",
    "provider_account_revisions",
    "provider_account_selections",
    "provider_account_selection_revisions",
    "openlist_connections",
    "openlist_connection_revisions",
    "openlist_mounts",
    "openlist_mount_revisions",
    "storage_targets",
    "storage_target_revisions",
    "storage_target_account_bindings",
    "storage_target_binding_revisions",
    "source_destination_assignments",
)


class _DatabaseWorkspace:
    def __enter__(self) -> RepositorySet:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="backup-", dir=WORK_TEST_ROOT)
        self.path = Path(self.directory.name) / "app.db"
        self.database = Database(self.path)
        self.database.init_schema()
        return RepositorySet.from_database(self.database)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.database.close_all_connections()
        except AttributeError:
            pass
        if self.path.parent.resolve().parent != WORK_TEST_ROOT.resolve():
            raise RuntimeError("test database escaped its temporary workspace")
        self.directory.cleanup()


def _service(repos: RepositorySet, root_key: Any, reload_runtime: Any = lambda: {"reloaded": True}) -> FullBackupService:
    master_key = root_key if callable(root_key) else (lambda: root_key)
    return FullBackupService(
        db=repos,
        master_key=master_key,
        reload_runtime=reload_runtime,
    )


def _seed(repos: RepositorySet) -> None:
    repos.app_settings.set_many(
        {
            "advanced_config": {"pansou": {"base_url": "http://pansou.local", "password": "pansou-secret"}},
            "public.allow_anonymous_search": False,
            "submission.mode": "review",
            "search.providers": {"pansou": {"enabled": True, "priority": 10}},
            "notifications": {"rules": {}},
            "admin.profile": {"username": "admin", "password_hash": "$scalar$should-not-export"},
        }
    )
    credentials_cipher, credentials_key_id = encrypt(
        ROOT_KEY_A,
        json.dumps({"token": "quark-token"}),
        purpose=PURPOSE_PROVIDER_ACCOUNT,
    )
    with repos.connect() as conn:
        conn.execute(
            "INSERT INTO provider_accounts (account_id, provider_kind, name, enabled, current_revision,"
            " identity_hint, created_at, updated_at) VALUES ('acc-1', 'quark', '主账号', 1, 1, 'quark:13***', ?, ?)",
            ("2026-01-01T00:00:00Z",) * 2,
        )
        conn.execute(
            "INSERT INTO provider_account_revisions (account_id, revision, config_json, credentials_ciphertext,"
            " credential_key_id, created_at) VALUES ('acc-1', 1, ?, ?, ?, ?)",
            (json.dumps({"cookie": "uin=1"}), credentials_cipher, credentials_key_id, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO storage_targets (destination_id, name, provider_kind, account_scope, target_kind,"
            " enabled, audience, current_revision, created_at, updated_at)"
            " VALUES ('dest-1', '主存储', 'quark', 'default', 'provider', 1, 'admin', 1, ?, ?)",
            ("2026-01-01T00:00:00Z",) * 2,
        )

        conn.execute(
            "INSERT INTO storage_target_revisions (destination_id, revision, categories_json, display_json, created_at)"
            " VALUES ('dest-1', 1, ?, ?, ?)",
            (json.dumps({"movie": {"paths": ["/电影"]}}), "{}", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO storage_target_account_bindings (binding_id, destination_id, target_revision, account_id,"
            " enabled, current_revision, created_at, updated_at)"
            " VALUES ('bind-1', 'dest-1', 1, 'acc-1', 1, 1, ?, ?)",
            ("2026-01-01T00:00:00Z",) * 2,
        )
        conn.execute(
            "INSERT INTO storage_target_binding_revisions "
            "(binding_id, revision, locator_json, created_at, account_revision)"
            " VALUES ('bind-1', 1, ?, ?, 1)",
            (json.dumps({"mode": "native"}), "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO source_destination_assignments (search_source_id, category_key, destination_id,"
            " enabled, revision, created_at, updated_at) VALUES ('pansou', 'movie', 'dest-1', 1, 1, ?, ?)",
            ("2026-01-01T00:00:00Z",) * 2,
        )

    accounts = ProviderAccountRepository(repos)
    accounts.upsert_selection("quark", "default", "acc-1", 1, None)
    accounts.upsert_selection("quark", "default", "acc-1", 1, 1)
    connections = OpenListConnectionService(repos, lambda: ROOT_KEY_A)
    connections.create_connection(
        {
            "connection_id": "conn-1",
            "name": "OpenList",
            "base_url": "http://openlist.local/dav",
            "username": "admin",
            "password": "openlist-password",
        }
    )
    OpenListMountService(repos, connections).create_mount(
        {
            "mount_id": "mount-1",
            "connection_id": "conn-1",
            "name": "media",
            "provider_kind": "quark",
            "account_id": "acc-1",
            "webdav_root": "/media",
        }
    )
    connections.update_connection(
        "conn-1", {"expected_revision": 1, "password": "openlist-current-password"}
    )


def _read_table(repos: RepositorySet, table: str) -> list[dict[str, Any]]:
    with repos.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _empty_document() -> dict[str, Any]:
    return {
        "format": FULL_BACKUP_FORMAT,
        "version": FULL_BACKUP_VERSION,
        "app_settings": {},
        **{table: [] for table in CONFIG_TABLES},
    }


def _seed_auth_session(repos: RepositorySet) -> None:
    with repos.connect() as conn:
        conn.execute(
            "INSERT INTO provider_auth_sessions "
            "(session_id, provider_kind, auth_method, status, state_ciphertext, credential_key_id, "
            "account_id, created_at, updated_at) "
            "VALUES ('session-1', 'quark', 'qr', 'completed', ?, 'test-only', 'acc-1', ?, ?)",
            (b"test-only-state", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )


class FullBackupExportImportTests(unittest.TestCase):
    def assert_frozen_revisions_readable(self, repos: RepositorySet) -> None:
        accounts = ProviderAccountService(repos, lambda: ROOT_KEY_A)
        connections = OpenListConnectionService(repos, lambda: ROOT_KEY_A)
        mounts = OpenListMountService(repos, connections)
        validator = PlanValidator(PlanValidationDeps(accounts, None, mounts, None))
        for revision in (1, 2):
            with self.subTest(revision=revision):
                errors: list[str] = []
                validator._validate_account(
                    provider_kind="quark",
                    account_id="acc-1",
                    account_revision=1,
                    selection_revision=revision,
                    selection_scope="default",
                    role="source",
                    errors=errors,
                )
                self.assertEqual(errors, [])
                credentials = connections.resolve_credentials("conn-1", revision)
                self.assertEqual(
                    credentials["password"],
                    "openlist-password" if revision == 1 else "openlist-current-password",
                )
                mount = mounts.get_revision("mount-1", revision)
                self.assertIsNotNone(mount)
                self.assertEqual(mount["connection_revision"], revision)
                self.assertEqual(mount["namespace_id"], "openlist:conn-1:mount-1")

    def test_export_document_structure_and_settings_exclusion(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            body, status = _service(repos, ROOT_KEY_A).export_document()
        self.assertEqual(status, 200, body)
        document = body["document"]
        self.assertEqual(document["format"], FULL_BACKUP_FORMAT)
        self.assertEqual(document["version"], FULL_BACKUP_VERSION)
        self.assertNotIn("admin.profile", document["app_settings"])
        self.assertEqual(document["app_settings"]["submission.mode"], "review")
        self.assertEqual(body["meta"]["table_counts"]["provider_accounts"], 1)
        revision = document["provider_account_revisions"][0]
        self.assertEqual(revision["config"], {"cookie": "uin=1"})
        self.assertTrue(revision["credentials_ciphertext"])
        self.assertEqual(revision["credential_key_id"], "v2:provider-account")
        connection = document["openlist_connections"][0]
        self.assertTrue(connection["password_ciphertext"])
        for table in ("provider_account_selection_revisions", "openlist_connection_revisions", "openlist_mount_revisions"):
            self.assertEqual(body["meta"]["table_counts"][table], 2)
        historical_connection = document["openlist_connection_revisions"][0]
        self.assertIsInstance(historical_connection["config"], dict)
        self.assertTrue(historical_connection["password_ciphertext"])
        self.assertEqual(historical_connection["credential_key_id"], "v2:openlist")
        self.assertEqual(document["storage_target_revisions"][0]["categories"], {"movie": {"paths": ["/电影"]}})

    def test_round_trip_restores_every_table_with_identical_ciphertext(self) -> None:
        with _DatabaseWorkspace() as source:
            _seed(source)
            document = _service(source, ROOT_KEY_A).export_document()[0]["document"]
        with _DatabaseWorkspace() as target:
            target.app_settings.set_many({"advanced_config": {"pansou": {"timeout": 1}}, "admin.profile": {"username": "new-admin"}})
            with target.connect() as conn:
                conn.execute(
                    "INSERT INTO provider_accounts (account_id, provider_kind, name, enabled, current_revision,"
                    " identity_hint, created_at, updated_at) VALUES ('stale', 'quark', '旧账号', 1, 0, '', ?, ?)",
                    ("2025-01-01T00:00:00Z",) * 2,
                )
            result, status = _service(target, ROOT_KEY_A).import_document(document, confirm=True)
            self.assertEqual(status, 200, result)
            self.assertEqual(result["meta"]["table_counts"]["provider_accounts"], 1)

            for table in CONFIG_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(_read_table(target, table), _read_table_source(document, table))

            settings = target.app_settings.get_all()
            self.assertEqual(settings["submission.mode"], "review")
            self.assertFalse(settings["public.allow_anonymous_search"])
            self.assertEqual(settings["advanced_config"]["pansou"]["base_url"], "http://pansou.local")
            # 本机管理员凭据保持不变
            self.assertEqual(settings["admin.profile"]["username"], "new-admin")

            with target.connect() as conn:
                revision = conn.execute(
                    "SELECT credentials_ciphertext, credential_key_id FROM provider_account_revisions"
                    " WHERE account_id = 'acc-1'"
                ).fetchone()
            self.assertEqual(
                bytes(revision["credentials_ciphertext"]),
                base64.b64decode(document["provider_account_revisions"][0]["credentials_ciphertext"]),
            )
            self.assertEqual(revision["credential_key_id"], "v2:provider-account")
            self.assert_frozen_revisions_readable(target)

    def test_import_rejects_document_when_root_key_differs(self) -> None:
        with _DatabaseWorkspace() as source:
            _seed(source)
            document = _service(source, ROOT_KEY_A).export_document()[0]["document"]
        with _DatabaseWorkspace() as target:
            result, status = _service(target, ROOT_KEY_B).import_document(document, confirm=True)
            self.assertEqual(status, 400, result)
            self.assertIn("FNOS_ROOT_KEY", result["message"])
            # 未做部分写入
            self.assertEqual(_read_table(target, "provider_accounts"), [])
            self.assertNotIn("advanced_config", target.app_settings.get_all())

    def test_import_without_master_key_reports_configuration_error(self) -> None:
        from fnos_media_import.accounts.crypto import MasterKeyMissingError

        def missing_key() -> bytes:
            raise MasterKeyMissingError("FNOS_ROOT_KEY 未配置")

        with _DatabaseWorkspace() as source:
            _seed(source)
            document = _service(source, ROOT_KEY_A).export_document()[0]["document"]
        with _DatabaseWorkspace() as target:
            result, status = _service(target, missing_key).import_document(document, confirm=True)
            self.assertEqual(status, 400, result)
            self.assertIn("根密钥", result["message"])

    def test_reload_failure_rolls_back_to_previous_state(self) -> None:
        calls = 0

        def reload_runtime() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise RuntimeError("reload failed")

        with _DatabaseWorkspace() as source:
            _seed(source)
            document = _service(source, ROOT_KEY_A).export_document()[0]["document"]
        with _DatabaseWorkspace() as target:
            _seed(target)
            _seed_auth_session(target)
            target.app_settings.set_many({"advanced_config": {"pansou": {"timeout": 1}}})
            before = {table: _read_table(target, table) for table in CONFIG_TABLES}
            settings_before = target.app_settings.get_all()
            sessions_before = _read_table(target, "provider_auth_sessions")

            result, status = _service(target, ROOT_KEY_A, reload_runtime).import_document(document, confirm=True)
            self.assertEqual(status, 500, result)
            self.assertTrue(result["rolled_back"])
            self.assertFalse(result["runtime_restored"])
            self.assertEqual(calls, 2)
            for table in CONFIG_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(_read_table(target, table), before[table])
            self.assertEqual(target.app_settings.get_all(), settings_before)
            self.assertEqual(_read_table(target, "provider_auth_sessions"), sessions_before)
            self.assert_frozen_revisions_readable(target)

    def test_reload_failure_restores_runtime_from_rolled_back_database(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            document = _service(repos, ROOT_KEY_A).export_document()[0]["document"]
            document["app_settings"]["submission.mode"] = "auto"
            seen_modes: list[str] = []

            def reload_runtime() -> dict[str, Any]:
                seen_modes.append(repos.app_settings.get_all()["submission.mode"])
                if len(seen_modes) == 1:
                    raise RuntimeError("remote worker reload failed after local publication")
                return {"success": True}

            result, status = _service(repos, ROOT_KEY_A, reload_runtime).import_document(document, confirm=True)
            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertTrue(result["runtime_restored"])
            self.assertEqual(seen_modes, ["auto", "review"])
            self.assert_frozen_revisions_readable(repos)

    def test_reload_failure_result_is_not_reported_as_success(self) -> None:
        with _DatabaseWorkspace() as repos:
            document = {**_empty_document(), "app_settings": {"submission.mode": "auto"}}
            service = _service(repos, ROOT_KEY_A, lambda: {"success": False, "message": "worker unavailable"})
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertFalse(result["runtime_restored"])
            self.assertNotIn("submission.mode", repos.app_settings.get_all())

    def test_successful_import_detaches_retained_auth_session_before_account_deletion(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            _seed_auth_session(repos)
            service = _service(repos, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            self.assertNotIn("provider_auth_sessions", document)
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 200, result)
            sessions = _read_table(repos, "provider_auth_sessions")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["status"], "completed")
            self.assertIsNone(sessions[0]["account_id"])
            self.assert_frozen_revisions_readable(repos)

    def test_v1_backup_is_rejected_without_modifying_existing_configuration(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            service = _service(repos, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            document["version"] = 1
            for table in ("provider_account_selection_revisions", "openlist_connection_revisions", "openlist_mount_revisions"):
                del document[table]
            before = {table: _read_table(repos, table) for table in CONFIG_TABLES}
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 400)
            self.assertIn("v1", result["message"])
            self.assertIn("重新导出 v2", result["message"])
            self.assertEqual({table: _read_table(repos, table) for table in CONFIG_TABLES}, before)
            self.assert_frozen_revisions_readable(repos)

    def test_missing_or_null_sections_never_become_empty_tables(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            service = _service(repos, ROOT_KEY_A)
            original = service.export_document()[0]["document"]
            before = {table: _read_table(repos, table) for table in CONFIG_TABLES}
            for section in (*CONFIG_TABLES, "app_settings"):
                for omitted in (True, False):
                    with self.subTest(section=section, omitted=omitted):
                        document = copy.deepcopy(original)
                        if omitted:
                            del document[section]
                        else:
                            document[section] = None
                        result, status = service.import_document(document, confirm=True)
                        self.assertEqual(status, 400, result)
            self.assertEqual({table: _read_table(repos, table) for table in CONFIG_TABLES}, before)

    def test_empty_revision_sections_are_rejected_and_transaction_rolls_back(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            _seed_auth_session(repos)
            service = _service(repos, ROOT_KEY_A)
            original = service.export_document()[0]["document"]
            before = {table: _read_table(repos, table) for table in (*CONFIG_TABLES, "provider_auth_sessions")}
            for section in ("provider_account_selection_revisions", "openlist_connection_revisions", "openlist_mount_revisions"):
                with self.subTest(section=section):
                    document = copy.deepcopy(original)
                    document[section] = []
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 400, result)
                    self.assertIn("修订", result["message"])
            self.assertEqual({table: _read_table(repos, table) for table in before}, before)
            self.assert_frozen_revisions_readable(repos)

    def test_mismatched_selection_and_missing_historical_connection_are_rejected(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            service = _service(repos, ROOT_KEY_A)
            original = service.export_document()[0]["document"]
            documents = [copy.deepcopy(original), copy.deepcopy(original)]
            documents[0]["provider_account_selection_revisions"][-1]["scope"] = "wrong-scope"
            documents[1]["openlist_connection_revisions"] = [
                row for row in documents[1]["openlist_connection_revisions"] if row["revision"] == 2
            ]
            for document in documents:
                result, status = service.import_document(document, confirm=True)
                self.assertEqual(status, 400, result)
                self.assertIn("修订", result["message"])
            self.assert_frozen_revisions_readable(repos)

    def test_historical_openlist_credentials_are_verified_with_local_root_key(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            service = _service(repos, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            ciphertext, key_id = encrypt(ROOT_KEY_B, "historical-password", purpose=PURPOSE_OPENLIST)
            row = document["openlist_connection_revisions"][0]
            row["password_ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
            row["credential_key_id"] = key_id
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 400, result)
            self.assertIn("FNOS_ROOT_KEY", result["message"])
            self.assert_frozen_revisions_readable(repos)

    def test_dangling_binding_and_inconsistent_mount_revisions_do_not_modify_configuration(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            _seed_auth_session(repos)
            reloaded: list[bool] = []
            service = _service(repos, ROOT_KEY_A, lambda: reloaded.append(True) or {"success": True})
            original = service.export_document()[0]["document"]
            before = {table: _read_table(repos, table) for table in (*CONFIG_TABLES, "provider_auth_sessions")}
            cases = (
                ("storage_target_binding_revisions", "account_revision", None),
                ("storage_target_binding_revisions", "account_revision", 999),
                ("openlist_mount_revisions", "connection_revision", 1),
                ("openlist_mount_revisions", "namespace_id", "openlist:conn-1:another-mount"),
            )
            for table, field, value in cases:
                with self.subTest(table=table, field=field, value=value):
                    document = copy.deepcopy(original)
                    row = document[table][-1]
                    if table == "openlist_mount_revisions":
                        row = row["config"]
                    row[field] = value
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 400, result)
                    self.assertIn("修订", result["message"])
            self.assertEqual({table: _read_table(repos, table) for table in before}, before)
            self.assertEqual(reloaded, [])
            self.assert_frozen_revisions_readable(repos)

    def test_accountless_storage_binding_does_not_require_account_revision(self) -> None:
        with _DatabaseWorkspace() as repos:
            _seed(repos)
            service = _service(repos, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            document["storage_targets"][0]["provider_kind"] = "local"
            binding = document["storage_target_account_bindings"][0]
            binding["provider_kind"] = "local"
            binding["account_id"] = None
            document["storage_target_binding_revisions"][0]["account_revision"] = None
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 200, result)
            self.assertIsNone(_read_table(repos, "storage_target_account_bindings")[0]["account_id"])
            self.assertIsNone(_read_table(repos, "storage_target_binding_revisions")[0]["account_revision"])

    def test_invalid_documents_are_rejected(self) -> None:
        with _DatabaseWorkspace() as repos:
            service = _service(repos, ROOT_KEY_A)
            cases: list[tuple[Any, str]] = [
                ({"format": "wrong"}, "格式标识"),
                ({"version": FULL_BACKUP_VERSION + 1}, "版本"),
                ({"version": float(FULL_BACKUP_VERSION)}, "整数"),
                ({"version": True}, "整数"),
                ({"unknown_section": []}, "不支持的字段"),
                ({"app_settings": {"admin.profile": {}}}, "管理员"),
                ({"provider_accounts": [{"bogus": 1}]}, "不支持的字段"),
                ({"provider_accounts": [["not-an-object"]]}, "对象数组"),
                ({"provider_accounts": [{}]}, "必填字段"),
                ({"openlist_mount_revisions": [{"mount_id": "x", "revision": 1, "config": [], "created_at": "x"}]}, "JSON 对象"),
            ]
            for patch, needle in cases:
                document = {**_empty_document(), **patch}
                with self.subTest(patch=patch):
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 400)
                    self.assertIn(needle, result["message"])
            result, status = service.import_document(
                _empty_document(), confirm=False
            )
            self.assertEqual(status, 400)

    def test_round_trip_without_credentials_works_without_master_key(self) -> None:
        from fnos_media_import.accounts.crypto import MasterKeyMissingError

        def missing_key() -> bytes:
            raise MasterKeyMissingError("FNOS_ROOT_KEY 未配置")

        document = {**_empty_document(), "app_settings": {"submission.mode": "auto"}}
        with _DatabaseWorkspace() as target:
            result, status = _service(target, missing_key).import_document(document, confirm=True)
            self.assertEqual(status, 200, result)
            self.assertEqual(target.app_settings.get_all().get("submission.mode"), "auto")


def _read_table_source(document: dict[str, Any], table: str) -> list[dict[str, Any]]:
    """按导出文档重建源库行（密文/JSON 列反解），用于与目标库逐表对比。"""

    from fnos_media_import.services import backup_service

    spec = backup_service._TABLE_SPECS[table]
    raw_json = {raw: friendly for friendly, raw in spec.json_columns.items()}
    raw_blob = {raw: friendly for friendly, raw in spec.blob_columns.items()}

    rows: list[dict[str, Any]] = []
    for row in document.get(table, []):
        converted: dict[str, Any] = {}
        for key, value in row.items():
            if key in spec.json_columns:
                converted[spec.json_columns[key]] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            elif key in spec.blob_columns:
                converted[spec.blob_columns[key]] = base64.b64decode(value)
            else:
                converted[key] = value
        rows.append(converted)
    return rows


def encrypt_source_credentials() -> bytes:
    ciphertext, _ = encrypt(
        ROOT_KEY_A,
        json.dumps({"token": "quark-token"}),
        purpose=PURPOSE_PROVIDER_ACCOUNT,
    )
    return ciphertext


if __name__ == "__main__":
    unittest.main()
