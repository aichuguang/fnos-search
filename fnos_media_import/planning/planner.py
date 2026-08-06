"""ImportPlannerV4：解析账号、目标、能力与路径，冻结唯一 v4 执行计划。

只生成 v4 计划（§12.1）。同源原生、磁链六盘离线、跨盘 rclone、移动云中继四类
策略按 §12.2 算法选择；任何能力缺失都失败关闭，不静默降级，不根据当前后台配置
“补全”。计划不含任何明文凭证、OpenList 密码或任意 rclone 配置路径（§12.3）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..domain.models import LocalLocator, RcloneLocator, SourceDescriptor
from . import (
    DEFAULT_LOCAL_TARGET_ROOT,
    DEFAULT_RELAY_ROOT,
    EXECUTION_PLAN_VERSION,
    INGRESS_DIR,
    STAGING_DIR,
    STRATEGY_EXECUTOR,
    STRATEGY_NATIVE_OFFLINE,
    STRATEGY_NATIVE_SAVE,
    STRATEGY_RCLONE_COPY,
    STRATEGY_RELAY_UPLOAD_MOBILE,
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
    native_namespace,
    normalize_path,
    openlist_namespace,
)

# 需要来源落地区可读（OpenList 数据面）的策略。
_DATA_PLANE_STRATEGIES = frozenset({STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD_MOBILE})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _account_ctx(account: tuple[str, int, int] | None) -> dict[str, Any]:
    if account is None:
        return {"account_id": None, "revision": None}
    return {"account_id": account[0], "revision": account[1]}


class ImportPlannerV4:
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
        destination_id: str | None = None,
        audience: str = "guest",
        organizer_enabled: bool = False,
        media_refresh_enabled: bool = False,
    ) -> dict[str, Any]:
        """按 §12.2 冻结唯一 v4 执行计划并返回计划 dict。"""
        if int(job_id or 0) <= 0:
            raise PlanningError("plan_import 需要有效任务 ID", code="PLANNING_ERROR")
        if not str(category_key or "").strip():
            raise PlanningError("plan_import 缺少分类", code="PLANNING_ERROR")

        # 1. 目标解析：规则是唯一真源；客户端只允许提交 destination_id。
        if destination_id:
            descriptor = self.destination_resolver.resolve_requested(
                source, category_key, destination_id, audience
            )
        else:
            descriptor = self.destination_resolver.resolve(source, category_key, audience)
        if descriptor is None:
            raise DestinationRequired("当前资源没有可用的保存位置")

        # 2. 解析来源 Provider 当前账号，冻结 account_id + revision + selection_revision。
        source_account = self.destination_resolver.resolve_account(
            source.provider_kind, source.account_scope, role="source"
        )

        # 3. 读取目标修订与目标账号。
        target = self.target_service.get_revision(descriptor.destination_id, descriptor.target_revision)
        if target is None:
            raise TargetNotFound(
                f"目标修订不存在：{descriptor.destination_id}@{descriptor.target_revision}"
            )
        category = target.category(category_key)
        if category is None:
            raise TargetIncompatible(f"目标 {descriptor.destination_id} 未配置分类 {category_key}")
        destination_account = self._resolve_destination_account(target)

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

        # 6. 解析任务级路径。
        paths = self._build_paths(
            source=source,
            target=target,
            category=category,
            category_key=category_key,
            job_id=job_id,
            strategy=strategy,
            source_mount=source_mount,
            organizer_enabled=bool(organizer_enabled),
        )
        bindings = self._build_bindings(source_mount, strategy, category)

        idempotency_key = self._idempotency_key(
            source, category_key, descriptor, source_account, destination_account
        )

        return {
            "version": EXECUTION_PLAN_VERSION,
            "job_id": int(job_id),
            "created_at": _utc_now_iso(),
            "source": {
                "provider_kind": source.provider_kind,
                "source_form": source.source_form,
                "canonical_source_id": source.canonical_source_id,
                "selection_hash": source.selected_items_hash,
                "account_id": source_account[0],
                "account_revision": source_account[1],
                "account_selection_revision": source_account[2],
            },
            "destination": {
                "id": descriptor.destination_id,
                "provider_kind": descriptor.provider_kind,
                "target_revision": descriptor.target_revision,
                "account_id": destination_account[0] if destination_account else None,
                "account_revision": destination_account[1] if destination_account else None,
                "account_selection_revision": destination_account[2] if destination_account else None,
            },
            "transfer": {
                "strategy": strategy,
                "executor": STRATEGY_EXECUTOR[strategy],
                "idempotency_key": idempotency_key,
                "delete_source_after_ack": True,
            },
            "bindings": {
                "source_mount_id": bindings["source_mount_id"],
                "source_mount_revision": bindings["source_mount_revision"],
                "destination_mount_id": bindings["destination_mount_id"],
                "destination_mount_revision": bindings["destination_mount_revision"],
            },
            "paths": {
                "source_landing": paths["source_landing"],
                "destination_staging": paths["destination_staging"],
                "destination_final": paths["destination_final"],
                "organizer": paths["organizer"],
                "local_temp": paths["local_temp"],
            },
            "verification": {
                "minimum": ["file_count", "total_size"],
                "checksum": "when_supported",
            },
            "postprocess": {
                "organizer_enabled": bool(organizer_enabled),
                "media_refresh_enabled": bool(media_refresh_enabled),
            },
        }

    # ---- 账号解析 -----------------------------------------------------------

    def _resolve_destination_account(self, target: Any) -> tuple[str, int, int] | None:
        if target.account_scope is None:
            return None
        return self.destination_resolver.resolve_account(
            target.provider_kind, target.account_scope, role="destination"
        )

    def _resolve_source_mount(self, source: SourceDescriptor, source_account: tuple[str, int, int]) -> Any:
        if source_account is None:
            return None
        return self.mount_service.find_mount_for_account(
            source.provider_kind, source_account[0], readable=True
        )

    # ---- 策略选择（§12.2） ---------------------------------------------------

    def _select_strategy(
        self,
        *,
        source: SourceDescriptor,
        target: Any,
        category: Any,
        source_mount: Any,
        destination_account: tuple[str, int, int] | None,
    ) -> str:
        target_provider = target.provider_kind

        # 磁链：只允许 SixpanAdapter.native_offline -> 六盘 native_offline。
        if source.source_form == "magnet":
            if target_provider != "sixpan":
                raise NativeTransferUnavailable(
                    "磁链目标必须是六盘；其他目标一律拒绝",
                    code="MAGNET_TARGET_RESTRICTED",
                )
            if category.native_locator is None:
                raise NativeTransferUnavailable("六盘目标缺少 native_offline 定位器")
            adapter = self.registry.get("sixpan")
            if not adapter.capabilities().supports_native_offline:
                raise NativeTransferUnavailable("六盘 Adapter 未提供 native_offline 能力")
            return STRATEGY_NATIVE_OFFLINE

        # 同源：强制原生，不通过即报错，不尝试跨盘替代（§12.2 步骤 7）。
        if source.provider_kind == target_provider:
            if category.native_locator is None:
                raise NativeTransferUnavailable(f"{target_provider} 目标缺少同源 native 定位器")
            adapter = self.registry.get(source.provider_kind)
            decision = adapter.can_native_transfer(
                source, category.native_locator, _account_ctx(destination_account)
            )
            if not decision.available:
                raise NativeTransferUnavailable(f"同源原生转存不可用：{decision.reason}")
            return STRATEGY_NATIVE_SAVE

        # 目标为移动云且来源不是移动云：唯一例外链路 relay_upload_mobile。
        if target_provider == "mobile":
            if source_mount is None or not source_mount.readable:
                raise SourceLandingUnavailable("来源落地区不可读，无法走移动云中继")
            if destination_account is None:
                raise DestinationAccountUnavailable("移动云目标没有激活账号")
            mobile_adapter = self.registry.get("mobile")
            if mobile_adapter.relay_uploader() is None:
                raise DestinationAccountUnavailable("移动云 Adapter 未提供中继上传器")
            return STRATEGY_RELAY_UPLOAD_MOBILE

        # 其它目标：来源落地区可读 + 目标 rclone/本地定位器可写 -> rclone_copy。
        if source_mount is None or not source_mount.readable:
            raise SourceLandingUnavailable("来源落地区不可读，无法跨盘搬运")
        self._require_writable_target(category, target)
        return STRATEGY_RCLONE_COPY

    def _require_writable_target(self, category: Any, target: Any) -> Any:
        locator = category.rclone_locator
        if isinstance(locator, RcloneLocator):
            mount = self.mount_service.get(locator.mount_id)
            if mount is None or not mount.enabled or not mount.writable:
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
        organizer_enabled: bool,
    ) -> dict[str, Any]:
        job = job_dir(job_id)
        source_landing: dict[str, str] | None = None
        destination_staging: dict[str, str] | None = None
        destination_final: dict[str, str] | None = None
        organizer: dict[str, str] | None = None
        local_temp: dict[str, str] | None = None

        if strategy in _DATA_PLANE_STRATEGIES:
            if source_mount is None or not source_mount.readable:
                raise SourceLandingUnavailable("来源落地区不可读")
            source_landing = {
                "namespace_id": source_mount.namespace_id,
                "path": f"/{INGRESS_DIR}/{job}",
            }

        if strategy == STRATEGY_RCLONE_COPY:
            destination_staging, destination_final = self._rclone_paths(
                category, target, category_key, job
            )
        elif strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_NATIVE_OFFLINE, STRATEGY_RELAY_UPLOAD_MOBILE}:
            native = category.native_locator
            if native is None:
                raise NativeTransferUnavailable(f"{strategy} 缺少 native 定位器")
            namespace = native_namespace(target.provider_kind, target.destination_id)
            destination_final = {"namespace_id": namespace, "path": native.value}
            if strategy == STRATEGY_RELAY_UPLOAD_MOBILE:
                local_temp = {"path": f"{self._relay_root()}/{job}"}

        if organizer_enabled:
            organizer = self._build_organizer(category, category_key, job, destination_staging)

        return {
            "source_landing": source_landing,
            "destination_staging": destination_staging,
            "destination_final": destination_final,
            "organizer": organizer,
            "local_temp": local_temp,
        }

    def _rclone_paths(
        self, category: Any, target: Any, category_key: str, job: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        locator = category.rclone_locator
        if isinstance(locator, RcloneLocator):
            mount = self.mount_service.get(locator.mount_id)
            if mount is None:
                raise OpenlistUnavailable("目标 rclone 定位器挂载不存在")
            namespace = mount.namespace_id
            final_path = normalize_path(locator.relative_path)
            staging_path = f"/{STAGING_DIR}/{category_key}/{job}"
        elif isinstance(locator, LocalLocator):
            root = normalize_path(locator.root)
            if not local_contained(root, self._local_target_root()):
                raise OpenlistUnavailable("本地目标越出受控根目录")
            namespace = local_namespace(target.destination_id)
            final_path = normalize_path(f"{root}/{locator.relative_path}") if locator.relative_path else root
            staging_path = f"{root}/{STAGING_DIR}/{category_key}/{job}"
        else:
            raise OpenlistUnavailable("目标缺少可写的 rclone 或本地定位器")
        return (
            {"namespace_id": namespace, "path": staging_path},
            {"namespace_id": namespace, "path": final_path},
        )

    def _build_organizer(
        self,
        category: Any,
        category_key: str,
        job: str,
        destination_staging: dict[str, str] | None,
    ) -> dict[str, str]:
        organizer_locator = category.organizer_locator
        if organizer_locator is None:
            raise PlanningError(
                "Organizer 启用但目标分类未配置 organizer_locator",
                code="TARGET_INCOMPATIBLE",
            )
        namespace = openlist_namespace(organizer_locator.connection_id, organizer_locator.mount_id)
        if destination_staging is not None:
            # 执行暂存与 Organizer 可见目录是同一个任务级目录（§8.5）。
            return {"namespace_id": namespace, "path": f"/{STAGING_DIR}/{category_key}/{job}"}
        # native / 中继策略：Organizer 直接看到最终目录。
        return {"namespace_id": namespace, "path": organizer_locator.path}

    def _build_bindings(self, source_mount: Any, strategy: str, category: Any) -> dict[str, Any]:
        bindings: dict[str, Any] = {
            "source_mount_id": None,
            "source_mount_revision": None,
            "destination_mount_id": None,
            "destination_mount_revision": None,
        }
        if strategy in _DATA_PLANE_STRATEGIES and source_mount is not None:
            bindings["source_mount_id"] = source_mount.mount_id
            bindings["source_mount_revision"] = source_mount.revision
        if strategy == STRATEGY_RCLONE_COPY:
            locator = category.rclone_locator
            if isinstance(locator, RcloneLocator):
                mount = self.mount_service.get(locator.mount_id)
                if mount is not None:
                    bindings["destination_mount_id"] = mount.mount_id
                    bindings["destination_mount_revision"] = mount.revision
        return bindings

    def _idempotency_key(
        self,
        source: SourceDescriptor,
        category_key: str,
        descriptor: Any,
        source_account: tuple[str, int, int],
        destination_account: tuple[str, int, int] | None,
    ) -> str:
        destination_suffix = (
            f":{descriptor.destination_id}:{descriptor.target_revision}"
        )
        account_suffix = (
            f":{source_account[0]}:{source_account[1]}:{destination_account[0] if destination_account else '-'}"
        )
        return (
            f"import:v4:{source.canonical_source_id}"
            f":{source.selected_items_hash or '-'}:{category_key}"
            f"{destination_suffix}{account_suffix}"
        )

    # ---- 部署级配置 ---------------------------------------------------------

    def _local_target_root(self) -> str:
        return str(self.config.get("FNOS_LOCAL_TARGET_ROOT") or DEFAULT_LOCAL_TARGET_ROOT)

    def _relay_root(self) -> str:
        return str(self.config.get("FNOS_RELAY_ROOT") or DEFAULT_RELAY_ROOT)
