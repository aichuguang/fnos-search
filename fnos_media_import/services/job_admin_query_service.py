from __future__ import annotations

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
        return {"success": True, "job": self._deps.decorate(job)}, 200
