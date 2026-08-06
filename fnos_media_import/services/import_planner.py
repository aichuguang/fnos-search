"""ImportPlanner：根据来源、目标和驱动能力生成不可变 v3 执行计划。

计划在任务创建时冻结，运行期间不受后台配置修改影响。计划不含明文凭证，
Worker 通过稳定 destination_id 和受控配置解析密钥。
"""

from __future__ import annotations

from typing import Any

from ..constants import (
    ROUTE_CLOUD139_DIRECT,
    ROUTE_CLOUD189_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
)
from ..storage.registry import UnknownStorageDriver, get_driver, registered_driver_kinds
from ..storage.types import StorageTarget, TargetCategoryConfig

EXECUTION_PLAN_VERSION = 3
STRATEGY_NATIVE_SAVE = "native_save"
STRATEGY_RCLONE_COPY = "rclone_copy"
STRATEGY_RELAY_UPLOAD = "relay_upload"
STRATEGY_MANUAL_REVIEW = "manual_review"
SUPPORTED_NATIVE_INGRESSES = frozenset({ROUTE_CLOUD139_DIRECT})
LEGACY_INGRESS_ROUTES = frozenset({ROUTE_SIXPAN_OFFLINE, ROUTE_CLOUD189_DIRECT})


class StoragePlanError(ValueError):
    """Raised when a requested destination cannot be planned safely."""


def _requested_destination(value: Any) -> str:
    return str(value or "").strip()


def destination_scoped_idempotency_key(value: Any, destination_id: Any) -> str:
    """Bind an explicit idempotency key to its destination without changing legacy keys."""

    key = str(value or "").strip()
    destination = _requested_destination(destination_id)
    if not key or not destination:
        return key
    suffix = f":destination:{destination}"
    return key if key.endswith(suffix) else f"{key}{suffix}"


def execution_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """从任务 raw_data 读取冻结的 v3 执行计划。"""

    raw_data = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
    plan = raw_data.get("execution_plan") if isinstance(raw_data.get("execution_plan"), dict) else {}
    return plan if plan.get("version") == EXECUTION_PLAN_VERSION else {}


def validated_execution_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """校验冻结的 v3 执行计划与任务身份一致；不完整时抛错，绝不回退可变配置。"""

    raw_job = job or {}
    plan = execution_plan_from_job(raw_job)
    if not plan:
        try:
            persisted_version = int(raw_job.get("plan_version") or 0)
        except (TypeError, ValueError) as exc:
            raise StoragePlanError("任务的 plan_version 无效") from exc
        if persisted_version >= EXECUTION_PLAN_VERSION:
            raise StoragePlanError("任务标记为 v3，但没有固化 execution_plan")
        return {}
    missing: list[str] = []
    try:
        job_id = int(raw_job.get("id") or 0)
        plan_job_id = int(plan.get("job_id") or 0)
    except (TypeError, ValueError) as exc:
        raise StoragePlanError("执行计划的任务 ID 无效") from exc
    if job_id <= 0 or plan_job_id != job_id:
        missing.append("job_id")
    destination = plan.get("destination") if isinstance(plan.get("destination"), dict) else {}
    if not str(destination.get("id") or "").strip():
        missing.append("destination.id")
    if not str(destination.get("driver") or "").strip():
        missing.append("destination.driver")
    try:
        persisted_version = int(raw_job.get("plan_version") or 0)
    except (TypeError, ValueError) as exc:
        raise StoragePlanError("任务的 plan_version 无效") from exc
    if persisted_version != EXECUTION_PLAN_VERSION or int(plan.get("version") or 0) != persisted_version:
        missing.append("version_match")
    persisted_destination = str(raw_job.get("destination_id") or "").strip()
    plan_destination = str(destination.get("id") or "").strip()
    if not persisted_destination or persisted_destination != plan_destination:
        missing.append("destination_match")
    driver_kind = str(destination.get("driver") or "").strip().lower()
    if driver_kind not in registered_driver_kinds():
        missing.append("destination.driver_known")
    transfer = plan.get("transfer") if isinstance(plan.get("transfer"), dict) else {}
    if not str(transfer.get("strategy") or "").strip():
        missing.append("transfer.strategy")
    if str(transfer.get("strategy") or "").strip() not in {
        STRATEGY_NATIVE_SAVE,
        STRATEGY_RCLONE_COPY,
        STRATEGY_RELAY_UPLOAD,
        STRATEGY_MANUAL_REVIEW,
    }:
        missing.append("transfer.strategy_known")
    paths = plan.get("paths") if isinstance(plan.get("paths"), dict) else {}
    for key in ("staging", "final"):
        ref = paths.get(key) if isinstance(paths.get(key), dict) else {}
        if not str(ref.get("path") or "").strip() or not str(ref.get("namespace_id") or "").strip():
            missing.append(f"paths.{key}")
    if missing:
        raise StoragePlanError(f"执行计划缺少或包含无效字段：{', '.join(dict.fromkeys(missing))}")
    strategy = str(transfer.get("strategy") or "").strip()
    executor = str(transfer.get("executor") or "").strip().lower()
    expected_executor = (
        "rclone" if strategy in {STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD} else "none"
    )
    if executor != expected_executor:
        raise StoragePlanError("执行计划的传输策略与执行器不一致")
    source = plan.get("source") if isinstance(plan.get("source"), dict) else {}
    source_namespace = str(source.get("namespace_id") or "").strip()
    ingress = str(source.get("ingress") or "").strip().lower()
    native_ingress = str(destination.get("native_ingress") or "").strip().lower()
    if native_ingress and native_ingress not in SUPPORTED_NATIVE_INGRESSES:
        raise StoragePlanError("执行计划的 destination.native_ingress 不受支持")
    if strategy == STRATEGY_RCLONE_COPY and driver_kind != "rclone":
        raise StoragePlanError("rclone_copy 执行计划必须使用 rclone 目标驱动")
    if strategy == STRATEGY_RELAY_UPLOAD and driver_kind != "cmcc_api":
        raise StoragePlanError("relay_upload 执行计划必须使用 cmcc_api 目标驱动")
    if strategy == STRATEGY_NATIVE_SAVE and (
        driver_kind != "cmcc_api"
        or ingress != ROUTE_CLOUD139_DIRECT
        or not native_ingress
        or native_ingress != ingress
    ):
        # Compatibility for native plans persisted before the explicit binding
        # existed. New plans always freeze destination.native_ingress.
        legacy_primary = (
            driver_kind == "cmcc_api"
            and ingress == ROUTE_CLOUD139_DIRECT
            and not native_ingress
            and str(destination.get("id") or "").strip() == "mobile_main"
            and str(destination.get("namespace_id") or "").strip() == "cmcc:primary"
        )
        if not legacy_primary:
            raise StoragePlanError("native_save 执行计划的来源与目标绑定不匹配")
    keys_to_validate = ["staging", "final"]
    landing = paths.get("landing") if isinstance(paths.get("landing"), dict) else {}
    if strategy in {STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD}:
        if not str(landing.get("path") or "").strip() or not str(landing.get("namespace_id") or "").strip():
            raise StoragePlanError("rclone 执行计划缺少 paths.landing")
        if not source_namespace or source_namespace != str(landing.get("namespace_id") or "").strip():
            raise StoragePlanError("执行计划的来源命名空间不一致")
        keys_to_validate.append("landing")
    organizer = paths.get("organizer") if isinstance(paths.get("organizer"), dict) else {}
    if organizer:
        keys_to_validate.append("organizer")
    for key in keys_to_validate:
        ref = paths.get(key) if isinstance(paths.get(key), dict) else {}
        path = str(ref.get("path") or "").replace("\\", "/")
        namespace = str(ref.get("namespace_id") or "").strip()
        if (
            any(part in {".", ".."} for part in path.split("/"))
            or "\x00" in path
            or any(ord(character) < 32 for character in path)
        ):
            raise StoragePlanError(f"执行计划 paths.{key} 含不安全路径")
        if not namespace:
            raise StoragePlanError(f"执行计划 paths.{key} 缺少命名空间")
        if namespace.startswith("local:") and not (
            path == "/storage-targets" or path.startswith("/storage-targets/")
        ):
            raise StoragePlanError(f"执行计划 paths.{key} 越出受控本地挂载")
    destination_namespace = str(destination.get("namespace_id") or "").strip()
    staging = paths.get("staging") if isinstance(paths.get("staging"), dict) else {}
    final = paths.get("final") if isinstance(paths.get("final"), dict) else {}
    if (
        not destination_namespace
        or destination_namespace != str(staging.get("namespace_id") or "").strip()
        or destination_namespace != str(final.get("namespace_id") or "").strip()
    ):
        raise StoragePlanError("执行计划的目标命名空间不一致")
    if strategy in {STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD}:
        expected_job_dir = f"job-{job_id}"
        landing_leaf = str(landing.get("path") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        staging_leaf = str(staging.get("path") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if landing_leaf != expected_job_dir:
            raise StoragePlanError("执行计划的来源路径不是任务级目录")
        if staging_leaf != expected_job_dir:
            raise StoragePlanError("执行计划的暂存路径不是任务级目录")
        postprocess = plan.get("postprocess") if isinstance(plan.get("postprocess"), dict) else {}
        if bool(postprocess.get("organizer_enabled")):
            if not organizer:
                raise StoragePlanError("执行计划缺少 Organizer 可见路径")
            organizer_leaf = (
                str(organizer.get("path") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            )
            if organizer_leaf != expected_job_dir:
                raise StoragePlanError("执行计划的 Organizer 路径不是任务级目录")
        if _paths_overlap(
            landing.get("namespace_id"),
            landing.get("path"),
            staging.get("namespace_id"),
            staging.get("path"),
        ) or _paths_overlap(
            landing.get("namespace_id"),
            landing.get("path"),
            final.get("namespace_id"),
            final.get("path"),
        ):
            raise StoragePlanError("执行计划的来源与目标目录重叠")
    return plan


def _build_target(target_cfg: dict[str, Any], destination_id: str) -> StorageTarget:
    categories: dict[str, TargetCategoryConfig] = {}
    raw_categories = target_cfg.get("categories") if isinstance(target_cfg.get("categories"), dict) else {}
    for key, cfg in raw_categories.items():
        if not isinstance(cfg, dict):
            continue
        categories[key] = TargetCategoryConfig(
            final_root=str(cfg.get("final_root") or ""),
            organizer_namespace_id=str(cfg.get("organizer_namespace_id") or ""),
            organizer_root=str(cfg.get("organizer_root") or ""),
        )
    config = {key: value for key, value in target_cfg.items() if key != "categories"}
    return StorageTarget(
        destination_id=destination_id,
        name=str(target_cfg.get("name") or destination_id),
        driver=str(target_cfg.get("driver") or ""),
        enabled=bool(target_cfg.get("enabled", True)),
        guest_selectable=bool(target_cfg.get("guest_selectable", False)),
        config=config,
        categories=categories,
    )


def _validate_native_ingress(target: StorageTarget, driver_kind: str) -> str:
    native_ingress = str(target.config.get("native_ingress") or "").strip().lower()
    if not native_ingress:
        return ""
    if native_ingress not in SUPPORTED_NATIVE_INGRESSES:
        raise StoragePlanError(
            f"存储目标 {target.destination_id} 的 native_ingress 不受支持：{native_ingress}"
        )
    if str(driver_kind or "").strip().lower() != "cmcc_api":
        raise StoragePlanError(
            f"存储目标 {target.destination_id} 的驱动不支持 native_ingress：{driver_kind}"
        )
    return native_ingress


def resolve_destination_id(
    raw_config: dict[str, Any],
    requested: Any,
    *,
    require_guest_selectable: bool = False,
) -> str | None:
    """按 请求 destination_id > storage.default_target 解析目标。

    请求显式指定了目标时，若不存在或未启用则返回 None（拒绝，不回落默认），
    避免把用户意图静默改写到别的盘。未指定请求时才使用 default_target。
    """

    storage = raw_config.get("storage") if isinstance(raw_config.get("storage"), dict) else {}
    if not storage.get("enabled"):
        return None
    targets = storage.get("targets") if isinstance(storage.get("targets"), dict) else {}
    candidate = _requested_destination(requested)
    if candidate:
        target = targets.get(candidate)
        if (
            isinstance(target, dict)
            and target.get("enabled", True)
            and (not require_guest_selectable or target.get("guest_selectable", False))
        ):
            return candidate
        return None
    default = str(storage.get("default_target") or "").strip()
    target = targets.get(default)
    if (
        isinstance(target, dict)
        and target.get("enabled", True)
        and (not require_guest_selectable or target.get("guest_selectable", False))
    ):
        return default
    return None


def destination_strategy(
    *,
    raw_config: dict[str, Any],
    route: str,
    source_kind: str,
    category_key: str,
    category: dict[str, Any],
    destination_id: str,
) -> tuple[str, str]:
    """Validate a target and return ``(driver_kind, strategy)`` before a job is submitted."""

    storage = raw_config.get("storage") if isinstance(raw_config.get("storage"), dict) else {}
    targets = storage.get("targets") if isinstance(storage.get("targets"), dict) else {}
    target_cfg = targets.get(destination_id)
    if not isinstance(target_cfg, dict):
        raise StoragePlanError(f"存储目标不存在：{destination_id}")
    if not target_cfg.get("enabled", True):
        raise StoragePlanError(f"存储目标已禁用：{destination_id}")
    driver_kind = str(target_cfg.get("driver") or "").strip().lower()
    try:
        driver = get_driver(driver_kind)
    except UnknownStorageDriver as exc:
        raise StoragePlanError(str(exc)) from exc
    target = _build_target(target_cfg, destination_id)
    _validate_native_ingress(target, driver_kind)
    errors = driver.validate(target)
    if errors:
        raise StoragePlanError("；".join(errors))
    target_category = target.category(category_key)
    if not str(target_category.final_root or "").strip():
        raise StoragePlanError(f"存储目标 {destination_id} 未配置分类 {category_key} 的 final_root")
    strategy = _select_strategy(
        route,
        source_kind,
        driver_kind,
        driver.capabilities(target),
        target=target,
    )
    return driver_kind, strategy


def validate_public_destination(
    *,
    raw_config: dict[str, Any],
    requested: Any,
    route: str,
    source_kind: str,
    category_key: str,
    category: dict[str, Any],
) -> str | None:
    """Validate a guest-selected destination and return its stable ID."""

    requested_id = _requested_destination(requested)
    storage = raw_config.get("storage") if isinstance(raw_config.get("storage"), dict) else {}
    if not storage.get("enabled"):
        return None
    resolved = resolve_destination_id(raw_config, requested_id, require_guest_selectable=True)
    if not resolved:
        if requested_id:
            raise StoragePlanError(f"存储目标不可由访客选择：{requested_id}")
        raise StoragePlanError("当前没有可供访客使用的默认存储目标")
    _driver_kind, strategy = destination_strategy(
        raw_config=raw_config,
        route=route,
        source_kind=source_kind,
        category_key=category_key,
        category=category,
        destination_id=resolved,
    )
    if strategy == STRATEGY_MANUAL_REVIEW:
        if not requested_id and str(route or "").strip() in LEGACY_INGRESS_ROUTES:
            # No destination is frozen for legacy ingress. Its v2 path remains
            # available only after the default target passed the guest check.
            return None
        raise StoragePlanError("当前资源与该访客存储目标没有可用传输策略")
    return resolved


def _select_strategy(
    route: str,
    source_kind: str,
    destination_driver: str,
    capabilities: Any,
    *,
    target: StorageTarget | None = None,
) -> str:
    # 同盘原生转存优先：来源能直接写目标驱动时选择 native_save，不启动 rclone Worker。
    native_ingress = str(target.config.get("native_ingress") or "").strip().lower() if target else ""
    if (
        str(route or "").strip() == ROUTE_CLOUD139_DIRECT
        and str(destination_driver or "").strip() == "cmcc_api"
        and target is not None
        and capabilities.supports_native_save
        and native_ingress == ROUTE_CLOUD139_DIRECT
    ):
        return STRATEGY_NATIVE_SAVE
    if route == ROUTE_QUARK_TO_MOBILE:
        if capabilities.supports_rclone_dest:
            return STRATEGY_RCLONE_COPY
        if capabilities.supports_relay_upload:
            return STRATEGY_RELAY_UPLOAD
        return STRATEGY_MANUAL_REVIEW
    # sixpan / magnet / torrent / cloud189 在 v3 首批保持 legacy ingress。
    return STRATEGY_MANUAL_REVIEW


def _source_landing(
    route: str,
    category: dict[str, Any],
    job_id: int,
    *,
    namespace_id: str,
) -> dict[str, Any] | None:
    if route != ROUTE_QUARK_TO_MOBILE:
        return None
    quark_root = str(category.get("quark_save_path") or "").strip().replace("\\", "/")
    if not quark_root:
        return None
    landing = f"{quark_root.rstrip('/')}/job-{int(job_id)}"
    return {"namespace_id": namespace_id, "path": landing}


def build_execution_plan(
    *,
    raw_config: dict[str, Any],
    route: str,
    source_kind: str,
    category_key: str,
    category: dict[str, Any],
    job_id: int,
    destination_id: str | None,
    staging_enabled: bool,
    staging_dir_name: str,
) -> dict[str, Any] | None:
    """构建 v3 执行计划。

    ``None`` 只表示没有启用存储目标；一旦解析到目标，任何规划错误都抛出，
    调用方不得静默回退旧移动云路径。
    """

    resolved = resolve_destination_id(raw_config, destination_id)
    if not resolved:
        return None
    storage = raw_config.get("storage") if isinstance(raw_config.get("storage"), dict) else {}
    targets = storage.get("targets") if isinstance(storage.get("targets"), dict) else {}
    target_cfg = targets.get(resolved)
    if not isinstance(target_cfg, dict):
        raise StoragePlanError(f"存储目标不存在：{resolved}")
    driver_kind = str(target_cfg.get("driver") or "").strip().lower()
    try:
        driver = get_driver(driver_kind)
    except UnknownStorageDriver as exc:
        raise StoragePlanError(str(exc)) from exc
    target = _build_target(target_cfg, resolved)
    native_ingress = _validate_native_ingress(target, driver_kind)
    errors = driver.validate(target)
    if errors:
        raise StoragePlanError("；".join(errors))
    if int(job_id or 0) <= 0:
        raise StoragePlanError("执行计划缺少有效任务 ID")
    if not str(target.category(category_key).final_root or "").strip():
        raise StoragePlanError(f"存储目标 {resolved} 未配置分类 {category_key} 的 final_root")
    strategy = _select_strategy(
        route,
        source_kind,
        driver_kind,
        driver.capabilities(target),
        target=target,
    )
    effective_staging_enabled = bool(staging_enabled)
    try:
        paths = driver.resolve_paths(
            target=target,
            category_key=category_key,
            job_id=job_id,
            staging_enabled=effective_staging_enabled,
            staging_dir_name=staging_dir_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise StoragePlanError(f"目标路径规划失败：{exc}") from exc
    if not str(paths.staging.path or "").strip() or not str(paths.final.path or "").strip():
        raise StoragePlanError("目标驱动返回空的 staging/final 路径")
    landing = _source_landing(route, category, job_id, namespace_id=_source_namespace(raw_config))

    if landing:
        for label, destination_ref in (("暂存", paths.staging), ("最终", paths.final)):
            if _paths_overlap(
                landing.get("namespace_id"),
                landing.get("path"),
                destination_ref.namespace_id,
                destination_ref.path,
            ):
                raise StoragePlanError(
                    f"来源目录与目标{label}目录在同一命名空间重叠，已拒绝执行"
                )

    organizer_enabled = bool(
        (raw_config.get("organizer") if isinstance(raw_config.get("organizer"), dict) else {}).get("enabled", False)
    )
    if organizer_enabled and paths.organizer is None and strategy != STRATEGY_NATIVE_SAVE:
        raise StoragePlanError(f"存储目标 {resolved} 未配置 Organizer 可见路径")
    staging_path = str(paths.staging.path or "").strip()
    organizer_path = str(paths.organizer.path or "").strip() if paths.organizer is not None else ""
    if strategy in {STRATEGY_RCLONE_COPY, STRATEGY_RELAY_UPLOAD} and not effective_staging_enabled:
        # Even without Organizer staging, workers remain isolated to a durable
        # job directory. The directory lives directly below the final root so
        # disabling Organizer never strands data under _入库暂存.
        staging_path = _job_scoped_path(paths.final.path, job_id)
        if organizer_path:
            organizer_path = _job_scoped_path(organizer_path, job_id)
    postprocess: dict[str, Any] = {
        "organizer_enabled": organizer_enabled,
        "organizer_final_root": str(target.category(category_key).organizer_root or "").strip(),
        "strm_refresh_prefix": str(
            (raw_config.get("organizer") if isinstance(raw_config.get("organizer"), dict) else {}).get(
                f"strm_refresh_prefix_{str(category_key).lower()}"
            )
            or ""
        ),
        "media_refresh_enabled": False,
    }
    plan = {
        "version": EXECUTION_PLAN_VERSION,
        "job_id": int(job_id),
        "source": {
            "kind": str(source_kind or route or "unknown"),
            "ingress": str(route or "unknown"),
            "namespace_id": _source_namespace(raw_config),
        },
        "destination": {
            "id": resolved,
            "driver": driver_kind,
            "namespace_id": paths.namespace_id,
            "native_ingress": native_ingress,
            "config_revision": int(storage.get("plan_version") or EXECUTION_PLAN_VERSION),
        },
        "transfer": {
            "strategy": strategy,
            "executor": "none" if strategy in {STRATEGY_NATIVE_SAVE, STRATEGY_MANUAL_REVIEW} else "rclone",
            "delete_source_after_ack": True,
        },
        "paths": {
            "landing": landing,
            "staging": {"namespace_id": paths.staging.namespace_id, "path": staging_path},
            "final": {"namespace_id": paths.final.namespace_id, "path": paths.final.path},
            "organizer": (
                {"namespace_id": paths.organizer.namespace_id, "path": organizer_path}
                if paths.organizer is not None
                else None
            ),
        },
        "postprocess": postprocess,
    }
    return validated_execution_plan_from_job(
        {
            "id": int(job_id),
            "plan_version": EXECUTION_PLAN_VERSION,
            "destination_id": resolved,
            "raw_data": {"execution_plan": plan},
        }
    )


def _job_scoped_path(root: Any, job_id: int) -> str:
    value = str(root or "").strip().replace("\\", "/").rstrip("/")
    if not value:
        raise StoragePlanError("无法从空目标根目录生成任务级路径")
    return f"{value}/job-{int(job_id)}"


def _source_namespace(raw_config: dict[str, Any]) -> str:
    rclone = raw_config.get("rclone") if isinstance(raw_config.get("rclone"), dict) else {}
    remote = str(rclone.get("remote_name") or "MP").strip()
    return f"rclone:{remote}"


def _paths_overlap(namespace_a: Any, path_a: Any, namespace_b: Any, path_b: Any) -> bool:
    if str(namespace_a or "").strip() != str(namespace_b or "").strip():
        return False
    left = "/".join(part for part in str(path_a or "").replace("\\", "/").split("/") if part and part != ".").strip("/")
    right = "/".join(part for part in str(path_b or "").replace("\\", "/").split("/") if part and part != ".").strip("/")
    if not left or not right:
        return False
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def provider_target_from_plan(plan: dict[str, Any], route: str) -> str:
    """从 v3 计划还原 provider_target_path（供 legacy provider 提交使用）。"""

    paths = plan.get("paths") if isinstance(plan.get("paths"), dict) else {}
    landing = paths.get("landing") if isinstance(paths.get("landing"), dict) else {}
    staging = paths.get("staging") if isinstance(paths.get("staging"), dict) else {}
    if route == ROUTE_QUARK_TO_MOBILE:
        return str(landing.get("path") or "")
    return str(staging.get("path") or "")
