from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class RcloneQueries(Protocol):
    def status(self) -> dict[str, Any]: ...
    def get_logs(self, *, limit: int) -> list[Any]: ...
    def list_runs(self, *, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def list_events(self, *, run_id: int | None, limit: int) -> list[dict[str, Any]]: ...
    def list_file_events(self, **filters: Any) -> list[dict[str, Any]]: ...


class RcloneCounts(Protocol):
    def count_rclone_runs(self) -> int: ...
    def count_rclone_file_events(self, **filters: Any) -> int: ...


@dataclass(frozen=True)
class RcloneAdminQueryDependencies:
    rclone: RcloneQueries
    counts: RcloneCounts


class RcloneAdminQueryService:
    def __init__(self, dependencies: RcloneAdminQueryDependencies) -> None:
        self._deps = dependencies

    def status(self) -> dict[str, Any]:
        return {"success": True, "status": self._deps.rclone.status()}

    def logs(self, limit: int) -> dict[str, Any]:
        return {"success": True, "items": self._deps.rclone.get_logs(limit=limit)}

    def runs(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {"items": self._deps.rclone.list_runs(limit=limit, offset=offset), "total": self._deps.counts.count_rclone_runs()}

    def events(self, *, run_id: int | None, limit: int) -> dict[str, Any]:
        return {"success": True, "items": self._deps.rclone.list_events(run_id=run_id, limit=limit)}

    def file_events(self, *, run_id: int | None, job_id: int | None, status: str | None, category: str | None, limit: int, offset: int) -> dict[str, Any]:
        filters = {"run_id": run_id, "job_id": job_id, "status": status, "category": category}
        return {
            "items": self._deps.rclone.list_file_events(**filters, limit=limit, offset=offset),
            "total": self._deps.counts.count_rclone_file_events(**filters),
        }


class RcloneCommands(Protocol):
    def start(self, *, reason: str) -> dict[str, Any]: ...
    def stop(self) -> dict[str, Any]: ...
    def check_environment(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RcloneAdminCommandDependencies:
    rclone: RcloneCommands


class RcloneAdminCommandService:
    def __init__(self, dependencies: RcloneAdminCommandDependencies) -> None:
        self._rclone = dependencies.rclone

    def start(self, payload: dict[str, Any], *, default_reason: str = "admin_manual") -> dict[str, Any]:
        return self._rclone.start(reason=str(payload.get("reason") or default_reason))

    def stop(self) -> dict[str, Any]:
        return self._rclone.stop()

    def check(self) -> dict[str, Any]:
        return self._rclone.check_environment()
