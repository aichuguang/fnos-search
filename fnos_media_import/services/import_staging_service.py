"""V1 execution-plan helpers for the retired staging façade.

The application now creates and executes one immutable ``execution_plan`` per
job.  The former provider/category staging configuration is intentionally not
read here.  A few names remain importable for extensions and diagnostics, but
they either operate on an already-frozen V1 plan or fail closed.
"""

from __future__ import annotations

import re
from typing import Any

from ..planning.job_plan import plan_strategy, validated_v1_execution_plan_from_job


DEFAULT_STAGING_DIR_NAME = "\u5165\u5e93\u6682\u5b58"


def normalize_staging_segment(value: Any) -> str:
    """Normalize a caller-supplied display segment without resolving paths."""

    text = str(value or "").strip().replace("\\", "/").strip("/")
    if not text or text in {".", ".."} or "/" in text:
        return DEFAULT_STAGING_DIR_NAME
    text = re.sub(r'[\x00-\x1f<>:"|?*]+', "", text).strip(" .")
    return text or DEFAULT_STAGING_DIR_NAME


def staging_category_root(
    final_category_root: Any,
    *,
    category_label: Any = "",
    staging_dir_name: Any = "",
) -> str:
    """Pure path helper retained for diagnostics; it reads no configuration."""

    raw = str(final_category_root or "").strip().replace("\\", "/")
    leading_slash = raw.startswith("/")
    parts = [part for part in raw.strip("/").split("/") if part]
    label = str(category_label or "").strip().replace("\\", "/").strip("/")
    leaf = parts[-1] if parts else label
    if not leaf:
        return ""
    staging_segment = normalize_staging_segment(staging_dir_name)
    if staging_segment.casefold() == leaf.casefold():
        staging_segment = DEFAULT_STAGING_DIR_NAME
    result = "/".join([*parts[:-1], staging_segment, leaf])
    return f"/{result}" if leading_slash else result


def staging_job_root(category_root: Any, job_id: int) -> str:
    """Return a task-scoped child of an already-frozen root."""

    root = str(category_root or "").strip().replace("\\", "/").rstrip("/")
    try:
        normalized_job_id = int(job_id or 0)
    except (TypeError, ValueError):
        normalized_job_id = 0
    if not root or normalized_job_id <= 0:
        return ""
    return f"{root}/job-{normalized_job_id}"


def staging_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Never return the removed staging-plan envelope."""

    return {}


def validated_staging_plan_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Reject the removed envelope for explicit migration diagnostics only."""

    raw = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
    legacy_key = "staging_" + "plan"
    if isinstance(raw.get(legacy_key), dict):
        raise ValueError("legacy staging_plan is unsupported after the V1 cutover")
    return {}


def _v1_rclone_descriptor(job: dict[str, Any] | None) -> dict[str, Any]:
    try:
        plan = validated_v1_execution_plan_from_job(job)
    except Exception as exc:  # noqa: BLE001 - expose a stable fail-closed error
        raise ValueError(f"V1 execution_plan required: {exc}") from exc
    if plan_strategy(plan) != "rclone_copy":
        return {}
    raise ValueError(
        "V1 OpenList namespace 必须由绑定 revision 服务的 RcloneService 派生 descriptor"
    )


def rclone_staging_run_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Return a worker descriptor only for a V1 ``rclone_copy`` job."""

    return _v1_rclone_descriptor(job)


def rclone_copy_run_from_job(job: dict[str, Any] | None) -> dict[str, Any]:
    """Return a worker descriptor only for a V1 ``rclone_copy`` job."""

    return _v1_rclone_descriptor(job)


def map_staging_path_to_openlist(path: Any, plan: dict[str, Any] | None) -> str:
    """Legacy mapping is disabled; V1 paths are already namespace-qualified."""

    return ""


def map_execution_path_to_organizer(path: Any, plan: dict[str, Any] | None) -> str:
    """Map a frozen V1 rclone staging path into the frozen Organizer path."""

    value_plan = plan if isinstance(plan, dict) else {}
    paths = value_plan.get("paths") if isinstance(value_plan.get("paths"), dict) else {}
    staging = paths.get("destination_staging")
    organizer = paths.get("organizer")
    if not isinstance(staging, dict) or not isinstance(organizer, dict):
        return ""
    value = _normalize_path(path)
    source_root = _normalize_path(staging.get("path"))
    visible_root = _normalize_path(organizer.get("path"))
    if (
        not value
        or not source_root
        or not visible_root
        or _has_unsafe_path_segments(value)
        or _has_unsafe_path_segments(source_root)
        or _has_unsafe_path_segments(visible_root)
    ):
        return ""
    value_folded = value.casefold()
    source_folded = source_root.casefold()
    if value_folded == source_folded:
        return f"/{visible_root}"
    if value_folded.startswith(f"{source_folded}/"):
        return f"/{visible_root}/{value[len(source_root) + 1:]}"
    return ""


class ImportStagingService:
    """Retired façade kept for imports that have not yet been removed."""

    def __init__(self, config: Any) -> None:
        raw = getattr(config, "raw", {}) if config is not None else {}
        self.raw = raw if isinstance(raw, dict) else {}
        organizer = self.raw.get("organizer")
        self.organizer_config = organizer if isinstance(organizer, dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(
            self.organizer_config.get("enabled", False)
            and self.organizer_config.get("staging_enabled", True)
        )

    @property
    def directory_name(self) -> str:
        return normalize_staging_segment(self.organizer_config.get("staging_dir_name"))

    def build(
        self,
        *,
        job_id: int,
        route: str,
        category_key: str,
        category: dict[str, Any],
    ) -> dict[str, Any]:
        """Reject the removed mutable staging planner.

        ``job_id``, ``route`` and ``category`` are intentionally not consulted:
        accepting any of them would recreate the old path fallback.  Callers
        must invoke ``planning.planner.ImportPlanner.plan_import`` instead.
        """

        del job_id, route, category_key, category
        raise ValueError("legacy ImportStagingService is disabled; use the V1 execution planner")


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def _has_unsafe_path_segments(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    return any(part in {".", ".."} for part in text.split("/"))


__all__ = [
    "DEFAULT_STAGING_DIR_NAME",
    "ImportStagingService",
    "map_execution_path_to_organizer",
    "map_staging_path_to_openlist",
    "normalize_staging_segment",
    "rclone_copy_run_from_job",
    "rclone_staging_run_from_job",
    "staging_category_root",
    "staging_job_root",
    "staging_plan_from_job",
    "validated_staging_plan_from_job",
]
