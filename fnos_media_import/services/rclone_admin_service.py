from __future__ import annotations

import logging

from ..constants import RCLONE_JOB_REQUIRED_MESSAGE

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .transfer_status_projection import project_transfer_status, transfer_job_id


logger = logging.getLogger(__name__)


class RcloneQueries(Protocol):
    def status(self) -> dict[str, Any]: ...
    def get_logs(self, *, limit: int) -> list[Any] | dict[str, Any]: ...
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
    load_transfer_states: Callable[[list[int]], list[dict[str, Any]]] | None = None


class RcloneAdminQueryService:
    def __init__(self, dependencies: RcloneAdminQueryDependencies) -> None:
        self._deps = dependencies

    def status(self) -> dict[str, Any]:
        status = dict(self._deps.rclone.status())
        http_status = int(status.pop("_http_status", 200) or 200)
        if http_status < 400 and self._deps.load_transfer_states is not None:
            observations = status.get("transfers")
            observations = observations if isinstance(observations, list) else []
            job_ids = [job_id for item in observations if isinstance(item, dict)
                       if (job_id := transfer_job_id(item.get("job_id"))) is not None]
            try:
                records = self._deps.load_transfer_states(job_ids)
                status["transfers"] = project_transfer_status(observations, records)
                status["transfer_state_source"] = "import_jobs_and_transfer_executions"
            except Exception:  # State-read failure must not revive stale success from worker memory.
                logger.exception("Failed to read authoritative transfer task state")
                status["transfers"] = []
                return {
                    "success": False, "status": status, "_http_status": 503,
                    "error_code": "task_state_unavailable",
                    "message": "入库任务状态暂不可用，请稍后刷新；未更新为旧的传输状态。",
                }
        response = {"success": http_status < 400, "status": status, "_http_status": http_status}
        if http_status >= 400:
            error_code = str(status.get("error_code") or status.get("status") or "worker_unavailable")
            response.update(
                {
                    "error_code": error_code,
                    "message": str(status.get("last_error") or "rclone Worker 不可用"),
                }
            )
        return response

    def logs(self, limit: int) -> dict[str, Any]:
        result = self._deps.rclone.get_logs(limit=limit)
        if isinstance(result, dict):
            return result
        return {"success": True, "items": result}

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
        # This legacy endpoint has no immutable job descriptor.  A client-
        # supplied job id/path cannot substitute for the guarded job workflow.
        return {
            "success": False,
            "code": "RCLONE_JOB_REQUIRED",
            "message": RCLONE_JOB_REQUIRED_MESSAGE,
            "next_action": "open_jobs",
            "_http_status": 409,
        }

    def stop(self) -> dict[str, Any]:
        return self._rclone.stop()

    def check(self) -> dict[str, Any]:
        return self._rclone.check_environment()
