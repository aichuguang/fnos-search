"""ImportPlanner：解析账号、目标、能力与路径，冻结唯一 V1 执行计划。

只生成 V1 计划（§12.1）。同源原生、磁链六盘离线、跨盘 rclone、Provider 原生 API 中继四类
策略按 §12.2 算法选择；任何能力缺失都失败关闭，不静默降级，不根据当前后台配置
“补全”。计划不含任何明文凭证、OpenList 密码或任意 rclone 配置路径（§12.3）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..domain.models import LocalLocator, RcloneLocator, SelectionSnapshot, SourceDescriptor
from ..storage.target_service import encode_categories
from ..storage.assignment_service import assignment_id
from . import (
    DEFAULT_LOCAL_TARGET_ROOT,
    DEFAULT_RELAY_ROOT,
    DATA_PLANE_STRATEGIES,
    EXECUTION_PLAN_VERSION,
    INGRESS_DIR,
    NATIVE_STAGING_DIR,
    STAGING_DIR,
    STRATEGY_EXECUTOR,
    STRATEGY_NATIVE_OFFLINE,
    STRATEGY_NATIVE_SAVE,
    STRATEGY_RCLONE_COPY,
    STRATEGY_RELAY_UPLOAD_PROVIDER,
    AdapterRegistry,
    AccountsService,
    DestinationAccountUnavailable,
    DestinationRequired,
    DestinationResolver,
    MountService,
    NativeTransferUnavailable,
    OpenlistUnavailable,
    PlanningError,
    RulesService,
    SourceLandingUnavailable,
    TargetIncompatible,
    TargetNotFound,
    TargetService,
    job_dir,
    local_contained,
    local_namespace,
    mount_absolute_path,
    mount_webdav_root,
    native_namespace,
    native_staging_locator,
    normalize_path,
    openlist_namespace,
    is_relay_upload_strategy,
)
from .job_plan import attach_plan_integrity, compute_operation_key

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _account_ctx(account: tuple[str, int] | tuple[str, int, int] | None) -> dict[str, Any]:
    if account is None:
        return {"account_id": None, "revision": None}
    return {"account_id": account[0], "revision": account[1]}


class ImportPlanner:
    def __init__(
        self,
        *,
        accounts_service: AccountsService,
        target_service: TargetService,
        rules_service: RulesService,
        mount_service: MountService,
        registry: AdapterRegistry,
        config: Mapping[str, Any] | None = None,
        destination_resolver: DestinationResolver | None = None,
    ):
        self.accounts_service = accounts_service
        self.target_service = target_service
        self.rules_service = rules_service
        self.mount_service = mount_service
        self.registry = registry
        self.config = config if isinstance(config, Mapping) else {}
        self.destination_resolver = destination_resolver or DestinationResolver(
            rules_service, target_service, accounts_service
        )

    def plan_import(
        self,
        *,
        source: SourceDescriptor,
        category_key: str,
        job_id: int,
        audience: str = "guest",
        organizer_enabled: bool = False,
        media_refresh_enabled: bool = False,
    ) -> dict[str, Any]:
        """冻结唯一 V1 执行计划并返回计划 dict。"""
        if int(job_id or 0) <= 0:
            raise PlanningError("plan_import 需要有效任务 ID", code="PLANNING_ERROR")
        if not str(category_key or "").strip():
            raise PlanningError("plan_import 缺少分类", code="PLANNING_ERROR")

        # 1. 先冻结管理员 assignment。之后任何一次解析若看见不同 revision
        # 都立即失败，避免“两次 current 查询”拼出不一致的目标计划。
        routing = self._routing_snapshot(source, category_key)
        descriptor = self.destination_resolver.resolve(source, category_key, audience)
        if descriptor is None:
            raise DestinationRequired("当前资源没有可用的保存位置")
        self._validate_routing_descriptor(routing, descriptor)

        # 2. 解析来源 Provider 当前账号，冻结 account_id + revision + selection_revision。
        source_account = self.destination_resolver.resolve_account(
            source.provider_kind, source.account_scope, role="source"
        )

        # 3. 读取目标修订与目标账号。
        target = self.target_service.get_target(descriptor.destination_id, descriptor.target_revision)
        if target is None:
            raise TargetNotFound(
                f"目标修订不存在：{descriptor.destination_id}@{descriptor.target_revision}"
            )
        category = target.category(category_key)
        if category is None:
            raise TargetIncompatible(f"目标 {descriptor.destination_id} 未配置分类 {category_key}")
        if (
            organizer_enabled
            and isinstance(category.rclone_locator, LocalLocator)
            and category.organizer_locator is None
        ):
            raise PlanningError(
                "本地保存位置启用 Organizer 时必须绑定到同一存储的 OpenList mount",
                code="LOCAL_ORGANIZER_REQUIRES_OPENLIST_MOUNT",
            )
        binding_getter = getattr(self.target_service, "get_binding", None)
        if not callable(binding_getter):
            raise DestinationAccountUnavailable("目标服务不支持冻结 binding revision")
        destination_binding = binding_getter(
            descriptor.binding_id, descriptor.binding_revision
        )
        if destination_binding is None:
            raise DestinationAccountUnavailable(
                f"目标 binding 修订不存在：{descriptor.binding_id}@{descriptor.binding_revision}"
            )
        self._validate_binding_descriptor(destination_binding, descriptor)
        destination_account = self._destination_account(destination_binding)

        # 4. 来源 OpenList 挂载（数据面策略必需，native 类策略允许缺失）。
        source_mount = self._resolve_source_mount(source, source_account)

        # 5. 选择唯一传输策略（§12.2 步骤 6-9）。
        strategy = self._select_strategy(
            source=source,
            target=target,
            category=category,
            source_mount=source_mount,
            destination_account=destination_account,
        )

        destination_mount = self._resolve_destination_mount(category, strategy)
        organizer_mount = self._resolve_organizer_mount(
            category, enabled=bool(organizer_enabled)
        )

        # 6. 解析任务级路径。
        paths = self._build_paths(
            source=source,
            target=target,
            category=category,
            category_key=category_key,
            job_id=job_id,
            strategy=strategy,
            source_mount=source_mount,
            destination_mount=destination_mount,
            organizer_mount=organizer_mount,
            organizer_enabled=bool(organizer_enabled),
        )
        bindings = self._build_bindings(
            source_mount,
            strategy,
            category,
            destination_mount=destination_mount,
            organizer_mount=organizer_mount,
        )

        selection = source.selection or SelectionSnapshot(
            provider_kind=source.provider_kind,
            selection_hash=source.selected_items_hash,
        )
        # Re-read only to detect a concurrent route change.  The plan keeps the
        # first immutable snapshot and never replaces it with this current row.
        self._assert_routing_still_current(source, category_key, routing)

        plan = {
            "version": EXECUTION_PLAN_VERSION,
            "plan_schema_version": EXECUTION_PLAN_VERSION,
            "job_id": int(job_id),
            "category": category_key,
            "target_category": encode_categories({category_key: category})[category_key],
            "created_at": _utc_now_iso(),
            "source": {
                "provider_kind": source.provider_kind,
                "source_form": source.source_form,
                "canonical_source_id": source.canonical_source_id,
                "selection_hash": selection.selection_hash,
                "selection": selection.to_dict(),
                "search_source_id": source.search_source_id or source.provider_kind,
                "account_scope": source.account_scope,
                "record_id": source.source_record_id,
                "record_revision": source.source_record_revision,
                "account_id": source_account[0],
                "account_revision": source_account[1],
                "account_selection_revision": source_account[2],
            },
            "destination": {
                "id": descriptor.destination_id,
                "provider_kind": descriptor.provider_kind,
                "target_revision": descriptor.target_revision,
                "binding_id": destination_binding.binding_id,
                "binding_revision": destination_binding.current_revision,
                "account_id": destination_account[0] if destination_account else None,
                "account_revision": destination_account[1] if destination_account else None,
            },
            "transfer": {
                "strategy": strategy,
                "executor": STRATEGY_EXECUTOR[strategy],
                "idempotency_key": "",
                "delete_source_after_ack": True,
                "source": paths["source_landing"],
                "destination_staging": paths["destination_staging"],
                "destination_final": paths["destination_final"],
                "local_temp": paths["local_temp"],
            },
            "bindings": {
                "source_mount_id": bindings["source_mount_id"],
                "source_mount_revision": bindings["source_mount_revision"],
                "source_mount_namespace_id": bindings["source_mount_namespace_id"],
                "source_mount_storage_identity_id": bindings["source_mount_storage_identity_id"],
                "source_mount_identity_root": bindings["source_mount_identity_root"],
                "source_native_locator": bindings["source_native_locator"],
                "destination_mount_id": bindings["destination_mount_id"],
                "destination_mount_revision": bindings["destination_mount_revision"],
                "destination_mount_namespace_id": bindings["destination_mount_namespace_id"],
                "destination_mount_storage_identity_id": bindings["destination_mount_storage_identity_id"],
                "destination_mount_identity_root": bindings["destination_mount_identity_root"],
                "destination_connection_id": bindings["destination_connection_id"],
                "destination_connection_revision": bindings["destination_connection_revision"],
                "source_connection_id": bindings["source_connection_id"],
                "source_connection_revision": bindings["source_connection_revision"],
                "organizer_mount_id": bindings["organizer_mount_id"],
                "organizer_mount_revision": bindings["organizer_mount_revision"],
                "organizer_mount_namespace_id": bindings["organizer_mount_namespace_id"],
                "organizer_mount_storage_identity_id": bindings["organizer_mount_storage_identity_id"],
                "organizer_mount_identity_root": bindings["organizer_mount_identity_root"],
                "organizer_connection_id": bindings["organizer_connection_id"],
                "organizer_connection_revision": bindings["organizer_connection_revision"],
            },
            "paths": {
                "source_landing": paths["source_landing"],
                "destination_staging": paths["destination_staging"],
                "destination_final": paths["destination_final"],
                "organizer": paths["organizer"],
                "organizer_final": paths["organizer_final"],
                "local_temp": paths["local_temp"],
            },
            "verification": {
                "minimum": ["file_count", "total_size"],
                "expected_file_count": int(source.expected_file_count or 0),
                "expected_total_size": int(source.expected_total_bytes or 0),
                "checksum": "when_supported",
                "selection_hash": selection.selection_hash,
            },
            "postprocess": {
                "organizer_enabled": bool(organizer_enabled),
                "media_refresh_enabled": bool(media_refresh_enabled),
            },
            "routing": routing,
        }
        plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
        return attach_plan_integrity(plan)

    def _routing_snapshot(self, source: SourceDescriptor, category_key: str) -> dict[str, Any]:
        """Freeze the required assignment revision; missing routing fails closed."""

        getter = getattr(self.rules_service, "get_assignment", None)
        assignment = getter(source.search_source_id or source.provider_kind, category_key) if callable(getter) else None
        if assignment is None:
            raise DestinationRequired("当前搜索来源和分类没有管理员保存位置 assignment")
        if isinstance(assignment, Mapping):
            search_source_id = str(
                assignment.get("search_source_id")
                or source.search_source_id
                or source.provider_kind
            )
            selected_category = str(assignment.get("category_key") or category_key)
            revision = int(assignment.get("revision") or 0)
            destination_id = str(assignment.get("destination_id") or "")
            enabled = bool(assignment.get("enabled", True))
            stable_id = str(
                assignment.get("assignment_id")
                or assignment.get("id")
                or assignment_id(search_source_id, selected_category)
            )
        else:
            search_source_id = str(
                getattr(assignment, "search_source_id", "")
                or source.search_source_id
                or source.provider_kind
            )
            selected_category = str(
                getattr(assignment, "category_key", "") or category_key
            )
            revision = int(getattr(assignment, "revision", 0) or 0)
            destination_id = str(getattr(assignment, "destination_id", "") or "")
            enabled = bool(getattr(assignment, "enabled", False))
            stable_id = str(
                getattr(assignment, "assignment_id", "")
                or assignment_id(search_source_id, selected_category)
            )
        expected_source = source.search_source_id or source.provider_kind
        if (
            search_source_id != expected_source
            or selected_category != category_key
            or not stable_id
            or revision <= 0
            or not destination_id
            or not enabled
        ):
            raise DestinationRequired("管理员保存位置 assignment 不完整或已停用")
        historical_getter = getattr(self.rules_service, "get_revision", None)
        if not callable(historical_getter):
            raise DestinationRequired("assignment 服务不支持不可变 revision")
        historical = historical_getter(stable_id, revision)
        if not isinstance(historical, Mapping):
            raise DestinationRequired("assignment 历史修订不存在")
        if (
            str(historical.get("search_source_id") or "") != search_source_id
            or str(historical.get("category_key") or "") != selected_category
            or str(historical.get("destination_id") or "") != destination_id
            or not bool(historical.get("enabled"))
        ):
            raise DestinationRequired("assignment 当前行与历史修订不一致")
        return {
            "search_source_id": search_source_id,
            "category_key": selected_category,
            "assignment_id": stable_id,
            "assignment_revision": revision,
            "destination_id": destination_id,
            "enabled": True,
        }

    @staticmethod
    def _validate_routing_descriptor(routing: Mapping[str, Any], descriptor: Any) -> None:
        if str(routing.get("destination_id") or "") != str(
            getattr(descriptor, "destination_id", "") or ""
        ):
            raise DestinationRequired("assignment 在目标解析期间发生变化")

    def _assert_routing_still_current(
        self,
        source: SourceDescriptor,
        category_key: str,
        frozen: Mapping[str, Any],
    ) -> None:
        current = self._routing_snapshot(source, category_key)
        for key in (
            "search_source_id",
            "category_key",
            "assignment_id",
            "assignment_revision",
            "destination_id",
            "enabled",
        ):
            if current.get(key) != frozen.get(key):
                raise DestinationRequired("assignment 在计划生成期间发生变化，请重试")

    @staticmethod
    def _validate_binding_descriptor(binding: Any, descriptor: Any) -> None:
        expected = {
            "binding_id": getattr(descriptor, "binding_id", None),
            "current_revision": getattr(descriptor, "binding_revision", None),
            "destination_id": getattr(descriptor, "destination_id", None),
            "target_revision": getattr(descriptor, "target_revision", None),
            "provider_kind": getattr(descriptor, "provider_kind", None),
            "account_id": getattr(descriptor, "account_id", None),
            "account_revision": getattr(descriptor, "account_revision", None),
        }
        for field, expected_value in expected.items():
            if getattr(binding, field, None) != expected_value:
                raise DestinationAccountUnavailable(
                    f"目标 binding 冻结字段不一致：{field}"
                )

    # ---- 账号解析 -----------------------------------------------------------

    @staticmethod
    def _destination_account(binding: Any) -> tuple[str, int] | None:
        if not binding.account_id:
            return None
        if not binding.account_revision:
            raise DestinationAccountUnavailable("目标 binding 缺少冻结账号修订")
        return binding.account_id, binding.account_revision

    def _resolve_source_mount(self, source: SourceDescriptor, source_account: tuple[str, int, int]) -> Any:
        if source_account is None:
            return None
        current = self.mount_service.find_mount_for_account(
            source.provider_kind, source_account[0], readable=True
        )
        return self._freeze_mount(current, role="来源") if current is not None else None

    def _freeze_mount(self, current: Any, *, role: str) -> dict[str, Any]:
        if not isinstance(current, Mapping):
            raise OpenlistUnavailable(f"{role} mount 数据无效")
        mount_id = str(current.get("mount_id") or "").strip()
        revision = int(current.get("revision") or 0)
        getter = getattr(self.mount_service, "get_revision", None)
        if not mount_id or revision <= 0 or not callable(getter):
            raise OpenlistUnavailable(f"{role} mount 缺少不可变 revision")
        frozen = getter(mount_id, revision)
        if not isinstance(frozen, Mapping):
            raise OpenlistUnavailable(f"{role} mount 修订不存在：{mount_id}@{revision}")
        result = dict(frozen)
        required = (
            "connection_id",
            "connection_revision",
            "namespace_id",
            "storage_identity_id",
            "identity_root",
            "enabled",
            "identity_status",
            "readable",
            "writable",
            # OpenList 全局绝对路径必须由 webdav_root 拼出；缺了它只能冻结出
            # 挂载内相对路径，Organizer 调 /api/fs/list 会直接 storage not found。
            # 与其静默降级，不如失败关闭。
            "webdav_root",
        )
        if any(key not in result for key in required):
            raise OpenlistUnavailable(f"{role} mount 修订快照不完整")
        if not str(result.get("storage_identity_id") or "").strip():
            raise OpenlistUnavailable(f"{role} mount 缺少存储身份")
        if str(result.get("identity_status") or "") not in {"verified", "declared"}:
            raise OpenlistUnavailable(f"{role} mount 身份未确认")
        if not bool(result.get("enabled")):
            raise OpenlistUnavailable(f"{role} mount 已停用")
        for field in (
            "connection_id",
            "namespace_id",
            "provider_kind",
            "account_id",
            "storage_identity_id",
            "identity_root",
        ):
            current_value = current.get(field)
            if current_value is not None and current_value != result.get(field):
                raise OpenlistUnavailable(f"{role} mount 当前行与冻结修订不一致：{field}")
        result["mount_id"] = mount_id
        result["revision"] = revision
        connection_service = getattr(self.mount_service, "connection_service", None)
        connection_revision = int(result.get("connection_revision") or 0)
        if (
            connection_service is None
            or connection_revision <= 0
            or connection_service.get_revision(
                str(result.get("connection_id") or ""), connection_revision
            )
            is None
        ):
            raise OpenlistUnavailable(f"{role} OpenList connection 修订不存在")
        return result

    def _resolve_destination_mount(self, category: Any, strategy: str) -> dict[str, Any] | None:
        if strategy != STRATEGY_RCLONE_COPY and not is_relay_upload_strategy(strategy):
            return None
        locator = category.rclone_locator
        if not isinstance(locator, RcloneLocator):
            locator = category.organizer_locator
        if locator is None or not getattr(locator, "mount_id", None):
            if is_relay_upload_strategy(strategy):
                raise OpenlistUnavailable("Provider relay 目标缺少 OpenList mount")
            return None
        current = self.mount_service.get_mount(locator.mount_id)
        if current is None:
            raise OpenlistUnavailable("目标定位器挂载不存在")
        frozen = self._freeze_mount(current, role="目标")
        if str(frozen.get("connection_id") or "") != locator.connection_id:
            raise OpenlistUnavailable("目标 locator connection 与 mount 修订不一致")
        if strategy == STRATEGY_RCLONE_COPY and not bool(frozen.get("writable")):
            raise OpenlistUnavailable("目标 mount 修订不可写")
        if is_relay_upload_strategy(strategy) and not self._mount_supports_provider_upload(frozen):
            raise OpenlistUnavailable("目标 mount 尚未通过 Provider 原生上传探测")
        return frozen

    def _resolve_organizer_mount(
        self, category: Any, *, enabled: bool
    ) -> dict[str, Any] | None:
        if not enabled:
            return None
        locator = category.organizer_locator
        if locator is None:
            raise PlanningError(
                "Organizer 启用但目标分类未配置 organizer_locator",
                code="TARGET_INCOMPATIBLE",
            )
        current = self.mount_service.get_mount(locator.mount_id)
        if current is None:
            raise OpenlistUnavailable("Organizer mount 不存在")
        frozen = self._freeze_mount(current, role="Organizer")
        if str(frozen.get("connection_id") or "") != locator.connection_id:
            raise OpenlistUnavailable("Organizer locator connection 与 mount 修订不一致")
        if not self._mount_supports_organizer(frozen):
            raise OpenlistUnavailable("Organizer mount 修订未通过读取与整理能力探测")
        return frozen

    @staticmethod
    def _mount_supports_provider_upload(mount: Mapping[str, Any]) -> bool:
        native = mount.get("native_locator") if isinstance(mount.get("native_locator"), Mapping) else {}
        if (
            mount.get("readable")
            and native.get("upload_verified")
            and native.get("organizer_mutation_verified")
        ):
            return True
        return False

    @staticmethod
    def _mount_supports_organizer(mount: Mapping[str, Any]) -> bool:
        native = mount.get("native_locator") if isinstance(mount.get("native_locator"), Mapping) else {}
        return bool(
            mount.get("readable")
            and (mount.get("writable") or native.get("organizer_mutation_verified"))
        )

    # ---- 策略选择（§12.2） ---------------------------------------------------

    def _select_strategy(
        self,
        *,
        source: SourceDescriptor,
        target: Any,
        category: Any,
        source_mount: Any,
        destination_account: tuple[str, int] | None,
    ) -> str:
        target_provider = str(target.provider_kind or "")

        if source.source_form == "magnet":
            if target_provider != "sixpan":
                raise NativeTransferUnavailable(
                    "magnet resources can only be routed to sixpan",
                    code="MAGNET_TARGET_RESTRICTED",
                )
            if category.native_locator is None:
                raise NativeTransferUnavailable("magnet target lacks native_offline locator")
            adapter = self.registry.get("sixpan")
            if not self._adapter_capabilities(adapter).supports_native_offline:
                raise NativeTransferUnavailable("sixpan Adapter lacks native_offline capability")
            return STRATEGY_NATIVE_OFFLINE

        if source.provider_kind == target_provider:
            if category.native_locator is None:
                raise NativeTransferUnavailable(f"{target_provider} target lacks native locator")
            adapter = self.registry.get(source.provider_kind)
            if not self._adapter_capabilities(adapter).supports_native_save:
                raise NativeTransferUnavailable(f"{target_provider} Adapter lacks native_save capability")
            decision = adapter.can_native_transfer(
                source, category.native_locator, _account_ctx(destination_account)
            )
            if not decision.available:
                raise NativeTransferUnavailable(f"same-provider native transfer unavailable: {decision.reason}")
            return STRATEGY_NATIVE_SAVE

        if target_provider not in {"local", "generic", "openlist_generic"}:
            target_adapter = self.registry.get(target_provider)
            target_caps = self._adapter_capabilities(target_adapter)
        else:
            target_adapter = None
            target_caps = None

        if target_adapter is not None and target_caps.supports_relay_upload:
            if source_mount is None or not source_mount.get("readable"):
                raise SourceLandingUnavailable("source has no readable landing mount")
            if destination_account is None:
                raise DestinationAccountUnavailable(
                    f"{target_provider} target has no bound account"
                )
            if category.native_locator is None:
                raise DestinationAccountUnavailable(
                    f"{target_provider} target lacks a native upload locator"
                )
            uploader_factory = getattr(target_adapter, "relay_uploader", None)
            if not callable(uploader_factory) or uploader_factory() is None:
                raise DestinationAccountUnavailable(
                    f"{target_provider} Adapter lacks relay upload capability"
                )
            source_adapter = self.registry.get(source.provider_kind)
            if not self._adapter_capabilities(source_adapter).supports_source_materialization:
                raise SourceLandingUnavailable("source Adapter cannot materialize to landing")
            return STRATEGY_RELAY_UPLOAD_PROVIDER

        if source_mount is None or not source_mount.get("readable"):
            raise SourceLandingUnavailable("source has no readable landing mount")
        source_adapter = self.registry.get(source.provider_kind)
        if not self._adapter_capabilities(source_adapter).supports_source_materialization:
            raise SourceLandingUnavailable("source Adapter cannot materialize to landing")
        if target_adapter is not None:
            if not target_caps.supports_rclone_dest:
                raise OpenlistUnavailable("target Adapter does not support rclone destination")
        self._require_writable_target(category, target)
        return STRATEGY_RCLONE_COPY

    @staticmethod
    def _adapter_capabilities(adapter: Any) -> Any:
        from ..domain.models import ProviderCapabilities

        manifest = getattr(adapter, "manifest", None)
        method = getattr(adapter, "capabilities", None)
        if callable(method):
            try:
                value = method()
            except Exception:  # noqa: BLE001 - capability failures fail closed
                return ProviderCapabilities()
            if isinstance(value, ProviderCapabilities):
                return value
            if isinstance(value, Mapping):
                allowed = ProviderCapabilities.__dataclass_fields__
                return ProviderCapabilities(**{
                    key: bool(value.get(key, False))
                    for key in allowed
                    if key in value
                })
            return ProviderCapabilities()
        if isinstance(manifest, object) and isinstance(getattr(manifest, "capabilities", None), ProviderCapabilities):
            return manifest.capabilities
        return ProviderCapabilities()

    def _require_writable_target(self, category: Any, target: Any) -> Any:
        locator = category.rclone_locator
        if isinstance(locator, RcloneLocator):
            mount = self.mount_service.get_mount(locator.mount_id)
            if mount is None or not mount.get("enabled") or not mount.get("writable"):
                raise OpenlistUnavailable("目标 rclone 定位器的挂载不可写")
            return mount
        if isinstance(locator, LocalLocator):
            if not local_contained(locator.root, self._local_target_root()):
                raise OpenlistUnavailable("本地目标越出受控根目录")
            return None
        raise OpenlistUnavailable("目标缺少可写的 rclone 或本地定位器")

    # ---- 路径与绑定 ---------------------------------------------------------

    def _build_paths(
        self,
        *,
        source: SourceDescriptor,
        target: Any,
        category: Any,
        category_key: str,
        job_id: int,
        strategy: str,
        source_mount: Any,
        destination_mount: Any = None,
        organizer_mount: Any = None,
        organizer_enabled: bool = False,
    ) -> dict[str, Any]:
        job = job_dir(job_id)
        source_landing: dict[str, str] | None = None
        destination_staging: dict[str, str] | None = None
        destination_final: dict[str, str] | None = None
        organizer: dict[str, str] | None = None
        local_temp: dict[str, str] | None = None

        if strategy in DATA_PLANE_STRATEGIES:
            if source_mount is None or not source_mount.get("readable"):
                raise SourceLandingUnavailable("来源落地区不可读")
            source_landing = {
                "kind": "openlist",
                "namespace_id": source_mount["namespace_id"],
                "path": f"/{INGRESS_DIR}/{job}",
                "mount_id": source_mount.get("mount_id"),
                "mount_revision": source_mount.get("revision"),
                "connection_id": source_mount.get("connection_id"),
                "connection_revision": source_mount.get("connection_revision"),
                "storage_identity_id": source_mount.get("storage_identity_id") or "",
                "identity_root": source_mount.get("identity_root") or "/",
            }

        if strategy == STRATEGY_RCLONE_COPY:
            destination_staging, destination_final = self._rclone_paths(
                category, target, category_key, job, destination_mount=destination_mount
            )
        elif strategy in {
            STRATEGY_NATIVE_SAVE,
            STRATEGY_NATIVE_OFFLINE,
            STRATEGY_RELAY_UPLOAD_PROVIDER,
        }:
            native = category.native_locator
            if native is None:
                raise NativeTransferUnavailable(f"{strategy} 缺少 native 定位器")
            namespace = native_namespace(target.provider_kind, target.destination_id)
            destination_final = {
                "kind": "native",
                "namespace_id": namespace,
                "path": native.value,
                "locator_kind": native.kind,
            }
            if strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_NATIVE_OFFLINE} and organizer_enabled:
                # 原生转存先落到「分类目录 / _入库暂存 / job-N」，再由 OpenList
                # Organizer 搬进正式分类目录。只有 Organizer 启用时才冻结暂存目录：
                # 原生 Provider 没有 move 能力，TransferFinalizer 也没有 native 分支，
                # Organizer 是唯一能把暂存搬走的执行者。否则保持直写分类目录，
                # 避免出现「进了暂存却没人搬」的死链路。
                staging_value = native_staging_locator(native.kind, native.value, job)
                if staging_value:
                    destination_staging = {
                        "kind": "native",
                        "namespace_id": namespace,
                        "path": staging_value,
                        "locator_kind": native.kind,
                        "role": "native_staging",
                    }
            if is_relay_upload_strategy(strategy):
                # Relay cache is a distinct, controlled local namespace.  It
                # is not a destination staging directory and must be frozen
                # explicitly so cleanup cannot infer it from current config.
                local_temp = {
                    "kind": "relay",
                    "namespace_id": "local:relay",
                    "path": f"{self._relay_root()}/{job}",
                }

        organizer_final: dict[str, str] | None = None
        if organizer_enabled:
            organizer, organizer_final = self._build_organizer(
                category,
                category_key,
                job,
                destination_staging,
                organizer_mount=organizer_mount,
            )

        return {
            "source_landing": source_landing,
            "destination_staging": destination_staging,
            "destination_final": destination_final,
            "organizer": organizer,
            "organizer_final": organizer_final,
            "local_temp": local_temp,
        }

    def _rclone_paths(
        self,
        category: Any,
        target: Any,
        category_key: str,
        job: str,
        *,
        destination_mount: Any = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        locator = category.rclone_locator
        if isinstance(locator, RcloneLocator):
            mount = destination_mount
            if not isinstance(mount, Mapping):
                raise OpenlistUnavailable("目标 rclone 定位器挂载不存在")
            namespace = mount["namespace_id"]
            final_path = normalize_path(f"{locator.relative_path}/{job}")
            staging_path = f"/{STAGING_DIR}/{category_key}/{job}"
        elif isinstance(locator, LocalLocator):
            root = normalize_path(locator.root)
            if not local_contained(root, self._local_target_root()):
                raise OpenlistUnavailable("本地目标越出受控根目录")
            namespace = local_namespace(target.destination_id)
            final_root = normalize_path(f"{root}/{locator.relative_path}") if locator.relative_path else root
            final_path = normalize_path(f"{final_root}/{job}")
            staging_path = f"{root}/{STAGING_DIR}/{category_key}/{job}"
        else:
            raise OpenlistUnavailable("目标缺少可写的 rclone 或本地定位器")
        path_kind = "openlist" if isinstance(locator, RcloneLocator) else "local"
        extras: dict[str, Any] = {}
        if isinstance(locator, RcloneLocator):
            extras = {
                "mount_id": locator.mount_id,
                "mount_revision": mount.get("revision") if mount is not None else None,
                "connection_id": mount.get("connection_id") if mount is not None else locator.connection_id,
                "connection_revision": mount.get("connection_revision") if mount is not None else None,
                "storage_identity_id": mount.get("storage_identity_id") if mount is not None else "",
                "identity_root": mount.get("identity_root") if mount is not None else "/",
            }
        return (
            {"kind": path_kind, "namespace_id": namespace, "path": staging_path, **extras},
            {"kind": path_kind, "namespace_id": namespace, "path": final_path, **extras},
        )

    def _build_organizer(
        self,
        category: Any,
        category_key: str,
        job: str,
        destination_staging: dict[str, str] | None,
        *,
        organizer_mount: Any = None,
    ) -> tuple[dict[str, str], dict[str, str] | None]:
        organizer_locator = category.organizer_locator
        if organizer_locator is None:
            raise PlanningError(
                "Organizer 启用但目标分类未配置 organizer_locator",
                code="TARGET_INCOMPATIBLE",
            )
        if not isinstance(organizer_mount, Mapping):
            raise OpenlistUnavailable("Organizer 缺少冻结 mount 修订")
        namespace = str(organizer_mount.get("namespace_id") or "")
        if not namespace:
            namespace = openlist_namespace(organizer_locator.connection_id, organizer_locator.mount_id)
        # 分类的 organizer_locator.path 是「挂载内相对路径」，而 Organizer 调用
        # OpenList /api/fs/* 时必须给全局绝对路径。前缀取自冻结的 mount 快照，
        # 保持「冻结即确定」，不读实时配置。webdav_root 只属于 OpenList 坐标系，
        # 与天翼/移动/夸克的原生根无关，因此只作用于下面这些 openlist 路径。
        webdav_root = mount_webdav_root(organizer_mount)
        extras = {
            "mount_id": organizer_locator.mount_id,
            "mount_revision": organizer_mount.get("revision"),
            "connection_id": organizer_mount.get("connection_id"),
            "connection_revision": organizer_mount.get("connection_revision"),
            "storage_identity_id": organizer_mount.get("storage_identity_id") or "",
            "identity_root": organizer_mount.get("identity_root") or "/",
        }
        staging_kind = str((destination_staging or {}).get("kind") or "")
        if staging_kind == "openlist":
            # 执行暂存与 Organizer 可见目录是同一个任务级目录（§8.5）。
            # 暂存目录由 rclone 侧（remote_path 自行拼根）与 Organizer 侧共同访问，
            # 计划里冻结的是挂载内相对路径，Organizer 消费时按 webdav_root 补齐。
            return (
                {
                    "kind": "openlist",
                    "namespace_id": namespace,
                    "path": mount_absolute_path(
                        webdav_root, f"/{STAGING_DIR}/{category_key}/{job}"
                    ),
                    **extras,
                },
                None,
            )
        if staging_kind == "native":
            # 原生暂存：Organizer 从 OpenList 侧看到同一个「分类目录/_入库暂存/job-N」，
            # 并把文件搬到 OpenList 可见的正式分类目录 organizer_final。
            # paths.destination_final 对天翼云是 ``{root_folder_id}::{相对路径}``
            # 复合定位器，OpenList 无法直接把它当目标根使用，所以正式分类目录
            # 必须单独冻结一份 OpenList 可见路径。
            final_path = mount_absolute_path(webdav_root, organizer_locator.path)
            staging_path = normalize_path(
                f"{final_path}/{NATIVE_STAGING_DIR}/{job}"
            )
            return (
                {
                    "kind": "openlist",
                    "namespace_id": namespace,
                    "path": staging_path,
                    **extras,
                },
                {
                    "kind": "openlist",
                    "namespace_id": namespace,
                    "path": final_path,
                    **extras,
                },
            )
        # native 直写 / 中继策略：Organizer 直接看到最终目录。
        return (
            {
                "kind": "openlist",
                "namespace_id": namespace,
                "path": mount_absolute_path(webdav_root, organizer_locator.path),
                **extras,
            },
            None,
        )

    def _build_bindings(
        self,
        source_mount: Any,
        strategy: str,
        category: Any,
        *,
        destination_mount: Any = None,
        organizer_mount: Any = None,
    ) -> dict[str, Any]:
        bindings: dict[str, Any] = {
            "source_mount_id": None,
            "source_mount_revision": None,
            "source_mount_namespace_id": None,
            "source_mount_storage_identity_id": None,
            "source_mount_identity_root": None,
            "source_connection_id": None,
            "source_connection_revision": None,
            "source_native_locator": None,
            "destination_mount_id": None,
            "destination_mount_revision": None,
            "destination_mount_namespace_id": None,
            "destination_mount_storage_identity_id": None,
            "destination_mount_identity_root": None,
            "destination_connection_id": None,
            "destination_connection_revision": None,
            "organizer_mount_id": None,
            "organizer_mount_revision": None,
            "organizer_mount_namespace_id": None,
            "organizer_mount_storage_identity_id": None,
            "organizer_mount_identity_root": None,
            "organizer_connection_id": None,
            "organizer_connection_revision": None,
        }
        if strategy in DATA_PLANE_STRATEGIES and source_mount is not None:
            bindings["source_mount_id"] = source_mount["mount_id"]
            bindings["source_mount_revision"] = source_mount["revision"]
            bindings["source_mount_namespace_id"] = source_mount.get("namespace_id")
            bindings["source_connection_id"] = source_mount.get("connection_id")
            bindings["source_connection_revision"] = source_mount.get("connection_revision")
            bindings["source_mount_storage_identity_id"] = source_mount.get("storage_identity_id") or None
            bindings["source_mount_identity_root"] = source_mount.get("identity_root") or "/"
            native_locator = source_mount.get("native_locator")
            if not isinstance(native_locator, dict) or not native_locator.get("kind") or not native_locator.get("value"):
                raise SourceLandingUnavailable(
                    "来源 mount 缺少 Provider 原生根定位器，无法把分享物化到 OpenList landing"
                )
            bindings["source_native_locator"] = {
                "kind": str(native_locator["kind"]),
                "value": str(native_locator["value"]),
            }
        if strategy == STRATEGY_RCLONE_COPY or is_relay_upload_strategy(strategy):
            locator = category.rclone_locator
            if not isinstance(locator, RcloneLocator):
                locator = category.organizer_locator
            mount = destination_mount
            if locator is not None and mount is not None:
                bindings["destination_mount_id"] = mount["mount_id"]
                bindings["destination_mount_revision"] = mount["revision"]
                bindings["destination_mount_namespace_id"] = mount.get("namespace_id")
                bindings["destination_mount_storage_identity_id"] = mount.get("storage_identity_id") or None
                bindings["destination_mount_identity_root"] = mount.get("identity_root") or "/"
                bindings["destination_connection_id"] = mount["connection_id"]
                bindings["destination_connection_revision"] = mount.get("connection_revision")

        if organizer_mount is not None:
            bindings["organizer_mount_id"] = organizer_mount.get("mount_id")
            bindings["organizer_mount_revision"] = organizer_mount.get("revision")
            bindings["organizer_mount_namespace_id"] = organizer_mount.get("namespace_id")
            bindings["organizer_mount_storage_identity_id"] = organizer_mount.get("storage_identity_id") or None
            bindings["organizer_mount_identity_root"] = organizer_mount.get("identity_root") or "/"
            bindings["organizer_connection_id"] = organizer_mount.get("connection_id")
            bindings["organizer_connection_revision"] = organizer_mount.get("connection_revision")
        return bindings

    def _idempotency_key(
        self,
        source: SourceDescriptor,
        category_key: str,
        descriptor: Any,
        source_account: tuple[str, int, int],
        destination_account: tuple[str, int] | None,
    ) -> str:
        destination_suffix = (
            f":{descriptor.destination_id}:{descriptor.target_revision}"
            f":{descriptor.binding_id}:{descriptor.binding_revision}"
        )
        account_suffix = (
            f":src={source_account[0]}@{source_account[1]}"
            f":dst={destination_account[0] + '@' + str(destination_account[1]) if destination_account else '-'}"
        )
        selection_hash = source.selection.selection_hash if source.selection is not None else source.selected_items_hash
        return (
            f"import:v1:{source.source_record_id}:{source.source_record_revision}"
            f":{source.canonical_source_id}"
            f":{selection_hash or '-'}:{category_key}"
            f"{destination_suffix}{account_suffix}"
        )

    # ---- 部署级配置 ---------------------------------------------------------

    def _local_target_root(self) -> str:
        return str(self.config.get("FNOS_LOCAL_TARGET_ROOT") or DEFAULT_LOCAL_TARGET_ROOT)

    def _relay_root(self) -> str:
        return str(self.config.get("FNOS_RELAY_ROOT") or DEFAULT_RELAY_ROOT)
