from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class JobQueries(Protocol):
    def count(self, **filters: Any) -> int: ...
    def list(self, *, limit: int, offset: int, **filters: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class JobAdminQueryDependencies:
    jobs: JobQueries
    load_detail: Callable[[int], dict[str, Any] | None]
    reconcile: Callable[[dict[str, Any], str], dict[str, Any]]
    decorate: Callable[[dict[str, Any]], dict[str, Any]]
    load_transfer_execution: Callable[[int], Any] | None = None


class JobAdminQueryService:
    """Read-side orchestration for administrator import-job views."""

    def __init__(self, dependencies: JobAdminQueryDependencies) -> None:
        self._deps = dependencies

    def list_jobs(self, *, limit: int, offset: int, reconcile_reason: str = "admin_jobs_list", **filters: Any) -> dict[str, Any]:
        normalized = {key: value for key, value in filters.items() if value}
        total = self._deps.jobs.count(**normalized)
        items = self._deps.jobs.list(limit=limit, offset=offset, **normalized)
        return {
            "total": total,
            "items": [
                self._deps.decorate(self._deps.reconcile(item, reconcile_reason)) for item in items
            ],
        }

    def detail(self, job_id: int, *, reconcile_reason: str = "admin_job_detail") -> tuple[dict[str, Any], int]:
        job = self._deps.load_detail(job_id)
        if not job:
            return {"success": False, "message": "任务不存在"}, 404
        reconciled = self._deps.reconcile(job, reconcile_reason)
        if str(reconciled.get("status") or "") != str(job.get("status") or ""):
            job = self._deps.load_detail(job_id) or reconciled
        decorated = self._deps.decorate(job)
        if self._deps.load_transfer_execution is not None:
            execution = self._deps.load_transfer_execution(job_id)
            decorated["transfer_execution"] = serialize_transfer_execution(execution)
        return {"success": True, "job": decorated}, 200


def serialize_transfer_execution(execution: Any) -> dict[str, Any] | None:
    """Serialize durable execution state without exposing frozen locators."""

    if execution is None:
        return None
    cleanup = _field(execution, "cleanup", {})
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    receipt = _field(execution, "receipt", None)
    receipt_summary = _receipt_summary(receipt if isinstance(receipt, dict) else None)
    return {
        "phase": str(_field(execution, "phase", "") or ""),
        "strategy": str(_field(execution, "strategy", "") or ""),
        "attempts": int(_field(execution, "attempts", 0) or 0),
        "resume_at": _field(execution, "resume_at", None),
        "staging_verified": bool(_field(execution, "staging_verified", False)),
        "final_verified": bool(_field(execution, "final_verified", False)),
        "app_acked": bool(_field(execution, "app_acked", False)),
        "organizer_task_id": _field(execution, "organizer_task_id", None),
        "cleanup": {
            key: cleanup[key]
            for key in ("status", "success", "completed", "pending", "message")
            if key in cleanup
        },
        "last_error": {
            "code": str(_field(execution, "last_error_code", "") or ""),
            "message": str(_field(execution, "last_error_message", "") or ""),
        },
        "receipt": receipt_summary,
    }


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _receipt_summary(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not receipt:
        return None
    metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    operation_key = str(
        metadata.get("operation_key")
        or metadata.get("idempotency_key")
        or receipt.get("operation_key")
        or ""
    )
    return {
        "provider_kind": str(receipt.get("provider_kind") or ""),
        "status": str(receipt.get("status") or ""),
        "operation_id_hint": _identifier_hint(receipt.get("operation_id")),
        "run_id_hint": _identifier_hint(metadata.get("run_id")),
        "operation_key_hash": (
            hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:16]
            if operation_key
            else ""
        ),
    }


def _identifier_hint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}…{text[-3:]}"
