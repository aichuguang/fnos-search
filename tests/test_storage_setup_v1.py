from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from flask import Flask

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.config import CATEGORY_KEYS
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.domain.models import AccountTestResult
from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.storage.assignment_service import AssignmentConflictError, SourceAssignmentService
from fnos_media_import.storage.setup_blueprint import StorageSetupBlueprintContext, create_storage_setup_blueprint
from fnos_media_import.services.rclone_webdav_config_service import (
    RCLONE_UNAVAILABLE,
    RcloneWebdavConfigError,
)
from fnos_media_import.storage import setup_service as setup_service_module
from fnos_media_import.storage.setup_service import StorageSetupError, StorageSetupService
from fnos_media_import.storage.target_service import StorageTargetService


KEY = bytes.fromhex("31" * 32)
WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "storage-setup-v1-tests"


class MemoryWebDAV(RepositoryFakeMixin):
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


class FakeProviderUploader(RepositoryFakeMixin):
    def __init__(
        self,
        webdav: MemoryWebDAV,
        *,
        provider_kind: str,
        display_name: str,
        webdav_root: str,
        root_folder_id: str = "-11",
    ) -> None:
        self.webdav = webdav
        self.provider_kind = provider_kind
        self.display_name = display_name
        self.webdav_root = webdav_root
        self.root_folder_id = str(root_folder_id or "-11")
        self.upload_ok = True
        self.organizer_ok = True
        self.probe_calls: list[dict[str, Any]] = []
        self.folder_calls: list[str] = []
        self.folders = {
            self.root_folder_id: [
                {"folder_id": "root-folder-id", "name": "媒体库", "parent_folder_id": self.root_folder_id},
            ],
            "root-folder-id": [
                {"folder_id": "movie-folder-id", "name": "电影", "parent_folder_id": "root-folder-id"},
            ],
        }

    def list_folders(self, parent_folder_id: str = "-11") -> list[dict[str, Any]]:
        parent = str(parent_folder_id or self.root_folder_id)
        self.folder_calls.append(parent)
        return [dict(item) for item in self.folders.get(parent, [])]

    def probe_target(self, *, target_locator: dict[str, Any], observer: Any = None) -> dict[str, Any]:
        self.probe_calls.append(dict(target_locator or {}))
        if not self.upload_ok:
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "PROVIDER_UPLOAD_PROBE_FAILED",
                "message": "native upload failed",
            }
        if not self.organizer_ok:
            return {
                "ok": False,
                "upload_verified": True,
                "organizer_mutation_verified": False,
                "code": "OPENLIST_MUTATION_PROBE_FAILED",
                "message": "organizer mutation failed",
            }
        file_name = "fnos-native-probe.txt"
        content = b"provider-native-probe"
        self.webdav.files[f"{self.webdav_root}/{file_name}"] = content
        observed = observer(
            {
                "file_id": "probe-file-id",
                "file_name": file_name,
                "file_size": len(content),
                "content": content,
            }
        ) if callable(observer) else {}
        observed = dict(observed or {}) if isinstance(observed, dict) else {}
        return {
            "ok": bool(observed.get("organizer_mutation_verified", not callable(observer))),
            "upload_verified": True,
            "organizer_mutation_verified": bool(
                observed.get("organizer_mutation_verified", not callable(observer))
            ),
            "message": observed.get("message") or "native upload probe passed",
        }


class StorageSetupV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.db_path = WORK_TEST_ROOT / f"setup-{uuid.uuid4().hex}.db"
        self._db_infrastructure = Database(self.db_path)
        self._db_infrastructure.init_schema()
        self.db = RepositorySet.from_database(self._db_infrastructure)
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
        self.rclone_sync_calls: list[dict[str, Any]] = []
        self.rclone_state: dict[str, Any] = {}
        self.rclone_sync_error: Exception | None = None
        self.service = StorageSetupService(
            accounts_service=self.accounts,
            connection_service=self.connections,
            mount_service=self.mounts,
            target_service=self.targets,
            assignment_service=self.assignments,
            connection_factory=self.db.connect,
            rclone_webdav_sync=self._sync_rclone_webdav,
            rclone_webdav_test=self._test_rclone_webdav,
            organizer_enabled=lambda: False,
        )
        self.mobile_uploader = FakeProviderUploader(
            self.webdav,
            provider_kind="mobile",
            display_name="移动云盘",
            webdav_root="/移动",
            root_folder_id="/",
        )
        self.quark_uploader = FakeProviderUploader(
            self.webdav,
            provider_kind="quark",
            display_name="夸克网盘",
            webdav_root="/夸克",
            root_folder_id="0",
        )
        self.tianyi_uploader = FakeProviderUploader(
            self.webdav,
            provider_kind="tianyi",
            display_name="天翼云盘",
            webdav_root="/天翼",
        )
        self.service.set_provider_uploader_resolver(
            lambda provider_kind, _account_id, _revision: (
                {
                    "mobile": self.mobile_uploader,
                    "quark": self.quark_uploader,
                    "tianyi": self.tianyi_uploader,
                }.get(provider_kind)
            )
        )
        self.service.set_provider_folder_resolver(
            lambda provider_kind, _account_id, _revision: (
                {
                    "mobile": self.mobile_uploader,
                    "quark": self.quark_uploader,
                    "tianyi": self.tianyi_uploader,
                }.get(provider_kind)
            )
        )
        self.mounts.set_relay_upload_capability_resolver(
            lambda provider_kind: provider_kind in {"mobile", "tianyi"}
        )

    def _sync_rclone_webdav(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.rclone_sync_calls.append(dict(payload))
        if self.rclone_sync_error is not None:
            raise self.rclone_sync_error
        self.rclone_state = {
            "success": True,
            "configured": True,
            "remote_name": "MP",
            "type": "webdav",
            "url": str(payload.get("url") or "").rstrip("/"),
            "user": str(payload.get("username") or ""),
            "password_set": bool(payload.get("password")),
            "connection_status": "success",
            "message": "rclone WebDAV connection passed",
        }
        return dict(self.rclone_state)

    def _test_rclone_webdav(self) -> dict[str, Any]:
        if self.rclone_sync_error is not None:
            raise self.rclone_sync_error
        if not self.rclone_state:
            raise RuntimeError("rclone remote is unconfigured")
        return dict(self.rclone_state)

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
        mobile = self._account("mobile", "移动云主账号")
        tianyi = self._account("tianyi", "天翼云主账号")
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
        self.service.save_location(
            {
                "name": "移动云主盘",
                "kind": "provider",
                "account_id": mobile,
                "connection_id": "openlist_main",
                "webdav_root": "/移动",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "confirm_identity": True,
            }
        )
        self.service.save_location(
            {
                "name": "天翼云主盘",
                "kind": "provider",
                "account_id": tianyi,
                "connection_id": "openlist_main",
                "webdav_root": "/天翼",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
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
        self.assertEqual(len(snapshot["locations"]), 4)
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

        extra = self.connections.create_connection(
            {
                "connection_id": "unused_backup",
                "name": "未使用的备用连接",
                "base_url": "http://unused-openlist:5244/dav",
                "username": "admin",
                "password": "password",
            }
        )
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

    def test_routes_allow_partial_configuration_and_blank_cancels_assignment(self) -> None:
        tianyi = self._account("tianyi", "天翼云主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        tianyi_location = self.service.save_location(
            {
                "name": "天翼云主盘",
                "kind": "provider",
                "account_id": tianyi,
                "connection_id": "openlist_main",
                "webdav_root": "/天翼",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "confirm_identity": True,
            }
        )
        destination_id = tianyi_location["destination_id"]

        # 只接入了天翼一个网盘时，只提交天翼规则也必须能保存，其余来源保持未分配。
        self.service.save_routes(
            {"routes": [{"search_source_id": "tianyi", "default_destination_id": destination_id}]}
        )
        assigned = self.assignments.list_assignments()
        self.assertEqual({item.search_source_id for item in assigned}, {"tianyi"})
        self.assertEqual(
            {item.category_key for item in assigned},
            set(CATEGORY_KEYS),
        )
        snapshot = self.service.snapshot()
        unassigned = [
            route for route in snapshot["routes"] if route["search_source_id"] != "tianyi"
        ]
        self.assertTrue(unassigned)
        self.assertTrue(all(route["default_destination_id"] is None for route in unassigned))
        self.assertTrue(all(route["overrides"] == {} for route in unassigned))

        # 留空等于取消该来源的分配。
        self.service.save_routes(
            {"routes": [{"search_source_id": "tianyi", "default_destination_id": ""}]}
        )
        self.assertEqual(self.assignments.list_assignments(), [])

        # 默认目标留空、只设了单个分类覆盖时，只写入被覆盖的分类。
        self.service.save_routes(
            {
                "routes": [
                    {
                        "search_source_id": "tianyi",
                        "default_destination_id": "",
                        "overrides": {"movie": destination_id},
                    }
                ]
            }
        )
        partial = self.assignments.list_assignments()
        self.assertEqual(
            [(item.search_source_id, item.category_key, item.destination_id) for item in partial],
            [("tianyi", "movie", destination_id)],
        )

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

    def test_mobile_put_405_creates_native_only_location(self) -> None:
        mobile = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        def reject_put(_path: str, _content: bytes) -> None:
            raise RuntimeError("写入文件失败：HTTP 405")

        self.webdav.put_bytes = reject_put
        result = self.service.save_location(
            {
                "name": "移动云主盘",
                "kind": "provider",
                "account_id": mobile,
                "connection_id": "openlist_main",
                "webdav_root": "/移动",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "confirm_identity": True,
            }
        )

        self.assertIn("Provider 原生直传", result["message"])
        target = self.targets.get_target(result["destination_id"])
        mount_id = self.service._target_mount_id(target)
        mount = self.mounts.get_mount(mount_id)
        self.assertTrue(mount["readable"])
        self.assertFalse(mount["writable"])
        self.assertEqual(mount["identity_status"], "declared")
        self.assertTrue(mount["native_locator"]["upload_verified"])
        self.assertTrue(mount["native_locator"]["organizer_mutation_verified"])
        self.assertEqual(mount["native_locator"]["display_name"], "/媒体库")
        self.assertTrue(mount["native_locator"]["probe_fingerprint"])

        probe = self.mounts.probe_mount(mount_id, persist_result=False)
        self.assertTrue(probe["ok"], probe)
        self.assertFalse(probe["webdav_file_write_required"])
        self.assertFalse(probe["writable"])
        create_check = next(
            item for item in probe["checks"] if item["name"] == "临时目录与文件创建"
        )
        self.assertFalse(create_check["required"])

        snapshot = self.service.snapshot()
        location = next(
            item for item in snapshot["locations"] if item["destination_id"] == result["destination_id"]
        )
        self.assertTrue(location["ready"], location)
        self.assertTrue(location["native_only"])
        self.assertIn("mobile", location["eligible_source_ids"])
        self.assertIn("tianyi", location["eligible_source_ids"])
        self.assertIn("quark", location["eligible_source_ids"])
        self.assertEqual(location["native_root_name"], "/媒体库")
        self.assertEqual(len(self.mobile_uploader.probe_calls), 1)

    def test_mobile_native_probe_failure_leaves_no_location(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        self.mobile_uploader.upload_ok = False

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
                {
                    "name": "移动云主盘",
                    "kind": "provider",
                    "account_id": account_id,
                    "connection_id": "openlist_main",
                    "webdav_root": "/移动",
                    "native_root": "root-folder-id",
                    "confirm_identity": True,
                }
            )

        self.assertEqual(raised.exception.code, "PROVIDER_UPLOAD_PROBE_FAILED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

    def test_tianyi_probe_auth_failure_is_reported_as_re_login(self) -> None:
        account_id = self._account("tianyi", "天翼云主账号")
        self.webdav.dirs.add("/天翼")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        self.tianyi_uploader.probe_target = lambda **_kwargs: {
            "ok": False,
            "upload_verified": False,
            "organizer_mutation_verified": False,
            "code": "AUTH_EXPIRED",
            "message": "天翼云登录已过期",
        }

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
                {
                    "name": "天翼云主盘",
                    "kind": "provider",
                    "account_id": account_id,
                    "connection_id": "openlist_main",
                    "webdav_root": "/天翼",
                    "native_root": "root-folder-id",
                    "confirm_identity": True,
                }
            )

        self.assertEqual(raised.exception.code, "PROVIDER_AUTH_EXPIRED")
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("重新登录", str(raised.exception))

    def test_tianyi_probe_quota_failure_is_reported_as_storage_full(self) -> None:
        account_id = self._account("tianyi", "天翼云主账号")
        self.webdav.dirs.add("/天翼")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        self.tianyi_uploader.probe_target = lambda **_kwargs: {
            "ok": False,
            "upload_verified": False,
            "organizer_mutation_verified": False,
            "code": "InsufficientStorageSpace",
            "message": "天翼云盘免费空间已用尽，请先清理空间或升级容量后再试",
        }

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
                {
                    "name": "天翼云主盘",
                    "kind": "provider",
                    "account_id": account_id,
                    "connection_id": "openlist_main",
                    "webdav_root": "/天翼",
                    "native_root": "root-folder-id",
                    "confirm_identity": True,
                }
            )

        self.assertEqual(raised.exception.code, "PROVIDER_STORAGE_INSUFFICIENT")
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("空间", str(raised.exception))

    def test_tianyi_put_405_creates_provider_relay_location(self) -> None:
        account_id = self._account("tianyi", "天翼云主账号")
        self.webdav.dirs.add("/天翼")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        def reject_put(_path: str, _content: bytes) -> None:
            raise RuntimeError("写入文件失败：HTTP 405")

        self.webdav.put_bytes = reject_put
        result = self.service.save_location(
            {
                "name": "天翼云主盘",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/天翼",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "confirm_identity": True,
            }
        )

        self.assertIn("Provider 原生直传", result["message"])
        target = self.targets.get_target(result["destination_id"])
        mount_id = self.service._target_mount_id(target)
        mount = self.mounts.get_mount(mount_id)
        self.assertEqual(mount["native_locator"]["kind"], "folder_id")
        self.assertEqual(mount["native_locator"]["value"], "root-folder-id")
        self.assertTrue(mount["readable"])
        self.assertFalse(mount["writable"])
        self.assertTrue(mount["native_locator"]["upload_verified"])
        self.assertTrue(mount["native_locator"]["organizer_mutation_verified"])

        probe = self.mounts.probe_mount(mount_id, persist_result=False)
        self.assertTrue(probe["ok"], probe)
        self.assertFalse(probe["webdav_file_write_required"])
        self.assertFalse(probe["writable"])

        snapshot = self.service.snapshot()
        location = next(
            item for item in snapshot["locations"] if item["destination_id"] == result["destination_id"]
        )
        self.assertTrue(location["ready"], location)
        self.assertTrue(location["native_only"])
        self.assertTrue(location["native_upload_verified"])
        self.assertTrue(location["organizer_mutation_verified"])
        self.assertIn("quark", location["eligible_source_ids"])
        self.assertIn("mobile", location["eligible_source_ids"])
        self.assertIn("tianyi", location["eligible_source_ids"])
        self.assertEqual(len(self.tianyi_uploader.probe_calls), 1)

        for category_key in target.categories:
            self.assignments.upsert(
                "tianyi",
                category_key,
                result["destination_id"],
            )
        preflight = self.service.preflight()
        directory_check = next(
            item for item in preflight["checks"] if item["name"] == f"目录 {mount['name']}"
        )
        self.assertTrue(directory_check["ok"], directory_check)

    def test_tianyi_relay_requires_upload_and_organizer_proofs(self) -> None:
        account_id = self._account("tianyi", "天翼云主账号")
        self.webdav.dirs.add("/天翼")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        payload = {
            "name": "天翼云主盘",
            "kind": "provider",
            "account_id": account_id,
            "connection_id": "openlist_main",
            "webdav_root": "/天翼",
            "native_root": "root-folder-id",
            "confirm_identity": True,
        }

        self.tianyi_uploader.upload_ok = False
        with self.assertRaises(StorageSetupError) as upload_failure:
            self.service.save_location(payload)
        self.assertEqual(upload_failure.exception.code, "PROVIDER_UPLOAD_PROBE_FAILED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

        self.tianyi_uploader.upload_ok = True
        self.tianyi_uploader.organizer_ok = False
        with self.assertRaises(StorageSetupError) as organizer_failure:
            self.service.save_location(payload)
        self.assertEqual(organizer_failure.exception.code, "OPENLIST_MUTATION_PROBE_FAILED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

    def test_webdav_writable_state_cannot_replace_provider_native_proofs(self) -> None:
        writable_mount = {
            "provider_kind": "tianyi",
            "readable": True,
            "writable": True,
            "native_locator": {"kind": "folder_id", "value": "root-folder-id"},
        }

        self.assertEqual(
            self.service._mount_native_proofs("tianyi", writable_mount),
            (False, False),
        )
        self.assertEqual(
            self.service._mount_native_proofs("mobile", writable_mount),
            (False, False),
        )

    def test_mobile_organizer_probe_failure_leaves_no_location(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        self.mobile_uploader.organizer_ok = False

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
                {
                    "name": "移动云主盘",
                    "kind": "provider",
                    "account_id": account_id,
                    "connection_id": "openlist_main",
                    "webdav_root": "/移动",
                    "native_root": "root-folder-id",
                    "confirm_identity": True,
                }
            )

        self.assertEqual(raised.exception.code, "OPENLIST_MUTATION_PROBE_FAILED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

    def test_native_probe_failure_logs_pairing_diagnostics(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        def broken_move(source: str, destination: str) -> None:
            raise RuntimeError("MOVE 失败")

        self.webdav.move = broken_move  # type: ignore[method-assign]

        with self.assertLogs("fnos_media_import.storage_setup", level="WARNING") as captured:
            with self.assertRaises(StorageSetupError):
                self.service.save_location(
                    {
                        "name": "移动云主盘",
                        "kind": "provider",
                        "account_id": account_id,
                        "connection_id": "openlist_main",
                        "webdav_root": "/移动",
                        "native_root": "root-folder-id",
                        "confirm_identity": True,
                    }
                )

        failures = [
            line for line in captured.output if "event=storage_native_probe_failed" in line
        ]
        self.assertEqual(len(failures), 1)
        line = failures[0]
        # 一行里同时给出 OpenList 目录、原生定位符和真实探测路径，配对错了看日志就知道。
        self.assertIn("provider=mobile", line)
        self.assertIn(f"account_id={account_id}", line)
        self.assertIn("connection_id=openlist_main", line)
        self.assertIn("webdav_root=/移动", line)
        self.assertIn("native_locator_kind=provider_path", line)
        self.assertIn("native_locator_value=root-folder-id", line)
        self.assertIn("probe_root=/移动", line)
        self.assertIn(
            "probe_source_path=/移动/fnos-native-probe.txt", line
        )
        self.assertIn(
            "probe_moved_path=/移动/fnos-native-probe.txt.moved", line
        )
        self.assertIn("upload_verified=True", line)
        self.assertIn("organizer_mutation_verified=False", line)

    def test_mobile_organizer_probe_retries_transient_read_failure(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        original_read = self.webdav.read_bytes
        attempts = {"n": 0}

        def flaky_read(path: str) -> bytes:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("读取文件失败：HTTP 400")
            return original_read(path)

        self.webdav.read_bytes = flaky_read  # type: ignore[method-assign]

        result = self.service.save_location(
            {
                "name": "移动云主盘",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/移动",
                "native_root": "root-folder-id",
                "confirm_identity": True,
            }
        )

        self.assertGreaterEqual(attempts["n"], 2)
        target = self.targets.get_target(result["destination_id"])
        mount_id = self.service._target_mount_id(target)
        mount = self.mounts.get_mount(mount_id)
        self.assertTrue(mount["native_locator"]["upload_verified"])
        self.assertTrue(mount["native_locator"]["organizer_mutation_verified"])

    def test_identity_confirmation_is_checked_before_native_probe(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
                {
                    "name": "移动云主盘",
                    "kind": "provider",
                    "account_id": account_id,
                    "connection_id": "openlist_main",
                    "webdav_root": "/移动",
                    "native_root": "root-folder-id",
                }
            )

        self.assertEqual(raised.exception.code, "IDENTITY_CONFIRMATION_REQUIRED")
        self.assertEqual(self.mobile_uploader.probe_calls, [])

    def test_unchanged_mobile_location_reuses_native_probe_proof(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        self.webdav.dirs.add("/移动")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        location = self.service.save_location(
            {
                "name": "移动云主盘",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/移动",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])

        self.service.save_location(
            {
                "name": "移动云主盘",
                "kind": "provider",
                "account_id": account_id,
                "connection_id": "openlist_main",
                "webdav_root": "/移动",
                "native_root": "root-folder-id",
                "native_root_name": "/媒体库",
                "expected_revision": target.current_revision,
            },
            location["destination_id"],
        )

        self.assertEqual(len(self.mobile_uploader.probe_calls), 1)

    def test_tianyi_native_folder_browser_service_and_api(self) -> None:
        account_id = self._account("tianyi", "天翼主账号")
        root = self.service.list_account_folders(account_id)
        self.assertEqual(root["parent_id"], "-11")
        self.assertEqual(root["items"][0]["folder_id"], "root-folder-id")

        app = Flask(__name__ + "_folders")
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(admin_required=lambda view: view, service=self.service)
            )
        )
        response = app.test_client().get(
            f"/api/admin/storage-setup/accounts/{account_id}/folders?parent_id=root-folder-id"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["folder_id"], "movie-folder-id")
        self.assertEqual(self.tianyi_uploader.folder_calls, ["-11", "root-folder-id"])

    def test_quark_native_folder_browser_service_and_api(self) -> None:
        account_id = self._account("quark", "夸克主账号")
        root = self.service.list_account_folders(account_id, "0")
        self.assertEqual(root["parent_id"], "0")
        self.assertEqual(root["items"][0]["folder_id"], "root-folder-id")

        app = Flask(__name__ + "_quark_folders")
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(admin_required=lambda view: view, service=self.service)
            )
        )
        response = app.test_client().get(
            f"/api/admin/storage-setup/accounts/{account_id}/folders?parent_id=root-folder-id"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["folder_id"], "movie-folder-id")
        self.assertEqual(self.quark_uploader.folder_calls, ["0", "root-folder-id"])

    def test_mobile_native_folder_browser_service_and_api(self) -> None:
        account_id = self._account("mobile", "移动云主账号")
        root = self.service.list_account_folders(account_id, "/")
        self.assertEqual(root["parent_id"], "/")
        self.assertEqual(root["items"][0]["folder_id"], "root-folder-id")

        app = Flask(__name__ + "_mobile_folders")
        app.register_blueprint(
            create_storage_setup_blueprint(
                StorageSetupBlueprintContext(admin_required=lambda view: view, service=self.service)
            )
        )
        response = app.test_client().get(
            f"/api/admin/storage-setup/accounts/{account_id}/folders?parent_id=root-folder-id"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["folder_id"], "movie-folder-id")
        self.assertEqual(self.mobile_uploader.folder_calls, ["/", "root-folder-id"])

    def test_non_relay_provider_put_405_still_fails_closed(self) -> None:
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )

        def reject_put(_path: str, _content: bytes) -> None:
            raise RuntimeError("写入文件失败：HTTP 405")

        self.webdav.put_bytes = reject_put
        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_location(
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

        self.assertEqual(raised.exception.code, "OPENLIST_PROBE_FAILED")
        self.assertEqual(self.mounts.list_mounts(), [])
        self.assertEqual(self.targets.list_targets(), [])

    def test_storage_setup_rejects_a_second_managed_openlist_connection(self) -> None:
        first = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist-a:5244/dav",
                "username": "admin-a",
                "password": "password-a",
            }
        )["item"]
        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_openlist(
                {
                    "name": "备用 OpenList",
                    "base_url": "http://openlist-b:5244/dav",
                    "username": "admin-b",
                    "password": "password-b",
                }
            )

        self.assertEqual(first["connection_id"], "openlist_main")
        self.assertEqual(raised.exception.code, "OPENLIST_CONNECTION_LIMIT")
        self.assertEqual(len(self.connections.list_connections()), 1)
        self.assertEqual(
            self.connections.get_connection(first["connection_id"])["base_url"],
            "http://openlist-a:5244/dav",
        )

        with self.assertRaises(StorageSetupError) as explicit_id:
            self.service.save_openlist(
                {
                    "connection_id": "openlist_backup",
                    "name": "显式备用 OpenList",
                    "base_url": "http://openlist-c:5244/dav",
                    "username": "admin-c",
                    "password": "password-c",
                }
            )
        self.assertEqual(explicit_id.exception.code, "OPENLIST_CONNECTION_LIMIT")
        self.assertEqual(len(self.connections.list_connections()), 1)

    def test_openlist_save_syncs_rclone_without_returning_password(self) -> None:
        result = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist:5244/dav/",
                "username": "admin",
                "password": "server-only-secret",
            }
        )

        self.assertEqual(len(self.rclone_sync_calls), 1)
        self.assertEqual(self.rclone_sync_calls[0]["password"], "server-only-secret")
        self.assertEqual(result["rclone"]["connection_status"], "success")
        self.assertNotIn("server-only-secret", str(result))
        self.assertNotIn("password", result["rclone"])

    def test_openlist_update_reuses_encrypted_password_for_rclone_sync(self) -> None:
        original = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist-old:5244/dav",
                "username": "admin",
                "password": "saved-secret",
            }
        )["item"]

        self.service.save_openlist(
            {
                "connection_id": original["connection_id"],
                "expected_revision": original["revision"],
                "name": "主 OpenList",
                "base_url": "http://openlist-new:5244/dav",
                "username": "new-admin",
            }
        )

        self.assertEqual(self.rclone_sync_calls[-1]["password"], "saved-secret")
        self.assertEqual(
            self.rclone_sync_calls[-1]["url"],
            "http://openlist-new:5244/dav",
        )
        self.assertEqual(self.rclone_sync_calls[-1]["username"], "new-admin")

    def test_missing_rclone_binary_still_saves_openlist(self) -> None:
        # rclone 只用于跨盘搬运；OpenList 还要驱动整理和 STRM。
        # 本地没有 rclone 时不能因此拒绝保存 OpenList。
        self.rclone_sync_error = RcloneWebdavConfigError(
            "rclone 管理命令不可用",
            503,
            code=RCLONE_UNAVAILABLE,
        )

        result = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["rclone_available"])
        self.assertIn("rclone", result["message"])
        self.assertEqual(result["rclone"]["connection_status"], "unavailable")
        saved = self.connections.get_connection(result["item"]["connection_id"])
        self.assertIsNotNone(saved)
        self.assertEqual(saved["base_url"], "http://openlist:5244/dav")

    def test_unreachable_rclone_worker_still_saves_openlist(self) -> None:
        class WorkerDown(RuntimeError):
            status_code = 503
            payload = {
                "success": False,
                "status": "worker_unavailable",
                "error_code": "worker_unavailable",
                "message": "无法连接 rclone Worker，请稍后重试",
            }

        self.rclone_sync_error = WorkerDown("无法连接 rclone Worker，请稍后重试")

        result = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["rclone_available"])
        self.assertIsNotNone(
            self.connections.get_connection(result["item"]["connection_id"])
        )

    def test_openlist_saves_when_no_rclone_bridge_is_wired(self) -> None:
        service = StorageSetupService(
            accounts_service=self.accounts,
            connection_service=self.connections,
            mount_service=self.mounts,
            target_service=self.targets,
            assignment_service=self.assignments,
            connection_factory=self.db.connect,
            organizer_enabled=lambda: False,
        )

        result = service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["rclone_available"])

    def test_genuine_rclone_rejection_still_fails_the_save(self) -> None:
        # rclone 在位但拒绝请求（凭据错误、Remote 名被占用）仍必须回滚，
        # 否则会留下一个指向错误 Remote 的 OpenList 连接。
        self.rclone_sync_error = RcloneWebdavConfigError(
            "Remote“MP”已存在且类型不是 WebDAV，未覆盖原配置",
            409,
        )

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_openlist(
                {
                    "name": "主 OpenList",
                    "base_url": "http://openlist:5244/dav",
                    "username": "admin",
                    "password": "secret",
                }
            )

        self.assertEqual(raised.exception.code, "RCLONE_WEBDAV_SYNC_FAILED")
        self.assertEqual(self.connections.list_connections(), [])

    def test_preflight_still_fails_cross_provider_routes_without_rclone(self) -> None:
        # 保存放行了，但真正需要跨盘搬运的路由必须在预检里明确报错，
        # 否则会让人以为没有 rclone 也能跨盘搬运。
        mobile = self._account("mobile", "移动云盘")
        quark = self._account("quark", "夸克主账号")
        self.service.save_openlist(
            {
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "password",
            }
        )
        location = self.service.save_location(
            {
                "name": "夸克目标",
                "kind": "provider",
                "account_id": quark,
                "connection_id": "openlist_main",
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        self.assignments.sources = lambda: {
            "mobile": {
                "search_source_id": "mobile",
                "provider_kind": "mobile",
                "label": "移动云盘",
                "source_form": "share",
            }
        }
        self.assignments.matrix = lambda: {
            "rows": [
                {
                    "search_source_id": "mobile",
                    "provider_kind": "mobile",
                    "label": "移动云盘",
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
        self.assertTrue(mobile)
        self.rclone_sync_error = RcloneWebdavConfigError(
            "rclone 管理命令不可用",
            503,
            code=RCLONE_UNAVAILABLE,
        )

        result = self.service.preflight()

        rclone_check = next(
            item for item in result["checks"] if item["name"] == "rclone OpenList Remote"
        )
        self.assertFalse(rclone_check["ok"])
        self.assertIn("rclone", str(rclone_check["message"]))
        self.assertIn("不可用", str(rclone_check["message"]))
        self.assertFalse(result["ok"])

    def test_rclone_sync_failure_restores_connection_and_mount_probe_state(self) -> None:
        quark = self._account("quark", "夸克主账号")
        original_connection = self.service.save_openlist(
            {
                "name": "主 OpenList",
                "base_url": "http://openlist-old:5244/dav",
                "username": "admin",
                "password": "saved-secret",
            }
        )["item"]
        location = self.service.save_location(
            {
                "name": "夸克主盘",
                "kind": "provider",
                "account_id": quark,
                "connection_id": original_connection["connection_id"],
                "webdav_root": "/夸克",
                "native_root": "/媒体库",
                "confirm_identity": True,
            }
        )
        target = self.targets.get_target(location["destination_id"])
        mount_id = self.service._target_mount_id(target)
        original_mount = self.mounts.get_mount(mount_id)
        self.rclone_sync_error = RuntimeError("worker unavailable")

        with self.assertRaises(StorageSetupError) as raised:
            self.service.save_openlist(
                {
                    "connection_id": original_connection["connection_id"],
                    "expected_revision": original_connection["revision"],
                    "name": "错误的新连接",
                    "base_url": "http://openlist-new:5244/dav",
                    "username": "new-admin",
                    "password": "new-secret",
                }
            )

        self.assertEqual(raised.exception.code, "RCLONE_WEBDAV_SYNC_FAILED")
        restored_connection = self.connections.get_connection(
            original_connection["connection_id"]
        )
        self.assertEqual(
            restored_connection["base_url"],
            original_connection["base_url"],
        )
        restored_mount = self.mounts.get_mount(mount_id)
        self.assertEqual(restored_mount["identity_status"], original_mount["identity_status"])
        self.assertEqual(restored_mount["readable"], original_mount["readable"])
        self.assertEqual(restored_mount["writable"], original_mount["writable"])
        self.assertEqual(restored_mount["native_locator"], original_mount["native_locator"])
        self.assertEqual(
            restored_mount["connection_revision"],
            restored_connection["revision"],
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


class OpenListMutationObserverTests(unittest.TestCase):
    """Cover the OpenList-side half of the native upload probe.

    These exercise ``_openlist_mutation_observer`` directly: the failures they
    guard against are invisible through ``save_location``, which only sees the
    aggregated probe verdict.
    """

    def setUp(self) -> None:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.db_path = WORK_TEST_ROOT / f"observer-{uuid.uuid4().hex}.db"
        self._db_infrastructure = Database(self.db_path)
        self._db_infrastructure.init_schema()
        self.db = RepositorySet.from_database(self._db_infrastructure)
        self.addCleanup(self._cleanup_database)
        self.webdav = MemoryWebDAV()
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
        self.service = StorageSetupService(
            accounts_service=ProviderAccountService(
                self.db,
                lambda: KEY,
                tester=lambda *_args: AccountTestResult(True, "user***1", "账号检测通过"),
            ),
            connection_service=self.connections,
            mount_service=self.mounts,
            target_service=self.targets,
            assignment_service=SourceAssignmentService(self.db, self.targets),
            connection_factory=self.db.connect,
            organizer_enabled=lambda: False,
        )
        self.service.save_openlist(
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "password"}
        )
        # Keep the probe's own polling from making these tests slow: the
        # production window is 12 attempts x 0.5s per visibility check.
        patcher = mock.patch.object(setup_service_module.time, "sleep", lambda _seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def _observe(self, file_name: str, content: bytes = b"probe") -> dict[str, Any]:
        observer = self.service._openlist_mutation_observer("openlist_main", "/天翼")
        return observer(
            {
                "file_id": "probe-file-id",
                "file_name": file_name,
                "file_size": len(content),
                "content": content,
            }
        )

    def test_invisible_probe_file_is_not_reported_as_deleted(self) -> None:
        """``deleted`` gates the Provider's own cleanup, so it must not be guessed.

        Inferring it from ``not exists()`` made an unreadable file look tidied
        up, and the native uploader then skipped its cleanup — leaking one probe
        file into the cloud drive on every failure.
        """

        self.webdav.dirs.add("/天翼")
        result = self._observe("fnos-native-probe-abc.txt")

        self.assertFalse(result["organizer_mutation_verified"])
        self.assertFalse(result["deleted"])
        self.assertIn("未暴露原生探测文件", result["message"])

    def test_probe_file_visible_to_openlist_passes_and_is_cleaned(self) -> None:
        self.webdav.dirs.add("/天翼")
        content = b"probe"
        self.webdav.files["/天翼/fnos-native-probe-abc.txt"] = content

        result = self._observe("fnos-native-probe-abc.txt", content)

        self.assertTrue(result["organizer_mutation_verified"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.webdav.files, {})

    def test_leftover_probe_file_is_deleted_and_reported_on_failure(self) -> None:
        """A file OpenList can see but not serve must still be cleaned up."""

        self.webdav.dirs.add("/天翼")
        self.webdav.files["/天翼/fnos-native-probe-abc.txt"] = b"probe"

        def unreadable(_path: str) -> bytes:
            raise RuntimeError("OpenList GET failed")

        self.webdav.read_bytes = unreadable

        result = self._observe("fnos-native-probe-abc.txt", b"probe")

        self.assertFalse(result["organizer_mutation_verified"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.webdav.files, {})

    def test_stale_directory_cache_is_refreshed_until_the_file_appears(self) -> None:
        """The probe file lands via the Provider API, behind OpenList's cache.

        WebDAV has no refresh verb, so without an explicit invalidation the poll
        just re-reads the same stale listing for its whole window.
        """

        self.webdav.dirs.add("/天翼")
        path = "/天翼/fnos-native-probe-abc.txt"
        content = b"probe"
        refreshed: list[str] = []

        class RefreshingApiClient:
            def list_dir(inner_self, target: str, *, refresh: bool = False) -> list[Any]:
                if refresh:
                    refreshed.append(target)
                    self.webdav.files[path] = content
                return []

        self.service.openlist_api_factory = lambda _credentials: RefreshingApiClient()

        result = self._observe("fnos-native-probe-abc.txt", content)

        self.assertTrue(result["organizer_mutation_verified"])
        self.assertEqual(refreshed[0], "/天翼")

    def test_probe_survives_a_missing_or_broken_refresh_client(self) -> None:
        """Refresh is an optimisation; setup must not fail when it is absent."""

        self.webdav.dirs.add("/天翼")
        content = b"probe"
        self.webdav.files["/天翼/fnos-native-probe-abc.txt"] = content

        def exploding_factory(_credentials: dict[str, Any]) -> Any:
            raise RuntimeError("OpenList API unreachable")

        self.service.openlist_api_factory = exploding_factory

        result = self._observe("fnos-native-probe-abc.txt", content)

        self.assertTrue(result["organizer_mutation_verified"])


if __name__ == "__main__":
    unittest.main()
