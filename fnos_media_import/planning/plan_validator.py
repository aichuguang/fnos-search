"""V1 PlanValidator：校验冻结计划与任务身份一致。

生成器和校验器必须执行相同的安全约束，校验器不能比生成器宽松。任何字段缺失、
修订被非法删除或身份不一致都失败关闭，绝不根据当前后台配置“补全”计划
（§12.4）。计划校验使用冻结计划中的数据与任务数据库列，只读实时服务状态用于
存在性/修订/能力复核，不重建计划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..domain.models import (
    LocalLocator,
    NativeLocator,
    ProviderCapabilities,
    RcloneLocator,
    SelectionSnapshot,
    SourceDescriptor,
)
from ..storage.assignment_service import assignment_id as stable_assignment_id
from ..storage.target_service import encode_categories
from . import (
    DEFAULT_LOCAL_TARGET_ROOT,
    DEFAULT_RELAY_ROOT,
    DATA_PLANE_STRATEGIES,
    EXECUTION_PLAN_VERSION,
    NATIVE_STAGING_DIR,
    STAGING_DIR,
    STRATEGY_EXECUTOR,
    STRATEGY_NATIVE_OFFLINE,
    STRATEGY_NATIVE_SAVE,
    STRATEGY_RCLONE_COPY,
    STRATEGY_RELAY_UPLOAD_PROVIDER,
    VALID_SOURCE_FORMS,
    VALID_STRATEGIES,
    AdapterRegistry,
    AccountsService,
    MountService,
    PlanValidationError,
    TargetService,
    job_dir,
    local_contained,
    mount_absolute_path,
    mount_webdav_root,
    namespace_kind,
    native_staging_locator,
    native_staging_path,
    normalize_path,
    path_is_safe,
    path_leaf,
    paths_overlap,
    split_openlist_namespace,
    is_relay_upload_strategy,
)
from .job_plan import canonical_plan_json, compute_operation_key, compute_plan_hash

@dataclass(frozen=True)
class PlanValidationDeps:
    """校验依赖。测试用 fakes 注入；真实服务后续按同一接口接入。"""

    accounts_service: AccountsService
    target_service: TargetService
    mount_service: MountService
    registry: AdapterRegistry
    source_record_service: Any = None
    assignment_service: Any = None
    config: Mapping[str, Any] = field(default_factory=dict)


def validate_plan(plan: dict[str, Any], job_record: dict[str, Any], *, deps: PlanValidationDeps) -> dict[str, Any]:
    """对冻结计划执行完整 V1 校验；失败抛 ``PlanValidationError``。"""
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
        schema_version = _int_or(plan.get("plan_schema_version"), 0)
        if plan.get("version") != EXECUTION_PLAN_VERSION:
            errors.append(f"计划版本必须为 {EXECUTION_PLAN_VERSION}")
        if schema_version != EXECUTION_PLAN_VERSION:
            errors.append(f"计划 plan_schema_version 必须为 {EXECUTION_PLAN_VERSION}")
        if persisted_version != EXECUTION_PLAN_VERSION:
            errors.append("任务 plan_version 不是 V1")
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
        verification = _mapping(plan.get("verification"))

        # ---- 基础传输字段 -----------------------------------------------------
        strategy = _str(transfer.get("strategy"))
        if strategy not in VALID_STRATEGIES:
            errors.append(f"未知传输策略：{strategy or '(空)'}")
        executor = _str(transfer.get("executor"))
        if strategy and executor != STRATEGY_EXECUTOR.get(strategy):
            errors.append("传输策略与执行器不一致")
        if not _str(transfer.get("idempotency_key")):
            errors.append("计划缺少 transfer.idempotency_key")
        else:
            expected_operation_key = compute_operation_key(plan)
            if _str(transfer.get("idempotency_key")) != expected_operation_key:
                errors.append("transfer.idempotency_key 与冻结身份不一致")
        integrity = _mapping(plan.get("integrity"))
        if _str(integrity.get("algorithm")).lower() != "sha256":
            errors.append("计划 integrity.algorithm 必须为 sha256")
        if _str(integrity.get("plan_hash")) != compute_plan_hash(plan):
            errors.append("计划 integrity.plan_hash 校验失败")


        # ---- Provider / 账号 / 修订存在性与一致性 ------------------------------
        source_provider = _str(source.get("provider_kind"))
        source_form = _str(source.get("source_form"))
        if not source_provider:
            errors.append("计划来源缺少 provider_kind")
        if source_form not in VALID_SOURCE_FORMS:
            errors.append(f"来源形态不受支持：{source_form or '(空)'}")
        if not _str(source.get("canonical_source_id")):
            errors.append("计划来源缺少 canonical_source_id")
        source_record_id = _str(source.get("record_id"))
        source_record_revision = _int_or(source.get("record_revision"), 0)
        if not source_record_id or source_record_revision <= 0:
            errors.append("计划来源缺少不可变来源快照")
        elif self.deps.source_record_service is None:
            errors.append("来源快照校验服务未装配")
        elif not self.deps.source_record_service.has_revision(
            source_record_id, source_record_revision
        ):
            errors.append(f"来源快照修订不存在：{source_record_id}@{source_record_revision}")
        elif callable(getattr(self.deps.source_record_service, "resolve", None)):
            self._validate_source_snapshot(
                source=source,
                source_record_id=source_record_id,
                source_record_revision=source_record_revision,
                verification=verification,
                errors=errors,
            )

        # A V1 plan must carry the complete provider-neutral selection, not
        # only a denormalised hash.  The immutable source-record comparison
        # below then proves that this exact selection was captured.
        selection_payload = source.get("selection")
        selection_hash = _str(source.get("selection_hash"))
        if not isinstance(selection_payload, Mapping):
            errors.append("计划来源缺少 selection 快照")
        if not selection_hash:
            errors.append("计划来源缺少 selection_hash")
        self._validate_verification(
            verification=verification,
            source_selection_hash=selection_hash,
            errors=errors,
        )

        source_account_id = _str(source.get("account_id"))
        source_account_revision = _int_or(source.get("account_revision"))
        source_selection_revision = _int_or(source.get("account_selection_revision"))
        if source_provider:
            if (
                not source_account_id
                or source_account_revision is None
                or source_account_revision <= 0
                or source_selection_revision is None
                or source_selection_revision <= 0
            ):
                errors.append("source account revision/selection revision 不完整")
            else:
                self._validate_account(
                    provider_kind=source_provider,
                    account_id=source_account_id,
                    account_revision=source_account_revision,
                    selection_revision=source_selection_revision,
                    selection_scope=_str(source.get("account_scope")) or "default",
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
            try:
                target = self.target_service.get_target(plan_destination, target_revision)
            except Exception as exc:  # noqa: BLE001 - validation must fail closed
                target = None
                errors.append(
                    f"目标修订读取失败：{plan_destination}@{target_revision} ({exc})"
                )
            if target is None:
                errors.append(f"目标修订不存在：{plan_destination}@{target_revision}")
            elif target.provider_kind != dest_provider:
                errors.append("目标修订的 Provider 与计划不一致")

        dest_account_id = _str(destination.get("account_id"))
        binding_id = _str(destination.get("binding_id"))
        binding_revision = _int_or(destination.get("binding_revision"))
        binding = None
        if not binding_id or binding_revision is None:
            errors.append("计划目标缺少 binding 身份")
        else:
            try:
                binding = self.target_service.get_binding(binding_id, binding_revision)
            except Exception as exc:  # noqa: BLE001 - validation must fail closed
                binding = None
                errors.append(
                    f"目标 binding 修订读取失败：{binding_id}@{binding_revision} ({exc})"
                )
            if binding is None:
                errors.append(f"目标 binding 修订不存在：{binding_id}@{binding_revision}")
            else:
                if binding.destination_id != plan_destination or binding.target_revision != target_revision:
                    errors.append("目标 binding 与目标修订不一致")
                if binding.provider_kind != dest_provider:
                    errors.append("目标 binding Provider 不匹配")
                if binding.account_id != (dest_account_id or None):
                    errors.append("目标 binding 账号与计划不一致")
                if _int_or(getattr(binding, "current_revision", 0), 0) != binding_revision:
                    errors.append("目标 binding 修订与计划不一致")
        if dest_account_id:
            dest_account_revision = _int_or(destination.get("account_revision"))
            if dest_account_revision is None or dest_account_revision <= 0:
                errors.append("destination account revision 不完整")
            elif dest_provider:
                if binding is not None and binding.account_revision != dest_account_revision:
                    errors.append("目标 binding 账号修订与计划不一致")
                self._validate_account(
                    provider_kind=dest_provider,
                    account_id=dest_account_id,
                    account_revision=dest_account_revision,
                    selection_revision=_int_or(destination.get("account_selection_revision")),
                    selection_scope=_str(destination.get("account_scope")) or "default",
                    role="destination",
                    errors=errors,
                )

        category_key = self._category_key(plan, job)
        planned_category = _str(plan.get("category"))
        persisted_category = _str(job.get("category"))
        if not planned_category:
            errors.append("计划缺少 category")
        elif persisted_category and planned_category != persisted_category:
            errors.append("计划 category 与任务 category 不一致")
        if not category_key:
            errors.append("计划缺少 category_key")
        category = None
        if target is not None:
            category = target.category(category_key)
            if category is None and plan_destination:
                errors.append(f"target {plan_destination} 没有分类 {category_key or '(空)'}")

        self._validate_target_category_snapshot(
            plan=plan,
            category=category,
            category_key=category_key,
            errors=errors,
        )

        self._validate_routing(plan, source, destination, category_key, errors)

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
            category=category,
            category_key=category_key,
            paths=paths,
            bindings=bindings,
            strategy=strategy,
            errors=errors,
            job_id=job_id,
        )

        # ---- Organizer --------------------------------------------------------
        self._validate_organizer(plan, paths, bindings, category, errors)

        if errors:
            raise PlanValidationError(
                "执行计划校验失败：" + "；".join(dict.fromkeys(errors)),
                code="PLAN_VALIDATION_FAILED",
            )
        return plan

    def _validate_source_snapshot(
        self,
        *,
        source: dict[str, Any],
        source_record_id: str,
        source_record_revision: int,
        verification: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Compare every plan source identity against the immutable snapshot."""

        try:
            snapshot_payload = self.deps.source_record_service.resolve(
                source_record_id, source_record_revision
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, keep diagnostics
            errors.append(f"source snapshot 无法解析：{exc}")
            return
        if not isinstance(snapshot_payload, Mapping):
            errors.append("source snapshot 不是对象")
            return
        for plan_key, snapshot_key in (
            ("provider_kind", "provider_kind"),
            ("source_form", "source_form"),
            ("canonical_source_id", "canonical_source_id"),
        ):
            value = _str(snapshot_payload.get(snapshot_key))
            planned = _str(source.get(plan_key))
            if not value or not planned:
                errors.append(f"source snapshot 缺少 {plan_key}")
            elif value != planned:
                errors.append(f"source snapshot {plan_key} 与计划不一致")

        for plan_key, snapshot_key in (
            ("expected_file_count", "expected_file_count"),
            ("expected_total_size", "expected_total_bytes"),
        ):
            try:
                planned_value = int(verification.get(plan_key))
                stored_value = int(snapshot_payload.get(snapshot_key))
            except (TypeError, ValueError):
                errors.append(f"source snapshot {snapshot_key} metadata 无效")
                continue
            if planned_value < 0 or planned_value != stored_value:
                errors.append(f"verification.{plan_key} 与 source snapshot 不一致")

        selection_payload = source.get("selection")
        snapshot_selection_payload = snapshot_payload.get("selection")
        try:
            planned_selection = SelectionSnapshot.from_dict(
                selection_payload if isinstance(selection_payload, Mapping) else {},
                provider_kind=_str(source.get("provider_kind")),
            )
            # ``SourceRecordService`` stores the canonical selection under the
            # dedicated ``selection`` key.  Re-parsing the whole decrypted
            # payload with ``from_payload`` would treat that serialized
            # snapshot as another provider-specific selection and change the
            # hash (an integrity false positive).  Prefer the canonical object
            # and only use the generic parser for historical snapshots that do
            # not contain it.
            if isinstance(snapshot_selection_payload, Mapping):
                stored_selection = SelectionSnapshot.from_dict(
                    snapshot_selection_payload,
                    provider_kind=_str(source.get("provider_kind")),
                )
            else:
                stored_selection = SelectionSnapshot.from_payload(
                    _str(source.get("provider_kind")), snapshot_payload
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"source selection snapshot 无效：{exc}")
            return
        planned_hash = _str(source.get("selection_hash"))
        if selection_payload and not planned_selection.is_valid:
            errors.append("source.selection hash 无效")
        if selection_payload and planned_hash and planned_hash != planned_selection.selection_hash:
            errors.append("source.selection_hash 与 source.selection 不一致")
        if selection_payload and stored_selection.selection_hash != planned_selection.selection_hash:
            errors.append("source selection 与不可变 source snapshot 不一致")
        if snapshot_selection_payload and isinstance(snapshot_selection_payload, Mapping):
            stored_declared_hash = _str(
                snapshot_selection_payload.get("selection_hash")
                or snapshot_selection_payload.get("hash")
            )
            if stored_declared_hash and stored_declared_hash != stored_selection.selection_hash:
                errors.append("存储的 source selection hash 无效")

    @staticmethod
    def _validate_verification(
        *,
        verification: dict[str, Any],
        source_selection_hash: str,
        errors: list[str],
    ) -> None:
        """确保执行计划不能通过篡改验证阈值来提前完成。"""

        if not verification:
            errors.append("计划缺少 verification 快照")
            return
        for key in ("expected_file_count", "expected_total_size"):
            value = _int_or(verification.get(key), None)
            if value is None or value < 0:
                errors.append(f"verification.{key} 无效")
        minimum = verification.get("minimum")
        if not isinstance(minimum, (list, tuple, set, frozenset)):
            errors.append("verification.minimum 无效")
        elif not {str(item) for item in minimum}.issuperset({"file_count", "total_size"}):
            errors.append("verification.minimum 必须包含 file_count 和 total_size")
        declared_selection_hash = _str(verification.get("selection_hash"))
        if not declared_selection_hash:
            errors.append("verification.selection_hash 缺失")
        elif declared_selection_hash != source_selection_hash:
            errors.append("verification.selection_hash 与 source.selection_hash 不一致")

    @staticmethod
    def _validate_target_category_snapshot(
        *,
        plan: dict[str, Any],
        category: Any,
        category_key: str,
        errors: list[str],
    ) -> None:
        """证明执行器读取的 locator 属于冻结 target revision。"""

        if category is None:
            return
        frozen = plan.get("target_category")
        if not isinstance(frozen, Mapping):
            errors.append("计划缺少 target_category 冻结快照")
            return
        expected = encode_categories({category_key: category}).get(category_key)
        if canonical_plan_json(frozen) != canonical_plan_json(expected):
            errors.append("target_category 与目标修订 locator 不一致")

    def _validate_routing(
        self,
        plan: dict[str, Any],
        source: dict[str, Any],
        destination: dict[str, Any],
        category_key: str,
        errors: list[str],
    ) -> None:
        routing = _mapping(plan.get("routing"))
        if not routing:
            errors.append("计划缺少冻结 routing assignment")
            return
        expected_source = _str(source.get("search_source_id"))
        if not expected_source:
            errors.append("source.search_source_id 缺失")
        elif _str(routing.get("search_source_id")) != expected_source:
            errors.append("routing search_source_id 与 source 不一致")
        if _str(routing.get("category_key")) != category_key:
            errors.append("routing category 与计划不一致")
        routed_destination = _str(routing.get("destination_id"))
        if not routed_destination or routed_destination != _str(destination.get("id")):
            errors.append("routing destination 与计划不一致")
        revision = _int_or(routing.get("assignment_revision"))
        assignment_id = _str(routing.get("assignment_id"))
        if not assignment_id or revision is None or revision <= 0:
            errors.append("routing assignment revision 缺失")
        elif expected_source and _str(routing.get("category_key")):
            expected_assignment_id = stable_assignment_id(
                expected_source, _str(routing.get("category_key"))
            )
            if assignment_id != expected_assignment_id:
                errors.append("routing assignment_id 不符合稳定来源/分类身份")
        if not bool(routing.get("enabled")):
            errors.append("routing assignment 已停用")
        # If the service exposes historical assignment revisions, compare the
        # frozen destination.  Never replace the plan with the current route.
        getter = getattr(self.deps, "assignment_service", None)
        if getter is None:
            errors.append("assignment 历史校验服务未装配")
        else:
            resolver = getattr(getter, "get_revision", None)
            if not callable(resolver):
                errors.append("assignment 服务不支持历史 revision")
            elif assignment_id and revision is not None:
                try:
                    historical = resolver(assignment_id, revision)
                except Exception:  # noqa: BLE001
                    historical = None
                if historical is None:
                    errors.append("routing assignment revision 不存在")
                elif isinstance(historical, Mapping):
                    if (
                        _str(historical.get("search_source_id")) != expected_source
                        or _str(historical.get("category_key")) != category_key
                        or _str(historical.get("destination_id")) != _str(destination.get("id"))
                        or not bool(historical.get("enabled"))
                    ):
                        errors.append("routing assignment revision 身份不一致")
                else:
                    # Dataclass implementations are accepted only when every
                    # frozen field is present; no current-row fallback.
                    if (
                        _str(getattr(historical, "search_source_id", "")) != expected_source
                        or _str(getattr(historical, "category_key", "")) != category_key
                        or _str(getattr(historical, "destination_id", "")) != _str(destination.get("id"))
                        or not bool(getattr(historical, "enabled", False))
                    ):
                        errors.append("routing assignment revision 身份不一致")

    # ---- 账号一致性 ----------------------------------------------------------

    def _validate_account(
        self,
        *,
        provider_kind: str,
        account_id: str,
        account_revision: int,
        selection_revision: int | None,
        selection_scope: str,
        role: str,
        errors: list[str],
    ) -> None:
        account = self.accounts_service.get_account(account_id)
        if account is None:
            errors.append(f"{role} 账号不存在：{account_id}")
            return
        if account.provider_kind != provider_kind:
            errors.append(f"{role} 账号 Provider 不匹配")
        if not self.accounts_service.has_revision(account_id, account_revision):
            errors.append(f"{role} 账号修订不存在：{account_id}@{account_revision}")
        # 在途任务只依赖冻结账号修订。当前活动账号切换不得使计划失效；
        # selection_revision 通过追加历史表验证创建时的合法组合。
        if selection_revision is None or selection_revision <= 0:
            if role == "source":
                errors.append(f"{role} 账号选择修订无效")
            return
        getter = getattr(self.accounts_service, "get_selection_revision", None)
        if not callable(getter):
            # Test doubles and transitional composition roots may only expose
            # the minimal AccountsService protocol.  They still get the
            # account/revision checks above; a real service always implements
            # the historical lookup.
            return
        try:
            selection = getter(provider_kind, selection_scope, selection_revision)
        except Exception:  # noqa: BLE001 - fail closed without leaking details
            selection = None
        if selection is None:
            errors.append(f"{role} 账号选择修订不存在：{provider_kind}/{selection_scope}@{selection_revision}")
        elif (
            str(getattr(selection, "account_id", "") or "") != account_id
            or int(getattr(selection, "account_revision", 0) or 0) != account_revision
        ):
            errors.append(f"{role} 账号修订与选择修订不匹配")

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
        transfer = _mapping(plan.get("transfer"))
        strategy = _str(transfer.get("strategy"))
        source_provider = _str(source.get("provider_kind"))
        source_form = _str(source.get("source_form"))
        dest_provider = _str(destination.get("provider_kind"))
        source_adapter = self._adapter(source_provider, errors, "source") if source_provider else None
        # ``local``/``generic``/``openlist_generic`` are storage target
        # kinds, not Provider adapters.  They are deliberately represented by
        # database-owned StorageTarget/locator rows and therefore must not be
        # looked up in the Provider registry.  Looking them up made an
        # otherwise valid cross-provider rclone plan fail with
        # ``Provider Adapter unavailable: local``.  Real remote destinations
        # still go through the registry and fail closed when unregistered.
        destination_adapter = (
            None
            if dest_provider in {"local", "generic", "openlist_generic"}
            else self._adapter(dest_provider, errors, "destination")
        ) if dest_provider else None
        source_caps = self._capabilities(source_adapter)
        destination_caps = self._capabilities(destination_adapter)
        if dest_provider in {"local", "generic", "openlist_generic"}:
            # Storage targets of these kinds are validated by their frozen
            # locator/binding below.  Mark only the rclone destination
            # capability; never make them eligible for native/relay paths.
            destination_caps = ProviderCapabilities(supports_rclone_dest=True)

        if source_form == "magnet" and strategy != STRATEGY_NATIVE_OFFLINE:
            errors.append("磁链来源只支持 native_offline")

        if strategy == STRATEGY_NATIVE_SAVE:
            if source_provider != dest_provider:
                errors.append("native_save 要求来源与目标 Provider 一致")
            if source_form != "share":
                errors.append("native_save 要求分享来源")
            if category is None or category.native_locator is None:
                errors.append("native_save 要求原生目标 locator")
            if not source_caps.supports_native_save:
                errors.append("来源 Adapter 未声明 supports_native_save")
            if not destination_caps.supports_native_save:
                errors.append("目标 Adapter 未声明 supports_native_save")
            if source_adapter is not None and category is not None and category.native_locator is not None:
                try:
                    decision = source_adapter.can_native_transfer(
                        self._source_descriptor(source),
                        category.native_locator,
                        self._destination_account_ctx(destination),
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"native_save 能力检查失败：{exc}")
                else:
                    if not getattr(decision, "available", False):
                        errors.append(
                            f"native_save adapter decision rejected: {getattr(decision, 'reason', '')}"
                        )
        elif strategy == STRATEGY_NATIVE_OFFLINE:
            if source_form not in {"magnet", "torrent"}:
                errors.append("native_offline 要求磁链/种子来源")
            if source_form == "magnet" and _str(source.get("search_source_id")) != "sixpan_magnet":
                errors.append("磁链来源必须使用 sixpan_magnet 搜索源")
            if dest_provider != "sixpan":
                errors.append("native_offline 目标必须是六盘")
            if category is None or category.native_locator is None:
                errors.append("native_offline 要求原生 locator")
            elif not _str(category.native_locator.kind) or not _str(
                category.native_locator.value
            ):
                errors.append("native_offline locator 不完整")
            if not _str(destination.get("account_id")) or _int_or(
                destination.get("account_revision"), 0
            ) <= 0:
                errors.append("native_offline 要求冻结的六盘账号 revision")
            if not destination_caps.supports_native_offline:
                errors.append("目标 Adapter 未声明 supports_native_offline")
        elif is_relay_upload_strategy(strategy):
            if dest_provider in {"local", "generic", "openlist_generic"}:
                errors.append("Provider relay 要求已注册的 Provider 目标")
            if source_provider == dest_provider:
                errors.append("同 Provider 转存必须使用 native_save")
            if category is None or category.native_locator is None:
                errors.append("Provider relay 要求原生目标 locator")
            if not destination_caps.supports_relay_upload:
                errors.append("目标 Adapter 未声明 supports_relay_upload")
            elif destination_adapter is None or not callable(getattr(destination_adapter, "relay_uploader", None)):
                errors.append("目标 Adapter 没有 relay uploader 工厂")
            elif destination_adapter.relay_uploader() is None:
                errors.append("目标 Adapter relay uploader 不可用")
            if not source_caps.supports_source_materialization:
                errors.append("来源 Adapter 未声明 supports_source_materialization")
        elif strategy == STRATEGY_RCLONE_COPY:
            if category is None or category.rclone_locator is None:
                errors.append("rclone_copy 要求 rclone 或本地目标 locator")
            if source_form == "magnet":
                errors.append("磁链来源不能使用 rclone_copy")
            if dest_provider in {"mobile", "tianyi"}:
                errors.append(
                    "rclone_copy cannot write Mobile or Tianyi targets; use Provider API relay"
                )
            if not source_caps.supports_source_materialization:
                errors.append("来源 Adapter 未声明 supports_source_materialization")
            if dest_provider not in {"local", "generic", "openlist_generic"} and not destination_caps.supports_rclone_dest:
                errors.append("目标 Adapter 未声明 supports_rclone_dest")
        elif strategy:
            errors.append("未知转存策略")

    def _adapter(self, provider_kind: str, errors: list[str], role: str) -> Any:
        try:
            return self.registry.get(provider_kind)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{role} Provider Adapter unavailable: {provider_kind} ({exc})")
            return None

    @staticmethod
    def _capabilities(adapter: Any) -> ProviderCapabilities:
        if adapter is None:
            return ProviderCapabilities()
        manifest = getattr(adapter, "manifest", None)
        method = getattr(adapter, "capabilities", None)
        if callable(method):
            try:
                value: Any = method()
            except Exception:  # noqa: BLE001 - runtime capability failure is fail-closed
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
            # An adapter that exposes a capabilities method but returns an
            # unknown shape must not silently inherit its manifest's claims.
            return ProviderCapabilities()
        if manifest is not None and isinstance(getattr(manifest, "capabilities", None), ProviderCapabilities):
            return manifest.capabilities
        return ProviderCapabilities()

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
        category: Any = None,
        category_key: str = "",
        job_id: int = 0,
    ) -> None:
        job = job_dir(_int_or(plan.get("job_id"), 0))
        source_provider = _str(source.get("provider_kind"))
        source_account_id = _str(source.get("account_id"))
        dest_provider = _str(destination.get("provider_kind"))
        dest_account_id = _str(destination.get("account_id"))
        source_landing = _mapping(paths.get("source_landing"))
        staging = _mapping(paths.get("destination_staging"))
        final = _mapping(paths.get("destination_final"))
        local_temp = _mapping(paths.get("local_temp"))

        refs: list[tuple[str, dict[str, Any]]] = [
            ("source_landing", source_landing),
            ("destination_staging", staging),
            ("destination_final", final),
        ]
        strict_v1_paths = _int_or(plan.get("plan_schema_version"), 0) == EXECUTION_PLAN_VERSION
        valid_refs: list[tuple[str, str, str, str]] = []
        for label, ref in refs:
            if not ref:
                continue
            namespace = _str(ref.get("namespace_id"))
            path = _str(ref.get("path"))
            if not namespace or not path:
                errors.append(f"paths.{label} 缺少 namespace_id/path")
                continue
            if not path_is_safe(path):
                errors.append(f"paths.{label} 路径不安全")
            inferred_kind = namespace_kind(namespace)
            declared_kind = _str(ref.get("kind"))
            if strict_v1_paths and not declared_kind:
                errors.append(f"paths.{label} 必须显式声明 kind")
            kind = declared_kind or inferred_kind
            if kind == "relay":
                if inferred_kind != "local":
                    errors.append(f"paths.{label} relay 必须使用本地命名空间")
            elif kind not in {"openlist", "local", "native"}:
                errors.append(f"paths.{label} kind 不受支持")
            elif declared_kind and inferred_kind != kind:
                errors.append(f"paths.{label} kind 与命名空间不一致")
            if inferred_kind == "openlist" and split_openlist_namespace(namespace) is None:
                errors.append(f"paths.{label} openlist 命名空间格式不合法")
            valid_refs.append((label, namespace, path, kind))

        # Validate strategy-specific path kinds before mount checks.
        relay_expected_kinds = {
            "source_landing": {"openlist"},
            "destination_final": {"native"},
            "local_temp": {"relay"},
        }
        expected_kinds = {
            STRATEGY_NATIVE_SAVE: {
                "destination_final": {"native"},
                "destination_staging": {"native"},
            },
            STRATEGY_NATIVE_OFFLINE: {
                "destination_final": {"native"},
                "destination_staging": {"native"},
            },
            STRATEGY_RELAY_UPLOAD_PROVIDER: relay_expected_kinds,
            STRATEGY_RCLONE_COPY: {
                "source_landing": {"openlist"},
                "destination_staging": {"openlist", "local"},
                "destination_final": {"openlist", "local"},
            },
        }.get(strategy, {})
        refs_by_label = {label: ref for label, ref in refs}
        for label, allowed in expected_kinds.items():
            ref = refs_by_label.get(label)
            if not ref:
                continue
            actual = _str(ref.get("kind")) or namespace_kind(ref.get("namespace_id"))
            if actual not in allowed:
                errors.append(f"paths.{label} kind 必须是 {sorted(allowed)} 之一")

        identity_refs = [
            self._storage_identity_ref(label, namespace, path, bindings)
            for label, namespace, path, _kind in valid_refs
        ]
        # 原生入库暂存目录与分类目录同级（分类父目录/_入库暂存/分类名/job-N），
        # 二者在任何命名空间下都不重叠；其合法性由 _native_staging_errors
        # 逐项复核。若暂存与分类目录出现重叠，直接按重叠冲突拒绝。
        for i in range(len(identity_refs)):
            for j in range(i + 1, len(identity_refs)):
                label_a, namespace_a, path_a = identity_refs[i]
                label_b, namespace_b, path_b = identity_refs[j]
                if paths_overlap(namespace_a, path_a, namespace_b, path_b):
                    errors.append(f"paths.{label_a} 与 paths.{label_b} 在同一命名空间重叠")

        # 本地越界（§8.3）。
        local_root = _str(self.config.get("FNOS_LOCAL_TARGET_ROOT") or DEFAULT_LOCAL_TARGET_ROOT)
        for label, namespace, path, kind in valid_refs:
            if kind == "local" and not local_contained(path, local_root):
                errors.append(f"paths.{label} 越出受控本地根目录")

        # Relay 缓存使用独立的本地根，不能借用存储目标根，也不能从
        # 当前 rclone 配置推导。它只属于 Provider relay 策略。
        if local_temp:
            local_temp_namespace = _str(local_temp.get("namespace_id"))
            local_temp_path = _str(local_temp.get("path"))
            if not is_relay_upload_strategy(strategy):
                errors.append("paths.local_temp 只允许 Provider relay 使用")
            if strict_v1_paths and _str(local_temp.get("kind")) != "relay":
                errors.append("paths.local_temp 必须显式声明 kind=relay")
            if not local_temp_namespace or namespace_kind(local_temp_namespace) != "local":
                errors.append("paths.local_temp 必须位于 local 命名空间")
            if local_temp_namespace and local_temp_namespace != "local:relay":
                errors.append("paths.local_temp 必须使用 local:relay 命名空间")
            if not local_temp_path or not path_is_safe(local_temp_path):
                errors.append("paths.local_temp 路径不安全")
            relay_root = _str(self.config.get("FNOS_RELAY_ROOT") or DEFAULT_RELAY_ROOT)
            if local_temp_path and not local_contained(local_temp_path, relay_root):
                errors.append("paths.local_temp 越出受控 relay 根目录")
            if local_temp_path and path_leaf(local_temp_path) != job:
                errors.append("paths.local_temp 不是任务级目录")

        # 任务级 landing/staging 叶子必须与 job_id 一致（§12.4）。
        if strategy in DATA_PLANE_STRATEGIES:
            if not source_landing:
                errors.append("跨盘/中继策略缺少 source_landing")
            elif path_leaf(source_landing.get("path")) != job:
                errors.append("source_landing 不是任务级目录")
        if strategy == STRATEGY_RCLONE_COPY:
            if not staging:
                errors.append("rclone_copy 缺少 destination_staging")
            elif path_leaf(staging.get("path")) != job:
                errors.append("destination_staging 不是任务级目录")
        elif is_relay_upload_strategy(strategy):
            if staging:
                errors.append(f"{strategy} 不允许 destination_staging")
            if not local_temp or not _str(local_temp.get("path")):
                errors.append(f"{strategy} 缺少 local_temp")
            if not final:
                errors.append(f"{strategy} 缺少 destination_final")
        elif strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_NATIVE_OFFLINE}:
            if source_landing or local_temp:
                errors.append(f"{strategy} 不允许数据面 landing/local_temp")
            if not final:
                errors.append(f"{strategy} 缺少 destination_final")
            # 原生转存允许一个与分类目录同级的「分类父目录/_入库暂存/分类名/job-N」
            # 任务级暂存目录：Provider 先把文件转存进去，再由 OpenList Organizer
            # 搬进正式分类目录。它不是数据面 staging（没有 rclone 搬运），所以必须
            # 逐项复核：命名空间与 destination_final 完全一致、叶子是 job-N、且路径
            # 能由 destination_final 原样重新推导出来，防止冻结计划被改写成任意目录。
            if staging:
                errors.extend(self._native_staging_errors(strategy, staging, final, job))

        # destination_staging 是 rclone_copy 的任务级暂存目录；中继链路
        # 直接写入冻结 destination_final，不得把 staging 当成必需步骤。
        if strategy == STRATEGY_RCLONE_COPY and not final:
            errors.append("rclone_copy 缺少 destination_final")

        if final:
            final_kind = namespace_kind(final.get("namespace_id"))
            if strategy in {
                STRATEGY_NATIVE_SAVE,
                STRATEGY_NATIVE_OFFLINE,
                STRATEGY_RELAY_UPLOAD_PROVIDER,
            } and final_kind != "native":
                errors.append(f"{strategy} 的 destination_final 必须位于 native 命名空间")
            if strategy == STRATEGY_RCLONE_COPY and final_kind == "native":
                errors.append("rclone_copy 的 destination_final 不能位于 native 命名空间")

        self._validate_frozen_category_locator(
            category=category,
            destination=destination,
            paths=paths,
            bindings=bindings,
            strategy=strategy,
            category_key=category_key,
            job_id=job_id,
            errors=errors,
        )

        # ---- bindings / mount 一致性 -------------------------------------------
        source_mount_id = _str(bindings.get("source_mount_id"))
        destination_mount_id = _str(bindings.get("destination_mount_id"))
        if source_landing:
            if not source_mount_id:
                errors.append("source_landing 缺少来源 mount 绑定")
            else:
                revision = _int_or(bindings.get("source_mount_revision"), 0)
                frozen_mount = self._frozen_mount(source_mount_id, revision)
                if frozen_mount is None:
                    errors.append(f"来源 mount revision 不存在：{source_mount_id}@{revision}")
                else:
                    if not bool(frozen_mount.get("readable", True)):
                        errors.append("来源 mount revision 不可读")
                    mount_provider = _str(frozen_mount.get("provider_kind"))
                    if mount_provider and mount_provider != source_provider:
                        errors.append("来源 mount Provider 与计划来源不一致")
                    mount_account = _str(frozen_mount.get("account_id"))
                    if mount_account and mount_account != source_account_id:
                        errors.append("来源 mount 账号与计划来源不一致")
                    if _str(bindings.get("source_mount_storage_identity_id")) != _str(
                        frozen_mount.get("storage_identity_id")
                    ):
                        errors.append("来源 mount 存储身份与 revision 不一致")
                    if normalize_path(bindings.get("source_mount_identity_root")) != normalize_path(
                        frozen_mount.get("identity_root")
                    ):
                        errors.append("来源 mount identity root 与 revision 不一致")
                    expected_namespace = _str(bindings.get("source_mount_namespace_id")) or self._mount_namespace(source_mount_id, frozen_mount)
                    if expected_namespace and expected_namespace != _str(source_landing.get("namespace_id")):
                        errors.append("source landing 命名空间与冻结 mount 不一致")
                    self._validate_path_mount_identity(
                        source_landing,
                        role="source landing",
                        mount_id=source_mount_id,
                        revision=revision,
                        expected_namespace=expected_namespace,
                        expected_connection_id=_str(bindings.get("source_connection_id")),
                        expected_connection_revision=_int_or(bindings.get("source_connection_revision"), 0),
                        expected_storage_identity_id=_str(bindings.get("source_mount_storage_identity_id")),
                        expected_identity_root=_str(bindings.get("source_mount_identity_root")) or "/",
                        strict=strict_v1_paths,
                        errors=errors,
                    )
                    frozen_locator = _mapping(frozen_mount.get("native_locator"))
                    plan_locator = _mapping(bindings.get("source_native_locator"))
                    if not plan_locator.get("kind") or not plan_locator.get("value"):
                        errors.append("source landing 缺少冻结 Provider locator")
                    elif not frozen_locator:
                        errors.append("source mount revision 缺少 Provider locator")
                    # ``native_locator`` also stores probe evidence such as
                    # upload/Organizer verification timestamps and display
                    # labels.  The execution plan deliberately freezes only
                    # the Provider locator identity consumed by the source
                    # materializer.  Comparing the whole probe object makes a
                    # valid mobile/Tianyi cross-provider plan impossible as
                    # soon as those proofs are present on the mount revision.
                    elif plan_locator != {
                        "kind": _str(frozen_locator.get("kind")),
                        "value": _str(frozen_locator.get("value")),
                    }:
                        errors.append("来源 Provider locator 与 mount revision 不一致")
                    source_connection_id = _str(bindings.get("source_connection_id"))
                    source_connection_revision = _int_or(
                        bindings.get("source_connection_revision"), 0
                    )
                    if source_connection_id != _str(frozen_mount.get("connection_id")):
                        errors.append("来源 connection id 与 mount revision 不一致")
                    if source_connection_revision != _int_or(
                        frozen_mount.get("connection_revision"), 0
                    ):
                        errors.append("来源 connection revision 与 mount revision 不一致")
                    service = getattr(self.mount_service, "connection_service", None)
                    if (
                        not source_connection_id
                        or source_connection_revision <= 0
                        or service is None
                        or service.get_revision(
                            source_connection_id, source_connection_revision
                        )
                        is None
                    ):
                        errors.append("来源 OpenList connection revision 不存在")

        staging_kind = namespace_kind(staging.get("namespace_id")) if staging else ""
        if strategy == STRATEGY_RCLONE_COPY and staging:
            if staging_kind == "openlist":
                if not destination_mount_id:
                    errors.append("destination staging 缺少目标 mount 绑定")
                else:
                    revision = _int_or(bindings.get("destination_mount_revision"), 0)
                    frozen_mount = self._frozen_mount(destination_mount_id, revision)
                    if frozen_mount is None:
                        errors.append(f"目标 mount revision 不存在：{destination_mount_id}@{revision}")
                    else:
                        if not bool(frozen_mount.get("writable", True)):
                            errors.append("目标 mount revision 不可写")
                        mount_provider = _str(frozen_mount.get("provider_kind"))
                        if mount_provider and mount_provider != dest_provider:
                            errors.append("目标 mount Provider 与计划目标不一致")
                        mount_account = _str(frozen_mount.get("account_id"))
                        if mount_account and mount_account != dest_account_id:
                            errors.append("目标 mount 账号与计划目标不一致")
                        if _str(bindings.get("destination_mount_storage_identity_id")) != _str(
                            frozen_mount.get("storage_identity_id")
                        ):
                            errors.append("目标 mount 存储身份与 revision 不一致")
                        if normalize_path(
                            bindings.get("destination_mount_identity_root")
                        ) != normalize_path(frozen_mount.get("identity_root")):
                            errors.append("目标 mount identity root 与 revision 不一致")
                        expected_namespace = _str(bindings.get("destination_mount_namespace_id")) or self._mount_namespace(destination_mount_id, frozen_mount)
                        if expected_namespace and expected_namespace != _str(staging.get("namespace_id")):
                            errors.append("destination staging 命名空间与冻结 mount 不一致")
                        self._validate_path_mount_identity(
                            staging,
                            role="destination staging",
                            mount_id=destination_mount_id,
                            revision=revision,
                            expected_namespace=expected_namespace,
                            expected_connection_id=_str(bindings.get("destination_connection_id")),
                            expected_connection_revision=_int_or(bindings.get("destination_connection_revision"), 0),
                            expected_storage_identity_id=_str(bindings.get("destination_mount_storage_identity_id")),
                            expected_identity_root=_str(bindings.get("destination_mount_identity_root")) or "/",
                            strict=strict_v1_paths,
                            errors=errors,
                        )
                        connection_id = _str(bindings.get("destination_connection_id"))
                        connection_revision = _int_or(bindings.get("destination_connection_revision"), 0)
                        if connection_id != _str(frozen_mount.get("connection_id")):
                            errors.append("目标 connection id 与 mount revision 不一致")
                        if connection_revision != _int_or(
                            frozen_mount.get("connection_revision"), 0
                        ):
                            errors.append("目标 connection revision 与 mount revision 不一致")
                        service = getattr(self.mount_service, "connection_service", None)
                        if not connection_id or service is None or not service.get_revision(connection_id, connection_revision):
                            errors.append("目标 OpenList connection revision 不存在")
            elif staging_kind == "local":
                if destination_mount_id:
                    errors.append("本地目标不得绑定 OpenList mount")
            elif staging_kind == "native":
                errors.append("rclone_copy 暂存不能使用 native 命名空间")

        # Provider-neutral relay plans freeze the destination mount even though
        # the final write uses a native locator.  That immutable revision is the
        # proof that the selected account/root passed native upload and OpenList
        # organizer mutation checks.  V1 hard cutover does not accept an
        # unbound historical relay plan.
        if strategy == STRATEGY_RELAY_UPLOAD_PROVIDER:
            if not destination_mount_id:
                errors.append("Provider relay 缺少目标 mount 绑定")
            else:
                revision = _int_or(bindings.get("destination_mount_revision"), 0)
                frozen_mount = self._frozen_mount(destination_mount_id, revision)
                if frozen_mount is None:
                    errors.append(
                        f"destination mount revision does not exist: "
                        f"{destination_mount_id}@{revision}"
                    )
                else:
                    native_locator = _mapping(frozen_mount.get("native_locator"))
                    if _str(frozen_mount.get("provider_kind")) != dest_provider:
                        errors.append("目标 mount Provider 与计划目标不一致")
                    if _str(frozen_mount.get("account_id")) != dest_account_id:
                        errors.append("目标 mount 账号与计划目标不一致")
                    if not bool(frozen_mount.get("readable")):
                        errors.append("Provider relay 目标 mount 不可读")
                    if not bool(native_locator.get("upload_verified")):
                        errors.append("Provider relay 目标 mount 缺少原生上传能力探测")
                    if not bool(native_locator.get("organizer_mutation_verified")):
                        errors.append("Provider relay 目标 mount 缺少 Organizer 整理能力探测")
                    if _str(bindings.get("destination_mount_storage_identity_id")) != _str(
                        frozen_mount.get("storage_identity_id")
                    ):
                        errors.append("目标 mount 存储身份与 revision 不一致")
                    if normalize_path(
                        bindings.get("destination_mount_identity_root")
                    ) != normalize_path(frozen_mount.get("identity_root")):
                        errors.append("目标 mount identity root 与 revision 不一致")
                    if _str(bindings.get("destination_mount_namespace_id")) != _str(
                        frozen_mount.get("namespace_id")
                    ):
                        errors.append("目标 mount 命名空间与 revision 不一致")
                    connection_id = _str(bindings.get("destination_connection_id"))
                    connection_revision = _int_or(
                        bindings.get("destination_connection_revision"), 0
                    )
                    if connection_id != _str(frozen_mount.get("connection_id")):
                        errors.append("目标 connection id 与 mount revision 不一致")
                    if connection_revision != _int_or(
                        frozen_mount.get("connection_revision"), 0
                    ):
                        errors.append("目标 connection revision 与 mount revision 不一致")
                    service = getattr(self.mount_service, "connection_service", None)
                    if (
                        not connection_id
                        or connection_revision <= 0
                        or service is None
                        or service.get_revision(connection_id, connection_revision) is None
                    ):
                        errors.append("目标 OpenList connection revision 不存在")

        # For an OpenList rclone final path, the frozen destination mount is
        # the same one used by staging.  Requiring this explicitly prevents a
        # tampered plan from copying into a different mount after validation.
        if strategy == STRATEGY_RCLONE_COPY and final and namespace_kind(final.get("namespace_id")) == "openlist":
            if not destination_mount_id:
                errors.append("destination final 缺少目标 mount 绑定")
            else:
                revision = _int_or(bindings.get("destination_mount_revision"), 0)
                frozen_mount = self._frozen_mount(destination_mount_id, revision)
                if frozen_mount is None:
                    errors.append(f"目标 mount revision 不存在：{destination_mount_id}@{revision}")
                else:
                    expected_namespace = _str(bindings.get("destination_mount_namespace_id")) or self._mount_namespace(destination_mount_id, frozen_mount)
                    if expected_namespace and expected_namespace != _str(final.get("namespace_id")):
                        errors.append("destination final 命名空间与冻结 mount 不一致")
                    self._validate_path_mount_identity(
                        final,
                        role="destination final",
                        mount_id=destination_mount_id,
                        revision=revision,
                        expected_namespace=expected_namespace,
                        expected_connection_id=_str(bindings.get("destination_connection_id")),
                        expected_connection_revision=_int_or(bindings.get("destination_connection_revision"), 0),
                        expected_storage_identity_id=_str(bindings.get("destination_mount_storage_identity_id")),
                        expected_identity_root=_str(bindings.get("destination_mount_identity_root")) or "/",
                        strict=strict_v1_paths,
                        errors=errors,
                    )
            if staging and namespace_kind(staging.get("namespace_id")) == "openlist" and _str(staging.get("namespace_id")) != _str(final.get("namespace_id")):
                errors.append("destination staging/final 命名空间必须一致")
        elif strategy == STRATEGY_RCLONE_COPY and staging and final:
            # Local rclone plans also freeze one target namespace for both
            # legs; do not let a caller mix two local namespaces.
            if namespace_kind(staging.get("namespace_id")) == "local" and namespace_kind(final.get("namespace_id")) == "local" and _str(staging.get("namespace_id")) != _str(final.get("namespace_id")):
                errors.append("本地目标 staging/final 命名空间必须一致")

        if strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_NATIVE_OFFLINE}:
            if source_mount_id:
                errors.append("native 策略不应绑定来源 mount")

    def _validate_frozen_category_locator(
        self,
        *,
        category: Any,
        destination: dict[str, Any],
        paths: dict[str, Any],
        bindings: dict[str, Any],
        strategy: str,
        category_key: str,
        job_id: int,
        errors: list[str],
    ) -> None:
        """Compare execution paths with the selected target revision.

        Mount/namespace checks prove *where* a path lives, but not that a
        caller did not replace the frozen category locator with another path
        in the same mount.  Planner and validator therefore derive the exact
        task-level paths from the immutable target category and reject drift.
        """
        if category is None:
            return
        final = _mapping(paths.get("destination_final"))
        staging = _mapping(paths.get("destination_staging"))
        task_leaf = f"job-{int(job_id)}" if int(job_id or 0) > 0 else ""

        if strategy in {
            STRATEGY_NATIVE_SAVE,
            STRATEGY_NATIVE_OFFLINE,
            STRATEGY_RELAY_UPLOAD_PROVIDER,
        }:
            locator = getattr(category, "native_locator", None)
            if not isinstance(locator, NativeLocator) or not final:
                return
            if _str(final.get("locator_kind")) and _str(final.get("locator_kind")) != _str(locator.kind):
                errors.append("destination_final locator kind 与目标分类不一致")
            if normalize_path(final.get("path")) != normalize_path(locator.value):
                errors.append("destination_final path 与目标分类不一致")
            expected_namespace = f"native:{_str(destination.get('provider_kind'))}:{_str(destination.get('id'))}"
            if _str(final.get("namespace_id")) != expected_namespace:
                errors.append("destination_final native 命名空间与目标不一致")
            if is_relay_upload_strategy(strategy):
                openlist_locator = getattr(category, "organizer_locator", None)
                if openlist_locator is None:
                    openlist_locator = getattr(category, "rclone_locator", None)
                locator_mount_id = _str(getattr(openlist_locator, "mount_id", ""))
                locator_connection_id = _str(
                    getattr(openlist_locator, "connection_id", "")
                )
                frozen_mount_id = _str(bindings.get("destination_mount_id"))
                frozen_connection_id = _str(bindings.get("destination_connection_id"))
                if strategy == STRATEGY_RELAY_UPLOAD_PROVIDER and not locator_mount_id:
                    errors.append("Provider relay 目标分类缺少 OpenList mount locator")
                if locator_mount_id and frozen_mount_id != locator_mount_id:
                    errors.append("目标 mount 与目标分类不一致")
                if locator_connection_id and frozen_connection_id != locator_connection_id:
                    errors.append("目标 connection 与目标分类不一致")
            return

        if strategy != STRATEGY_RCLONE_COPY:
            return
        locator = getattr(category, "rclone_locator", None)
        if isinstance(locator, RcloneLocator):
            frozen_mount_id = _str(bindings.get("destination_mount_id"))
            if frozen_mount_id and frozen_mount_id != _str(locator.mount_id):
                errors.append("destination mount 与目标 rclone locator 不一致")
            frozen_connection_id = _str(bindings.get("destination_connection_id"))
            if frozen_connection_id and frozen_connection_id != _str(locator.connection_id):
                errors.append("destination connection 与目标 rclone locator 不一致")
            expected_namespace = _str(bindings.get("destination_mount_namespace_id"))
            if not expected_namespace:
                expected_namespace = f"openlist:{locator.connection_id}:{locator.mount_id}"
            expected_final = normalize_path(f"{locator.relative_path}/{task_leaf}") if task_leaf else ""
            # Do not require a derived path when called directly without the
            # plan id; the full validate() path always sets this context.
            if _str(final.get("namespace_id")) != expected_namespace:
                errors.append("destination_final 命名空间与目标 rclone locator 不一致")
            if expected_final and normalize_path(final.get("path")) != expected_final:
                errors.append("destination_final path 与目标 rclone locator 不一致")
        elif isinstance(locator, LocalLocator):
            expected_namespace = f"local:{_str(destination.get('id'))}"
            root = normalize_path(locator.root)
            relative = normalize_path(f"{root}/{locator.relative_path}") if locator.relative_path else root
            expected_final = normalize_path(f"{relative}/{task_leaf}") if task_leaf else ""
            if _str(final.get("namespace_id")) != expected_namespace:
                errors.append("destination_final 命名空间与目标本地 locator 不一致")
            if expected_final and normalize_path(final.get("path")) != expected_final:
                errors.append("destination_final path 与目标本地 locator 不一致")

        # Staging is always derived from the category key and job id.  It is
        # intentionally checked only when a descriptor exists; the strategy
        # branch above already reports it as missing when required.
        if staging and category_key and task_leaf:
            # OpenList staging paths are relative to the frozen mount root;
            # local targets carry their controlled absolute root in the path
            # itself.  Derive the same contract as ImportPlanner._rclone_paths
            # instead of applying the OpenList shape to local storage.
            if isinstance(locator, LocalLocator):
                expected_staging = normalize_path(
                    f"{locator.root}/{STAGING_DIR}/{category_key}/{task_leaf}"
                )
            else:
                expected_staging = normalize_path(
                    f"/{STAGING_DIR}/{category_key}/{task_leaf}"
                )
            if normalize_path(staging.get("path")) != expected_staging:
                errors.append("destination_staging path 与冻结任务契约不一致")

    @staticmethod
    def _validate_path_mount_identity(
        ref: Mapping[str, Any],
        *,
        role: str,
        mount_id: str,
        revision: int,
        expected_namespace: str,
        strict: bool,
        errors: list[str],
        expected_connection_id: str = "",
        expected_connection_revision: int = 0,
        expected_storage_identity_id: str = "",
        expected_identity_root: str = "/",
    ) -> None:
        """Ensure a path descriptor repeats its frozen mount identity.

        Namespace strings are not sufficient proof when a mount is revised or
        an administrator has multiple mounts on one OpenList connection. V1
        planners persist mount id/revision beside every OpenList path; older
        hand-built descriptors are tolerated only by the direct helper tests,
        while the full V1 validator requires the fields.
        """
        path_mount_id = _str(ref.get("mount_id"))
        path_revision = _int_or(ref.get("mount_revision"))
        if strict and not path_mount_id:
            errors.append(f"{role} missing mount_id")
        if strict and (path_revision is None or path_revision <= 0):
            errors.append(f"{role} missing mount_revision")
        if path_mount_id and path_mount_id != mount_id:
            errors.append(f"{role} mount_id does not match frozen binding")
        if path_revision is not None and path_revision != revision:
            errors.append(f"{role} mount_revision does not match frozen binding")
        namespace = _str(ref.get("namespace_id"))
        if expected_namespace and namespace and namespace != expected_namespace:
            errors.append(f"{role} namespace does not match frozen binding")
        path_connection_id = _str(ref.get("connection_id"))
        path_connection_revision = _int_or(ref.get("connection_revision"), 0)
        path_storage_identity = _str(ref.get("storage_identity_id"))
        path_identity_root = _str(ref.get("identity_root")) or "/"
        if strict and not path_connection_id:
            errors.append(f"{role} missing connection_id")
        if strict and path_connection_revision <= 0:
            errors.append(f"{role} missing connection_revision")
        if strict and not path_storage_identity:
            errors.append(f"{role} missing storage_identity_id")
        if expected_connection_id and path_connection_id != expected_connection_id:
            errors.append(f"{role} connection_id does not match frozen binding")
        if expected_connection_revision > 0 and path_connection_revision != expected_connection_revision:
            errors.append(f"{role} connection_revision does not match frozen binding")
        if expected_storage_identity_id and path_storage_identity != expected_storage_identity_id:
            errors.append(f"{role} storage identity does not match frozen binding")
        if normalize_path(path_identity_root) != normalize_path(expected_identity_root):
            errors.append(f"{role} identity_root does not match frozen binding")

    def _frozen_mount(self, mount_id: str, revision: int) -> dict[str, Any] | None:
        getter = getattr(self.mount_service, "get_revision", None)
        if not callable(getter) or not mount_id or revision <= 0:
            return None
        try:
            frozen = getter(mount_id, revision)
        except Exception:  # noqa: BLE001
            frozen = None
        if not isinstance(frozen, Mapping):
            return None
        result = dict(frozen)
        required = (
            "connection_id",
            "connection_revision",
            "namespace_id",
            "storage_identity_id",
            "identity_root",
            "readable",
            "writable",
            "enabled",
            "identity_status",
        )
        if any(key not in result for key in required):
            return None
        if not _str(result.get("connection_id")) or _int_or(
            result.get("connection_revision"), 0
        ) <= 0:
            return None
        if not _str(result.get("namespace_id")):
            return None
        if not _str(result.get("storage_identity_id")):
            return None
        if _str(result.get("identity_status")) not in {"verified", "declared"}:
            return None
        if not bool(result.get("enabled")):
            return None
        result["mount_id"] = mount_id
        result["revision"] = revision
        return result

    @staticmethod
    def _mount_namespace(mount_id: str, mount: Mapping[str, Any]) -> str:
        explicit = _str(mount.get("namespace_id"))
        if explicit:
            return explicit
        connection_id = _str(mount.get("connection_id"))
        return f"openlist:{connection_id}:{mount_id}" if connection_id and mount_id else ""

    # ---- Organizer -----------------------------------------------------------

    def _validate_organizer(
        self,
        plan: dict[str, Any],
        paths: dict[str, Any],
        bindings: dict[str, Any],
        category: Any,
        errors: list[str],
    ) -> None:
        postprocess = _mapping(plan.get("postprocess"))
        if not bool(postprocess.get("organizer_enabled")):
            return
        organizer = _mapping(paths.get("organizer"))
        if not organizer:
            errors.append("已启用 Organizer 但计划缺少 organizer path")
            return
        namespace = _str(organizer.get("namespace_id"))
        path = _str(organizer.get("path"))
        if not namespace or not path:
            errors.append("organizer path 不完整")
        if not path_is_safe(path):
            errors.append("organizer path 不安全")
        if _str(organizer.get("kind")) not in {"", "openlist"}:
            errors.append("organizer path kind 必须是 openlist")
        if namespace_kind(namespace) != "openlist":
            errors.append("organizer 必须使用 openlist 命名空间")
        mount_id = _str(bindings.get("organizer_mount_id"))
        mount_revision = _int_or(bindings.get("organizer_mount_revision"), 0)
        frozen_mount: dict[str, Any] | None = None
        if not mount_id or mount_revision <= 0:
            errors.append("Organizer 缺少冻结 mount revision")
        else:
            frozen_mount = self._frozen_mount(mount_id, mount_revision)
            if frozen_mount is None:
                errors.append(f"Organizer mount revision 不存在：{mount_id}@{mount_revision}")
            else:
                native_locator = _mapping(frozen_mount.get("native_locator"))
                organizer_mutation_ready = bool(
                    frozen_mount.get("writable")
                    or native_locator.get("organizer_mutation_verified")
                )
                if not bool(frozen_mount.get("readable")) or not organizer_mutation_ready:
                    errors.append("Organizer mount revision 未通过读取与整理能力探测")
                if _str(bindings.get("organizer_mount_storage_identity_id")) != _str(
                    frozen_mount.get("storage_identity_id")
                ):
                    errors.append("Organizer storage identity 与 mount revision 不一致")
                if normalize_path(
                    bindings.get("organizer_mount_identity_root")
                ) != normalize_path(frozen_mount.get("identity_root")):
                    errors.append("Organizer identity root 与 mount revision 不一致")
                expected_namespace = _str(bindings.get("organizer_mount_namespace_id"))
                if expected_namespace != self._mount_namespace(mount_id, frozen_mount):
                    errors.append("Organizer namespace 与 mount revision 不一致")
                self._validate_path_mount_identity(
                    organizer,
                    role="organizer",
                    mount_id=mount_id,
                    revision=mount_revision,
                    expected_namespace=expected_namespace,
                    strict=True,
                    errors=errors,
                    expected_connection_id=_str(bindings.get("organizer_connection_id")),
                    expected_connection_revision=_int_or(
                        bindings.get("organizer_connection_revision"), 0
                    ),
                    expected_storage_identity_id=_str(
                        bindings.get("organizer_mount_storage_identity_id")
                    ),
                    expected_identity_root=_str(
                        bindings.get("organizer_mount_identity_root")
                    )
                    or "/",
                )
                connection_id = _str(bindings.get("organizer_connection_id"))
                connection_revision = _int_or(
                    bindings.get("organizer_connection_revision"), 0
                )
                if connection_id != _str(frozen_mount.get("connection_id")):
                    errors.append("Organizer connection id 与 mount revision 不一致")
                if connection_revision != _int_or(
                    frozen_mount.get("connection_revision"), 0
                ):
                    errors.append("Organizer connection revision 与 mount revision 不一致")
                service = getattr(self.mount_service, "connection_service", None)
                if (
                    service is None
                    or connection_revision <= 0
                    or service.get_revision(connection_id, connection_revision) is None
                ):
                    errors.append("Organizer connection revision 不存在")
        # 分类定位器保存的是「挂载内相对路径」，计划冻结的是 OpenList 全局绝对
        # 路径（挂载根 + 相对路径）。比对必须在同一坐标系里做，否则 webdav_root
        # 非 "/" 的挂载会被误判成不一致。挂载根只属于 OpenList，与天翼/移动/夸克
        # 的原生根无关，因此 native 侧的 destination_* 一律不参与这里的换算。
        webdav_root = mount_webdav_root(frozen_mount) if frozen_mount else "/"

        def locator_absolute(value: Any) -> str:
            return mount_absolute_path(webdav_root, value)

        locator = getattr(category, "organizer_locator", None) if category is not None else None
        if locator is None:
            errors.append("已启用 Organizer 但目标分类缺少 organizer locator")
        else:
            if _str(getattr(locator, "mount_id", "")) != mount_id:
                errors.append("Organizer mount 与目标分类 locator 不一致")
            if _str(getattr(locator, "connection_id", "")) != _str(
                bindings.get("organizer_connection_id")
            ):
                errors.append("Organizer connection 与目标分类 locator 不一致")
        staging = _mapping(paths.get("destination_staging"))
        staging_kind = _str(staging.get("kind")) or namespace_kind(staging.get("namespace_id"))
        job = job_dir(_int_or(plan.get("job_id"), 0))
        if staging and staging_kind == "openlist":
            if path_leaf(path) != job:
                errors.append("organizer path 不是任务级目录")
        elif staging and staging_kind == "native":
            # 原生入库暂存链路：Organizer 的扫描根是 OpenList 侧看到的、与分类
            # 目录同级的「分类父目录/_入库暂存/分类名/job-N」，搬运目标则是单独
            # 冻结的 paths.organizer_final（destination_final 对天翼云是复合定位器，
            # OpenList 不能直接当目标根使用）。
            organizer_final = _mapping(paths.get("organizer_final"))
            final_path = _str(organizer_final.get("path"))
            if not organizer_final:
                errors.append("原生入库暂存链路缺少 paths.organizer_final")
            elif namespace_kind(organizer_final.get("namespace_id")) != "openlist":
                errors.append("paths.organizer_final 必须位于 openlist 命名空间")
            elif _str(organizer_final.get("namespace_id")) != _str(organizer.get("namespace_id")):
                errors.append("paths.organizer_final 必须与 paths.organizer 同命名空间")
            if path_leaf(path) != job:
                errors.append("organizer path 不是任务级目录")
            if final_path and normalize_path(path) != normalize_path(
                native_staging_path(final_path, job)
            ):
                errors.append("Organizer path 不是 organizer_final 同级的入库暂存目录")
            if (
                locator is not None
                and final_path
                and normalize_path(final_path)
                != locator_absolute(getattr(locator, "path", ""))
            ):
                errors.append("paths.organizer_final 与目标分类 locator 不一致")
        else:
            final = _mapping(paths.get("destination_final"))
            if (_str(final.get("kind")) or namespace_kind(final.get("namespace_id"))) != "native":
                errors.append("native 策略的 Organizer 映射无法复核")
            elif locator is not None and normalize_path(path) != locator_absolute(
                getattr(locator, "path", "")
            ):
                errors.append("Organizer path 与目标分类 locator 不一致")

    def _native_staging_errors(
        self,
        strategy: str,
        staging: dict[str, Any],
        final: dict[str, Any],
        job: str,
    ) -> list[str]:
        """复核原生入库暂存描述符（分类父目录 / _入库暂存 / 分类名 / job-N）。

        暂存目录必须能由 destination_final 原样重新推导出来，否则视为被改写。
        返回空列表表示该暂存描述符合法。
        """

        errors: list[str] = []
        staging_kind = _str(staging.get("kind")) or namespace_kind(staging.get("namespace_id"))
        if staging_kind != "native":
            errors.append(f"{strategy} 的 destination_staging 必须是 native 暂存目录")
            return errors
        staging_namespace = _str(staging.get("namespace_id"))
        if staging_namespace != _str(final.get("namespace_id")):
            errors.append("destination_staging 必须与 destination_final 同命名空间")
        locator_kind = _str(staging.get("locator_kind")) or _str(final.get("locator_kind"))
        expected = native_staging_locator(locator_kind, _str(final.get("path")), job)
        staging_path = _str(staging.get("path"))
        if not expected:
            errors.append("该原生定位器不支持入库暂存目录")
        elif staging_path != expected:
            errors.append("destination_staging 无法由 destination_final 推导")
        if staging_path and path_leaf(staging_path) != job:
            errors.append("destination_staging 不是任务级目录")
        if staging_path and NATIVE_STAGING_DIR not in staging_path:
            errors.append("destination_staging 缺少入库暂存目录段")
        if _str(staging.get("role")) != "native_staging":
            errors.append("destination_staging 缺少 role=native_staging 标记")
        return errors

    def _storage_identity_ref(
        self,
        label: str,
        namespace: str,
        path: str,
        bindings: dict[str, Any],
    ) -> tuple[str, str, str]:
        if not namespace.startswith("openlist:"):
            return label, namespace, path
        if label == "source_landing":
            identity = _str(bindings.get("source_mount_storage_identity_id"))
            identity_root = _str(bindings.get("source_mount_identity_root")) or "/"
            mount_id = _str(bindings.get("source_mount_id"))
            revision = _int_or(bindings.get("source_mount_revision"), 0)
        else:
            identity = _str(bindings.get("destination_mount_storage_identity_id"))
            identity_root = _str(bindings.get("destination_mount_identity_root")) or "/"
            mount_id = _str(bindings.get("destination_mount_id"))
            revision = _int_or(bindings.get("destination_mount_revision"), 0)
        # Plans created before explicit identity fields can still be checked by
        # reading the immutable revision, never by trusting the current mount.
        if not identity and mount_id and revision:
            frozen = self._frozen_mount(mount_id, revision)
            if frozen:
                identity = _str(frozen.get("storage_identity_id"))
                identity_root = _str(frozen.get("identity_root")) or identity_root
        identity = identity or namespace
        return label, identity, normalize_path(f"{identity_root}/{str(path).lstrip('/')}")

    # ---- 工具 ----------------------------------------------------------------

    def _category_key(self, plan: dict[str, Any], job: dict[str, Any]) -> str:
        plan_key = _str(plan.get("category"))
        if plan_key:
            return plan_key
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


__all__ = ["PlanValidationDeps", "PlanValidator", "validate_plan"]
