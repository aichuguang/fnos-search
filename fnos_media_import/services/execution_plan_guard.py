"""Fail-closed guard for the canonical V1 import execution plan.

Retired task metadata may still exist in databases created by older releases,
but it is never interpreted here.  Callers either receive a validated V1
plan or atomically move the job to manual review and stop the old execution
path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..constants import COMPLETION_STAGE_REVIEW, EVENT_ERROR, EVENT_WARN, JOB_REVIEW
from ..planning import is_data_plane_strategy, is_relay_upload_strategy
from ..planning.job_plan import (
    JobPlanError,
    plan_path,
    plan_strategy,
    validated_v1_execution_plan_from_job,
)


RETIRED_PLAN_FIELD = "staging_" + "plan"
RETIRED_SIXPAN_REFRESH_FIELD = "sixpan_" + "legacy_" + "refresh"


def require_v1_execution_plan(
    job: dict[str, Any] | None,
    *,
    execution_repository: Any = None,
) -> dict[str, Any]:
    """Return the canonical V1 plan or raise ``JobPlanError``.

    This is intentionally a thin wrapper around ``planning.job_plan`` so all
    services share one version and identity check.
    """

    candidate = _job_with_canonical_plan(job, execution_repository)
    return validated_v1_execution_plan_from_job(candidate)


def reject_non_v1_job(
    database: Any,
    job: dict[str, Any] | None,
    *,
    context: str,
    error: Exception | str | None = None,
    level: str = EVENT_WARN,
    execution_repository: Any = None,
) -> dict[str, Any]:
    """Stop an old/invalid task and durably record manual-review state.

    Retired fields are detected only to improve the audit message; their
    contents are never parsed or used to derive paths, accounts, or commands.
    """

    current = job if isinstance(job, dict) else {}
    job_id = _positive_int(current.get("id"))
    raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
    reason = str(error or "任务缺少可执行的 V1 execution_plan").strip()
    retired_fields = [
        field
        for field in (RETIRED_PLAN_FIELD, RETIRED_SIXPAN_REFRESH_FIELD)
        if field in raw_data
    ]
    message = f"{context}已拒绝旧版或无效任务并转人工审核：{reason}"
    marker = {
        "state": "manual_review",
        "reason": "unsupported_execution_plan",
        "message": message,
        "retired_fields": retired_fields,
        "recorded_at": _utc_now(),
    }
    updated = False
    if database is not None and job_id > 0:
        latest = database.job_queries.get(job_id) or current
        latest_raw = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        completion = (
            latest_raw.get("completion")
            if isinstance(latest_raw.get("completion"), dict)
            else {}
        )
        next_raw = {
            **latest_raw,
            "execution_plan_guard": marker,
            "completion": {
                **completion,
                "stage": COMPLETION_STAGE_REVIEW,
                "message": message,
                "retryable": False,
                "retry_action": "create_new_job",
            },
        }
        current_status = str(latest.get("status") or "").strip()
        if current_status not in {"cancelled", "done", "success", "skipped_existing", "rejected"}:
            if current_status:
                updated = bool(
                    database.job_commands.update_if_status(
                        job_id,
                        {current_status},
                        status=JOB_REVIEW,
                        error_message=message,
                        raw_data=next_raw,
                    )
                )
            else:
                latest_check = database.job_queries.get(job_id) or latest
                latest_status = str(latest_check.get("status") or "").strip()
                if not current_status or latest_status == current_status:
                    database.job_commands.update(
                        job_id,
                        status=JOB_REVIEW,
                        error_message=message,
                        raw_data=next_raw,
                    )
                    updated = True
        _mark_execution_manual_review(execution_repository, job_id, message)
        database.job_commands.add_event(
            job_id,
            EVENT_ERROR if level == EVENT_ERROR else EVENT_WARN,
            message,
            {
                "execution_plan_guard": marker,
                "status_updated": updated,
            },
        )
    return {
        "success": False,
        "rejected": True,
        "manual_review": True,
        "job_id": job_id,
        "message": message,
        "retired_fields": retired_fields,
        "status_updated": updated,
    }


def v1_plan_or_review(
    database: Any,
    job: dict[str, Any] | None,
    *,
    context: str,
    level: str = EVENT_WARN,
    execution_repository: Any = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return ``(plan, rejection)`` without allowing a legacy fallback."""

    try:
        return require_v1_execution_plan(
            job,
            execution_repository=execution_repository,
        ), None
    except (JobPlanError, TypeError, ValueError) as exc:
        return {}, reject_non_v1_job(
            database,
            job,
            context=context,
            error=exc,
            level=level,
            execution_repository=execution_repository,
        )


def v1_rclone_copy_run(
    job: dict[str, Any],
    plan: dict[str, Any] | None = None,
    *,
    execution_repository: Any = None,
) -> dict[str, Any]:
    """Derive the shell-worker copy descriptor solely from a validated V1 plan."""

    plan = plan or require_v1_execution_plan(job, execution_repository=execution_repository)
    strategy = plan_strategy(plan)
    if not is_data_plane_strategy(strategy):
        return {}
    source = plan_path(plan, "source_landing")
    target = plan_path(
        plan,
        "destination_staging" if not is_relay_upload_strategy(strategy) else "local_temp",
    )
    source_root, source_leaf = _split_job_path(source.get("path"))
    target_root, target_leaf = _split_job_path(target.get("path"))
    job_id = _positive_int(job.get("id"))
    expected_leaf = f"job-{job_id}"
    if source_leaf != expected_leaf or target_leaf != expected_leaf:
        raise JobPlanError("V1 rclone_copy 路径不是当前任务的 job 级目录")

    target_namespace = str(target.get("namespace_id") or "local:relay").strip()
    source_namespace = str(source.get("namespace_id") or "").strip()
    if source_namespace.lower().startswith("openlist:") or target_namespace.lower().startswith(
        "openlist:"
    ):
        raise JobPlanError(
            "OpenList namespace 不是 rclone remote；必须由托管 locator + 冻结 revision 派生"
        )
    source_remote = _remote_from_namespace(source_namespace)
    target_remote = _remote_from_namespace(target_namespace)
    local_target = target_root if target_namespace.lower().startswith("local:") else ""
    if not source_remote or (not target_remote and not local_target):
        raise JobPlanError("V1 rclone_copy 缺少可解析的来源或目标 namespace")
    category = str(job.get("category") or plan.get("category") or "").strip().lower()
    suffix = {
        "movie": "MOVIE",
        "tv": "TV",
        "anime": "ANIME",
        "variety": "VARIETY",
        "other": "OTHER",
    }.get(category)
    if not suffix:
        raise JobPlanError("V1 rclone_copy 分类无效")
    transfer = plan.get("transfer") if isinstance(plan.get("transfer"), dict) else {}
    destination = plan.get("destination") if isinstance(plan.get("destination"), dict) else {}
    return {
        "copy_mode": True,
        "job_id": job_id,
        "category": category,
        "category_suffix": suffix,
        "job_dir_name": expected_leaf,
        "source_remote": source_remote,
        "source_root": source_root,
        "target_remote": target_remote,
        "target_root": target_root,
        "local_target": local_target,
        "destination_id": str(destination.get("id") or "").strip(),
        "delete_source_after_ack": bool(transfer.get("delete_source_after_ack", True)),
    }


def _job_with_canonical_plan(
    job: dict[str, Any] | None,
    execution_repository: Any,
) -> dict[str, Any]:
    """Inject a DB-frozen plan into an ephemeral validation snapshot.

    ``import_execution_plans.plan_json`` is the only execution truth.  The
    temporary ``raw_data`` copy exists solely for the existing pure validator;
    it is never written back to ``import_jobs`` and therefore cannot become a
    second mutable plan source.
    """

    candidate = dict(job or {})
    if execution_repository is None:
        return candidate
    job_id = _positive_int(candidate.get("id"))
    getter = getattr(execution_repository, "plan", None)
    if job_id <= 0 or not callable(getter):
        raise JobPlanError("任务缺少有效 job_id 或执行计划仓储")
    frozen = getter(job_id)
    if not isinstance(frozen, dict):
        raise JobPlanError("数据库缺少不可变 import_execution_plans 记录")
    # Do not mutate the caller's row; the persisted job row may be reused by
    # cancellation/status code after validation.  The marker is ephemeral and
    # deliberately lives outside raw_data so the database row cannot become a
    # second plan source.
    candidate["_canonical_execution_plan"] = frozen
    return candidate


def plan_requires_organizer(plan: dict[str, Any] | None) -> bool:
    postprocess = (
        plan.get("postprocess")
        if isinstance(plan, dict) and isinstance(plan.get("postprocess"), dict)
        else {}
    )
    return bool(postprocess.get("organizer_enabled"))


def plan_allows_media_refresh(plan: dict[str, Any] | None) -> bool:
    postprocess = (
        plan.get("postprocess")
        if isinstance(plan, dict) and isinstance(plan.get("postprocess"), dict)
        else {}
    )
    return bool(postprocess.get("media_refresh_enabled"))


def _mark_execution_manual_review(repository: Any, job_id: int, message: str) -> None:
    if repository is None or job_id <= 0:
        return
    getter = getattr(repository, "get", None)
    saver = getattr(repository, "save", None)
    if not callable(getter) or not callable(saver):
        return
    try:
        execution = getter(job_id)
        if execution is None or str(getattr(execution, "phase", "")) == "manual_review":
            return
        snapshot = dict(getattr(execution, "snapshot", {}) or {})
        snapshot.update(
            {
                "phase": "manual_review",
                "message": message,
                "error_code": "UNSUPPORTED_PLAN_VERSION",
            }
        )
        saver(
            execution,
            snapshot,
            phase="manual_review",
            last_error_code="UNSUPPORTED_PLAN_VERSION",
            last_error_message=message,
        )
    except Exception:  # noqa: BLE001 - job review is still persisted independently
        return


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _split_job_path(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().replace("\\", "/")
    absolute = text.startswith("/")
    parts = [part for part in text.strip("/").split("/") if part]
    if not parts or not parts[-1].startswith("job-"):
        return (f"/{'/'.join(parts)}" if absolute and parts else "/" if absolute else "/".join(parts)), ""
    root = "/".join(parts[:-1])
    return (f"/{root}" if absolute else root), parts[-1]


def _remote_from_namespace(value: Any) -> str:
    text = str(value or "").strip()
    return text[len("rclone:") :] if text.lower().startswith("rclone:") else ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "RETIRED_PLAN_FIELD",
    "RETIRED_SIXPAN_REFRESH_FIELD",
    "plan_allows_media_refresh",
    "plan_requires_organizer",
    "reject_non_v1_job",
    "require_v1_execution_plan",
    "v1_plan_or_review",
    "v1_rclone_copy_run",
]
