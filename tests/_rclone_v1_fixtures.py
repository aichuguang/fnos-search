"""Canonical V1 rclone fixtures shared by safety-focused tests.

These helpers intentionally model the immutable execution-plan envelope.  No
legacy ``staging_plan`` or mutable runtime path fields are included.
"""

from __future__ import annotations

from typing import Any

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.openlist.locator_resolver import RcloneLocatorResolver
from fnos_media_import.planning.job_plan import (
    attach_plan_integrity,
    compute_operation_key,
    compute_persisted_plan_hash,
)


def rclone_v1_plan(
    job_id: int = 42,
    *,
    category: str = "tv",
    source_root: str | None = None,
    staging_root: str | None = None,
    final_root: str | None = None,
    expected_file_count: int = 1,
    expected_total_size: int = 1,
) -> dict[str, Any]:
    """Build a fully validated immutable V1 ``rclone_copy`` plan."""

    job_id = int(job_id)
    category = str(category or "tv").strip().lower()
    source_category = str(source_root or f"/quark/{category}").rstrip("/")
    staging_category = str(staging_root or f"/openlist/staging/{category}").rstrip("/")
    final_category = str(final_root or f"/openlist/final/{category}").rstrip("/")
    source_path = f"{source_category}/job-{job_id}"
    staging_path = f"{staging_category}/job-{job_id}"

    source_mount = {
        "kind": "openlist",
        "namespace_id": "openlist:ol:source",
        "path": source_path,
        "mount_id": "source",
        "mount_revision": 1,
        "connection_id": "ol",
        "connection_revision": 1,
        "storage_identity_id": "storage:source",
        "identity_root": "/",
    }
    staging_mount = {
        "kind": "openlist",
        "namespace_id": "openlist:ol:destination",
        "path": staging_path,
        "mount_id": "destination",
        "mount_revision": 1,
        "connection_id": "ol",
        "connection_revision": 1,
        "storage_identity_id": "storage:destination",
        "identity_root": "/",
    }
    final_mount = {
        **staging_mount,
        "path": final_category,
    }
    selection = SelectionSnapshot(
        provider_kind="quark",
        selected_items=(f"share-item-{job_id}",),
    )
    selection_payload = selection.to_dict()
    selection_hash = selection.selection_hash
    plan: dict[str, Any] = {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": job_id,
        "category": category,
        "source": {
            "provider_kind": "quark",
            "source_form": "share",
            "search_source_id": "pansou",
            "canonical_source_id": f"quark:share:{job_id}",
            "record_id": f"source-record-{job_id}",
            "record_revision": 1,
            "account_id": "account-quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection": selection_payload,
            "selection_hash": selection_hash,
        },
        "destination": {
            "id": "library-main",
            "provider_kind": "sixpan",
            "target_revision": 1,
            "binding_id": "binding-library-main",
            "binding_revision": 1,
            "account_id": "account-sixpan",
            "account_revision": 1,
        },
        "routing": {
            "assignment_id": "assignment-quark-tv",
            "assignment_revision": 1,
            "search_source_id": "pansou",
            "category_key": category,
            "destination_id": "library-main",
            "enabled": True,
        },
        "target_category": {"category_key": category, "public_label": category},
        "bindings": {
            "source_mount_id": "source",
            "source_mount_revision": 1,
            "source_mount_namespace_id": "openlist:ol:source",
            "source_connection_id": "ol",
            "source_connection_revision": 1,
            "source_mount_storage_identity_id": "storage:source",
            "source_mount_identity_root": "/",
            "destination_mount_id": "destination",
            "destination_mount_revision": 1,
            "destination_mount_namespace_id": "openlist:ol:destination",
            "destination_connection_id": "ol",
            "destination_connection_revision": 1,
            "destination_mount_storage_identity_id": "storage:destination",
            "destination_mount_identity_root": "/",
        },
        "transfer": {
            "strategy": "rclone_copy",
            "executor": "rclone",
            "idempotency_key": "",
            "delete_source_after_ack": True,
        },
        "paths": {
            "source_landing": source_mount,
            "destination_staging": staging_mount,
            "destination_final": final_mount,
        },
        "verification": {
            "expected_file_count": max(0, int(expected_file_count)),
            "expected_total_size": max(0, int(expected_total_size)),
            "minimum": ["file_count", "total_size"],
            "selection_hash": selection_hash,
        },
        "postprocess": {
            "organizer_enabled": False,
            "media_refresh_enabled": False,
        },
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)


def rclone_v1_job(
    plan: dict[str, Any] | None = None,
    *,
    job_id: int = 42,
    status: str = "transferring",
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a canonical plan as an import-job snapshot used by services."""

    plan = plan if isinstance(plan, dict) else rclone_v1_plan(job_id)
    job_id = int(plan.get("job_id") or job_id)
    payload = dict(raw_data or {})
    payload.setdefault(
        "execution_plan_ref",
        {"version": 1, "plan_hash": compute_persisted_plan_hash(plan)},
    )
    return {
        "id": job_id,
        "status": status,
        "plan_version": 1,
        "category": str(plan.get("category") or "tv"),
        "destination_id": str((plan.get("destination") or {}).get("id") or "library-main"),
        "source_provider_kind": "quark",
        "source_form": "share",
        "raw_data": payload,
        "_canonical_execution_plan": plan,
    }


def rclone_v1_manifest(job_id: int = 42, *names: str) -> dict[str, Any]:
    """Build a normalized source manifest for a V1 rclone job."""

    root = f"/quark/tv/job-{int(job_id)}"
    paths = [f"{root}/{name}" for name in (names or ("E01.mkv",))]
    return {
        "version": 1,
        "source_paths": paths,
        "expected_file_count": len(paths),
    }


def rclone_v1_resolver() -> RcloneLocatorResolver:
    """Return a deterministic managed locator for descriptor-oriented tests."""

    mounts = {
        "source": {
            "mount_id": "source",
            "revision": 1,
            "connection_id": "ol",
            "connection_revision": 1,
            "namespace_id": "openlist:ol:source",
            "enabled": True,
            "identity_status": "declared",
            "storage_identity_id": "storage:source",
            "identity_root": "/",
            "webdav_root": "/source-mount",
            "readable": True,
            "writable": True,
        },
        "destination": {
            "mount_id": "destination",
            "revision": 1,
            "connection_id": "ol",
            "connection_revision": 1,
            "namespace_id": "openlist:ol:destination",
            "enabled": True,
            "identity_status": "declared",
            "storage_identity_id": "storage:destination",
            "identity_root": "/",
            "webdav_root": "/destination-mount",
            "readable": True,
            "writable": True,
        },
    }
    resolver = RcloneLocatorResolver(
        {
            "remote_name": "OL",
            "config_path": "/config/rclone/rclone.conf",
            "local_target_root": "/storage-targets",
            "relay_root": "/data/relay",
        }
    )
    resolver.update_config(
        {
            "mount_revision_resolver": lambda mount_id, _revision: mounts.get(mount_id),
            "connection_revision_resolver": lambda _connection_id, _revision: {
                "connection_id": "ol",
                "revision": 1,
                "enabled": True,
                "credential_configured": True,
                "last_test_status": "ok",
            },
        }
    )
    return resolver


__all__ = [
    "rclone_v1_plan",
    "rclone_v1_job",
    "rclone_v1_manifest",
    "rclone_v1_resolver",
]
