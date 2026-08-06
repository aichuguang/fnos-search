"""V1.0 存储目标与来源规则契约测试。

覆盖：目标 CRUD 与修订不可变、本地路径包含（拒绝 .. 越界）、openlist_generic
要求现有挂载定位器、规则默认唯一 + 精确分类优先 + 磁链→六盘约束、
``resolve_destination`` 服务端分配或 None、禁用 vs 删除的引用保护，以及
§16.3 管理 API 不泄露密钥。

运行：C:\\WorkSpace\\fnos-search\\.venv\\Scripts\\python.exe -m unittest tests.test_storage_target_v1
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify

from fnos_media_import.database import Database
from fnos_media_import.domain.models import (
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    RcloneLocator,
    TargetCategory,
)
from fnos_media_import.storage.blueprint import StorageBlueprintContext, create_storage_blueprint
from fnos_media_import.storage.rules_service import (
    DestinationRulesService,
    RuleNotFoundError,
    RuleValidationError,
)
from fnos_media_import.storage.target_service import (
    StorageTargetService,
    TargetInUseError,
    TargetNotFoundError,
    TargetValidationError,
    target_to_dict,
)

ROOT = Path(__file__).resolve().parents[1]

_CONNECTION_SQL = (
    "INSERT OR IGNORE INTO openlist_connections "
    "(connection_id, name, base_url, username, password_ciphertext, credential_key_id, enabled, "
    " revision, last_test_status, last_tested_at, created_at, updated_at) "
    "VALUES (?, ?, 'https://openlist.test', 'admin', X'00', 'test-key', 1, 1, 'untested', NULL, "
    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
)


def _insert_mount(database: Database, mount_id: str, *, connection_id: str = "ol_main", writable: bool = True) -> None:
    """插入一条 openlist_connection + mount，供目标挂载校验使用。"""
    with database.connect() as conn:
        conn.execute(_CONNECTION_SQL, (connection_id, connection_id))
        conn.execute(
            "INSERT INTO openlist_mounts "
            "(mount_id, connection_id, name, webdav_root, namespace_id, readable, writable, "
            " enabled, revision, identity_status) VALUES (?, ?, ?, '/', ?, 1, ?, 1, 1, 'declared')",
            (mount_id, connection_id, mount_id, f"openlist:{connection_id}:{mount_id}", 1 if writable else 0),
        )


def _provider_category(final_path: str = "影视/电影", *, mount_id: str = "mount_quark") -> TargetCategory:
    return TargetCategory(
        category_key="movie",
        public_label="电影库",
        rclone_locator=RcloneLocator(connection_id="ol_main", mount_id=mount_id, relative_path=final_path),
    )


def _local_category(relative: str = "电影", *, root: str = "/storage-targets/local-lib") -> TargetCategory:
    return TargetCategory(
        category_key="movie",
        public_label="本地影视库",
        rclone_locator=LocalLocator(root=root, relative_path=relative),
    )


def _openlist_generic_category(final_path: str = "下载", *, mount_id: str = "mount_generic") -> TargetCategory:
    return TargetCategory(
        category_key="movie",
        public_label="通用影视库",
        rclone_locator=RcloneLocator(connection_id="ol_main", mount_id=mount_id, relative_path=final_path),
    )


class StorageTargetServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = ROOT / f"test-storage-target-v1-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        _insert_mount(self.database, "mount_quark")
        _insert_mount(self.database, "mount_sixpan")
        _insert_mount(self.database, "mount_generic")
        self.service = StorageTargetService(self.database)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_quark_target(self, destination_id: str = "quark_library") -> Any:
        return self.service.create_target(
            destination_id=destination_id,
            name="夸克影视库",
            provider_kind="quark",
            target_kind="provider",
            audience="guest",
            categories={"movie": _provider_category()},
        )

    def _create_sixpan_target(self) -> Any:
        return self.service.create_target(
            destination_id="sixpan_offline",
            name="六盘离线区",
            provider_kind="sixpan",
            target_kind="provider",
            audience="guest",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="离线区",
                    native_locator=NativeLocator(kind="provider_path", value="离线下载"),
                )
            },
        )

    def _create_local_target(self) -> Any:
        return self.service.create_target(
            destination_id="local_library",
            name="飞牛本地影视库",
            provider_kind="local",
            target_kind="local",
            audience="guest",
            categories={"movie": _local_category()},
        )

    # ---------------------------------------------------------------- CRUD

    def test_create_and_get_target(self) -> None:
        target = self._create_quark_target()
        self.assertEqual(target.destination_id, "quark_library")
        self.assertEqual(target.current_revision, 1)
        self.assertEqual(target.provider_kind, "quark")
        self.assertEqual(target.account_scope, "default")
        self.assertEqual(target.target_kind, "provider")
        self.assertTrue(target.enabled)
        self.assertEqual(target.audience, "guest")

        loaded = self.service.get_target("quark_library")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.current_revision, 1)
        movie = loaded.categories["movie"]
        self.assertEqual(movie.public_label, "电影库")
        assert isinstance(movie.rclone_locator, RcloneLocator)
        self.assertEqual(movie.rclone_locator.mount_id, "mount_quark")

    def test_list_targets_orders_by_destination_id(self) -> None:
        self._create_quark_target()
        self._create_local_target()
        ids = [t.destination_id for t in self.service.list_targets()]
        self.assertEqual(ids, ["local_library", "quark_library"])

    def test_reject_invalid_destination_id(self) -> None:
        for bad in ("Bad ID", "1x!", "UPPER", "-lead", "_lead", "has space", "中文"):
            with self.subTest(destination_id=bad), self.assertRaises(TargetValidationError):
                self.service.create_target(
                    destination_id=bad,
                    name="x",
                    provider_kind="quark",
                    categories={"movie": _provider_category()},
                )

    def test_reject_invalid_target_kind(self) -> None:
        with self.assertRaises(TargetValidationError):
            self.service.create_target(
                destination_id="bad_kind",
                name="x",
                provider_kind="quark",
                target_kind="s3",
                categories={"movie": _provider_category()},
            )

    def test_target_requires_at_least_one_category(self) -> None:
        with self.assertRaisesRegex(TargetValidationError, "至少需要一个分类"):
            self.service.create_target(
                destination_id="empty_target", name="x", provider_kind="quark", categories={}
            )

    def test_reject_duplicate_target(self) -> None:
        self._create_quark_target()
        with self.assertRaisesRegex(TargetValidationError, "已存在"):
            self._create_quark_target()

    # ------------------------------------------------------- 修订不可变

    def test_revision_is_immutable(self) -> None:
        self._create_quark_target()
        revised = self.service.revise_target(
            "quark_library",
            name="夸克影视库V2",
            categories={"movie": _provider_category(final_path="影视/电影/新版")},
        )
        self.assertEqual(revised.current_revision, 2)
        self.assertEqual(revised.categories["movie"].public_label, "电影库")

        # 当前修订看到新分类路径。
        current = self.service.get_target("quark_library")
        assert current is not None
        assert isinstance(current.categories["movie"].rclone_locator, RcloneLocator)
        self.assertEqual(current.categories["movie"].rclone_locator.relative_path, "影视/电影/新版")
        self.assertEqual(current.current_revision, 2)
        self.assertEqual(current.name, "夸克影视库V2")

        # 旧修订仍可解析，且内容不被覆盖。
        old = self.service.resolve_target("quark_library", 1)
        assert isinstance(old.categories["movie"].rclone_locator, RcloneLocator)
        self.assertEqual(old.categories["movie"].rclone_locator.relative_path, "影视/电影")
        self.assertEqual(old.name, "夸克影视库")
        self.assertEqual(old.current_revision, 1)

        # 修订历史完整。
        revisions = self.service.list_revisions("quark_library")
        self.assertEqual([r["revision"] for r in revisions], [1, 2])

    def test_revise_without_changes_raises(self) -> None:
        self._create_quark_target()
        with self.assertRaisesRegex(TargetValidationError, "没有要修订的内容"):
            self.service.revise_target("quark_library")

    def test_resolve_unknown_revision_raises(self) -> None:
        self._create_quark_target()
        with self.assertRaises(TargetNotFoundError):
            self.service.resolve_target("missing_target")
        with self.assertRaises(Exception):
            self.service.resolve_target("quark_library", 99)

    def test_meta_update_does_not_bump_revision(self) -> None:
        self._create_quark_target()
        updated = self.service.update_target_meta("quark_library", enabled=False, account_scope="archive")
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.account_scope, "archive")
        self.assertEqual(updated.current_revision, 1)

    # ------------------------------------------------------- 本地路径包含

    def test_local_target_rejects_parent_escape(self) -> None:
        with self.assertRaisesRegex(TargetValidationError, "本地路径不合法"):
            self.service.create_target(
                destination_id="escape_target",
                name="越界",
                provider_kind="local",
                target_kind="local",
                categories={"movie": _local_category(relative="../../etc/passwd")},
            )

    def test_local_target_rejects_unsafe_root(self) -> None:
        for root in ("/tmp/lib", "/", "relative/path", "/etc/passwd"):
            with self.subTest(root=root), self.assertRaisesRegex(TargetValidationError, "本地路径不合法"):
                self.service.create_target(
                    destination_id="bad_root_target",
                    name="错误根",
                    provider_kind="local",
                    target_kind="local",
                    categories={"movie": _local_category(root=root)},
                )

    def test_local_target_valid_under_storage_targets(self) -> None:
        target = self.service.create_target(
            destination_id="local_library",
            name="飞牛本地影视库",
            provider_kind="local",
            target_kind="local",
            categories={"movie": _local_category(relative="电影/2026")},
        )
        loc = target.categories["movie"].rclone_locator
        assert isinstance(loc, LocalLocator)
        self.assertEqual(loc.root, "/storage-targets/local-lib")
        self.assertEqual(loc.relative_path, "电影/2026")

    def test_local_mount_root_requires_high_risk_flag(self) -> None:
        # 不写任何子目录 = 写挂载根，默认拒绝。
        with self.assertRaisesRegex(TargetValidationError, "最终路径不能为空"):
            self.service.create_target(
                destination_id="local_root",
                name="根目录",
                provider_kind="local",
                target_kind="local",
                categories={"movie": _local_category(relative="")},
            )
        # 显式高风险确认后允许。
        allowed = self.service.create_target(
            destination_id="local_root_ok",
            name="根目录",
            provider_kind="local",
            target_kind="local",
            categories={"movie": _local_category(relative="")},
            allow_mount_root=True,
        )
        self.assertTrue(allowed.enabled)

    def test_provider_mount_root_rejected_unless_high_risk(self) -> None:
        with self.assertRaisesRegex(TargetValidationError, "最终路径不能为空"):
            self.service.create_target(
                destination_id="root_target",
                name="根目录",
                provider_kind="quark",
                target_kind="provider",
                categories={"movie": _provider_category(final_path="")},
            )
        ok = self.service.create_target(
            destination_id="root_target_ok",
            name="根目录",
            provider_kind="quark",
            target_kind="provider",
            categories={"movie": _provider_category(final_path="")},
            allow_mount_root=True,
        )
        self.assertEqual(ok.current_revision, 1)

    # --------------------------------------------------- openlist_generic

    def test_openlist_generic_requires_mount_locator(self) -> None:
        # 只有 native 定位器：没有挂载定位器，拒绝。
        with self.assertRaisesRegex(TargetValidationError, "引用现有挂载"):
            self.service.create_target(
                destination_id="generic_bad",
                name="通用盘",
                provider_kind="generic",
                target_kind="openlist_generic",
                categories={
                    "movie": TargetCategory(
                        category_key="movie",
                        public_label="通用库",
                        native_locator=NativeLocator(kind="folder_id", value="abc"),
                    )
                },
            )

    def test_openlist_generic_rejects_unknown_mount(self) -> None:
        with self.assertRaisesRegex(TargetValidationError, "引用不存在的挂载"):
            self.service.create_target(
                destination_id="generic_unknown",
                name="通用盘",
                provider_kind="generic",
                target_kind="openlist_generic",
                categories={"movie": _openlist_generic_category(mount_id="mount_missing")},
            )

    def test_provider_target_rejects_empty_mount_id(self) -> None:
        with self.assertRaisesRegex(TargetValidationError, "缺少 connection_id 或 mount_id"):
            self.service.create_target(
                destination_id="empty_mount",
                name="空挂载",
                provider_kind="quark",
                target_kind="provider",
                categories={
                    "movie": TargetCategory(
                        category_key="movie",
                        public_label="库",
                        rclone_locator=RcloneLocator(connection_id="", mount_id="", relative_path="影视/电影"),
                    )
                },
            )

    def test_openlist_generic_with_existing_mount_ok(self) -> None:
        target = self.service.create_target(
            destination_id="generic_ok",
            name="通用盘",
            provider_kind="generic",
            target_kind="openlist_generic",
            categories={"movie": _openlist_generic_category()},
        )
        self.assertEqual(target.provider_kind, "generic")
        self.assertEqual(target.target_kind, "openlist_generic")

    def test_openlist_generic_organizer_locator_satisfies_mount(self) -> None:
        # 任务允许「rclone 或 OpenList 定位器」引用现有挂载。
        target = self.service.create_target(
            destination_id="generic_organizer",
            name="通用盘",
            provider_kind="generic",
            target_kind="openlist_generic",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="通用库",
                    organizer_locator=OpenListLocator(
                        connection_id="ol_main", mount_id="mount_generic", path="/下载"
                    ),
                )
            },
        )
        self.assertEqual(target.current_revision, 1)

    # ------------------------------------------------------ test / disable

    def test_test_target_reports_per_category_status(self) -> None:
        self._create_quark_target()
        result = self.service.test_target("quark_library")
        self.assertTrue(result["ok"])
        self.assertTrue(result["categories"]["movie"]["has_rclone"])
        self.assertEqual(result["categories"]["movie"]["final_path"], "影视/电影")

    def test_disable_is_idempotent(self) -> None:
        self._create_quark_target()
        disabled = self.service.disable_target("quark_library")
        self.assertFalse(disabled.enabled)
        again = self.service.disable_target("quark_library")
        self.assertFalse(again.enabled)


class StorageTargetReferenceProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = ROOT / f"test-storage-ref-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        _insert_mount(self.database, "mount_quark")
        self.target_service = StorageTargetService(self.database)
        self.rules_service = DestinationRulesService(self.database, self.target_service)
        self.target_service.create_target(
            destination_id="quark_library",
            name="夸克影视库",
            provider_kind="quark",
            target_kind="provider",
            categories={"movie": _provider_category()},
        )

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_delete_blocked_by_rule_reference_but_disable_allowed(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            is_default=True,
            guest_allowed=True,
        )
        # 禁用始终允许（引用保护场景下的推荐操作）。
        disabled = self.target_service.disable_target("quark_library")
        self.assertFalse(disabled.enabled)
        # 物理删除被规则引用拒绝。
        with self.assertRaisesRegex(TargetInUseError, "规则引用"):
            self.target_service.delete_target("quark_library")

        # 解除引用后可以删除。
        self.rules_service.delete_rule(self.rules_service.list_rules()[0].rule_id)
        self.assertTrue(self.target_service.delete_target("quark_library"))
        self.assertIsNone(self.target_service.get_target("quark_library"))

    def test_delete_blocked_by_job_reference(self) -> None:
        self.database.create_job(
            {
                "title": "引用目标的旧任务",
                "category": "movie",
                "category_label": "电影",
                "source_type": "share",
                "source_url": f"https://example.test/share/{uuid.uuid4().hex}",
                "target_route": "quark_save",
                "status": "done",
                "destination_id": "quark_library",
                "plan_version": 4,
            }
        )
        with self.assertRaisesRegex(TargetInUseError, "任务引用"):
            self.target_service.delete_target("quark_library")
        # 禁用不受影响。
        self.target_service.disable_target("quark_library")


class DestinationRulesServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = ROOT / f"test-rules-v1-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        for mount_id in ("mount_quark", "mount_mobile", "mount_tianyi", "mount_sixpan"):
            _insert_mount(self.database, mount_id)
        self.target_service = StorageTargetService(self.database)
        self.rules_service = DestinationRulesService(self.database, self.target_service)

        def _provider_target(destination_id: str, provider_kind: str) -> None:
            self.target_service.create_target(
                destination_id=destination_id,
                name=destination_id,
                provider_kind=provider_kind,
                target_kind="provider",
                categories={
                    "movie": TargetCategory(
                        category_key="movie",
                        public_label="库",
                        rclone_locator=RcloneLocator(
                            connection_id="ol_main", mount_id=f"mount_{provider_kind}", relative_path="影视/电影"
                        ),
                    )
                },
            )

        _provider_target("quark_library", "quark")
        _provider_target("mobile_library", "mobile")
        _provider_target("tianyi_library", "tianyi")
        self.target_service.create_target(
            destination_id="sixpan_offline",
            name="六盘离线区",
            provider_kind="sixpan",
            target_kind="provider",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="离线区",
                    native_locator=NativeLocator(kind="provider_path", value="离线下载"),
                )
            },
        )

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_magnet_rule_must_target_sixpan(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="sixpan",
            source_form="magnet",
            category_key="movie",
            destination_id="sixpan_offline",
            is_default=True,
            guest_allowed=True,
        )
        # 磁链指向非六盘目标被拒绝。
        with self.assertRaisesRegex(RuleValidationError, "磁链只允许保存到六盘"):
            self.rules_service.create_rule(
                source_provider_kind="quark",
                source_form="magnet",
                category_key="movie",
                destination_id="quark_library",
            )

    def test_rule_requires_existing_destination(self) -> None:
        with self.assertRaises(TargetNotFoundError):
            self.rules_service.create_rule(
                source_provider_kind="quark",
                source_form="share",
                category_key="movie",
                destination_id="missing_target",
            )

    def test_default_uniqueness_per_source_category_audience(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            is_default=True,
            guest_allowed=True,
            admin_allowed=True,
        )
        # 同一 (quark/share/movie) 的第二个访客默认被拒绝。
        with self.assertRaisesRegex(RuleValidationError, "访客默认目标已存在"):
            self.rules_service.create_rule(
                source_provider_kind="quark",
                source_form="share",
                category_key="movie",
                destination_id="tianyi_library",
                is_default=True,
                guest_allowed=True,
            )
        # 不同分类的默认互不冲突。
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="tv",
            destination_id="tianyi_library",
            is_default=True,
            guest_allowed=True,
        )

    def test_admin_and_guest_default_can_differ_for_same_source(self) -> None:
        # 同一来源同一分类：访客默认与管理员默认是两个不同的默认（受众维度不同）。
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            is_default=True,
            guest_allowed=True,
            admin_allowed=False,
        )
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="mobile_library",
            is_default=True,
            guest_allowed=False,
            admin_allowed=True,
        )
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "movie", "guest"),
            "quark_library",
        )
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "movie", "admin"),
            "mobile_library",
        )

    def test_exact_category_default_wins_over_wildcard(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="*",
            destination_id="mobile_library",
            is_default=True,
            guest_allowed=True,
        )
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            is_default=True,
            guest_allowed=True,
        )
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "movie", "guest"),
            "quark_library",
        )
        # 未配置精确默认的分类回退到 Provider 级默认。
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "tv", "guest"),
            "mobile_library",
        )

    def test_resolve_destination_admin_assigned_or_none(self) -> None:
        # 无规则 -> None。
        self.assertIsNone(self.rules_service.resolve_destination("quark", "share", "movie", "guest"))
        # 只有非默认规则 -> None（管理员未分配默认）。
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            guest_allowed=True,
        )
        self.assertIsNone(self.rules_service.resolve_destination("quark", "share", "movie", "guest"))
        # 默认规则被禁用后 -> None。
        rule = self.rules_service.list_rules()[0]
        self.rules_service.update_rule(rule.rule_id, is_default=True)
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "movie", "guest"),
            "quark_library",
        )
        self.rules_service.update_rule(rule.rule_id, enabled=False)
        self.assertIsNone(self.rules_service.resolve_destination("quark", "share", "movie", "guest"))

    def test_guest_resolution_ignores_admin_only_rules(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="mobile_library",
            is_default=True,
            guest_allowed=False,
            admin_allowed=True,
        )
        self.assertIsNone(self.rules_service.resolve_destination("quark", "share", "movie", "guest"))
        self.assertEqual(
            self.rules_service.resolve_destination("quark", "share", "movie", "admin"),
            "mobile_library",
        )

    def test_put_rules_replaces_whole_set(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
        )
        self.rules_service.put_rules(
            [
                {
                    "source_provider_kind": "quark",
                    "source_form": "share",
                    "category_key": "movie",
                    "destination_id": "tianyi_library",
                    "guest_allowed": True,
                    "is_default": True,
                }
            ]
        )
        rules = self.rules_service.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].destination_id, "tianyi_library")

    def test_put_rules_rejects_magnet_to_non_sixpan(self) -> None:
        with self.assertRaises(RuleValidationError):
            self.rules_service.put_rules(
                [
                    {
                        "source_provider_kind": "quark",
                        "source_form": "magnet",
                        "category_key": "movie",
                        "destination_id": "quark_library",
                    }
                ]
            )

    def test_transfer_matrix_built_from_rules(self) -> None:
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
            is_default=True,
            guest_allowed=True,
        )
        matrix = self.rules_service.transfer_matrix()
        self.assertTrue(matrix["success"])
        self.assertEqual(matrix["categories"], ["movie", "tv", "anime", "variety", "other"])
        row = matrix["rows"][0]
        self.assertEqual(row["source_provider_kind"], "quark")
        self.assertEqual(row["guest_destinations"], ["quark_library"])
        self.assertEqual(row["defaults"]["movie"], "quark_library")
        self.assertIsNone(row["defaults"]["tv"])


class StorageBlueprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = ROOT / f"test-storage-api-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        _insert_mount(self.database, "mount_quark")
        self.target_service = StorageTargetService(self.database)
        self.rules_service = DestinationRulesService(self.database, self.target_service)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _build_app(self, *, deny_admin: bool = False) -> Flask:
        app = Flask(__name__)

        def protected(handler: Callable[..., Any]) -> Callable[..., Any]:
            if deny_admin:

                def denied(*_args: Any, **_kwargs: Any) -> Any:
                    return jsonify({"success": False}), 401

                return denied

            return handler

        app.register_blueprint(
            create_storage_blueprint(
                StorageBlueprintContext(
                    admin_required=protected,
                    target_service=self.target_service,
                    rules_service=self.rules_service,
                )
            )
        )
        return app

    def _category_payload(self) -> dict[str, Any]:
        return {
            "movie": {
                "public_label": "电影库",
                "rclone_locator": {
                    "kind": "rclone",
                    "connection_id": "ol_main",
                    "mount_id": "mount_quark",
                    "relative_path": "影视/电影",
                },
            }
        }

    def test_admin_routes_are_protected(self) -> None:
        app = self._build_app(deny_admin=True)
        client = app.test_client()
        self.assertEqual(client.get("/api/admin/storage-targets").status_code, 401)
        self.assertEqual(client.post("/api/admin/storage-targets", json={}).status_code, 401)
        self.assertEqual(client.patch("/api/admin/storage-targets/x", json={}).status_code, 401)
        self.assertEqual(client.delete("/api/admin/storage-targets/x").status_code, 401)
        self.assertEqual(client.get("/api/admin/destination-rules").status_code, 401)
        self.assertEqual(client.put("/api/admin/destination-rules", json={}).status_code, 401)
        self.assertEqual(client.get("/api/admin/transfer-matrix").status_code, 401)

    def test_create_list_and_get_target_via_api(self) -> None:
        app = self._build_app()
        client = app.test_client()
        response = client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "target_kind": "provider",
                "audience": "guest",
                "categories": self._category_payload(),
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["target"]["current_revision"], 1)
        # 响应不泄露任何密钥/凭证字段。
        raw = response.get_data(as_text=True)
        self.assertNotIn("password", raw.lower())
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("cookie", raw.lower())

        listed = client.get("/api/admin/storage-targets").get_json()
        self.assertEqual([t["destination_id"] for t in listed["targets"]], ["quark_library"])

        detail = client.get("/api/admin/storage-targets/quark_library").get_json()
        self.assertEqual(detail["target"]["provider_kind"], "quark")
        self.assertEqual([r["revision"] for r in detail["revisions"]], [1])

    def test_create_target_rejects_invalid_payload(self) -> None:
        app = self._build_app()
        client = app.test_client()
        response = client.post(
            "/api/admin/storage-targets",
            json={"destination_id": "Bad ID", "name": "x", "provider_kind": "quark", "categories": self._category_payload()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "TARGET_INVALID")

    def test_revise_target_via_api_bumps_revision(self) -> None:
        app = self._build_app()
        client = app.test_client()
        client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "categories": self._category_payload(),
            },
        )
        revised = client.patch(
            "/api/admin/storage-targets/quark_library",
            json={"name": "夸克影视库V2", "categories": self._category_payload()},
        )
        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.get_json()["target"]["current_revision"], 2)
        self.assertEqual(revised.get_json()["target"]["name"], "夸克影视库V2")

    def test_disable_and_test_via_api(self) -> None:
        app = self._build_app()
        client = app.test_client()
        client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "categories": self._category_payload(),
            },
        )
        tested = client.post("/api/admin/storage-targets/quark_library/test").get_json()
        self.assertTrue(tested["ok"])
        disabled = client.post("/api/admin/storage-targets/quark_library/disable").get_json()
        self.assertFalse(disabled["target"]["enabled"])

    def test_delete_returns_in_use_then_succeeds(self) -> None:
        app = self._build_app()
        client = app.test_client()
        client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "categories": self._category_payload(),
            },
        )
        self.rules_service.create_rule(
            source_provider_kind="quark",
            source_form="share",
            category_key="movie",
            destination_id="quark_library",
        )
        blocked = client.delete("/api/admin/storage-targets/quark_library")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["code"], "TARGET_IN_USE")
        self.rules_service.delete_rule(self.rules_service.list_rules()[0].rule_id)
        deleted = client.delete("/api/admin/storage-targets/quark_library")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["success"])

    def test_rules_put_and_transfer_matrix_via_api(self) -> None:
        app = self._build_app()
        client = app.test_client()
        client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "categories": self._category_payload(),
            },
        )
        put = client.put(
            "/api/admin/destination-rules",
            json={
                "rules": [
                    {
                        "source_provider_kind": "quark",
                        "source_form": "share",
                        "category_key": "movie",
                        "destination_id": "quark_library",
                        "guest_allowed": True,
                        "is_default": True,
                    }
                ]
            },
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual(len(put.get_json()["rules"]), 1)

        got = client.get("/api/admin/destination-rules").get_json()
        self.assertEqual(got["rules"][0]["destination_id"], "quark_library")

        matrix = client.get("/api/admin/transfer-matrix").get_json()
        self.assertTrue(matrix["success"])
        self.assertEqual(matrix["rows"][0]["defaults"]["movie"], "quark_library")

    def test_rules_put_rejects_magnet_to_quark(self) -> None:
        app = self._build_app()
        client = app.test_client()
        client.post(
            "/api/admin/storage-targets",
            json={
                "destination_id": "quark_library",
                "name": "夸克影视库",
                "provider_kind": "quark",
                "categories": self._category_payload(),
            },
        )
        response = client.put(
            "/api/admin/destination-rules",
            json={
                "rules": [
                    {
                        "source_provider_kind": "quark",
                        "source_form": "magnet",
                        "category_key": "movie",
                        "destination_id": "quark_library",
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("磁链", response.get_json()["message"])


class TargetSerializationTests(unittest.TestCase):
    def test_categories_round_trip_via_json(self) -> None:
        from fnos_media_import.storage.target_service import decode_categories, encode_categories

        categories = {"movie": _provider_category()}
        encoded = encode_categories(categories)
        text = json.dumps(encoded)
        decoded = decode_categories(json.loads(text))
        self.assertEqual(decoded["movie"].public_label, "电影库")
        self.assertIsInstance(decoded["movie"].rclone_locator, RcloneLocator)


if __name__ == "__main__":
    unittest.main()
