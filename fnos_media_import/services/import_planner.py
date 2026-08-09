"""V1-only compatibility façade for the former service planner.

Planning is implemented by :mod:`fnos_media_import.planning.planner`.  This
module remains importable because a few administrative/read-only paths used to
import its helpers, but it does not parse old plans, global storage targets, or
category path fields.  New execution code must use the V1 planner and the
canonical ``planning.job_plan`` validator directly.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import EXECUTION_PLAN_VERSION
from ..planning.job_plan import (
    JobPlanError,
    plan_path,
    plan_strategy,
    validated_v1_execution_plan_from_job,
    v1_execution_plan_from_job,
)


class StoragePlanError(ValueError):
    """Raised when a removed planner entry point is invoked."""


EXECUTION_PLAN_VERSION = EXECUTION_PLAN_VERSION
STRATEGY_NATIVE_SAVE = "native_save"
STRATEGY_NATIVE_OFFLINE = "native_offline"
STRATEGY_RCLONE_COPY = "rclone_copy"
STRATEGY_RELAY_UPLOAD = "relay_upload_mobile"
STRATEGY_MANUAL_REVIEW = "manual_review"


def destination_scoped_idempotency_key(value: Any, destination_id: Any) -> str:
    """Bind an explicit destination to an idempotency key."""

    key = str(value or "").strip()
    destination = str(destination_id or "").strip()
    if not key or not destination:
        return key
    suffix = f":destination:{destination}"
    return key if key.endswith(suffix) else f"{key}{suffix}"


def execution_plan_from_job(job: dict[str, Any] | None, *, execution_repository: Any = None) -> dict[str, Any]:
    """Return a V1 envelope, never a legacy plan."""

    if execution_repository is not None:
        from .execution_plan_guard import require_v1_execution_plan

        return require_v1_execution_plan(job, execution_repository=execution_repository)
    return v1_execution_plan_from_job(job)


def validated_execution_plan_from_job(
    job: dict[str, Any] | None,
    *,
    execution_repository: Any = None,
) -> dict[str, Any]:
    """Validate only the canonical V1 execution plan.

    The old v2/v3 validator was an execution fallback and is intentionally
    gone.  Invalid or historical jobs raise a single actionable error so the
    caller can move them to manual review.
    """

    try:
        if execution_repository is not None:
            from .execution_plan_guard import require_v1_execution_plan

            return require_v1_execution_plan(job, execution_repository=execution_repository)
        return validated_v1_execution_plan_from_job(job)
    except (JobPlanError, TypeError, ValueError) as exc:
        raise StoragePlanError(str(exc)) from exc


def resolve_destination_id(
    raw_config: dict[str, Any],
    requested: Any,
    *,
    require_guest_selectable: bool = False,
) -> str | None:
    """Fail closed; destination assignments now come from the V1 database.

    ``raw_config`` is accepted only to keep the old call signature stable.  It
    is deliberately not inspected, so a stale ``storage.targets`` section can
    never influence a new subscription or import.
    """

    del raw_config, requested, require_guest_selectable
    return None


def destination_strategy(**kwargs: Any) -> tuple[str, str]:
    """Reject the removed config-driven strategy resolver."""

    del kwargs
    raise StoragePlanError(
        "legacy destination strategy resolver is disabled; use the V1 assignment planner"
    )


def validate_public_destination(**kwargs: Any) -> str | None:
    """Reject public destination selection through the removed config API."""

    del kwargs
    raise StoragePlanError(
        "legacy public destination resolver is disabled; use the V1 source assignment"
    )


def build_execution_plan(**kwargs: Any) -> dict[str, Any] | None:
    """Reject the removed v2/v3 planner entry point.

    Keeping this explicit failure is preferable to silently constructing a
    partially V1-shaped plan from mutable config.  The production planner is
    ``planning.planner.ImportPlanner.plan_import``.
    """

    del kwargs
    raise StoragePlanError(
        "legacy services.import_planner is disabled; use planning.planner.ImportPlanner"
    )


def provider_target_from_plan(plan: dict[str, Any], route: str = "") -> str:
    """Return a path from a validated V1 plan for read-only diagnostics."""

    del route
    value = plan if isinstance(plan, dict) else {}
    transfer = value.get("transfer") if isinstance(value.get("transfer"), dict) else {}
    strategy = str(transfer.get("strategy") or "").strip().lower()
    path_name = "source_landing" if strategy in {
        STRATEGY_RCLONE_COPY,
        STRATEGY_RELAY_UPLOAD,
    } else "destination_final"
    return str(plan_path(value, path_name).get("path") or "")


__all__ = [
    "EXECUTION_PLAN_VERSION",
    "STRATEGY_MANUAL_REVIEW",
    "STRATEGY_NATIVE_OFFLINE",
    "STRATEGY_NATIVE_SAVE",
    "STRATEGY_RCLONE_COPY",
    "STRATEGY_RELAY_UPLOAD",
    "StoragePlanError",
    "build_execution_plan",
    "destination_scoped_idempotency_key",
    "destination_strategy",
    "execution_plan_from_job",
    "provider_target_from_plan",
    "resolve_destination_id",
    "validate_public_destination",
    "validated_execution_plan_from_job",
]
