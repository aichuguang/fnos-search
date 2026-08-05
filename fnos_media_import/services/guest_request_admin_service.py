from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class GuestRequestQueries(Protocol):
    def count(self, status: str | None = None) -> int: ...
    def list(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]: ...
    def get(self, request_id: int) -> dict[str, Any] | None: ...
    def list_events(self, request_id: int) -> list[dict[str, Any]]: ...


class JobQueries(Protocol):
    def get(self, job_id: int) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class GuestRequestAdminDependencies:
    requests: GuestRequestQueries
    jobs: JobQueries
    sync_one: Callable[[dict[str, Any]], dict[str, Any]]
    sync_many: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class GuestRequestAdminService:
    """Read-side application service for administrator request management."""

    def __init__(self, dependencies: GuestRequestAdminDependencies) -> None:
        self._deps = dependencies

    def list_requests(self, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
        total = self._deps.requests.count(status)
        items = self._deps.requests.list(limit=limit, status=status, offset=offset)
        return {"total": total, "items": self._deps.sync_many(items)}

    def detail(self, request_id: int) -> tuple[dict[str, Any], int]:
        item = self._deps.requests.get(request_id)
        if not item:
            return {"success": False, "message": "访客提交不存在"}, 404
        item = self._deps.sync_one(item)
        job = self._deps.jobs.get(int(item["job_id"])) if item.get("job_id") else None
        return {
            "success": True,
            "request": item,
            "job": job,
            "events": self._deps.requests.list_events(request_id),
        }, 200
