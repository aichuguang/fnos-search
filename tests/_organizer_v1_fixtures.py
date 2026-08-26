from __future__ import annotations

from typing import Any

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.planning.job_plan import (
    attach_plan_integrity,
    compute_operation_key,
    compute_persisted_plan_hash,
)


def organizer_v1_plan(
    job_id: int,
    *,
    category: str = "movie",
    organizer_root: str | None = None,
    destination_final: str = "/媒体",
    destination_id: str = "native-main",
    connection_id: str = "main",
    connection_revision: int = 3,
    mount_id: str = "media",
    mount_revision: int = 2,
    storage_identity_id: str = "provider:quark:account:acc-quark",
    identity_root: str = "/",
    strategy: str = "native_save",
    source_landing: str | None = None,
    destination_staging: str | None = None,
    local_temp: str | None = None,
) -> dict[str, Any]:
    """Build the smallest fully valid canonical Organizer V1 plan for tests."""

    job_id = int(job_id)
    organizer_root = str(organizer_root or f"/入库/job-{job_id}")
    namespace = f"openlist:{connection_id}:{mount_id}"
    selection = SelectionSnapshot.from_payload(
        "quark",
        {"share_id": f"share-{job_id}"},
    ).to_dict()
    selection_hash = str(selection["selection_hash"])
    strategy = str(strategy or "native_save").strip().lower()
    destination_provider = {
        "native_save": "quark",
        "native_offline": "sixpan",
        "rclone_copy": "sixpan",
        "relay_upload_provider": "mobile",
    }.get(strategy, "quark")
    executor = {
        "native_save": "native",
        "native_offline": "native_offline",
        "rclone_copy": "rclone",
        "relay_upload_provider": "provider_relay",
    }.get(strategy, "native")
    destination_final_ref = {
        "kind": "openlist" if strategy == "rclone_copy" else "native",
        "namespace_id": (
            namespace
            if strategy == "rclone_copy"
            else f"native:{destination_provider}:{destination_id}"
        ),
        "path": str(destination_final),
    }
    paths: dict[str, Any] = {
        "destination_final": destination_final_ref,
        "organizer": {
            "kind": "openlist",
            "namespace_id": namespace,
            "path": organizer_root,
            "mount_id": mount_id,
            "mount_revision": int(mount_revision),
            "connection_id": connection_id,
            "connection_revision": int(connection_revision),
            "storage_identity_id": storage_identity_id,
            "identity_root": identity_root,
        },
    }
    if strategy in {"rclone_copy", "relay_upload_provider"}:
        paths["source_landing"] = {
            "kind": "openlist",
            "namespace_id": "openlist:source:landing",
            "path": str(source_landing or f"/来源/job-{job_id}"),
        }
    if strategy == "rclone_copy":
        paths["destination_staging"] = {
            "kind": "openlist",
            "namespace_id": namespace,
            "path": str(destination_staging or organizer_root),
        }
    if strategy == "relay_upload_provider":
        paths["local_temp"] = {
            "kind": "relay",
            "namespace_id": "local:relay",
            "path": str(local_temp or f"/relay/job-{job_id}"),
        }
    plan: dict[str, Any] = {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": job_id,
        "category": str(category),
        "source": {
            "provider_kind": "quark",
            "source_form": "share",
            "canonical_source_id": f"quark:share:{job_id}",
            "search_source_id": "pansou",
            "record_id": f"record-{job_id}",
            "record_revision": 1,
            "account_id": "acc-quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection": selection,
            "selection_hash": selection_hash,
        },
        "destination": {
            "id": destination_id,
            "provider_kind": destination_provider,
            "target_revision": 1,
            "binding_id": f"binding-{job_id}",
            "binding_revision": 1,
        },
        "transfer": {
            "strategy": strategy,
            "executor": executor,
            "idempotency_key": "",
        },
        "bindings": {
            "organizer_mount_id": mount_id,
            "organizer_mount_revision": int(mount_revision),
            "organizer_mount_namespace_id": namespace,
            "organizer_mount_storage_identity_id": storage_identity_id,
            "organizer_mount_identity_root": identity_root,
            "organizer_connection_id": connection_id,
            "organizer_connection_revision": int(connection_revision),
        },
        "paths": paths,
        "target_category": {},
        "routing": {
            "assignment_id": f"assignment-{job_id}",
            "assignment_revision": 1,
            "search_source_id": "pansou",
            "category_key": str(category),
            "destination_id": destination_id,
            "enabled": True,
        },
        "verification": {
            "minimum": ["file_count", "total_size"],
            "expected_file_count": 1,
            "expected_total_size": 1,
            "selection_hash": selection_hash,
        },
        "postprocess": {
            "organizer_enabled": True,
            "media_refresh_enabled": False,
        },
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)


def organizer_v1_job(
    plan: dict[str, Any],
    *,
    status: str = "waiting_organizer",
    title: str = "测试资源",
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_id = int(plan["job_id"])
    payload = dict(raw_data or {})
    payload["execution_plan_ref"] = {
        "version": 1,
        "plan_hash": compute_persisted_plan_hash(plan),
    }
    return {
        "id": job_id,
        "title": title,
        "category": str(plan["category"]),
        "source_provider_kind": str(plan["source"]["provider_kind"]),
        "source_form": str(plan["source"]["source_form"]),
        "destination_id": str(plan["destination"]["id"]),
        "plan_version": 1,
        "status": status,
        "_canonical_execution_plan": plan,
        "raw_data": payload,
    }


def attach_organizer_v1_task(
    task: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
    plan_hash = compute_persisted_plan_hash(plan)
    task.update(
        {
            "job_id": int(plan["job_id"]),
            "category": str(plan["category"]),
            **(
                {"revision": max(1, int(task.get("revision") or 1))}
                if task.get("id")
                else {}
            ),
            "openlist_root_path": str(plan["paths"]["organizer"]["path"]),
            # The immutable plan is owned by import_execution_plans.  Keep the
            # old-looking field only as a non-executable test payload when a
            # fixture explicitly needs to inspect it; all Organizer guards use
            # this hash reference and the repository snapshot instead.
            "raw_data": {
                **raw_data,
                "execution_plan_ref": {
                    "schema_version": 1,
                    "job_id": int(plan["job_id"]),
                    "plan_hash": plan_hash,
                    "strategy": str(plan.get("transfer", {}).get("strategy") or ""),
                },
            },
        }
    )
    return task


class InMemoryExecutionPlanRepository:
    """Small immutable-plan seam for Organizer unit-test doubles.

    Production Organizer instances receive ``TransferExecutionRepository``
    from app wiring.  Test databases generally expose only task/job methods,
    so this object supplies the same ``plan(job_id)`` read without introducing
    a second mutable execution path.
    """

    def __init__(self, plans: dict[int, dict[str, Any]] | None = None) -> None:
        self._plans = {int(key): value for key, value in (plans or {}).items()}

    def plan(self, job_id: int) -> dict[str, Any] | None:
        value = self._plans.get(int(job_id))
        return value if isinstance(value, dict) else None


def bind_organizer_v1_execution_repository(
    service: Any,
    *plans: dict[str, Any],
) -> InMemoryExecutionPlanRepository:
    """Bind one or more canonical plans to a lightweight Organizer service."""

    collected: dict[int, dict[str, Any]] = {}
    for plan in plans:
        if isinstance(plan, dict) and int(plan.get("job_id") or 0) > 0:
            collected[int(plan["job_id"])] = plan
    repository = InMemoryExecutionPlanRepository(collected)
    setter = getattr(service, "set_execution_repository", None)
    if callable(setter):
        setter(repository)
    else:
        service._execution_repository = repository
    return repository


def organizer_v1_task_and_job(
    task: dict[str, Any],
    *,
    job_id: int,
    category: str | None = None,
    organizer_root: str | None = None,
    destination_final: str = "/媒体",
    job_status: str = "waiting_organizer",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = organizer_v1_plan(
        job_id,
        category=category or str(task.get("category") or "movie"),
        organizer_root=organizer_root or str(task.get("openlist_root_path") or "") or None,
        destination_final=destination_final,
    )
    return attach_organizer_v1_task(task, plan), organizer_v1_job(plan, status=job_status), plan


class ScopedOpenListProxy:
    """Distinct task client that delegates to an existing Organizer test fake."""

    def __init__(self, target: Any) -> None:
        self._target = target

    @property
    def configured(self) -> bool:
        return bool(getattr(self._target, "configured", True))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def close(self) -> None:
        # Test doubles are usually shared assertions; do not close/mutate them.
        return None


def bind_task_openlist_resolver(
    service: Any,
    client: Any | None = None,
    *plans: dict[str, Any],
) -> None:
    fallback = type("NoopOpenList", (), {"configured": True})()
    if not plans:
        # Most fakes expose the linked job through ``db.get_job``; harvest its
        # canonical envelope solely to populate the immutable-plan test repo.
        task = getattr(getattr(service, "db", None), "task", None)
        if isinstance(task, dict):
            raw = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
            candidate = raw.get("execution_plan")
            if isinstance(candidate, dict):
                plans = (candidate,)
        if not plans:
            job_id = int((task or {}).get("job_id") or 0) if isinstance(task, dict) else 0
            getter = getattr(getattr(service, "db", None), "get_job", None)
            if job_id > 0 and callable(getter):
                try:
                    job = getter(job_id)
                except Exception:  # noqa: BLE001 - fixture discovery only
                    job = None
                candidate = (job or {}).get("_canonical_execution_plan") if isinstance(job, dict) else None
                if isinstance(candidate, dict):
                    plans = (candidate,)
    if plans:
        bind_organizer_v1_execution_repository(service, *plans)
    def resolve(_job: dict[str, Any], _plan: dict[str, Any]) -> ScopedOpenListProxy:
        # When no explicit client is supplied, read the diagnostics fake at
        # resolution time.  Tests can replace ``service.openlist`` between the
        # scan and apply phases without rebuilding the V1 plan repository.
        target = client if client is not None else getattr(service, "openlist", None)
        return ScopedOpenListProxy(target if target is not None else fallback)

    service.set_openlist_client_resolver(resolve)


__all__ = [
    "attach_organizer_v1_task",
    "bind_organizer_v1_execution_repository",
    "bind_task_openlist_resolver",
    "InMemoryExecutionPlanRepository",
    "organizer_v1_job",
    "organizer_v1_plan",
    "organizer_v1_task_and_job",
    "ScopedOpenListProxy",
]
