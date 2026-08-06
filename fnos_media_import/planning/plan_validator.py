"""PlanValidator v4：按 §12.4 校验冻结计划与任务身份一致。

生成器和校验器必须执行相同的安全约束，校验器不能比生成器宽松。任何字段缺失、
修订被非法删除或身份不一致都失败关闭，绝不根据当前后台配置“补全”计划
（§12.4）。计划校验使用冻结计划中的数据与任务数据库列，只读实时服务状态用于
存在性/修订/能力复核，不重建计划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..domain.models import SourceDescriptor
from . import (
    DEFAULT_LOCAL_TARGET_ROOT,
    EXECUTION_PLAN_VERSION,
    STAGING_DIR,
    STRATEGY_EXECUTOR,
    STRATEGY_NATIVE_OFFLINE,
    STRATEGY_NATIVE_SAVE,
    STRATEGY_RCLONE_COPY,
    STRATEGY_RELAY_UPLOAD_MOBILE,
    VALID_SOURCE_FORMS,
    VALID_STRATEGIES,
    AdapterRegistry,
    AccountsService,
    MountService,
    PlanValidationError,
    TargetService,
    job_dir,
    local_contained,
    namespace_kind,
    path_is_safe,
    path_leaf,
    paths_overlap,
    split_openlist_namespace,
)

_DATA_PLANE_STRATEGIES = frozenset({STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD_MOBILE})


@dataclass(frozen=True)
class PlanValidationDeps:
    """校验依赖。测试用 fakes 注入；真实服务后续按同一接口接入。"""

    accounts_service: AccountsService
    target_service: TargetService
    mount_service: MountService
    registry: AdapterRegistry
    config: Mapping[str, Any] = field(default_factory=dict)


def validate_v4_plan(plan: dict[str, Any], job_record: dict[str, Any], *, deps: PlanValidationDeps) -> dict[str, Any]:
    """对冻结计划执行完整 v4 校验；失败抛 ``PlanValidationError``。"""
    return PlanValidator(deps).validate(plan, job_record)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_or(value: Any, default: Any = None) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str:
    return str(value or "").strip()


class PlanValidator:
    def __init__(self, deps: PlanValidationDeps):
        self.deps = deps
        self.accounts_service = deps.accounts_service
        self.target_service = deps.target_service
        self.mount_service = deps.mount_service
        self.registry = deps.registry
        self.config = deps.config if isinstance(deps.config, Mapping) else {}

    def validate(self, plan: dict[str, Any], job_record: dict[str, Any]) -> dict[str, Any]:
        plan = plan if isinstance(plan, dict) else {}
        job = job_record if isinstance(job_record, dict) else {}
        errors: list[str] = []

        # ---- 版本 / job_id / destination_id 与数据库列一致 --------------------
        job_id = _int_or(job.get("id"), 0)
        plan_job_id = _int_or(plan.get("job_id"), 0)
        persisted_version = _int_or(job.get("plan_version"), 0)
        if plan.get("version") != EXECUTION_PLAN_VERSION:
            errors.append(f"计划版本必须为 {EXECUTION_PLAN_VERSION}")
        if persisted_version != EXECUTION_PLAN_VERSION:
            errors.append("任务 plan_version 不是 v4")
        if job_id <= 0 or plan_job_id != job_id:
            errors.append("任务 id 与计划 job_id 不一致")

        destination = _mapping(plan.get("destination"))
        plan_destination = _str(destination.get("id"))
        persisted_destination = _str(job.get("destination_id"))
        if not plan_destination:
            errors.append("计划缺少 destination.id")
        if not persisted_destination or persisted_destination != plan_destination:
            errors.append("计划目标与任务 destination_id 不一致")

        source = _mapping(plan.get("source"))
        transfer = _mapping(plan.get("transfer"))
        bindings = _mapping(plan.get("bindings"))
        paths = _mapping(plan.get("paths"))
        postprocess = _mapping(plan.get("postprocess"))

        # ---- 基础传输字段 -----------------------------------------------------
        strategy = _str(transfer.get("strategy"))
        if strategy not in VALID_STRATEGIES:
            errors.append(f"未知传输策略：{strategy or '(空)'}")
        executor = _str(transfer.get("executor"))
        if strategy and executor != STRATEGY_EXECUTOR.get(strategy):
            errors.append("传输策略与执行器不一致")
        if not _str(transfer.get("idempotency_key")):
            errors.append("计划缺少 transfer.idempotency_key")

        # ---- Provider / 账号 / 修订存在性与一致性 ------------------------------
        source_provider = _str(source.get("provider_kind"))
        source_form = _str(source.get("source_form"))
        if not source_provider:
            errors.append("计划来源缺少 provider_kind")
        if source_form not in VALID_SOURCE_FORMS:
            errors.append(f"来源形态不受支持：{source_form or '(空)'}")
        if not _str(source.get("canonical_source_id")):
            errors.append("计划来源缺少 canonical_source_id")

        source_account_id = _str(source.get("account_id"))
        source_account_revision = _int_or(source.get("account_revision"))
        source_selection_revision = _int_or(source.get("account_selection_revision"))
        if source_provider:
            if not source_account_id or source_account_revision is None or source_selection_revision is None:
                errors.append("计划来源缺少完整账号身份")
            else:
                self._validate_account(
                    provider_kind=source_provider,
                    account_id=source_account_id,
                    account_revision=source_account_revision,
                    selection_revision=source_selection_revision,
                    role="source",
                    errors=errors,
                )

        dest_provider = _str(destination.get("provider_kind"))
        target_revision = _int_or(destination.get("target_revision"))
        if not dest_provider:
            errors.append("计划目标缺少 provider_kind")
        target = None
        if plan_destination and target_revision is None:
            errors.append("计划目标缺少 target_revision")
        elif plan_destination:
            target = self.target_service.get_revision(plan_destination, target_revision)
            if target is None:
                errors.append(f"目标修订不存在：{plan_destination}@{target_revision}")
            elif target.provider_kind != dest_provider:
                errors.append("目标修订的 Provider 与计划不一致")

        dest_account_id = _str(destination.get("account_id"))
        if dest_account_id:
            dest_account_revision = _int_or(destination.get("account_revision"))
            dest_selection_revision = _int_or(destination.get("account_selection_revision"))
            if dest_account_revision is None or dest_selection_revision is None:
                errors.append("计划目标缺少完整账号身份")
            elif dest_provider:
                self._validate_account(
                    provider_kind=dest_provider,
                    account_id=dest_account_id,
                    account_revision=dest_account_revision,
                    selection_revision=dest_selection_revision,
                    role="destination",
                    errors=errors,
                )

        category = None
        if target is not None:
            category_key = self._category_key(plan, job)
            category = target.category(category_key)
            if category is None and plan_destination:
                errors.append(f"目标 {plan_destination} 未配置分类 {category_key or '(空)'}")

        # ---- 策略约束 ---------------------------------------------------------
        self._validate_strategy(
            plan=plan,
            source=source,
            destination=destination,
            target=target,
            category=category,
            errors=errors,
        )

        # ---- 命名空间 / 路径身份、重叠、本地越界、任务级叶子 -------------------
        self._validate_paths(
            plan=plan,
            source=source,
            destination=destination,
            target=target,
            paths=paths,
            bindings=bindings,
            strategy=strategy,
            errors=errors,
        )

        # ---- Organizer --------------------------------------------------------
        self._validate_organizer(plan, paths, errors)

        if errors:
            raise PlanValidationError(
                "执行计划校验失败：" + "；".join(dict.fromkeys(errors)),
                code="PLAN_VALIDATION_FAILED",
            )
        return plan

    # ---- 账号一致性 ----------------------------------------------------------

    def _validate_account(
        self,
        *,
        provider_kind: str,
        account_id: str,
        account_revision: int,
        selection_revision: int,
        role: str,
        errors: list[str],
    ) -> None:
        account = self.accounts_service.get_account(account_id)
        if account is None:
            errors.append(f"{role} 账号不存在：{account_id}")
            return
        if account.provider_kind != provider_kind:
            errors.append(f"{role} 账号 Provider 不匹配")
        if not account.enabled:
            errors.append(f"{role} 账号已禁用：{account_id}")
        if not self.accounts_service.has_revision(account_id, account_revision):
            errors.append(f"{role} 账号修订不存在：{account_id}@{account_revision}")
        selection = self.accounts_service.get_selection(provider_kind, "default")
        if selection is None:
            errors.append(f"{role} Provider 没有激活账号选择：{provider_kind}")
        elif (
            selection.account_id != account_id
            or selection.account_revision != account_revision
            or selection.selection_revision != selection_revision
        ):
            errors.append(f"{role} 账号选择修订与计划不一致")

    # ---- 策略约束 ------------------------------------------------------------

    def _validate_strategy(
        self,
        *,
        plan: dict[str, Any],
        source: dict[str, Any],
        destination: dict[str, Any],
        target: Any,
        category: Any,
        errors: list[str],
    ) -> None:
        strategy = _str(plan.get("transfer", {}).get("strategy"))
        source_provider = _str(source.get("provider_kind"))
        source_form = _str(source.get("source_form"))
        dest_provider = _str(destination.get("provider_kind"))

        if source_form == "magnet" and strategy != STRATEGY_NATIVE_OFFLINE:
            errors.append("磁链来源只允许 native_offline 策略")

        if strategy == STRATEGY_NATIVE_SAVE:
            if source_provider != dest_provider:
                errors.append("native_save 必须同源同目标 Provider")
            if source_form != "share":
                errors.append("native_save 只适用于分享来源")
            if category is None or category.native_locator is None:
                errors.append("native_save 需要目标 native 定位器")
            else:
                decision = self.registry.get(source_provider).can_native_transfer(
                    self._source_descriptor(source),
                    category.native_locator,
                    self._destination_account_ctx(destination),
                )
                if not decision.available:
                    errors.append(f"native_save 未通过 Adapter 原生绑定校验：{decision.reason}")
        elif strategy == STRATEGY_NATIVE_OFFLINE:
            if source_form != "magnet":
                errors.append("native_offline 只适用于磁链来源")
            if dest_provider != "sixpan":
                errors.append("native_offline 目标必须是 sixpan")
            if category is None or category.native_locator is None:
                errors.append("native_offline 需要六盘 native 定位器")
            if dest_provider == "sixpan":
                if not self.registry.get("sixpan").capabilities().supports_native_offline:
                    errors.append("六盘 Adapter 未提供 native_offline 能力")
        elif strategy == STRATEGY_RELAY_UPLOAD_MOBILE:
            if dest_provider != "mobile":
                errors.append("relay_upload_mobile 目标必须是 mobile")
            if source_provider == "mobile":
                errors.append("移动来源不能走移动云中继")
            if dest_provider == "mobile" and self.registry.get("mobile").relay_uploader() is None:
                errors.append("移动云 Adapter 未提供中继上传器")
        elif strategy == STRATEGY_RCLONE_COPY:
            # 可读/可写定位器在 _validate_paths 中按 mount/本地根复核。
            if category is None or category.rclone_locator is None:
                errors.append("rclone_copy 需要目标 rclone 或本地定位器")
            if source_form == "magnet":
                errors.append("磁链不允许 rclone_copy")
        else:
            errors.append("计划策略未知，无法校验")

    # ---- 路径 / 命名空间 ------------------------------------------------------

    def _validate_paths(
        self,
        *,
        plan: dict[str, Any],
        source: dict[str, Any],
        destination: dict[str, Any],
        target: Any,
        paths: dict[str, Any],
        bindings: dict[str, Any],
        strategy: str,
        errors: list[str],
    ) -> None:
        job = job_dir(_int_or(plan.get("job_id"), 0))
        source_landing = _mapping(paths.get("source_landing"))
        staging = _mapping(paths.get("destination_staging"))
        final = _mapping(paths.get("destination_final"))
        local_temp = _mapping(paths.get("local_temp"))

        refs: list[tuple[str, dict[str, Any]]] = [
            ("source_landing", source_landing),
            ("destination_staging", staging),
            ("destination_final", final),
        ]
        valid_refs: list[tuple[str, str, str, str]] = []
        for label, ref in refs:
            if not ref:
                continue
            namespace = _str(ref.get("namespace_id"))
            path = _str(ref.get("path"))
            if not namespace or not path:
                errors.append(f"paths.{label} 缺少 namespace_id 或 path")
                continue
            if not path_is_safe(path):
                errors.append(f"paths.{label} 路径不安全")
            kind = namespace_kind(namespace)
            if kind not in {"openlist", "local", "native"}:
                errors.append(f"paths.{label} 命名空间不受支持")
            if kind == "openlist" and split_openlist_namespace(namespace) is None:
                errors.append(f"paths.{label} openlist 命名空间格式非法")
            valid_refs.append((label, namespace, path, kind))

        # 相同命名空间下 landing/staging/final 不得重叠（§8.5）。
        for i in range(len(valid_refs)):
            for j in range(i + 1, len(valid_refs)):
                label_a, namespace_a, path_a, _kind_a = valid_refs[i]
                label_b, namespace_b, path_b, _kind_b = valid_refs[j]
                if namespace_a == namespace_b and paths_overlap(namespace_a, path_a, namespace_b, path_b):
                    errors.append(f"paths.{label_a} 与 paths.{label_b} 在同一命名空间重叠")

        # 本地越界（§8.3）。
        local_root = _str(self.config.get("FNOS_LOCAL_TARGET_ROOT") or DEFAULT_LOCAL_TARGET_ROOT)
        for label, namespace, path, kind in valid_refs:
            if kind == "local" and not local_contained(path, local_root):
                errors.append(f"paths.{label} 越出受控本地根目录")

        # 任务级 landing/staging 叶子必须与 job_id 一致（§12.4）。
        if strategy in _DATA_PLANE_STRATEGIES:
            if not source_landing:
                errors.append("跨盘/中继策略缺少 source_landing")
            elif path_leaf(source_landing.get("path")) != job:
                errors.append("source_landing 不是任务级目录")
        if strategy == STRATEGY_RCLONE_COPY:
            if not staging:
                errors.append("rclone_copy 缺少 destination_staging")
            elif path_leaf(staging.get("path")) != job:
                errors.append("destination_staging 不是任务级目录")
        elif strategy == STRATEGY_RELAY_UPLOAD_MOBILE:
            if not local_temp or not _str(local_temp.get("path")):
                errors.append("relay_upload_mobile 缺少 local_temp")

        # ---- bindings / mount 一致性 -------------------------------------------
        source_mount_id = _str(bindings.get("source_mount_id"))
        destination_mount_id = _str(bindings.get("destination_mount_id"))
        if source_landing:
            if not source_mount_id:
                errors.append("source_landing 缺少来源 mount 绑定")
            else:
                mount = self.mount_service.get(source_mount_id)
                if mount is None:
                    errors.append(f"来源 mount 不存在：{source_mount_id}")
                else:
                    if not mount.readable:
                        errors.append("来源 mount 不可读")
                    if mount.namespace_id != _str(source_landing.get("namespace_id")):
                        errors.append("来源 landing 命名空间与 mount 不一致")
                    if _int_or(bindings.get("source_mount_revision"), 0) != mount.revision:
                        errors.append("来源 mount 修订不匹配")

        staging_kind = namespace_kind(staging.get("namespace_id")) if staging else ""
        if strategy == STRATEGY_RCLONE_COPY and staging:
            if staging_kind == "openlist":
                if not destination_mount_id:
                    errors.append("目标暂存缺少目标 mount 绑定")
                else:
                    mount = self.mount_service.get(destination_mount_id)
                    if mount is None:
                        errors.append(f"目标 mount 不存在：{destination_mount_id}")
                    else:
                        if not mount.writable:
                            errors.append("目标 mount 不可写")
                        if mount.namespace_id != _str(staging.get("namespace_id")):
                            errors.append("目标暂存命名空间与 mount 不一致")
                        if _int_or(bindings.get("destination_mount_revision"), 0) != mount.revision:
                            errors.append("目标 mount 修订不匹配")
            elif staging_kind == "local":
                if destination_mount_id:
                    errors.append("本地目标不应绑定 OpenList mount")
            elif staging_kind == "native":
                errors.append("rclone_copy 目标暂存不能在 native 命名空间")

        if strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_NATIVE_OFFLINE}:
            if source_mount_id:
                errors.append("native 策略不应绑定来源 mount")

    # ---- Organizer -----------------------------------------------------------

    def _validate_organizer(self, plan: dict[str, Any], paths: dict[str, Any], errors: list[str]) -> None:
        postprocess = _mapping(plan.get("postprocess"))
        if not bool(postprocess.get("organizer_enabled")):
            return
        organizer = _mapping(paths.get("organizer"))
        if not organizer:
            errors.append("Organizer 启用但计划缺少 organizer 路径")
            return
        namespace = _str(organizer.get("namespace_id"))
        path = _str(organizer.get("path"))
        if not namespace or not path:
            errors.append("organizer 路径不完整")
        if not path_is_safe(path):
            errors.append("organizer 路径不安全")
        if namespace_kind(namespace) != "openlist":
            errors.append("organizer 必须位于 openlist 命名空间")
        staging = _mapping(paths.get("destination_staging"))
        if staging:
            if path_leaf(path) != job_dir(_int_or(plan.get("job_id"), 0)):
                errors.append("organizer 路径不是任务级目录")
        else:
            final = _mapping(paths.get("destination_final"))
            if namespace_kind(final.get("namespace_id")) != "native":
                errors.append("native 策略缺少可验证的 organizer 映射")

    # ---- 工具 ----------------------------------------------------------------

    def _category_key(self, plan: dict[str, Any], job: dict[str, Any]) -> str:
        key = _str(job.get("category"))
        if key:
            return key
        staging = _mapping(plan.get("paths", {}).get("destination_staging"))
        path = _str(staging.get("path"))
        marker = f"/{STAGING_DIR}/"
        if marker in path:
            rest = path.split(marker, 1)[1]
            if "/" in rest:
                return rest.split("/", 1)[0]
        return ""

    @staticmethod
    def _source_descriptor(source: dict[str, Any]) -> SourceDescriptor:
        return SourceDescriptor(
            provider_kind=_str(source.get("provider_kind")),
            source_form=_str(source.get("source_form")),
            canonical_source_id=_str(source.get("canonical_source_id")),
            selected_items_hash=_str(source.get("selection_hash")),
        )

    @staticmethod
    def _destination_account_ctx(destination: dict[str, Any]) -> dict[str, Any]:
        return {
            "account_id": _str(destination.get("account_id")) or None,
            "revision": _int_or(destination.get("account_revision")),
        }
