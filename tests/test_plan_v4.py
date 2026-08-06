"""Import Planner v4 + 校验器契约测试（§12.2 能力矩阵 / §12.4 计划校验）。

覆盖：
- §11 能力矩阵每个单元格选择唯一正确策略。
- 磁链只允许六盘 native_offline，其它目标一律 MAGNET_TARGET_RESTRICTED。
- 同源强制原生，天翼 native 未实现时 NATIVE_TRANSFER_UNAVAILABLE，不降级 rclone。
- relay_upload_mobile 只允许非移动来源 -> 移动目标。
- rclone_copy 需要来源落地区可读 + 目标定位器可写。
- 校验器拒绝伪造计划：版本/destination 不一致、namespace 重叠、本地越界、
  native_save 不同 Provider、磁链非六盘、relay 非移动、目标不可写。
- SourceResolver 不信任客户端 provider 字段。
"""

from __future__ import annotations

import copy
import json
import unittest
import uuid
from pathlib import Path

from fnos_media_import.constants import JOB_CREATED
from fnos_media_import.database import Database
from fnos_media_import.domain.models import (
    AccountSelection,
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    OpenListMount,
    ProviderAccount,
    ProviderCapabilities,
    RcloneLocator,
    SourceDescriptor,
    StorageTarget,
    TargetCategory,
)
from fnos_media_import.planning import (
    STRATEGY_NATIVE_OFFLINE,
    STRATEGY_NATIVE_SAVE,
    STRATEGY_RCLONE_COPY,
    STRATEGY_RELAY_UPLOAD_MOBILE,
    DestinationAccountUnavailable,
    DestinationResolver,
    MagnetTargetRestricted,
    NativeTransferUnavailable,
    PlanValidationDeps,
    PlanValidationError,
    PlanningError,
    SourceLandingUnavailable,
    SourceResolver,
    validate_v4_plan,
)
from fnos_media_import.planning.planner import ImportPlannerV4


# ---------------------------------------------------------------------------
# Fakes：真实服务（账号/目标/规则/OpenList/Adapter）后续按相同接口接入。
# ---------------------------------------------------------------------------


class FakeAccountsService:
    def __init__(self) -> None:
        self.accounts: dict[str, ProviderAccount] = {}
        self.selections: dict[tuple[str, str], AccountSelection] = {}
        self.revisions: set[tuple[str, int]] = set()

    def add_account(self, *, provider_kind: str, account_id: str, revision: int, enabled: bool = True) -> ProviderAccount:
        account = ProviderAccount(
            account_id=account_id,
            provider_kind=provider_kind,
            name=account_id,
            enabled=enabled,
            current_revision=revision,
            identity_hint="",
            last_test_status="ok",
            last_test_message="",
            last_tested_at=None,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.accounts[account_id] = account
        self.revisions.add((account_id, revision))
        return account

    def add_selection(
        self, *, provider_kind: str, account_id: str, revision: int, scope: str = "default", selection_revision: int = 1
    ) -> AccountSelection:
        selection = AccountSelection(
            provider_kind=provider_kind,
            scope=scope,
            account_id=account_id,
            account_revision=revision,
            selection_revision=selection_revision,
        )
        self.selections[(provider_kind, scope)] = selection
        return selection

    def get_selection(self, provider_kind: str, scope: str = "default") -> AccountSelection | None:
        return self.selections.get((provider_kind, scope))

    def get_account(self, account_id: str) -> ProviderAccount | None:
        return self.accounts.get(account_id)

    def has_revision(self, account_id: str, revision: int) -> bool:
        return (account_id, revision) in self.revisions


class FakeTargetService:
    def __init__(self) -> None:
        self.targets: dict[str, StorageTarget] = {}

    def add(self, target: StorageTarget) -> None:
        self.targets[target.destination_id] = target

    def get(self, destination_id: str) -> StorageTarget | None:
        return self.targets.get(destination_id)

    def get_revision(self, destination_id: str, revision: int) -> StorageTarget | None:
        target = self.targets.get(destination_id)
        if target is not None and target.current_revision == revision:
            return target
        return None


class FakeRulesService:
    def __init__(self) -> None:
        # (source_provider_kind, source_form, category_key, destination_id, audience, is_default, enabled)
        self.rules: list[tuple[str, str, str, str, str, bool, bool]] = []

    def add(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        destination_id: str,
        audience: str = "guest",
        is_default: bool = False,
        enabled: bool = True,
    ) -> None:
        self.rules.append(
            (source_provider_kind, source_form, category_key, destination_id, audience, is_default, enabled)
        )

    def resolve_default(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        audience: str,
    ) -> str | None:
        for kind, form, cat, dest, aud, default, enabled in self.rules:
            if not default or not enabled or aud != audience:
                continue
            if kind != source_provider_kind or form != source_form:
                continue
            if cat == category_key or cat == "*":
                return dest
        return None

    def is_allowed(
        self,
        *,
        source_provider_kind: str,
        source_form: str,
        category_key: str,
        destination_id: str,
        audience: str,
    ) -> bool:
        for kind, form, cat, dest, aud, _default, enabled in self.rules:
            if not enabled or aud != audience:
                continue
            if kind != source_provider_kind or form != source_form or dest != destination_id:
                continue
            if cat == category_key or cat == "*":
                return True
        return False


class FakeMountService:
    def __init__(self) -> None:
        self.mounts: dict[str, OpenListMount] = {}

    def add(self, mount: OpenListMount) -> None:
        self.mounts[mount.mount_id] = mount

    def replace(self, mount: OpenListMount) -> None:
        self.mounts[mount.mount_id] = mount

    def get(self, mount_id: str) -> OpenListMount | None:
        return self.mounts.get(mount_id)

    def find_mount_for_account(
        self,
        provider_kind: str,
        account_id: str,
        *,
        readable: bool = True,
    ) -> OpenListMount | None:
        for mount in self.mounts.values():
            if mount.provider_kind == provider_kind and mount.account_id == account_id:
                if not readable or mount.readable:
                    return mount
        return None


class FakeAdapter:
    def __init__(
        self,
        kind: str,
        *,
        native_available: bool = True,
        native_reason: str = "",
        has_relay_uploader: bool = False,
        supports_offline: bool = False,
    ) -> None:
        self.kind = kind
        self.native_available = native_available
        self.native_reason = native_reason
        self.has_relay_uploader = has_relay_uploader
        self.supports_offline = supports_offline

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_native_save=self.native_available,
            supports_native_offline=self.supports_offline,
            supports_relay_upload=self.has_relay_uploader,
        )

    def can_native_transfer(self, source: object, destination: object, account: object) -> object:
        from fnos_media_import.planning import CapabilityDecision

        return CapabilityDecision(self.native_available, reason=self.native_reason)

    def relay_uploader(self) -> object | None:
        return object() if self.has_relay_uploader else None


class FakeRegistry:
    def __init__(self) -> None:
        self.adapters: dict[str, FakeAdapter] = {}

    def add(self, adapter: FakeAdapter) -> None:
        self.adapters[adapter.kind] = adapter

    def get(self, provider_kind: str) -> FakeAdapter:
        try:
            return self.adapters[provider_kind]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {provider_kind}") from exc


# ---------------------------------------------------------------------------
# 测试环境工厂
# ---------------------------------------------------------------------------


def _mount(mount_id: str, connection_id: str, provider_kind: str, account_id: str, *, readable: bool, writable: bool, revision: int) -> OpenListMount:
    return OpenListMount(
        mount_id=mount_id,
        connection_id=connection_id,
        name=mount_id,
        provider_kind=provider_kind,
        account_id=account_id,
        webdav_root=f"/{mount_id}",
        namespace_id=f"openlist:{connection_id}:{mount_id}",
        readable=readable,
        writable=writable,
        enabled=True,
        revision=revision,
        identity_status="verified",
    )


def _target(destination_id: str, provider_kind: str, *, native: str, rclone: tuple[str, str, str]) -> StorageTarget:
    native_locator = NativeLocator(kind="folder_id" if provider_kind == "quark" else "provider_path", value=native)
    rclone_locator = RcloneLocator(connection_id=rclone[0], mount_id=rclone[1], relative_path=rclone[2])
    category = TargetCategory(
        category_key="movie",
        public_label="电影库",
        native_locator=native_locator,
        rclone_locator=rclone_locator,
        organizer_locator=OpenListLocator(connection_id=rclone[0], mount_id=rclone[1], path=rclone[2]),
    )
    return StorageTarget(
        destination_id=destination_id,
        name=destination_id,
        provider_kind=provider_kind,
        account_scope="default",
        target_kind="provider",
        enabled=True,
        audience="guest",
        current_revision=1,
        categories={"movie": category},
    )


def _target_local(destination_id: str, root: str, relative: str) -> StorageTarget:
    category = TargetCategory(
        category_key="movie",
        public_label="电影库",
        native_locator=None,
        rclone_locator=LocalLocator(root=root, relative_path=relative),
        organizer_locator=None,
    )
    return StorageTarget(
        destination_id=destination_id,
        name=destination_id,
        provider_kind="local",
        account_scope=None,
        target_kind="local",
        enabled=True,
        audience="guest",
        current_revision=1,
        categories={"movie": category},
    )


class PlanEnvironment:
    def __init__(self) -> None:
        self.accounts = FakeAccountsService()
        self.targets = FakeTargetService()
        self.rules = FakeRulesService()
        self.mounts = FakeMountService()
        self.registry = FakeRegistry()
        self.config = {
            "FNOS_LOCAL_TARGET_ROOT": "/storage-targets",
            "FNOS_RELAY_ROOT": "/data/relay",
        }

        self.accounts.add_account(provider_kind="mobile", account_id="acc_mobile_a", revision=3)
        self.accounts.add_selection(provider_kind="mobile", account_id="acc_mobile_a", revision=3, selection_revision=1)
        self.accounts.add_account(provider_kind="quark", account_id="acc_quark_a", revision=7)
        self.accounts.add_selection(provider_kind="quark", account_id="acc_quark_a", revision=7, selection_revision=3)
        self.accounts.add_account(provider_kind="tianyi", account_id="acc_tianyi_a", revision=4)
        self.accounts.add_selection(provider_kind="tianyi", account_id="acc_tianyi_a", revision=4, selection_revision=2)
        self.accounts.add_account(provider_kind="sixpan", account_id="acc_sixpan_a", revision=2)
        self.accounts.add_selection(provider_kind="sixpan", account_id="acc_sixpan_a", revision=2, selection_revision=1)

        self.mounts.add(_mount("mount_mobile_main", "ol_main", "mobile", "acc_mobile_a", readable=True, writable=True, revision=5))
        self.mounts.add(_mount("mount_quark_main", "ol_main", "quark", "acc_quark_a", readable=True, writable=True, revision=8))
        self.mounts.add(_mount("mount_tianyi_main", "ol_main", "tianyi", "acc_tianyi_a", readable=True, writable=True, revision=6))
        self.mounts.add(_mount("mount_sixpan_main", "ol_main", "sixpan", "acc_sixpan_a", readable=True, writable=True, revision=4))

        self.targets.add(_target("mobile_main", "mobile", native="/移动云/影视库", rclone=("ol_main", "mount_mobile_main", "/影视/电影")))
        self.targets.add(_target("quark_library", "quark", native="folder-123", rclone=("ol_main", "mount_quark_main", "/影视/电影")))
        self.targets.add(_target("tianyi_library", "tianyi", native="/归档/影视库", rclone=("ol_main", "mount_tianyi_main", "/影视/电影")))
        self.targets.add(_target("sixpan_offline", "sixpan", native="/电影", rclone=("ol_main", "mount_sixpan_main", "/电影")))
        self.targets.add(_target_local("local_library", "/storage-targets/local-library", "Movies"))

        self.registry.add(FakeAdapter("mobile", native_available=True, has_relay_uploader=True))
        self.registry.add(FakeAdapter("quark", native_available=True))
        self.registry.add(FakeAdapter("tianyi", native_available=True))
        self.registry.add(FakeAdapter("sixpan", native_available=False, supports_offline=True))

        for source_kind, source_form in [("mobile", "share"), ("quark", "share"), ("tianyi", "share")]:
            for destination_id in ["mobile_main", "quark_library", "tianyi_library", "sixpan_offline", "local_library"]:
                self.rules.add(
                    source_provider_kind=source_kind,
                    source_form=source_form,
                    category_key="*",
                    destination_id=destination_id,
                )
        self.rules.add(source_provider_kind="sixpan", source_form="magnet", category_key="*", destination_id="sixpan_offline")

        self.rules.add(source_provider_kind="mobile", source_form="share", category_key="movie", destination_id="mobile_main", is_default=True)
        self.rules.add(source_provider_kind="quark", source_form="share", category_key="movie", destination_id="quark_library", is_default=True)
        self.rules.add(source_provider_kind="tianyi", source_form="share", category_key="movie", destination_id="tianyi_library", is_default=True)
        self.rules.add(source_provider_kind="sixpan", source_form="magnet", category_key="movie", destination_id="sixpan_offline", is_default=True)

        self.planner = ImportPlannerV4(
            accounts_service=self.accounts,
            target_service=self.targets,
            rules_service=self.rules,
            mount_service=self.mounts,
            registry=self.registry,
            config=self.config,
        )
        self.deps = PlanValidationDeps(
            accounts_service=self.accounts,
            target_service=self.targets,
            mount_service=self.mounts,
            registry=self.registry,
            config=self.config,
        )


def _source(provider_kind: str, source_form: str, suffix: str = "") -> SourceDescriptor:
    return SourceDescriptor(
        provider_kind=provider_kind,
        source_form=source_form,
        canonical_source_id=f"{provider_kind}:{source_form}:{suffix or uuid.uuid4().hex[:8]}",
        selected_items_hash="",
    )


# ---------------------------------------------------------------------------
# SourceResolver
# ---------------------------------------------------------------------------


class _LegacyLink:
    def __init__(self, source_type: str, url: str) -> None:
        self.source_type = source_type
        self.url = url
        self.password = ""
        self.supported = True
        self.route = "legacy"
        self.reason = ""


class SourceResolverTests(unittest.TestCase):
    def test_ignores_client_provider_fields(self) -> None:
        resolver = SourceResolver(lambda url: {"provider_kind": "quark", "source_form": "share"})
        source = resolver.resolve(
            {
                "url": "https://pan.quark.cn/s/abc123",
                "provider_kind": "evil",
                "account_id": "acc_hacked",
                "mount_id": "mount_hacked",
                "target_path": "/tmp/x",
            }
        )
        self.assertEqual(source.provider_kind, "quark")
        self.assertEqual(source.source_form, "share")
        self.assertTrue(source.canonical_source_id.startswith("quark:share:"))

    def test_legacy_link_classification_is_translated(self) -> None:
        resolver = SourceResolver(lambda url: _LegacyLink(source_type="quark", url=url))
        source = resolver.resolve({"url": "https://pan.quark.cn/s/abc123"})
        self.assertEqual(source.provider_kind, "quark")
        self.assertEqual(source.source_form, "share")

    def test_magnet_maps_to_sixpan_offline(self) -> None:
        resolver = SourceResolver(lambda url: _LegacyLink(source_type="magnet", url=url))
        source = resolver.resolve({"url": "magnet:?xt=urn:btih:ABCDEF1234567890"})
        self.assertEqual(source.provider_kind, "sixpan")
        self.assertEqual(source.source_form, "magnet")
        self.assertTrue(source.canonical_source_id.startswith("sixpan:magnet:"))

    def test_selected_items_hash(self) -> None:
        resolver = SourceResolver(lambda url: {"provider_kind": "quark", "source_form": "share"})
        with_items = resolver.resolve(
            {"url": "https://pan.quark.cn/s/abc", "selected_items": [{"fid": "1"}, {"fid": "2"}]}
        )
        self.assertTrue(with_items.selected_items_hash.startswith("sha256:"))
        empty = resolver.resolve({"url": "https://pan.quark.cn/s/abc"})
        self.assertEqual(empty.selected_items_hash, "")

    def test_unknown_provider_rejected(self) -> None:
        resolver = SourceResolver(lambda url: {"provider_kind": "aliyun", "source_form": "share"})
        with self.assertRaises(PlanningError):
            resolver.resolve({"url": "https://www.aliyundrive.com/s/abc"})

    def test_missing_url_rejected(self) -> None:
        resolver = SourceResolver(lambda url: {"provider_kind": "quark", "source_form": "share"})
        with self.assertRaises(PlanningError):
            resolver.resolve({"title": "no url"})


# ---------------------------------------------------------------------------
# DestinationResolver
# ---------------------------------------------------------------------------


class DestinationResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = PlanEnvironment()
        self.resolver = DestinationResolver(self.env.rules, self.env.targets, self.env.accounts)

    def test_resolve_default_for_share(self) -> None:
        descriptor = self.resolver.resolve(_source("quark", "share"), "movie", audience="guest")
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.destination_id, "quark_library")
        self.assertEqual(descriptor.target_revision, 1)
        self.assertEqual(descriptor.provider_kind, "quark")

    def test_resolve_default_for_magnet(self) -> None:
        descriptor = self.resolver.resolve(_source("sixpan", "magnet"), "movie", audience="guest")
        self.assertEqual(descriptor.destination_id, "sixpan_offline")

    def test_resolve_none_when_no_rule(self) -> None:
        self.assertIsNone(self.resolver.resolve(_source("quark", "share"), "anime", audience="guest"))

    def test_resolve_blocks_when_target_disabled(self) -> None:
        original = self.env.targets.get("quark_library")
        disabled = StorageTarget(
            destination_id=original.destination_id,
            name=original.name,
            provider_kind=original.provider_kind,
            account_scope=original.account_scope,
            target_kind=original.target_kind,
            enabled=False,
            audience=original.audience,
            current_revision=original.current_revision,
            categories=original.categories,
        )
        self.env.targets.add(disabled)
        self.assertIsNone(self.resolver.resolve(_source("quark", "share"), "movie", audience="guest"))

    def test_resolve_account_returns_selection_tuple(self) -> None:
        account_id, revision, selection_revision = self.resolver.resolve_account("quark", role="source")
        self.assertEqual((account_id, revision, selection_revision), ("acc_quark_a", 7, 3))

    def test_resolve_account_missing_selection_raises(self) -> None:
        with self.assertRaises(PlanningError):
            self.resolver.resolve_account("115", role="destination")


# ---------------------------------------------------------------------------
# Planner 能力矩阵
# ---------------------------------------------------------------------------


class MatrixStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = PlanEnvironment()

    def _plan(self, provider_kind: str, source_form: str, destination_id: str, job_id: int) -> dict:
        return self.env.planner.plan_import(
            source=_source(provider_kind, source_form, suffix=f"job-{job_id}"),
            category_key="movie",
            job_id=job_id,
            destination_id=destination_id,
            audience="guest",
            organizer_enabled=False,
        )

    def test_every_matrix_cell_chooses_correct_strategy(self) -> None:
        cases = [
            ("mobile", "share", "mobile_main", STRATEGY_NATIVE_SAVE),
            ("mobile", "share", "quark_library", STRATEGY_RCLONE_COPY),
            ("mobile", "share", "tianyi_library", STRATEGY_RCLONE_COPY),
            ("mobile", "share", "sixpan_offline", STRATEGY_RCLONE_COPY),
            ("mobile", "share", "local_library", STRATEGY_RCLONE_COPY),
            ("quark", "share", "mobile_main", STRATEGY_RELAY_UPLOAD_MOBILE),
            ("quark", "share", "quark_library", STRATEGY_NATIVE_SAVE),
            ("quark", "share", "tianyi_library", STRATEGY_RCLONE_COPY),
            ("quark", "share", "sixpan_offline", STRATEGY_RCLONE_COPY),
            ("quark", "share", "local_library", STRATEGY_RCLONE_COPY),
            ("tianyi", "share", "mobile_main", STRATEGY_RELAY_UPLOAD_MOBILE),
            ("tianyi", "share", "quark_library", STRATEGY_RCLONE_COPY),
            ("tianyi", "share", "tianyi_library", STRATEGY_NATIVE_SAVE),
            ("tianyi", "share", "sixpan_offline", STRATEGY_RCLONE_COPY),
            ("tianyi", "share", "local_library", STRATEGY_RCLONE_COPY),
            ("sixpan", "magnet", "sixpan_offline", STRATEGY_NATIVE_OFFLINE),
        ]
        expected_executor = {
            STRATEGY_NATIVE_SAVE: "native",
            STRATEGY_NATIVE_OFFLINE: "native_offline",
            STRATEGY_RCLONE_COPY: "rclone",
            STRATEGY_RELAY_UPLOAD_MOBILE: "mobile_relay",
        }
        for index, (provider, form, dest, expected) in enumerate(cases, start=1):
            with self.subTest(provider=provider, form=form, dest=dest):
                plan = self._plan(provider, form, dest, job_id=300 + index)
                self.assertEqual(plan["version"], 4)
                self.assertEqual(plan["transfer"]["strategy"], expected)
                self.assertEqual(plan["transfer"]["executor"], expected_executor[expected])

    def test_magnet_to_any_non_sixpan_is_blocked(self) -> None:
        for destination_id in ["mobile_main", "quark_library", "tianyi_library", "local_library"]:
            with self.subTest(destination_id=destination_id):
                with self.assertRaises(MagnetTargetRestricted):
                    self.env.planner.plan_import(
                        source=_source("sixpan", "magnet"),
                        category_key="movie",
                        job_id=401,
                        destination_id=destination_id,
                    )

    def test_rclone_copy_paths_are_job_scoped(self) -> None:
        plan = self._plan("quark", "share", "tianyi_library", job_id=420)
        self.assertEqual(plan["paths"]["source_landing"]["path"], "/_fnos_ingress/job-420")
        self.assertEqual(plan["paths"]["destination_staging"]["path"], "/_fnos_staging/movie/job-420")
        self.assertEqual(plan["paths"]["source_landing"]["namespace_id"], "openlist:ol_main:mount_quark_main")
        self.assertEqual(plan["paths"]["destination_staging"]["namespace_id"], "openlist:ol_main:mount_tianyi_main")
        self.assertIsNone(plan["paths"]["local_temp"])

    def test_relay_plan_has_local_temp_and_native_final(self) -> None:
        plan = self._plan("quark", "share", "mobile_main", job_id=421)
        self.assertEqual(plan["paths"]["local_temp"]["path"], "/data/relay/job-421")
        self.assertEqual(plan["paths"]["destination_final"]["namespace_id"], "native:mobile:mobile_main")
        self.assertEqual(plan["transfer"]["executor"], "mobile_relay")

    def test_native_plan_has_native_final_and_no_source_landing(self) -> None:
        plan = self._plan("quark", "share", "quark_library", job_id=422)
        self.assertIsNone(plan["paths"]["source_landing"])
        self.assertEqual(plan["paths"]["destination_final"]["namespace_id"], "native:quark:quark_library")

    def test_plan_contains_no_credentials(self) -> None:
        plan = self._plan("quark", "share", "tianyi_library", job_id=423)
        blob = json.dumps(plan)
        for secret_word in ("password", "secret", "cookie", "access_token", "refresh_token"):
            self.assertNotIn(secret_word, blob)

    def test_tianyi_native_not_implemented_never_falls_back(self) -> None:
        self.env.registry.adapters["tianyi"] = FakeAdapter(
            "tianyi", native_available=False, native_reason="not implemented"
        )
        with self.assertRaisesRegex(NativeTransferUnavailable, "同源原生"):
            self.env.planner.plan_import(
                source=_source("tianyi", "share"),
                category_key="movie",
                job_id=424,
                destination_id="tianyi_library",
            )

    def test_rclone_copy_requires_readable_source_landing(self) -> None:
        self.env.mounts.replace(
            _mount("mount_quark_main", "ol_main", "quark", "acc_quark_a", readable=False, writable=True, revision=8)
        )
        with self.assertRaisesRegex(SourceLandingUnavailable, "落地区"):
            self.env.planner.plan_import(
                source=_source("quark", "share"),
                category_key="movie",
                job_id=425,
                destination_id="tianyi_library",
            )

    def test_relay_requires_mobile_uploader(self) -> None:
        self.env.registry.adapters["mobile"] = FakeAdapter("mobile", native_available=True, has_relay_uploader=False)
        with self.assertRaisesRegex(DestinationAccountUnavailable, "中继上传器"):
            self.env.planner.plan_import(
                source=_source("quark", "share"),
                category_key="movie",
                job_id=426,
                destination_id="mobile_main",
            )

    def test_organizer_enabled_requires_locator(self) -> None:
        with self.assertRaisesRegex(PlanningError, "organizer_locator"):
            self.env.planner.plan_import(
                source=_source("quark", "share"),
                category_key="movie",
                job_id=427,
                destination_id="local_library",
                organizer_enabled=True,
            )

    def test_organizer_plan_maps_staging_job_dir(self) -> None:
        plan = self.env.planner.plan_import(
            source=_source("quark", "share", suffix="job-428"),
            category_key="movie",
            job_id=428,
            destination_id="tianyi_library",
            organizer_enabled=True,
        )
        self.assertEqual(plan["paths"]["organizer"]["namespace_id"], "openlist:ol_main:mount_tianyi_main")
        self.assertEqual(plan["paths"]["organizer"]["path"], "/_fnos_staging/movie/job-428")


# ---------------------------------------------------------------------------
# 校验器（真实临时 Database）
# ---------------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = PlanEnvironment()
        self.root = Path(__file__).resolve().parents[1]
        self.db_path = self.root / f"plan-v4-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _make_job_plan(
        self,
        provider_kind: str,
        source_form: str,
        destination_id: str,
        *,
        organizer_enabled: bool = False,
        category: str = "movie",
    ) -> tuple[dict, dict]:
        job_id, _created = self.database.job_commands.create(
            {
                "title": "测试资源",
                "category": category,
                "category_label": "电影",
                "source_type": provider_kind,
                "source_url": f"https://test.local/plan-v4-{uuid.uuid4().hex}",
                "target_route": "plan_v4",
                "target_path": "/x",
                "status": JOB_CREATED,
            }
        )
        plan = self.env.planner.plan_import(
            source=_source(provider_kind, source_form, suffix=f"job-{job_id}"),
            category_key=category,
            job_id=job_id,
            destination_id=destination_id,
            audience="guest",
            organizer_enabled=organizer_enabled,
        )
        self.database.job_commands.update(
            job_id,
            {
                "raw_data": {"execution_plan": plan},
                "destination_id": plan["destination"]["id"],
                "plan_version": plan["version"],
            },
        )
        return plan, self.database.get_job(job_id)

    def _expect_rejected(self, plan: dict, job: dict, pattern: str) -> None:
        with self.assertRaisesRegex(PlanValidationError, pattern):
            validate_v4_plan(plan, job, deps=self.env.deps)

    # ---- 合法计划通过校验 ---------------------------------------------------

    def test_valid_rclone_plan_passes(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        self.assertEqual(validate_v4_plan(plan, job, deps=self.env.deps)["version"], 4)

    def test_valid_native_plan_passes(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "quark_library")
        validate_v4_plan(plan, job, deps=self.env.deps)

    def test_valid_relay_plan_passes(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "mobile_main")
        validate_v4_plan(plan, job, deps=self.env.deps)

    def test_valid_magnet_plan_passes(self) -> None:
        plan, job = self._make_job_plan("sixpan", "magnet", "sixpan_offline")
        validate_v4_plan(plan, job, deps=self.env.deps)

    def test_valid_local_plan_passes(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "local_library")
        validate_v4_plan(plan, job, deps=self.env.deps)

    def test_valid_organizer_plan_passes(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library", organizer_enabled=True)
        validate_v4_plan(plan, job, deps=self.env.deps)

    # ---- 伪造计划一律拒绝 ---------------------------------------------------

    def test_forged_plan_version_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged = copy.deepcopy(plan)
        forged["version"] = 3
        self._expect_rejected(forged, job, "版本")

    def test_forged_job_plan_version_column_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged_job = dict(job)
        forged_job["plan_version"] = 3
        self._expect_rejected(plan, forged_job, "plan_version")

    def test_forged_job_id_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged_job = dict(job)
        forged_job["id"] = int(job["id"]) + 1000
        self._expect_rejected(plan, forged_job, "job_id")

    def test_forged_destination_id_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged_job = dict(job)
        forged_job["destination_id"] = "quark_library"
        self._expect_rejected(plan, forged_job, "destination_id")

    def test_forged_plan_destination_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged = copy.deepcopy(plan)
        forged["destination"]["id"] = "quark_library"
        self._expect_rejected(forged, job, "destination_id")

    def test_forged_namespace_overlap(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged = copy.deepcopy(plan)
        landing = forged["paths"]["source_landing"]
        # 暂存目录被改成来源 landing 同一命名空间同一路径 -> 重叠。
        forged["paths"]["destination_staging"] = dict(landing)
        forged["bindings"]["destination_mount_id"] = "mount_quark_main"
        forged["bindings"]["destination_mount_revision"] = 8
        self._expect_rejected(forged, job, "重叠")

    def test_forged_local_escape(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "local_library")
        forged = copy.deepcopy(plan)
        forged["paths"]["destination_final"]["path"] = "/etc/passwd"
        self._expect_rejected(forged, job, "受控本地")

    def test_forged_native_save_different_provider(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "quark_library")
        forged = copy.deepcopy(plan)
        forged["destination"]["provider_kind"] = "tianyi"
        self._expect_rejected(forged, job, "同源")

    def test_forged_magnet_to_non_sixpan(self) -> None:
        plan, job = self._make_job_plan("sixpan", "magnet", "sixpan_offline")
        forged = copy.deepcopy(plan)
        forged["destination"]["provider_kind"] = "mobile"
        self._expect_rejected(forged, job, "sixpan")

    def test_forged_relay_to_non_mobile(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "mobile_main")
        forged = copy.deepcopy(plan)
        forged["destination"]["provider_kind"] = "quark"
        self._expect_rejected(forged, job, "mobile")

    def test_forged_magnet_relay_strategy(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "mobile_main")
        forged = copy.deepcopy(plan)
        forged["source"]["source_form"] = "magnet"
        self._expect_rejected(forged, job, "磁链")

    def test_forged_rclone_requires_writable_locator(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        mount = self.env.mounts.get("mount_tianyi_main")
        readonly = OpenListMount(
            mount_id=mount.mount_id,
            connection_id=mount.connection_id,
            name=mount.name,
            provider_kind=mount.provider_kind,
            account_id=mount.account_id,
            webdav_root=mount.webdav_root,
            namespace_id=mount.namespace_id,
            readable=mount.readable,
            writable=False,
            enabled=True,
            revision=mount.revision,
            identity_status=mount.identity_status,
        )
        self.env.mounts.replace(readonly)
        self._expect_rejected(plan, job, "不可写")

    def test_forged_mount_revision_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged = copy.deepcopy(plan)
        forged["bindings"]["destination_mount_revision"] = int(forged["bindings"]["destination_mount_revision"]) + 1
        self._expect_rejected(forged, job, "修订")

    def test_forged_account_revision_mismatch(self) -> None:
        plan, job = self._make_job_plan("quark", "share", "tianyi_library")
        forged = copy.deepcopy(plan)
        forged["source"]["account_revision"] = int(forged["source"]["account_revision"]) + 1
        self._expect_rejected(forged, job, "修订")


if __name__ == "__main__":
    unittest.main()
