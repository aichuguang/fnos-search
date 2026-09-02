from __future__ import annotations

import base64
import json
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from fnos_media_import.accounts.crypto import (
    PURPOSE_OPENLIST,
    PURPOSE_PROVIDER_ACCOUNT,
    encrypt,
)
from fnos_media_import.database import Database
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
    "openlist_connections",
    "openlist_mounts",
    "storage_targets",
    "storage_target_revisions",
    "storage_target_account_bindings",
    "storage_target_binding_revisions",
    "source_destination_assignments",
)


class _DatabaseWorkspace:
    def __enter__(self) -> RepositorySet:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = WORK_TEST_ROOT / f"backup-{uuid.uuid4().hex}.db"
        self.database = Database(self.path)
        self.database.init_schema()
        return RepositorySet.from_database(self.database)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.database.close_all_connections()
        except AttributeError:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                (self.path.parent / f"{self.path.name}{suffix}").unlink()
            except FileNotFoundError:
                pass
        try:
            if self.path.parent.exists() and not any(self.path.parent.iterdir()):
                shutil.rmtree(self.path.parent)
        except OSError:
            pass


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
    password_cipher, password_key_id = encrypt(
        ROOT_KEY_A,
        "openlist-password",
        purpose=PURPOSE_OPENLIST,
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
            "INSERT INTO provider_account_selections (provider_kind, scope, account_id, account_revision,"
            " selection_revision, selected_at) VALUES ('quark', 'default', 'acc-1', 1, 1, ?)",
            ("2026-01-01T00:00:00Z",),
        )
        conn.execute(
            "INSERT INTO openlist_connections (connection_id, name, base_url, username, password_ciphertext,"
            " credential_key_id, enabled, revision, created_at, updated_at)"
            " VALUES ('conn-1', 'OpenList', 'http://openlist.local/dav', 'admin', ?, ?, 1, 1, ?, ?)",
            (password_cipher, password_key_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO openlist_mounts (mount_id, connection_id, name, webdav_root, namespace_id,"
            " native_locator_json) VALUES ('mount-1', 'conn-1', 'media', '/media', 'ns', '{}')"
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
            "INSERT INTO storage_target_binding_revisions (binding_id, revision, locator_json, created_at)"
            " VALUES ('bind-1', 1, ?, ?)",
            (json.dumps({"mode": "native"}), "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO source_destination_assignments (search_source_id, category_key, destination_id,"
            " enabled, revision, created_at, updated_at) VALUES ('pansou', 'movie', 'dest-1', 1, 1, ?, ?)",
            ("2026-01-01T00:00:00Z",) * 2,
        )


def _read_table(repos: RepositorySet, table: str) -> list[dict[str, Any]]:
    with repos.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


class FullBackupExportImportTests(unittest.TestCase):
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
            target.app_settings.set_many({"advanced_config": {"pansou": {"timeout": 1}}})
            with target.connect() as conn:
                conn.execute(
                    "INSERT INTO provider_accounts (account_id, provider_kind, name, enabled, current_revision,"
                    " identity_hint, created_at, updated_at) VALUES ('local', 'quark', '本机账号', 1, 0, '', ?, ?)",
                    ("2025-01-01T00:00:00Z",) * 2,
                )
            before = {table: _read_table(target, table) for table in CONFIG_TABLES}
            settings_before = target.app_settings.get_all()

            result, status = _service(target, ROOT_KEY_A, reload_runtime).import_document(document, confirm=True)
            self.assertEqual(status, 500, result)
            self.assertTrue(result["rolled_back"])
            self.assertEqual(calls, 1)
            for table in CONFIG_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(_read_table(target, table), before[table])
            self.assertEqual(target.app_settings.get_all(), settings_before)

    def test_invalid_documents_are_rejected(self) -> None:
        with _DatabaseWorkspace() as repos:
            service = _service(repos, ROOT_KEY_A)
            cases: list[tuple[Any, str]] = [
                ({"format": "wrong", "version": 1}, "格式标识"),
                ({"format": FULL_BACKUP_FORMAT, "version": 2}, "版本"),
                ({"format": FULL_BACKUP_FORMAT, "version": 1, "unknown_section": []}, "不支持的字段"),
                (
                    {"format": FULL_BACKUP_FORMAT, "version": 1, "app_settings": {"admin.profile": {}}},
                    "管理员",
                ),
                (
                    {"format": FULL_BACKUP_FORMAT, "version": 1, "provider_accounts": [{"bogus": 1}]},
                    "不支持的字段",
                ),
                (
                    {"format": FULL_BACKUP_FORMAT, "version": 1, "provider_accounts": [["not-an-object"]]},
                    "对象数组",
                ),
            ]
            for document, needle in cases:
                with self.subTest(document=document):
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 400)
                    self.assertIn(needle, result["message"])
            result, status = service.import_document(
                {"format": FULL_BACKUP_FORMAT, "version": 1}, confirm=False
            )
            self.assertEqual(status, 400)

    def test_round_trip_without_credentials_works_without_master_key(self) -> None:
        from fnos_media_import.accounts.crypto import MasterKeyMissingError

        def missing_key() -> bytes:
            raise MasterKeyMissingError("FNOS_ROOT_KEY 未配置")

        document = {
            "format": FULL_BACKUP_FORMAT,
            "version": FULL_BACKUP_VERSION,
            "app_settings": {"submission.mode": "auto"},
        }
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
