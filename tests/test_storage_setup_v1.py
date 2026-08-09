from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from flask import Flask

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.database import Database
from fnos_media_import.domain.models import AccountTestResult
from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.storage.assignment_service import AssignmentConflictError, SourceAssignmentService
from fnos_media_import.storage.setup_blueprint import StorageSetupBlueprintContext, create_storage_setup_blueprint
from fnos_media_import.storage.setup_service import StorageSetupError, StorageSetupService
from fnos_media_import.storage.target_service import StorageTargetService


KEY = bytes.fromhex("31" * 32)
WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "storage-setup-v1-tests"


class MemoryWebDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs = {"/夸克", "/六盘"}

    def auth_probe(self) -> None:
        return None

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        root = path.rstrip("/") or "/"
        items = []
        for directory in sorted(self.dirs):
            parent, _, name = directory.rpartition("/")
            if (parent or "/") == root:
                items.append({"path": directory, "name": name, "is_dir": True})
        for file_path, body in self.files.items():
            parent, _, name = file_path.rpartition("/")
            if (parent or "/") == root:
                items.append({"path": file_path, "name": name, "is_dir": False, "size": len(body)})
        return items

    def mkcol(self, path: str) -> None:
        self.dirs.add(path.rstrip("/"))

    def put_bytes(self, path: str, content: bytes) -> None:
        self.files[path] = content

    def read_bytes(self, path: str) -> bytes:
        return self.files[path]

    def file_size(self, path: str) -> int:
        return len(self.files[path])

    def move(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.dirs.discard(path.rstrip("/"))

    def exists(self, path: str) -> bool:
        return path in self.files or path.rstrip("/") in self.dirs


class StorageSetupV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.db_path = WORK_TEST_ROOT / f"setup-{uuid.uuid4().hex}.db"
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.addCleanup(self._cleanup_database)
        self.webdav = MemoryWebDAV()
        self.accounts = ProviderAccountService(
            self.db,
            lambda: KEY,
            tester=lambda *_args: AccountTestResult(True, "user***1", "账号检测通过"),
        )
        self.connections = OpenListConnectionService(
            self.db,
            lambda: KEY,
            probe_runner=lambda _context: {"ok": True, "message": "连接成功"},
        )
        self.mounts = OpenListMountService(
            self.db,
            connection_service=self.connections,
            webdav_factory=lambda _credentials: self.webdav,
        )
        self.targets = StorageTargetService(self.db)
        self.assignments = SourceAssignmentService(self.db, self.targets)
        self.service = StorageSetupService(
            accounts_service=self.accounts,
            connection_service=self.connections,
            mount_service=self.mounts,
            target_service=self.targets,
            assignment_service=self.assignments,
        )

    def _cleanup_database(self) -> None:
        try:
            self.db.close_all_connections()
        except AttributeError:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.db_path}{suffix}").unlink()
            except FileNotFoundError:
                pass
        try:
            if WORK_TEST_ROOT.exists() and not any(WORK_TEST_ROOT.iterdir()):
                shutil.rmtree(WORK_TEST_ROOT)
        except OSError:
            pass

    def _account(self, provider: str, name: str) -> str:
        if provider == "quark":
            payload = {
                "provider_kind": provider,
                "name": name,
                "config": {},
                "credentials": {"cookie": "sid=secret-cookie"},
            }
        elif provider == "sixpan":
            payload = {
                "provider_kind": provider,
                "name": name,
                "config": {"client_id": "client"},
                "credentials": {"client_secret": "secret", "refresh_token": "refresh"},
            }
        elif provider == "mobile":
            payload = {
                "provider_kind": provider,
                "name": name,
                "config": {"phone": "13800000000"},
                "credentials": {"authorization": "Basic token"},
            }
        elif provider == "tianyi":
            payload = {
                "provider_kind": provider,
                "name": name,
                "config": {"identity": "user"},
                "credentials": {"cookie": "COOKIE=value"},
            }
        else:
            raise AssertionError(provider)
        account = self.accounts.create_account(
            provider_kind=provider,
            name=name,
            config=payload["config"],
            credentials=payload["credentials"],
        )
        self.accounts.test_account(account.account_id)
        current = self.accounts.get_selection(provider)
        self.accounts.activate_account(
            "default",
            account.account_id,
            current.selection_revision if current else None,
        )
        return account.account_id

    def test_empty_snapshot_exposes_product_steps_not_internal_objects(self) -> None:
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["progress"]["completed"], 0)
        self.assertFalse(snapshot["progress"]["ready"])
        self.assertEqual(
            [step["key"] for step in snapshot["progress"]["steps"]],
            ["accounts", "openlist", "locations", "routes"],
        )
        self.assertEqual(snapshot["locations"], [])

    def test_complete_setup_creates_mount_target_binding_and_routes(self) -> None:
        quark = self._account("quark", "夸克主账号")
        sixpan = self._account("sixpan", "六盘主账号")
        self._account("mobile", "移动云主账号")
        self._account("tianyi", "天翼云主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        quark_location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        sixpan_location = self.service.save_location(
            {
                "name": "六盘主盘",
                "kind": "provider",
                "account_id": sixpan,
                "connection_id": "openlist_main",
                "webdav_root": "/六盘",
                "native_root": "/",
                "confirm_identity": True,
            }
        )
        routes = [
            {"search_source_id": source, "default_destination_id": quark_location["destination_id"]}
            for source in ("mobile", "quark", "tianyi")
        ]
        routes.append(
            {"search_source_id": "sixpan_magnet", "default_destination_id": sixpan_location["destination_id"]}
        )
        self.service.save_routes({"routes": routes})

        snapshot = self.service.snapshot()
        self.assertEqual(len(snapshot["locations"]), 2)
        self.assertTrue(all(location["ready"] for location in snapshot["locations"]))
        self.assertTrue(snapshot["progress"]["ready"])
        self.assertTrue(
            all(
                isinstance(revision, int) and revision > 0
                for route in snapshot["routes"]
                for revision in route["assignment_revisions"].values()
            )
        )
        stale_routes = [
            {
                "search_source_id": route["search_source_id"],
                "default_destination_id": route["default_destination_id"],
                "overrides": dict(route["overrides"]),
                "assignment_revisions": dict(route["assignment_revisions"]),
            }
            for route in snapshot["routes"]
        ]
        quark_movie = self.assignments.get_assignment("quark", "movie")
        self.assignments.upsert(
            "quark",
            "movie",
            sixpan_location["destination_id"],
            expected_revision=quark_movie.revision,
        )
        with self.assertRaises(AssignmentConflictError):
            self.service.save_routes({"routes": stale_routes})
        target = self.targets.get_target(quark_location["destination_id"])
        self.assertEqual(target.categories["movie"].native_locator.value, "媒体库/电影")
        self.assertEqual(target.categories["tv"].rclone_locator.relative_path, "电视剧")

        extra = self.service.save_openlist(
            {
                "name": "未使用的备用连接",
                "base_url": "http://unused-openlist:5244/dav",
                "username": "admin",
                "password": "password",
            }
        )["item"]
        self.connections.probe_runner = lambda context: {
            "ok": context["connection_id"] != extra["connection_id"],
            "message": "连接成功" if context["connection_id"] != extra["connection_id"] else "连接失败",
        }
        preflight = self.service.preflight()
        self.assertTrue(preflight["ok"], preflight)
        self.assertFalse(
            any("未使用的备用连接" in item["name"] for item in preflight["checks"])
        )
        with self.db.connect() as connection:
            revisions_after_first = {
                str(row["mount_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT mount_id, revision FROM openlist_mounts"
                ).fetchall()
            }
            history_after_first = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions"
            ).fetchone()[0]

        repeated_preflight = self.service.preflight()
        self.assertTrue(repeated_preflight["ok"], repeated_preflight)
        with self.db.connect() as connection:
            revisions_after_second = {
                str(row["mount_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT mount_id, revision FROM openlist_mounts"
                ).fetchall()
            }
            history_after_second = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions"
            ).fetchone()[0]
        self.assertEqual(revisions_after_second, revisions_after_first)
        self.assertEqual(history_after_second, history_after_first)

    def test_location_rejects_removed_category_path_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "分类目录由系统统一生成"):
            self.service.save_location(
                {
                    "name": "旧配置",
                    "kind": "local",
                    "category_paths": {"movie": "自定义电影"},
                }
            )

    def test_location_requires_explicit_storage_identity_confirmation(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        with self.assertRaisesRegex(ValueError, "确认 OpenList 目录") as raised:
            self.service.save_location(
                {
                    "name": "夸克主盘",
                    "kind": "provider",
                    "account_id": quark,
                    "connection_id": "openlist_main",
                    "webdav_root": "/夸克",
                    "native_root": "/媒体库",
                }
            )

        self.assertEqual(raised.exception.code, "IDENTITY_CONFIRMATION_REQUIRED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

    def test_multiple_openlist_connections_do_not_overwrite_the_first(self) -> None:
        first = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist-a:5244/dav",
                "username": "admin-a",
                "password": "password-a",
            }
        )["item"]
        second = self.service.save_openlist(
            {
                "name": "备用 OpenList",
                "base_url": "http://openlist-b:5244/dav",
                "username": "admin-b",
                "password": "password-b",
            }
        )["item"]

        self.assertEqual(first["connection_id"], "openlist_main")
        self.assertNotEqual(second["connection_id"], first["connection_id"])
        self.assertEqual(
            self.connections.get_connection(first["connection_id"])["base_url"],
            "http://openlist-a:5244/dav",
        )

    def test_failed_target_revision_restores_existing_mount(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        original = self.mounts.get_mount(mount_id)
        self.assertGreater(int(original.get("connection_revision") or 0), 0)
        revise_target = self.targets.revise_target
        self.targets.revise_target = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("target write failed"))
        self.addCleanup(setattr, self.targets, "revise_target", revise_target)

        with self.assertRaisesRegex(RuntimeError, "target write failed"):
            self.service.save_location(
                {
                    "name": "夸克主盘",
                    "kind": "provider",
                    "account_id": quark,
                    "connection_id": "openlist_main",
                    "webdav_root": "/夸克-新目录",
                    "native_root": "/媒体库",
                    "confirm_identity": True,
                },
                location["destination_id"],
            )

        restored = self.mounts.get_mount(mount_id)
        self.assertEqual(restored["webdav_root"], original["webdav_root"])
        self.assertEqual(restored["identity_status"], original["identity_status"])
        self.assertEqual(restored["readable"], original["readable"])
        self.assertEqual(restored["writable"], original["writable"])
        self.assertEqual(restored["last_tested_at"], original["last_tested_at"])
        self.assertEqual(restored["connection_revision"], original["connection_revision"])

    def test_mount_compensation_refuses_to_overwrite_concurrent_revision(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        original = self.mounts.get_mount(mount_id)
        changed = self.mounts.update_mount(mount_id, {"name": "本次保存修改"})
        concurrent = self.mounts.update_mount(mount_id, {"name": "并发管理员修改"})

        self.service._restore_mount(
            original,
            None,
            expected_revision=changed["revision"],
        )

        current = self.mounts.get_mount(mount_id)
        self.assertEqual(current["name"], "并发管理员修改")
        self.assertEqual(current["revision"], concurrent["revision"])

    def test_mount_probe_exception_compensates_existing_mount(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        original = self.mounts.get_mount(mount_id)
        probe = self.mounts.probe_mount
        self.mounts.probe_mount = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("probe crashed")
        )
        self.addCleanup(setattr, self.mounts, "probe_mount", probe)
        with self.assertRaisesRegex(RuntimeError, "probe crashed"):
            self.service.save_location(
                {
                    "name": "夸克主盘",
                    "kind": "provider",
                    "account_id": quark,
                    "connection_id": "openlist_main",
                    "webdav_root": "/夸克-新目录",
                    "native_root": "/媒体库",
                    "confirm_identity": True,
                },
                location["destination_id"],
            )
        restored = self.mounts.get_mount(mount_id)
        self.assertEqual(restored["webdav_root"], original["webdav_root"])
        self.assertEqual(restored["identity_status"], original["identity_status"])

    def test_stale_location_revision_fails_before_mount_edit(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        self.targets.revise_target(
            target.destination_id,
            name="并发改名",
            expected_revision=target.current_revision,
        )
        before_mount = self.mounts.get_mount(mount_id)
        with self.assertRaisesRegex(StorageSetupError, "已被其他管理员修改"):
            self.service.save_location(
                {
                    "name": "旧页面名称",
                    "kind": "provider",
                    "account_id": quark,
                    "connection_id": "openlist_main",
                    "webdav_root": "/夸克-旧页面",
                    "native_root": "/媒体库",
                    "confirm_identity": True,
                    "expected_revision": target.current_revision,
                },
                target.destination_id,
            )
        after_mount = self.mounts.get_mount(mount_id)
        self.assertEqual(after_mount["webdav_root"], before_mount["webdav_root"])

    def test_repeated_location_save_does_not_reprobe_unchanged_provider_mount(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        before = self.mounts.get_mount(mount_id)
        self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
                "expected_revision": target.current_revision,
            },
            location["destination_id"],
        )
        after = self.mounts.get_mount(mount_id)
        self.assertEqual(after["revision"], before["revision"])

    def test_openlist_create_probe_failure_leaves_no_connection(self) -> None:
        self.connections.probe_runner = lambda _context: {
            "ok": False,
            "message": "WebDAV 不可达",
        }

        with self.assertRaisesRegex(StorageSetupError, "WebDAV 不可达"):
            self.service.save_openlist(
                {
                    "name": "失败连接",
                    "base_url": "http://broken-openlist:5244/dav",
                    "username": "admin",
                    "password": "new-secret",
                }
            )

        self.assertEqual(self.connections.list_connections(), [])

    def test_openlist_update_probe_failure_restores_previous_credentials(self) -> None:
        original = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist-old:5244/dav",
                "username": "old-user",
                "password": "old-secret",
            }
        )["item"]
        self.connections.probe_runner = lambda _context: {
            "ok": False,
            "message": "新连接检测失败",
        }

        with self.assertRaisesRegex(StorageSetupError, "新连接检测失败"):
            self.service.save_openlist(
                {
                    "connection_id": original["connection_id"],
                    "expected_revision": original["revision"],
                    "name": "错误的新名称",
                    "base_url": "http://openlist-new:5244/dav",
                    "username": "new-user",
                    "password": "new-secret",
                }
            )

        restored = self.connections.get_connection(original["connection_id"])
        self.assertEqual(restored["name"], "主 OpenList")
        self.assertEqual(restored["base_url"], "http://openlist-old:5244/dav")
        self.assertEqual(restored["username"], "old-user")
        credentials = self.connections.resolve_credentials(
            original["connection_id"], restored["revision"]
        )
        self.assertEqual(credentials["password"], "old-secret")

    def test_delete_location_removes_target_binding_and_private_mount(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)

        result = self.service.delete_location(location["destination_id"])

        self.assertTrue(result["deleted"])
        self.assertEqual(result["mount_id"], mount_id)
        self.assertIsNone(self.targets.get_target(location["destination_id"]))
        self.assertIsNone(self.mounts.get_mount(mount_id))
        with self.db.connect() as connection:
            bindings = connection.execute(
                "SELECT COUNT(*) FROM storage_target_account_bindings WHERE destination_id=?",
                (location["destination_id"],),
            ).fetchone()[0]
        self.assertEqual(bindings, 0)

    def test_delete_location_shared_mount_conflict_rolls_back_everything(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        self.targets.create_target(
            destination_id="shared_mount_target",
            name="共享挂载目标",
            provider_kind="quark",
            target_kind="provider",
            account_scope="default",
            categories=target.categories,
            account_id=quark,
        )

        with self.assertRaises(StorageSetupError) as raised:
            self.service.delete_location(location["destination_id"])

        self.assertEqual(raised.exception.code, "LOCATION_IN_USE")
        self.assertIsNotNone(self.targets.get_target(location["destination_id"]))
        self.assertIsNotNone(self.targets.get_target("shared_mount_target"))
        self.assertIsNotNone(self.mounts.get_mount(mount_id))

    def test_preflight_does_not_persist_account_connection_or_mount_probe_state(self) -> None:
        account_id = self._account("quark", "Quark")
        self.service.save_openlist(
            {
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "password",
            }
        )
        location = self.service.save_location(
            {
                "name": "Quark target",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/media",
                "native_root": "/library",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)

        original_sources = self.assignments.sources
        original_matrix = self.assignments.matrix
        self.addCleanup(setattr, self.assignments, "sources", original_sources)
        self.addCleanup(setattr, self.assignments, "matrix", original_matrix)
        self.assignments.sources = lambda: {
            "quark": {
                "search_source_id": "quark",
                "provider_kind": "quark",
                "label": "Quark",
                "source_form": "share",
            }
        }
        self.assignments.matrix = lambda: {
            "rows": [
                {
                    "search_source_id": "quark",
                    "provider_kind": "quark",
                    "label": "Quark",
                    "assignments": [
                        {
                            "category_key": "movie",
                            "destination_id": location["destination_id"],
                            "ready": True,
                        }
                    ],
                }
            ]
        }

        with self.db.connect() as connection:
            connection.execute(
                "UPDATE provider_accounts SET last_test_message=?, last_tested_at=? "
                "WHERE account_id=?",
                ("account-before", "account-before", account_id),
            )
            connection.execute(
                "UPDATE openlist_connections SET last_test_status=?, last_tested_at=? "
                "WHERE connection_id=?",
                ("connection-before", "connection-before", "openlist_main"),
            )
            connection.execute(
                "UPDATE openlist_mounts SET readable=0, writable=0, last_tested_at=? "
                "WHERE mount_id=?",
                ("mount-before", mount_id),
            )
            account_before = tuple(connection.execute(
                "SELECT current_revision, last_test_status, last_test_message, last_tested_at, identity_hint "
                "FROM provider_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone())
            connection_before = tuple(connection.execute(
                "SELECT revision, last_test_status, last_tested_at FROM openlist_connections "
                "WHERE connection_id=?",
                ("openlist_main",),
            ).fetchone())
            mount_before = tuple(connection.execute(
                "SELECT revision, readable, writable, last_tested_at, identity_status "
                "FROM openlist_mounts WHERE mount_id=?",
                (mount_id,),
            ).fetchone())
            history_before = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                (mount_id,),
            ).fetchone()[0]

        result = self.service.preflight()

        with self.db.connect() as connection:
            account_after = tuple(connection.execute(
                "SELECT current_revision, last_test_status, last_test_message, last_tested_at, identity_hint "
                "FROM provider_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone())
            connection_after = tuple(connection.execute(
                "SELECT revision, last_test_status, last_tested_at FROM openlist_connections "
                "WHERE connection_id=?",
                ("openlist_main",),
            ).fetchone())
            mount_after = tuple(connection.execute(
                "SELECT revision, readable, writable, last_tested_at, identity_status "
                "FROM openlist_mounts WHERE mount_id=?",
                (mount_id,),
            ).fetchone())
            history_after = connection.execute(
                "SELECT COUNT(*) FROM openlist_mount_revisions WHERE mount_id=?",
                (mount_id,),
            ).fetchone()[0]

        directory_check = next(
            item for item in result["checks"] if item["name"].startswith("目录 ")
        )
        self.assertTrue(directory_check["ok"], directory_check)
        self.assertEqual(account_after, account_before)
        self.assertEqual(connection_after, connection_before)
        self.assertEqual(mount_after, mount_before)
        self.assertEqual(history_after, history_before)

    def test_preflight_calls_mount_probe_in_read_only_mode(self) -> None:
        account_id = self._account("quark", "Quark")
        self.service.save_openlist(
            {
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "password",
            }
        )
        location = self.service.save_location(
            {
                "name": "Quark target",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/media",
                "native_root": "/library",
                "confirm_identity": True,
            }
        )
        original_sources = self.assignments.sources
        original_matrix = self.assignments.matrix
        original_probe = self.mounts.probe_mount
        self.addCleanup(setattr, self.assignments, "sources", original_sources)
        self.addCleanup(setattr, self.assignments, "matrix", original_matrix)
        self.addCleanup(setattr, self.mounts, "probe_mount", original_probe)
        self.assignments.sources = lambda: {
            "quark": {
                "search_source_id": "quark",
                "provider_kind": "quark",
                "label": "Quark",
                "source_form": "share",
            }
        }
        self.assignments.matrix = lambda: {
            "rows": [
                {
                    "search_source_id": "quark",
                    "provider_kind": "quark",
                    "label": "Quark",
                    "assignments": [
                        {
                            "category_key": "movie",
                            "destination_id": location["destination_id"],
                            "ready": True,
                        }
                    ],
                }
            ]
        }
        calls: list[dict[str, Any]] = []

        def probe_read_only(mount_id: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return original_probe(mount_id, **kwargs)

        self.mounts.probe_mount = probe_read_only
        result = self.service.preflight()
        directory_check = next(
            item for item in result["checks"] if item["name"].startswith("目录 ")
        )
        self.assertTrue(directory_check["ok"], directory_check)
        self.assertTrue(calls)
        self.assertFalse(calls[0]["persist_result"])

    def test_blueprint_is_admin_protected(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(
                    admin_required=lambda _handler: (lambda **_kwargs: ({"success": False}, 401)),
                    service=self.service,
                )
            )
        )
        self.assertEqual(app.test_client().get("/api/admin/storage-setup").status_code, 401)

    def test_blueprint_returns_structured_error_envelope(self) -> None:
        app = Flask(__name__ + "_errors")
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(admin_required=lambda view: view, service=self.service)
            )
        )
        client = app.test_client()

        invalid_json = client.post(
            "/api/admin/storage-setup/locations",
            data="not-json",
            content_type="text/plain",
            headers={"X-Request-ID": "trace-test"},
        )
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(invalid_json.get_json()["code"], "INVALID_JSON")
        self.assertEqual(invalid_json.get_json()["trace_id"], "trace-test")

        missing_name = client.post(
            "/api/admin/storage-setup/locations",
            json={"kind": "local"},
        )
        self.assertEqual(missing_name.status_code, 422)
        self.assertEqual(missing_name.get_json()["field_errors"], {"name": "必填"})
        self.assertIn("trace_id", missing_name.get_json())

        original_save_routes = self.service.save_routes
        self.service.save_routes = lambda _payload: (_ for _ in ()).throw(
            AssignmentConflictError("assignment revision conflict")
        )
        self.addCleanup(setattr, self.service, "save_routes", original_save_routes)
        conflict = client.put(
            "/api/admin/storage-setup/routes",
            json={"routes": []},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "ASSIGNMENT_CONFLICT")

    def test_provider_runtime_second_configuration_source_is_removed(self) -> None:
        snapshot = self.service.snapshot()
        for provider in snapshot["providers"]:
            self.assertNotIn("runtime_config", provider)
            self.assertNotIn("runtime_ready", provider)
            self.assertNotIn("runtime_message", provider)
        app = Flask(__name__ + "_runtime")
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(admin_required=lambda view: view, service=self.service)
            )
        )
        response = app.test_client().put(
            "/api/admin/storage-setup/provider-runtime/mobile",
            json={"phone": "13900000000", "access_token": "secret-token"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
