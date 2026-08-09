"""读取数据库任务中的 V1 冻结执行计划。

任务执行只允许使用 ``plan_schema_version=1``（当前字段名为
``execution_plan.version``）的不可变计划。这个模块是 Web、Worker、回调和
Organizer 共用的窄入口：它不会读取旧版 ``staging_plan``、分类路径或任何
当前运行时配置，也不会把旧任务“补全”为新计划。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..domain.models import EXECUTION_PLAN_VERSION, SelectionSnapshot


class JobPlanError(ValueError):
    """任务没有可安全执行的 V1 计划。"""

    code = "UNSUPPORTED_PLAN_VERSION"


def canonical_plan_json(value: Any) -> str:
    """Canonical UTF-8 JSON used by operation and plan integrity hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def operation_identity(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable business identity of an external transfer.

    Task-local paths/job ids are intentionally excluded so duplicate requests
    for the same frozen source/route/target can be deduplicated.  Every mutable
    configuration object is represented by its immutable revision.
    """

    source = _dict(plan.get("source"))
    destination = _dict(plan.get("destination"))
    routing = _dict(plan.get("routing"))
    bindings = _dict(plan.get("bindings"))
    transfer = _dict(plan.get("transfer"))
    return {
        "schema_version": _as_int(plan.get("plan_schema_version"), 0),
        "source": {
            "provider_kind": str(source.get("provider_kind") or ""),
            "source_form": str(source.get("source_form") or ""),
            "record_id": str(source.get("record_id") or ""),
            "record_revision": _as_int(source.get("record_revision"), 0),
            "selection_hash": str(source.get("selection_hash") or ""),
            "account_id": str(source.get("account_id") or ""),
            "account_revision": _as_int(source.get("account_revision"), 0),
            "account_selection_revision": _as_int(
                source.get("account_selection_revision"), 0
            ),
        },
        "category": str(plan.get("category") or ""),
        "routing": {
            "assignment_id": str(routing.get("assignment_id") or ""),
            "assignment_revision": _as_int(routing.get("assignment_revision"), 0),
            "search_source_id": str(routing.get("search_source_id") or ""),
            "category_key": str(routing.get("category_key") or ""),
            "destination_id": str(routing.get("destination_id") or ""),
        },
        "destination": {
            "id": str(destination.get("id") or ""),
            "provider_kind": str(destination.get("provider_kind") or ""),
            "target_revision": _as_int(destination.get("target_revision"), 0),
            "binding_id": str(destination.get("binding_id") or ""),
            "binding_revision": _as_int(destination.get("binding_revision"), 0),
            "account_id": str(destination.get("account_id") or ""),
            "account_revision": _as_int(destination.get("account_revision"), 0),
        },
        "bindings": {
            key: bindings.get(key)
            for key in (
                "source_mount_id",
                "source_mount_revision",
                "source_connection_id",
                "source_connection_revision",
                "destination_mount_id",
                "destination_mount_revision",
                "destination_connection_id",
                "destination_connection_revision",
                "organizer_mount_id",
                "organizer_mount_revision",
                "organizer_connection_id",
                "organizer_connection_revision",
            )
        },
        "strategy": str(transfer.get("strategy") or ""),
    }


def compute_operation_key(plan: dict[str, Any]) -> str:
    selection_hash = str(_dict(plan.get("source")).get("selection_hash") or "-")
    digest = hashlib.sha256(
        canonical_plan_json(operation_identity(plan)).encode("utf-8")
    ).hexdigest()
    return f"import:v1:{selection_hash}:{digest}"


def compute_plan_hash(plan: dict[str, Any]) -> str:
    candidate = copy.deepcopy(plan if isinstance(plan, dict) else {})
    integrity = candidate.get("integrity")
    if isinstance(integrity, dict):
        integrity.pop("plan_hash", None)
    return hashlib.sha256(canonical_plan_json(candidate).encode("utf-8")).hexdigest()


def attach_plan_integrity(plan: dict[str, Any]) -> dict[str, Any]:
    plan["integrity"] = {"algorithm": "sha256", "plan_hash": ""}
    plan["integrity"]["plan_hash"] = compute_plan_hash(plan)
    return plan


def validate_plan_integrity(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    transfer = _dict(plan.get("transfer"))
    operation_key = str(transfer.get("idempotency_key") or "").strip()
    expected_operation_key = compute_operation_key(plan)
    if operation_key != expected_operation_key:
        errors.append("transfer.idempotency_key")
    integrity = _dict(plan.get("integrity"))
    if str(integrity.get("algorithm") or "").lower() != "sha256":
        errors.append("integrity.algorithm")
    declared_hash = str(integrity.get("plan_hash") or "").strip()
    if not declared_hash or declared_hash != compute_plan_hash(plan):
        errors.append("integrity.plan_hash")
    return errors


def _raw_data(job: dict[str, Any] | None) -> dict[str, Any]:
    value = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
    return value if isinstance(value, dict) else {}


def v1_execution_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """返回任务中固化的 V1 计划；缺失、旧版或未来版均返回空字典。

    V1 has one envelope and two deliberately redundant version markers. Both
    markers must be present and equal to ``1``; accepting either marker alone
    would make a partially migrated payload look executable.
    """

    # The plan is injected by the execution-plan repository for the current
    # operation.  ``import_jobs.raw_data`` intentionally contains only an
    # ``execution_plan_ref`` summary and is never parsed as an executable plan.
    plan = (job or {}).get("_canonical_execution_plan") if isinstance((job or {}).get("_canonical_execution_plan"), dict) else {}
    if not plan:
        return {}
    version = _as_int(plan.get("version"), 0)
    schema_version = _as_int(plan.get("plan_schema_version"), 0)
    return (
        plan
        if version == EXECUTION_PLAN_VERSION and schema_version == EXECUTION_PLAN_VERSION
        else {}
    )


def validated_v1_execution_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the immutable V1 plan before handing it to a worker."""

    raw_job = job if isinstance(job, dict) else {}
    plan = v1_execution_plan_from_job(raw_job)
    if not plan:
        # Keep the rejected envelope long enough to report all actionable V1
        # omissions (for example a missing relay ``local_temp``), but never
        # return it as executable.  This avoids hiding the real defect behind
        # a generic version error while preserving the hard cutover.
        candidate = raw_job.get("_canonical_execution_plan") if isinstance(raw_job.get("_canonical_execution_plan"), dict) else {}
        candidate_version = _as_int(candidate.get("version"), 0)
        persisted = raw_job.get("plan_version")
        if candidate and candidate_version == EXECUTION_PLAN_VERSION and _as_int(persisted, 0) == EXECUTION_PLAN_VERSION:
            plan = candidate
        elif persisted in (None, "", 0, "0"):
            raise JobPlanError("job is missing a V1 execution_plan")
        else:
            raise JobPlanError(f"job plan version {persisted!r} is not supported V1")

    raw_data = _raw_data(raw_job)
    plan_ref = raw_data.get("execution_plan_ref") if isinstance(raw_data.get("execution_plan_ref"), dict) else {}
    declared_ref_hash = str(plan_ref.get("plan_hash") or "").strip()
    if declared_ref_hash and declared_ref_hash != compute_plan_hash(plan):
        raise JobPlanError("job execution_plan_ref hash does not match import_execution_plans")

    missing: list[str] = []
    job_id = _as_int(raw_job.get("id"), 0)
    plan_job_id = _as_int(plan.get("job_id"), 0)
    if job_id <= 0 or plan_job_id != job_id:
        missing.append("job_id")
    persisted_version = _as_int(raw_job.get("plan_version"), 0)
    plan_version = _as_int(plan.get("version"), 0)
    if persisted_version != EXECUTION_PLAN_VERSION or plan_version != persisted_version:
        missing.append("version")
    schema_version = _as_int(plan.get("plan_schema_version"), 0)
    # A candidate with V1 ``version`` but a missing schema marker is still
    # validated strictly so the final error lists every missing field.  It is
    # never accepted: ``plan_schema_version`` remains a required error below.
    strict_paths = plan_version == EXECUTION_PLAN_VERSION
    if schema_version != EXECUTION_PLAN_VERSION:
        missing.append("plan_schema_version")

    source = _dict(plan.get("source"))
    destination = _dict(plan.get("destination"))
    routing = _dict(plan.get("routing"))
    transfer = _dict(plan.get("transfer"))
    paths = _dict(plan.get("paths"))
    for key in ("provider_kind", "source_form", "canonical_source_id"):
        if not str(source.get(key) or "").strip():
            missing.append(f"source.{key}")
    if not str(source.get("search_source_id") or "").strip():
        missing.append("source.search_source_id")
    if not str(source.get("record_id") or "").strip() or _as_int(source.get("record_revision"), 0) <= 0:
        missing.append("source.record_id/revision")
    if not str(source.get("account_id") or "").strip() or _as_int(source.get("account_revision"), 0) <= 0:
        missing.append("source.account_id/revision")
    if _as_int(source.get("account_selection_revision"), 0) <= 0:
        missing.append("source.account_selection_revision")
    selection = source.get("selection")
    selection_hash = str(source.get("selection_hash") or "").strip()
    if strict_paths and not isinstance(selection, dict):
        missing.append("source.selection")
    elif isinstance(selection, dict):
        snapshot = SelectionSnapshot.from_dict(selection, provider_kind=str(source.get("provider_kind") or ""))
        declared = str(selection.get("selection_hash") or selection.get("hash") or "").strip()
        if not snapshot.is_valid:
            missing.append("source.selection.hash")
        if declared and declared != snapshot.selection_hash:
            missing.append("source.selection.hash")
        if selection_hash and selection_hash != snapshot.selection_hash:
            missing.append("source.selection_hash")

    # ``destination_id`` was a pre-V1 alias. A V1 plan must use the canonical
    # ``destination.id`` field so the database identity check cannot be
    # bypassed by supplying only the legacy spelling.
    destination_id = str(destination.get("id") or "").strip()
    if not destination_id:
        missing.append("destination.id")
    if not str(destination.get("provider_kind") or "").strip():
        missing.append("destination.provider_kind")
    if _as_int(destination.get("target_revision"), 0) <= 0:
        missing.append("destination.target_revision")
    if not str(destination.get("binding_id") or "").strip() or _as_int(destination.get("binding_revision"), 0) <= 0:
        missing.append("destination.binding_id/revision")
    if destination.get("account_id") and _as_int(destination.get("account_revision"), 0) <= 0:
        missing.append("destination.account_id/revision")
    persisted_destination = str(raw_job.get("destination_id") or "").strip()
    if not persisted_destination or persisted_destination != destination_id:
        missing.append("destination_id")
    category = str(plan.get("category") or "").strip()
    if not category:
        missing.append("category")
    elif raw_job.get("category") and str(raw_job.get("category")).strip() != category:
        missing.append("category")

    if not routing:
        missing.append("routing")
    else:
        for key in ("assignment_id", "search_source_id", "category_key", "destination_id"):
            if not str(routing.get(key) or "").strip():
                missing.append(f"routing.{key}")
        if _as_int(routing.get("assignment_revision"), 0) <= 0:
            missing.append("routing.assignment_revision")
        if not bool(routing.get("enabled")):
            missing.append("routing.enabled")
        if str(routing.get("search_source_id") or "") != str(source.get("search_source_id") or ""):
            missing.append("routing.search_source_id")
        if str(routing.get("category_key") or "") != category:
            missing.append("routing.category_key")
        if str(routing.get("destination_id") or "") != destination_id:
            missing.append("routing.destination_id")

    if not isinstance(plan.get("target_category"), dict):
        missing.append("target_category")

    verification = _dict(plan.get("verification"))
    if not verification:
        missing.append("verification")
    else:
        for key in ("expected_file_count", "expected_total_size"):
            if _as_int(verification.get(key), -1) < 0:
                missing.append(f"verification.{key}")
        minimum = verification.get("minimum")
        if not isinstance(minimum, (list, tuple, set, frozenset)) or not {
            str(item) for item in minimum
        }.issuperset({"file_count", "total_size"}):
            missing.append("verification.minimum")
        if str(verification.get("selection_hash") or "") != str(source.get("selection_hash") or ""):
            missing.append("verification.selection_hash")

    strategy = str(transfer.get("strategy") or "").strip().lower()
    executor = str(transfer.get("executor") or "").strip().lower()
    expected_executor = {
        "native_save": "native",
        "native_offline": "native_offline",
        "rclone_copy": "rclone",
        "relay_upload_provider": "provider_relay",
    }.get(strategy)
    if expected_executor is None:
        missing.append("transfer.strategy")
    elif executor != expected_executor:
        missing.append("transfer.executor")
    destination_provider = str(destination.get("provider_kind") or "").strip().lower()
    if strategy == "rclone_copy" and destination_provider in {"mobile", "tianyi"}:
        missing.append("transfer.strategy_for_destination")
    operation_key = str(transfer.get("idempotency_key") or "").strip()
    if not operation_key:
        missing.append("transfer.idempotency_key")
    missing.extend(validate_plan_integrity(plan))

    # Every V1 plan has an immutable final path. Data-plane strategies require
    # a task-scoped landing path.  rclone_copy also has a destination staging
    # path; Provider relay deliberately does not because it uploads from
    # the frozen local relay cache directly to the native destination.
    relay_strategies = {"relay_upload_provider"}
    required = ["destination_final"]
    if strategy == "rclone_copy" or strategy in relay_strategies:
        required.append("source_landing")
    if strategy == "rclone_copy":
        required.append("destination_staging")
    if strategy in relay_strategies:
        required.append("local_temp")
    for key in required:
        ref = _dict(paths.get(key))
        if not str(ref.get("namespace_id") or "").strip() or not str(ref.get("path") or "").strip():
            missing.append(f"paths.{key}")
            continue
        kind = _path_kind(ref)
        if strict_paths and not str(ref.get("kind") or "").strip():
            missing.append(f"paths.{key}.kind")
        allowed = {
            "destination_final": {"native"} if strategy in {"native_save", "native_offline", *relay_strategies} else {"openlist", "local"},
            "source_landing": {"openlist"},
            "destination_staging": {"openlist", "local"},
            "local_temp": {"relay"},
        }.get(key, set())
        if kind not in allowed:
            missing.append(f"paths.{key}.kind")
        if key in {"source_landing", "local_temp"} or (
            key == "destination_staging" and strategy == "rclone_copy"
        ):
            leaf = str(ref.get("path") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if leaf != f"job-{job_id}":
                missing.append(f"paths.{key}.job_scope")
    if strategy in relay_strategies and paths.get("destination_staging") not in (None, {}):
        missing.append(f"paths.destination_staging must be empty for {strategy}")
    if missing:
        raise JobPlanError("V1 execution_plan missing/invalid fields: " + ",".join(dict.fromkeys(missing)))
    return plan


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _path_kind(ref: dict[str, Any]) -> str:
    declared = str(ref.get("kind") or "").strip().lower()
    if declared:
        return declared
    namespace = str(ref.get("namespace_id") or "").strip().lower()
    if namespace.startswith("openlist:"):
        return "openlist"
    if namespace.startswith("native:"):
        return "native"
    if namespace.startswith("local:"):
        # ``local:relay`` is reserved for the task relay cache.
        return "relay" if namespace == "local:relay" else "local"
    return "unknown"

def job_plan_reason(job: dict[str, Any] | None) -> str:
    """返回可展示给管理员的拒绝原因，不抛异常。"""

    try:
        validated_v1_execution_plan_from_job(job)
    except Exception as exc:  # noqa: BLE001 - diagnostic helper
        return str(exc)
    return ""


def plan_strategy(plan: dict[str, Any] | None) -> str:
    transfer = plan.get("transfer") if isinstance(plan, dict) and isinstance(plan.get("transfer"), dict) else {}
    return str(transfer.get("strategy") or "").strip().lower()


def plan_path(plan: dict[str, Any] | None, name: str) -> dict[str, Any]:
    paths = plan.get("paths") if isinstance(plan, dict) and isinstance(plan.get("paths"), dict) else {}
    value = paths.get(name)
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "JobPlanError",
    "attach_plan_integrity",
    "canonical_plan_json",
    "compute_operation_key",
    "compute_plan_hash",
    "job_plan_reason",
    "plan_path",
    "plan_strategy",
    "validated_v1_execution_plan_from_job",
    "validate_plan_integrity",
    "v1_execution_plan_from_job",
]
