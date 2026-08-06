"""OpenList 连接与挂载（connection/mount）模块测试。

使用真实 ``Database`` 临时文件 + 注入的 fake probe runner / webdav factory，
不发真实网络请求。运行：``python -m unittest tests.test_openlist_v1``
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from flask import Flask

from fnos_media_import.database import Database
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
)
from fnos_media_import.openlist.locator_resolver import RcloneLocatorResolver
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.time_utils import utc_now_iso

MASTER_KEY_HEX = "0123456789abcdef0123456789abcdef"  # 16 字节


def _relative_path(root: str, target: str) -> str | None:
    """target 相对 root 的剩余路径；不在其下则返回 None。"""
    root = root.rstrip("/") or "/"
    target = target.rstrip("/") or "/"
    if root == "/":
        return target.lstrip("/") or None
    if target.startswith(root + "/"):
        return target[len(root) + 1:]
    return None


class FakeWebDAV:
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


def _new_database(testcase: unittest.TestCase) -> Database:
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    database = Database(Path(temporary.name) / "openlist-test.db")
    database.init_schema()
    return database


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

        # 直接查库：密文是 BLOB 且不等于明文，key_id 固定为 ol:v1
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
        self.assertIn("FNOS_ACCOUNT_MASTER_KEY", str(caught.exception))

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
            master_key_provider=lambda: "ffffffffffffffffffffffffffffffff",
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
            {"connection_id": "ol_main", "webdav_root": "/电影"}
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
        self.assertEqual(result["identity_status"], "declared")

        stored = self.mount_service.get_mount("m1")
        self.assertTrue(stored["readable"])
        self.assertTrue(stored["writable"])
        self.assertEqual(stored["identity_status"], "declared")
        self.assertIsNotNone(stored["last_tested_at"])

        # 探测临时目录只落在 /_fnos_probe，全部清理，从未污染 mount 根
        self.assertEqual(fake.dirs, set())
        self.assertEqual(fake.files, {})
        self.assertFalse(any(path.startswith("/影视") for path in fake.dirs))
        self.assertFalse(any(path.startswith("/影视") for path in fake.files))

    def test_probe_cleanup_failure_reports_residue_and_marks_unwritable(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m2", "webdav_root": "/影视"})
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
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m3", "webdav_root": "/影视"})
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
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m6", "webdav_root": "/影视"})
        fake = FakeWebDAV(put_fail=True)
        service = OpenListMountService(
            self.database,
            connection_service=self.connection_service,
            webdav_factory=lambda _creds: fake,
        )
        result = service.probe_mount("m6")
        self.assertFalse(result["success"])
        self.assertFalse(result["writable"])
        # mkcol 成功但写入失败；清理步骤仍应删除残留目录，不留下“需要清理”告警
        self.assertEqual(fake.dirs, set())
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

    def test_mount_delete(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m4", "webdav_root": "/影视"})
        result = self.mount_service.delete_mount("m4")
        self.assertTrue(result["deleted"])
        self.assertIsNone(self.mount_service.get_mount("m4"))
        with self.assertRaises(OpenListNotFoundError):
            self.mount_service.delete_mount("m4")

    def test_connection_delete_blocked_by_mounts(self) -> None:
        self.mount_service.create_mount({"connection_id": "ol_main", "mount_id": "m5", "webdav_root": "/影视"})
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
            json={"connection_id": "ol_main", "mount_id": "m1", "webdav_root": "/影视"},
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

    def test_not_found_returns_404(self) -> None:
        response = self.client.patch("/api/admin/openlist/connections/missing", json={"name": "x"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main()
