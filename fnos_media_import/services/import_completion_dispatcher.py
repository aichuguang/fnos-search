from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    EVENT_INFO,
    EVENT_WARN,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_TRANSFER,
)
from ..planning.job_plan import plan_strategy
from .execution_plan_guard import v1_plan_or_review


class ImportCompletionDispatcher:
    """Dispatch completion only through the canonical durable V1 queue."""

    def __init__(
        self,
        *,
        database: Any,
        enqueue_transfer: Callable[[int], Any] | None = None,
        execution_repository: Any = None,
    ) -> None:
        self.database = database
        self.enqueue_transfer = enqueue_transfer
        self.execution_repository = execution_repository

    def dispatch(self, result: dict[str, Any], reason: str) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("success", True):
            return None
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        if not job:
            return None
        plan, rejection = v1_plan_or_review(
            self.database,
            job,
            context="入库完成分发时",
            execution_repository=self.execution_repository,
        )
        if rejection:
            result["execution_rejection"] = rejection
            return rejection

        status = str(job.get("status") or "").strip()
        if status in {JOB_WAITING_TRANSFER, JOB_WAITING_OPENLIST}:
            if callable(self.enqueue_transfer):
                return self._enqueue_transfer_and_record(result, job, reason)
            return self._queue_unavailable(job, reason, plan_strategy(plan))
        return None

    def _queue_unavailable(
        self,
        job: dict[str, Any],
        reason: str,
        strategy: str,
    ) -> dict[str, Any]:
        job_id = _job_id(job)
        message = "V1 持久化传输队列未配置，任务未启动并需要人工检查"
        rejection = {
            "success": False,
            "rejected": True,
            "manual_review": True,
            "job_id": job_id,
            "message": message,
        }
        if job_id:
            self.database.job_commands.add_event(
                job_id,
                EVENT_WARN,
                message,
                {"reason": reason, "strategy": strategy},
            )
        return rejection

    def _enqueue_transfer_and_record(
        self,
        result: dict[str, Any],
        job: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Queue the canonical V1 transfer exactly once.

        ``WorkerTaskDispatcher.import_transfer_execute`` is idempotent by
        job id.  Treat a callback returning ``None`` as an unavailable queue,
        rather than claiming that the transfer was queued successfully.
        External callers may return either the normal worker result mapping or
        a simple truthy value, so normalize both forms here.
        """

        job_id = _job_id(job)
        try:
            queued = self.enqueue_transfer(job_id) if callable(self.enqueue_transfer) else None
        except Exception as exc:  # noqa: BLE001 - persist a bounded failure
            queued = {"success": False, "message": str(exc)}
        if isinstance(queued, dict):
            start_result = dict(queued)
        else:
            start_result = {
                "success": queued is not None and queued is not False,
                "queued": queued is not None and queued is not False,
                "worker": queued,
            }
        start_result.setdefault("job_id", job_id)
        start_result.setdefault(
            "message",
            "V1 持久化传输任务已入队"
            if start_result.get("success")
            else "V1 transfer queue is unavailable",
        )
        result["transfer_queue"] = start_result
        if job_id:
            self.database.job_commands.add_event(
                job_id,
                EVENT_INFO if start_result.get("success") else EVENT_WARN,
                start_result["message"],
                {"reason": reason, "transfer": start_result},
            )
        return start_result


def _job_id(job: dict[str, Any]) -> int:
    try:
        value = int(job.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 999999999 else 0
