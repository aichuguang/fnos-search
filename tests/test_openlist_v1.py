"""OpenList 连接与挂载（connection/mount）模块测试。

使用真实 ``Database`` 临时文件 + 注入的 fake probe runner / webdav factory，
不发真实网络请求。运行：``python -m unittest tests.test_openlist_v1``
"""

from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from flask import Flask

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.openlist import (
    OpenListCredentialError,
    OpenListMasterKeyError,
    OpenListNotFoundError,
    OpenListValidationError,
)
from fnos_media_import.openlist.blueprint import (
    OpenListRouteContext,
    create_openlist_blueprint,
    default_handlers,
)
from fnos_media_import.openlist.connection_service import (
    CONNECTION_KEY_ID,
    OpenListConnectionService,
    env_master_key_provider,
)
from fnos_media_import.openlist.locator_resolver import RcloneLocatorResolver
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.time_utils import utc_now_iso

MASTER_KEY_HEX = "0123456789abcdef" * 4  # 32 字节 AES-256
WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "openlist-v1-tests"


def _relative_path(root: str, target: str) -> str | None:
    """target 相对 root 的剩余路径；不在其下则返回 None。"""
    root = root.rstrip("/") or "/"
    target = target.rstrip("/") or "/"
    if root == "/":
        return target.lstrip("/") or None
    if target.startswith(root + "/"):
        return target[len(root) + 1:]
    return None


class FakeWebDAV(RepositoryFakeMixin):
    """内存 WebDAV 模拟：支持探测所需的全部操作，可注入各步骤失败。"""

    def __init__(
        self,
        *,
        auth_fail: bool = False,
        list_fail: bool = False,
        mkcol_fail: bool = False,
        put_fail: bool = False,
        read_fail: bool = False,
        move_fail: bool = False,
        delete_fail: bool = False,
    ) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.auth_fail = auth_fail
        self.list_fail = list_fail
        self.mkcol_fail = mkcol_fail
        self.put_fail = put_fail
        self.read_fail = read_fail
        self.move_fail = move_fail
        self.delete_fail = delete_fail

    def auth_probe(self) -> None:
        if self.auth_fail:
            raise RuntimeError("auth failed")

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        if self.list_fail:
            raise RuntimeError("list failed")
        items: list[dict[str, Any]] = []
        for directory in sorted(self.dirs):
            rest = _relative_path(path, directory)
            if rest is not None and "/" not in rest:
                items.append({"path": directory, "name": rest, "is_dir": True, "size": None})
        for file_path in sorted(self.files):
            rest = _relative_path(path, file_path)
            if rest is not None and "/" not in rest:
                items.append({"path": file_path, "name": rest, "is_dir": False, "size": len(self.files[file_path])})
        return items

    def mkcol(self, path: str) -> None:
        if self.mkcol_fail:
            raise RuntimeError("mkcol failed")
        self.dirs.add(path.rstrip("/"))

    def put_bytes(self, path: str, content: bytes) -> None:
        if self.put_fail:
            raise RuntimeError("put failed")
        self.files[path] = content

    def read_bytes(self, path: str) -> bytes:
        if self.read_fail:
            raise RuntimeError("read failed")
        return self.files[path]

    def file_size(self, path: str) -> int:
        return len(self.files.get(path, b""))

    def move(self, source: str, destination: str) -> None:
        if self.move_fail:
            raise RuntimeError("move failed")
        if source in self.files:
            self.files[destination] = self.files.pop(source)

    def delete(self, path: str) -> None:
        if self.delete_fail:
            raise RuntimeError("delete failed")
        self.files.pop(path, None)
        self.dirs.discard(path.rstrip("/"))

    def exists(self, path: str) -> bool:
        return path in self.files or path.rstrip("/") in self.dirs


def _new_database(testcase: unittest.TestCase) -> RepositorySet:
    WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    database_path = WORK_TEST_ROOT / f"openlist-{uuid.uuid4().hex}.db"
    database = Database(database_path)
    database.init_schema()

    def cleanup() -> None:
        try:
            database.close_all_connections()
        except AttributeError:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{database_path}{suffix}").unlink()
            except FileNotFoundError:
                pass
        try:
            if WORK_TEST_ROOT.exists() and not any(WORK_TEST_ROOT.iterdir()):
                shutil.rmtree(WORK_TEST_ROOT)
        except OSError:
            pass

    testcase.addCleanup(cleanup)
    return RepositorySet.from_database(database)


def _insert_provider_account(database: Database, account_id: str = "acc_quark_a", provider_kind: str = "quark") -> None:
    now = utc_now_iso()
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO provider_accounts (account_id, provider_kind, name, created_at, updated_at)
            VALUES (?,?,?,?,?)
            """,
            (account_id, provider_kind, "账号", now, now),
        )


class OpenListConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _new_database(self)
        self.service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: MASTER_KEY_HEX,
            probe_runner=lambda _ctx: {"ok": True, "message": "probe ok"},
        )

    def _create(self, connection_id: str = "ol_main", password: str = "s3cret") -> dict[str, Any]:
        return self.service.create_connection(
            {
                "connection_id": connection_id,
                "name": "主连接",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": password,
            }
        )

    def test_connection_crud_and_password_never_leaks(self) -> None:
        item = self._create()
        self.assertEqual(item["connection_id"], "ol_main")
        self.assertEqual(item["revision"], 1)
        self.assertTrue(item["credential_configured"])
        self.assertNotIn("password", item)
        self.assertNotIn("password_ciphertext", item)
        self.assertNotIn("credential_key_id", item)

        listed = self.service.list_connections()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("password", listed[0])
        self.assertNotIn("password_ciphertext", listed[0])

        # 直接查库：密文是 BLOB，key_id 记录用途派生版本。
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT password_ciphertext, credential_key_id FROM openlist_connections WHERE connection_id='ol_main'"
            ).fetchone()
        self.assertEqual(row["credential_key_id"], CONNECTION_KEY_ID)
        self.assertIsInstance(row["password_ciphertext"], bytes)
        self.assertNotEqual(row["password_ciphertext"], b"s3cret")
        self.assertNotIn(b"s3cret", row["password_ciphertext"])

        updated = self.service.update_connection("ol_main", {"name": "改名连接"})
        self.assertEqual(updated["name"], "改名连接")
        self.assertEqual(updated["revision"], 2)
        self.assertNotIn("password", updated)

        got = self.service.get_connection("ol_main")
        self.assertEqual(got["name"], "改名连接")
        self.assertTrue(got["credential_configured"])

        self.assertIsNone(self.service.get_connection("missing"))

    def test_connection_probe_can_be_read_only(self) -> None:
        self._create()
        before = self.service.get_connection("ol_main")

        result = self.service.test_connection(
            "ol_main",
            expected_revision=before["revision"],
            persist_result=False,
        )

        after = self.service.get_connection("ol_main")
        self.assertTrue(result["ok"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["last_test_status"], before["last_test_status"])
        self.assertEqual(after["last_tested_at"], before["last_tested_at"])

    def test_resolve_credentials_can_read_frozen_revision_after_edit(self) -> None:
        self._create(password="old-secret")
        updated = self.service.update_connection(
            "ol_main",
            {
                "base_url": "http://127.0.0.1:6244/dav",
                "username": "new-admin",
                "password": "new-secret",
            },
        )
        current = self.service.resolve_credentials("ol_main")
        frozen = self.service.resolve_credentials("ol_main", 1)
        self.assertEqual(current["revision"], updated["revision"])
        self.assertEqual(current["base_url"], "http://127.0.0.1:6244/dav")
        self.assertEqual(current["username"], "new-admin")
        self.assertEqual(current["password"], "new-secret")
        self.assertEqual(frozen["revision"], 1)
        self.assertEqual(frozen["base_url"], "http://127.0.0.1:5244/dav")
        self.assertEqual(frozen["username"], "admin")
        self.assertEqual(frozen["password"], "old-secret")

    def test_connection_update_uses_revision_cas(self) -> None:
        self._create()
        first = self.service.update_connection("ol_main", {"name": "第一次"})
        with self.assertRaises(OpenListValidationError) as caught:
            self.service.update_connection(
                "ol_main",
                {"name": "覆盖并发修改", "expected_revision": 1},
            )
        self.assertEqual(getattr(caught.exception, "code", ""), "OPENLIST_CONNECTION_CONFLICT")
        self.assertEqual(getattr(caught.exception, "status", 0), 409)
        self.assertEqual(self.service.get_connection("ol_main")["revision"], first["revision"])

    def test_operational_connection_edit_is_blocked_while_plan_is_active(self) -> None:
        self._create()
        now = utc_now_iso()
        job_id, created = self.database.job_commands.create(
            {
                "title": "active-openlist-plan",
                "category": "movie",
                "category_label": "Movie",
                "source_provider_kind": "quark",
                "source_form": "share",
                "source_url": "source-record:active-openlist-plan",
                "target_path": "/openlist/movie",
                "status": "waiting_transfer",
                "destination_id": "native-main",
                "plan_version": 1,
                "idempotency_key": "openlist:active-plan",
            }
        )
        self.assertTrue(created)
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO import_execution_plans(job_id, schema_version, plan_json, plan_hash, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    job_id,
                    1,
                    json.dumps(
                        {"bindings": {"destination_connection_id": "ol_main"}}
                    ),
                    "hash",
                    now,
                ),
            )

        with self.assertRaises(OpenListValidationError) as caught:
            self.service.update_connection(
                "ol_main",
                {"password": "rotated-secret", "expected_revision": 1},
            )

        self.assertEqual(
            getattr(caught.exception, "code", ""),
            "OPENLIST_CONNECTION_IN_USE",
        )
        self.assertEqual(getattr(caught.exception, "status", 0), 409)
        self.assertTrue(getattr(caught.exception, "retryable", False))
        self.assertEqual(self.service.get_connection("ol_main")["revision"], 1)

        # Display-only edits, including an explicitly repeated identical
        # password, do not rewrite the managed Remote identity.
        renamed = self.service.update_connection(
            "ol_main",
            {
                "name": "仅修改展示名称",
                "password": "s3cret",
                "expected_revision": 1,
            },
        )
        self.assertEqual(renamed["revision"], 2)
        self.assertTrue(self.service.runtime_identity_matches("ol_main", 1))

    def test_terminal_plan_allows_edit_but_old_runtime_identity_fails_closed(self) -> None:
        self._create(password="old-secret")
        now = utc_now_iso()
        job_id, created = self.database.job_commands.create(
            {
                "title": "completed-openlist-plan",
                "category": "movie",
                "category_label": "Movie",
                "source_provider_kind": "quark",
                "source_form": "share",
                "source_url": "source-record:completed-openlist-plan",
                "target_path": "/openlist/movie",
                "status": "done",
                "destination_id": "native-main",
                "plan_version": 1,
                "idempotency_key": "openlist:completed-plan",
            }
        )
        self.assertTrue(created)
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO import_execution_plans(job_id, schema_version, plan_json, plan_hash, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    job_id,
                    1,
                    json.dumps(
                        {"bindings": {"destination_connection_id": "ol_main"}}
                    ),
                    "hash",
                    now,
                ),
            )

        updated = self.service.update_connection(
            "ol_main",
            {"password": "new-secret", "expected_revision": 1},
        )

        self.assertEqual(updated["revision"], 2)
        self.assertFalse(self.service.runtime_identity_matches("ol_main", 1))
        self.assertTrue(self.service.runtime_identity_matches("ol_main", 2))

    def test_delete_connection_rejects_execution_plan_reference(self) -> None:
        self._create()
        now = utc_now_iso()
        job_id, created = self.database.job_commands.create(
            {
                "title": "plan-reference",
                "category": "movie",
                "category_label": "Movie",
                "source_provider_kind": "quark",
                "source_form": "share",
                "source_url": "source-record:openlist-plan-reference",
                "target_path": "/openlist/movie",
                "status": "queued",
                "destination_id": "native-main",
                "plan_version": 1,
                "idempotency_key": "openlist:plan-reference",
            }
        )
        self.assertTrue(created)
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO import_execution_plans(job_id, schema_version, plan_json, plan_hash, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    job_id,
                    1,
                    json.dumps({"bindings": {"destination_connection_id": "ol_main"}}),
                    "hash",
                    now,
                ),
            )
        with self.assertRaises(OpenListValidationError):
            self.service.delete_connection("ol_main")

    def test_delete_connection_rejects_transfer_execution_reference(self) -> None:
        self._create()
        now = utc_now_iso()
        job_id, created = self.database.job_commands.create(
            {
                "title": "transfer-reference",
                "category": "movie",
                "category_label": "Movie",
                "source_provider_kind": "quark",
                "source_form": "share",
                "source_url": "source-record:openlist-transfer-reference",
                "target_path": "/openlist/movie",
                "status": "queued",
                "destination_id": "native-main",
                "plan_version": 1,
                "idempotency_key": "openlist:transfer-reference",
            }
        )
        self.assertTrue(created)
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO transfer_executions
                  (job_id, strategy, phase, snapshot_json, cleanup_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    "rclone_copy",
                    "planned",
                    json.dumps({"destination": {"connection_id": "ol_main"}}),
                    "{}",
                    now,
                    now,
                ),
            )
        with self.assertRaises(OpenListValidationError):
            self.service.delete_connection("ol_main")

    def test_legacy_ol_v1_ciphertext_is_rejected(self) -> None:
        self._create()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE openlist_connections SET password_ciphertext=?, credential_key_id='ol:v1' "
                "WHERE connection_id='ol_main'",
                (b"legacy-ciphertext",),
            )
        with self.assertRaises(OpenListCredentialError):
            self.service.resolve_credentials("ol_main")
        self.assertFalse(self.service.get_connection("ol_main")["credential_configured"])

    def test_environment_provider_ignores_legacy_root_name(self) -> None:
        with mock.patch.dict(os.environ, {"FNOS_ACCOUNT_MASTER_KEY": MASTER_KEY_HEX}, clear=True):
            self.assertEqual(env_master_key_provider()(), "")

    def test_connection_test_updates_status(self) -> None:
        self._create()
        result = self.service.test_connection("ol_main")
        self.assertTrue(result["success"])
        self.assertEqual(result["last_test_status"], "ok")
        self.assertIsNotNone(result["last_tested_at"])
        stored = self.service.get_connection("ol_main")
        self.assertEqual(stored["last_test_status"], "ok")

    def test_connection_failed_probe_marks_failed(self) -> None:
        service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: MASTER_KEY_HEX,
            probe_runner=lambda _ctx: {"ok": False, "message": "401 认证失败"},
        )
        service.create_connection(
            {
                "connection_id": "ol_fail",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "wrong",
            }
        )
        result = service.test_connection("ol_fail")
        self.assertFalse(result["success"])
        self.assertEqual(service.get_connection("ol_fail")["last_test_status"], "failed")

    def test_connection_probe_result_cannot_overwrite_new_revision(self) -> None:
        self._create()

        def stale_probe(context: dict[str, Any]) -> dict[str, Any]:
            # Simulate an administrator rotating the credentials while the
            # network probe is still in flight.
            self.service.update_connection(
                "ol_main",
                {
                    "password": "rotated-secret",
                    "expected_revision": context["revision"],
                },
            )
            return {"ok": True, "message": "旧凭证探测成功"}

        service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: MASTER_KEY_HEX,
            probe_runner=stale_probe,
        )
        with self.assertRaises(OpenListValidationError) as caught:
            service.test_connection("ol_main", expected_revision=1)
        self.assertEqual(getattr(caught.exception, "code", ""), "OPENLIST_CONNECTION_CONFLICT")
        self.assertEqual(getattr(caught.exception, "status", 0), 409)
        current = service.get_connection("ol_main")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["last_test_status"], "untested")

    def test_missing_master_key_raises_clear_error(self) -> None:
        service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: "",
            probe_runner=lambda _ctx: {"ok": True},
        )
        with self.assertRaises(OpenListMasterKeyError) as caught:
            service.create_connection(
                {
                    "connection_id": "ol_nokey",
                    "base_url": "http://127.0.0.1:5244/dav",
                    "username": "admin",
                    "password": "s3cret",
                }
            )
        self.assertIn("FNOS_ROOT_KEY", str(caught.exception))

    def test_invalid_base_url_rejected(self) -> None:
        with self.assertRaises(OpenListValidationError):
            self.service.create_connection(
                {
                    "connection_id": "ol_bad",
                    "base_url": "file:///etc/passwd",
                    "username": "admin",
                    "password": "s3cret",
                }
            )
        with self.assertRaises(OpenListValidationError):
            self.service.create_connection(
                {
                    "connection_id": "ol_bad2",
                    "base_url": "http://user:pass@127.0.0.1:5244/dav",
                    "username": "admin",
                    "password": "s3cret",
                }
            )

    def test_wrong_master_key_cannot_decrypt(self) -> None:
        self._create()
        broken = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: "f" * 64,
            probe_runner=lambda _ctx: {"ok": True},
        )
        with self.assertRaises(OpenListCredentialError):
            broken.test_connection("ol_main")


class OpenListMountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _new_database(self)
        self.connection_service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: MASTER_KEY_HEX,
            probe_runner=lambda _ctx: {"ok": True, "message": "ok"},
        )
        self.connection_service.create_connection(
            {
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            }
        )
        _insert_provider_account(self.database)
        self.mount_service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        )

    def test_mount_create_auto_namespace_and_bind(self) -> None:
        mount = self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "mount_quark_main",
                "name": "夸克影视",
                "webdav_root": "/影视",
                "provider_kind": "quark",
                "account_id": "acc_quark_a",
            }
        )
        self.assertEqual(mount["namespace_id"], "openlist:ol_main:mount_quark_main")
        self.assertEqual(mount["provider_kind"], "quark")
        self.assertEqual(mount["account_id"], "acc_quark_a")
        self.assertEqual(mount["webdav_root"], "/影视")
        self.assertFalse(mount["readable"])
        self.assertFalse(mount["writable"])
        self.assertEqual(mount["revision"], 1)

    def test_mount_auto_generates_mount_id(self) -> None:
        mount = self.mount_service.create_mount(
            {"connection_id": "ol_main", "webdav_root": "/电影", "storage_identity_id": "storage:auto"}
        )
        self.assertTrue(mount["mount_id"].startswith("mount_"))
        self.assertEqual(mount["namespace_id"], f"openlist:ol_main:{mount['mount_id']}")

    def test_mount_bind_conflict_rejected(self) -> None:
        with self.assertRaises(OpenListValidationError):
            self.mount_service.create_mount(
                {
                    "connection_id": "ol_main",
                    "webdav_root": "/剧集",
                    "provider_kind": "quark",
                    "account_id": "missing_account",
                }
            )

    def test_probe_updates_readable_writable(self) -> None:
        self.mount_service.create_mount(
            {"connection_id": "ol_main", "mount_id": "m1", "webdav_root": "/影视", "account_id": "acc_quark_a"}
        )
        fake = FakeWebDAV()
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: fake,
        )
        result = service.probe_mount("m1")
        self.assertTrue(result["success"])
        self.assertTrue(result["readable"])
        self.assertTrue(result["writable"])
        self.assertEqual(result["residue_warnings"], [])
        self.assertEqual(result["identity_status"], "unverified")

        stored = self.mount_service.get_mount("m1")
        self.assertTrue(stored["readable"])
        self.assertTrue(stored["writable"])
        self.assertEqual(stored["identity_status"], "unverified")
        self.assertIsNotNone(stored["last_tested_at"])

        # 探测临时目录只落在 /_fnos_probe，全部清理；探测根目录本身保留（持久配置目录）
        self.assertEqual(fake.dirs, {"/影视/_fnos_probe"})
        self.assertEqual(fake.files, {})
        self.assertFalse(any("/fnos-probe-" in path for path in fake.dirs))
        self.assertFalse(any(path.startswith("/影视") for path in fake.files))

    def test_repeated_probe_with_same_health_keeps_revision_and_history(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_probe_stable",
                "webdav_root": "/影视",
                "account_id": "acc_quark_a",
            }
        )
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        )
        first = service.probe_mount("m_probe_stable")
        with self.database.connect() as connection:
            first_history = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                ("m_probe_stable",),
            ).fetchone()[0]

        second = service.probe_mount(
            "m_probe_stable",
            expected_revision=first["revision"],
            expected_connection_revision=first["connection_revision"],
        )
        with self.database.connect() as connection:
            second_history = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                ("m_probe_stable",),
            ).fetchone()[0]

        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second_history, first_history)

    def test_probe_health_transition_creates_a_new_revision(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_probe_transition",
                "webdav_root": "/影视",
                "account_id": "acc_quark_a",
            }
        )
        healthy = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        ).probe_mount("m_probe_transition")
        failed = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(auth_fail=True),
        ).probe_mount(
            "m_probe_transition",
            expected_revision=healthy["revision"],
            expected_connection_revision=healthy["connection_revision"],
        )

        self.assertFalse(failed["readable"])
        self.assertFalse(failed["writable"])
        self.assertEqual(failed["revision"], healthy["revision"] + 1)

    def test_read_only_probe_reports_health_without_advancing_revision(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_probe_read_only",
                "webdav_root": "/media",
                "account_id": "acc_quark_a",
            }
        )
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        )
        before = self.mount_service.get_mount("m_probe_read_only")
        with self.database.connect() as connection:
            history_before = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                ("m_probe_read_only",),
            ).fetchone()[0]

        result = service.probe_mount(
            "m_probe_read_only",
            expected_revision=before["revision"],
            expected_connection_revision=before["connection_revision"],
            persist_result=False,
        )

        after = self.mount_service.get_mount("m_probe_read_only")
        with self.database.connect() as connection:
            history_after = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                ("m_probe_read_only",),
            ).fetchone()[0]
        self.assertTrue(result["readable"])
        self.assertTrue(result["writable"])
        self.assertFalse(after["readable"])
        self.assertFalse(after["writable"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["last_tested_at"], before["last_tested_at"])
        self.assertEqual(history_after, history_before)

    def test_name_only_mount_update_preserves_health_and_identity(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_name",
                "webdav_root": "/影视",
                "provider_kind": "quark",
                "account_id": "acc_quark_a",
            }
        )
        self.mount_service.probe_mount("m_name")
        confirmed = self.mount_service.confirm_identity("m_name")
        renamed = self.mount_service.update_mount(
            "m_name",
            {"name": "新展示名称", "expected_revision": confirmed["revision"]},
        )
        self.assertEqual(renamed["name"], "新展示名称")
        self.assertEqual(renamed["identity_status"], "declared")
        self.assertTrue(renamed["readable"])
        self.assertTrue(renamed["writable"])
        self.assertEqual(renamed["last_tested_at"], confirmed["last_tested_at"])

    def test_connection_credential_change_invalidates_mount_health(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_conn_change",
                "webdav_root": "/影视",
                "provider_kind": "quark",
                "account_id": "acc_quark_a",
            }
        )
        self.mount_service.probe_mount("m_conn_change")
        confirmed = self.mount_service.confirm_identity("m_conn_change")
        updated_connection = self.connection_service.update_connection(
            "ol_main",
            {"password": "rotated-secret", "expected_revision": 1},
        )
        mount = self.mount_service.get_mount("m_conn_change")
        self.assertEqual(mount["identity_status"], "unverified")
        self.assertFalse(mount["readable"])
        self.assertFalse(mount["writable"])
        self.assertGreater(mount["revision"], confirmed["revision"])
        self.assertEqual(mount["connection_revision"], updated_connection["revision"])

    def test_connection_name_change_advances_mount_snapshot_without_invalidating_health(self) -> None:
        self.mount_service.create_mount(
            {
                "connection_id": "ol_main",
                "mount_id": "m_conn_name",
                "webdav_root": "/影视",
                "provider_kind": "quark",
                "account_id": "acc_quark_a",
            }
        )
        self.mount_service.probe_mount("m_conn_name")
        confirmed = self.mount_service.confirm_identity("m_conn_name")
        updated_connection = self.connection_service.update_connection(
            "ol_main",
            {"name": "新的展示名称", "expected_revision": 1},
        )
        mount = self.mount_service.get_mount("m_conn_name")
        self.assertEqual(mount["identity_status"], "declared")
        self.assertTrue(mount["readable"])
        self.assertTrue(mount["writable"])
        self.assertGreater(mount["revision"], confirmed["revision"])
        self.assertEqual(mount["connection_revision"], updated_connection["revision"])

    def test_connection_operational_change_resets_connection_health(self) -> None:
        self.connection_service.test_connection("ol_main")
        self.assertEqual(
            self.connection_service.get_connection("ol_main")["last_test_status"], "ok"
        )
        updated = self.connection_service.update_connection(
            "ol_main",
            {"password": "rotated-secret", "expected_revision": 1},
        )
        self.assertEqual(updated["last_test_status"], "untested")
        self.assertIsNone(updated["last_tested_at"])

    def test_probe_cleanup_failure_reports_residue_and_marks_unwritable(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m2", "webdav_root": "/影视", "storage_identity_id": "storage:m2"})
        failing = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(delete_fail=True),
        )
        result = failing.probe_mount("m2")
        self.assertFalse(result["success"])
        self.assertFalse(result["writable"])
        self.assertTrue(result["residue_warnings"])
        self.assertIn("fnos-probe-", result["residue_warnings"][0])
        stored = self.mount_service.get_mount("m2")
        self.assertFalse(stored["writable"])

    def test_probe_auth_failure_marks_unreadable(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m3", "webdav_root": "/影视", "storage_identity_id": "storage:m3"})
        failing = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(auth_fail=True),
        )
        result = failing.probe_mount("m3")
        self.assertFalse(result["success"])
        self.assertFalse(result["readable"])
        self.assertFalse(result["writable"])
        self.assertEqual(self.mount_service.get_mount("m3")["readable"], False)

    def test_probe_partial_creation_still_cleans_up(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m6", "webdav_root": "/影视", "storage_identity_id": "storage:m6"})
        fake = FakeWebDAV(put_fail=True)
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: fake,
        )
        result = service.probe_mount("m6")
        self.assertFalse(result["success"])
        self.assertFalse(result["writable"])
        # mkcol 成功但写入失败；清理步骤仍应删除临时目录，仅保留探测根目录，不留下“需要清理”告警
        self.assertEqual(fake.dirs, {"/影视/_fnos_probe"})
        self.assertEqual(fake.files, {})
        self.assertEqual(result["residue_warnings"], [])

    def test_list_mountable_dirs(self) -> None:
        fake = FakeWebDAV()
        fake.dirs.add("/影视")
        fake.dirs.add("/剧集")
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: fake,
        )
        items = service.list_mountable_dirs("ol_main")
        self.assertEqual([item["path"] for item in items], ["/剧集", "/影视"])
        self.assertTrue(all("name" in item for item in items))

    def test_list_mountable_dirs_cascades_into_subdirectory(self) -> None:
        fake = FakeWebDAV()
        fake.dirs.update({"/影视", "/影视/电影", "/影视/电视剧", "/影视/电影/2026"})
        fake.files["/影视/readme.txt"] = b"x"
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: fake,
        )
        # 只返回目录，且逐级展开时只给出直接子级
        items = service.list_mountable_dirs("ol_main", "/影视")
        self.assertEqual([item["path"] for item in items], ["/影视/电影", "/影视/电视剧"])
        self.assertEqual([item["name"] for item in items], ["电影", "电视剧"])
        deeper = service.list_mountable_dirs("ol_main", "/影视/电影")
        self.assertEqual([item["path"] for item in deeper], ["/影视/电影/2026"])

    def test_list_mountable_dirs_rejects_traversal(self) -> None:
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        )
        with self.assertRaises(OpenListValidationError):
            service.list_mountable_dirs("ol_main", "/影视/../../etc")

    def test_mount_delete(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m4", "webdav_root": "/影视", "storage_identity_id": "storage:m4"})
        result = self.mount_service.delete_mount("m4")
        self.assertTrue(result["deleted"])
        self.assertIsNone(self.mount_service.get_mount("m4"))
        with self.assertRaises(OpenListNotFoundError):
            self.mount_service.delete_mount("m4")

    def test_connection_delete_blocked_by_mounts(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m5", "webdav_root": "/影视", "storage_identity_id": "storage:m5"})
        with self.assertRaises(OpenListValidationError):
            self.connection_service.delete_connection("ol_main")


class RcloneLocatorResolverTests(unittest.TestCase):
    def test_resolves_remote_path_and_namespace(self) -> None:
        resolver = RcloneLocatorResolver(
            {
                "remote_name": "ol_main",
                "config_path": "/config/rclone.conf",
                "mount_root_resolver": lambda _cid, _mid: "/影视",
            }
        )
        self.assertEqual(
            resolver.resolve("ol_main", "mount_quark_main", "/电影/星球大战"),
            "ol_main:/影视/电影/星球大战",
        )
        self.assertEqual(resolver.namespace_id("ol_main", "mount_quark_main"), "openlist:ol_main:mount_quark_main")
        ref = resolver.path_ref("ol_main", "mount_quark_main", "/电影/")
        self.assertEqual(ref["namespace_id"], "openlist:ol_main:mount_quark_main")
        self.assertEqual(ref["path"], "/电影")

    def test_normalizes_backslashes_and_rejects_traversal(self) -> None:
        resolver = RcloneLocatorResolver(
            {"remote_name": "ol_main", "config_path": "/config/rclone.conf", "mount_root_resolver": lambda _c, _m: "影视\\电影"}
        )
        self.assertEqual(resolver.remote_path("影视\\电影", "sub"), "ol_main:/影视/电影/sub")
        with self.assertRaises(OpenListValidationError):
            resolver.path_ref("c", "m", "/../../etc/passwd")
        with self.assertRaises(OpenListValidationError):
            resolver.path_ref("c", "m", "http://evil.example/path")

    def test_rejects_arbitrary_remote_names_and_config_paths(self) -> None:
        with self.assertRaises(OpenListValidationError):
            RcloneLocatorResolver({"remote_name": "", "config_path": "/config/rclone.conf"})
        with self.assertRaises(OpenListValidationError):
            RcloneLocatorResolver({"remote_name": "evil:remote", "config_path": "/config/rclone.conf"})
        with self.assertRaises(OpenListValidationError):
            RcloneLocatorResolver({"remote_name": "ol_main", "config_path": ""})
        # resolve 不接收 remote 参数，只能使用受控 remote_name；即使相对路径
        # 形如 “别的remote:/x” 也只拼到受控 remote 下。
        resolver = RcloneLocatorResolver(
            {"remote_name": "ol_main", "config_path": "/config/rclone.conf", "mount_root_resolver": lambda _c, _m: "/影视"}
        )
        self.assertEqual(resolver.remote_path("/影视", "别的remote:/x"), "ol_main:/影视/别的remote:/x")

    def test_rejects_mount_root_as_task_path(self) -> None:
        resolver = RcloneLocatorResolver(
            {"remote_name": "ol_main", "config_path": "/config/rclone.conf", "mount_root_resolver": lambda _c, _m: "/影视"}
        )
        with self.assertRaises(OpenListValidationError):
            resolver.resolve("c", "m", "")

    def test_frozen_rclone_path_rejects_changed_runtime_connection_identity(self) -> None:
        resolver = RcloneLocatorResolver(
            {
                "remote_name": "ol_main",
                "config_path": "/config/rclone.conf",
                "connection_runtime_identity_validator": lambda _connection_id, _revision: False,
            }
        )
        mount_snapshot = {
            "mount_id": "mount-main",
            "revision": 3,
            "connection_id": "ol_main",
            "connection_revision": 2,
            "namespace_id": "openlist:ol_main:mount-main",
            "enabled": True,
            "identity_status": "declared",
            "storage_identity_id": "storage:main",
            "identity_root": "/",
            "webdav_root": "/影视",
            "readable": True,
            "writable": True,
        }
        connection_snapshot = {
            "connection_id": "ol_main",
            "revision": 2,
            "enabled": True,
            "credential_configured": True,
        }

        with self.assertRaisesRegex(
            OpenListValidationError,
            "当前托管 rclone Remote 身份不一致",
        ):
            resolver.resolve_frozen_remote(
                namespace_id="openlist:ol_main:mount-main",
                mount_id="mount-main",
                mount_revision=3,
                connection_id="ol_main",
                connection_revision=2,
                storage_identity_id="storage:main",
                identity_root="/",
                path="/电影/job-42",
                role="source",
                mount_snapshot=mount_snapshot,
                connection_snapshot=connection_snapshot,
            )


class OpenListBlueprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _new_database(self)
        self.connection_service = OpenListConnectionService(
            self.database,
            master_key_provider=lambda: MASTER_KEY_HEX,
            probe_runner=lambda _ctx: {"ok": True, "message": "ok"},
        )
        self.mount_service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: FakeWebDAV(),
        )
        blueprint = create_openlist_blueprint(
            OpenListRouteContext(
                admin_required=lambda view: view,
                handlers=default_handlers(self.connection_service, self.mount_service),
            )
        )
        application = Flask(__name__)
        application.register_blueprint(blueprint)
        self.client = application.test_client()

    def test_connection_api_never_returns_password(self) -> None:
        response = self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = json.dumps(response.get_json())
        self.assertNotIn("password", payload)
        self.assertNotIn("password_ciphertext", payload)

        response = self.client.get("/api/admin/openlist/connections")
        self.assertEqual(response.status_code, 200)
        payload = json.dumps(response.get_json())
        self.assertNotIn("password", payload)
        self.assertNotIn("password_ciphertext", payload)

    def test_managed_connection_writes_use_storage_setup_callback(self) -> None:
        calls: list[dict[str, Any]] = []

        def managed_save(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(dict(payload))
            return {
                "success": True,
                "item": {
                    "connection_id": str(payload.get("connection_id") or "openlist_main"),
                    "revision": len(calls),
                },
                "rclone": {"connection_status": "success"},
            }

        application = Flask("managed-openlist-writes")
        application.register_blueprint(
            create_openlist_blueprint(
                OpenListRouteContext(
                    admin_required=lambda view: view,
                    handlers=default_handlers(
                        self.connection_service,
                        self.mount_service,
                        connection_save=managed_save,
                    ),
                )
            )
        )
        client = application.test_client()

        created = client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "managed",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "server-only",
            },
        )
        updated = client.patch(
            "/api/admin/openlist/connections/managed",
            json={"name": "改名", "expected_revision": 1},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(calls[0]["connection_id"], "managed")
        self.assertEqual(calls[1]["connection_id"], "managed")
        self.assertEqual(
            updated.get_json()["rclone"]["connection_status"],
            "success",
        )

    def test_connection_test_route(self) -> None:
        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        response = self.client.post("/api/admin/openlist/connections/ol_main/test")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["last_test_status"], "ok")
        self.assertEqual(body["revision"], 1)

    def test_invalid_json_and_revision_return_structured_errors(self) -> None:
        invalid_json = self.client.post(
            "/api/admin/openlist/connections",
            data="not-json",
            content_type="text/plain",
            headers={"X-Request-ID": "openlist-trace"},
        )
        self.assertEqual(invalid_json.status_code, 400)
        invalid_body = invalid_json.get_json()
        self.assertEqual(invalid_body["code"], "INVALID_JSON")
        self.assertEqual(invalid_body["trace_id"], "openlist-trace")
        self.assertFalse(invalid_body["retryable"])

        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol-main-revision",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        invalid_revision = self.client.patch(
            "/api/admin/openlist/connections/ol-main-revision",
            json={"name": "x", "expected_revision": "bad"},
        )
        self.assertEqual(invalid_revision.status_code, 400)
        invalid_revision_body = invalid_revision.get_json()
        self.assertEqual(invalid_revision_body["code"], "INVALID_REVISION")
        self.assertEqual(
            invalid_revision_body["field_errors"],
            {"expected_revision": "必须是正整数"},
        )

    def test_confirm_identity_requires_expected_revisions(self) -> None:
        response = self.client.post(
            "/api/admin/openlist/mounts/any/confirm-identity",
            json={},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "INVALID_REVISION")
        self.assertIn("expected_revision", response.get_json()["field_errors"])

    def test_mount_confirm_identity_uses_revision_cas(self) -> None:
        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        self.client.post(
            "/api/admin/openlist/mounts",
            json={"connection_id": "ol_main", "mount_id": "m-cas", "webdav_root": "/影视", "storage_identity_id": "storage:m-cas"},
        )
        probe = self.client.post("/api/admin/openlist/mounts/m-cas/test")
        self.assertEqual(probe.status_code, 200)
        stale_revision = probe.get_json()["revision"]
        self.mount_service.update_mount(
            "m-cas", {"name": "并发改名", "expected_revision": stale_revision}
        )
        response = self.client.post(
            "/api/admin/openlist/mounts/m-cas/confirm-identity",
            json={
                "expected_revision": stale_revision,
                "expected_connection_revision": probe.get_json()["connection_revision"],
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "MOUNT_CONFLICT")

    def test_mount_confirm_identity_accepts_matching_revisions(self) -> None:
        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        self.client.post(
            "/api/admin/openlist/mounts",
            json={"connection_id": "ol_main", "mount_id": "m-cas-ok", "webdav_root": "/影视", "storage_identity_id": "storage:m-cas-ok"},
        )
        probe = self.client.post("/api/admin/openlist/mounts/m-cas-ok/test")
        probe_body = probe.get_json()
        response = self.client.post(
            "/api/admin/openlist/mounts/m-cas-ok/confirm-identity",
            json={
                "expected_revision": probe_body["revision"],
                "expected_connection_revision": probe_body["connection_revision"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["item"]["identity_status"], "declared")

    def test_mount_api_flow(self) -> None:
        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        response = self.client.post(
            "/api/admin/openlist/mounts",
            json={"connection_id": "ol_main", "mount_id": "m1", "webdav_root": "/影视", "storage_identity_id": "storage:m1"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["item"]["namespace_id"], "openlist:ol_main:m1")

        response = self.client.post("/api/admin/openlist/mounts/m1/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["readable"])
        self.assertTrue(response.get_json()["writable"])

        response = self.client.get("/api/admin/openlist/mounts/dirs?connection_id=ol_main")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [])

        response = self.client.delete("/api/admin/openlist/mounts/m1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deleted"])

    def test_mount_dirs_api_supports_path_cascade(self) -> None:
        self.client.post(
            "/api/admin/openlist/connections",
            json={
                "connection_id": "ol_main",
                "base_url": "http://127.0.0.1:5244/dav",
                "username": "admin",
                "password": "s3cret",
            },
        )
        fake = FakeWebDAV()
        fake.dirs.update({"/影视", "/影视/电影"})
        self.mount_service.webdav_factory = lambda _creds: fake

        response = self.client.get("/api/admin/openlist/mounts/dirs?connection_id=ol_main")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["path"] for item in response.get_json()["items"]], ["/影视"])

        response = self.client.get(
            "/api/admin/openlist/mounts/dirs",
            query_string={"connection_id": "ol_main", "path": "/影视"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["path"] for item in response.get_json()["items"]], ["/影视/电影"])

        # 越级路径必须被规范化层拒绝，不能借目录树逃出该连接的 WebDAV 视图
        response = self.client.get(
            "/api/admin/openlist/mounts/dirs",
            query_string={"connection_id": "ol_main", "path": "/影视/../../etc"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.get_json()["success"])

    def test_not_found_returns_404(self) -> None:
        response = self.client.patch("/api/admin/openlist/connections/missing", json={"name": "x"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main()
