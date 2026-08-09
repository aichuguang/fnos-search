from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    EVENT_INFO,
    EVENT_WARN,
    JOB_DONE,
    JOB_SUBMITTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
    JOB_WAITING_TRANSFER,
)
from ..planning.job_plan import plan_strategy, validated_v1_execution_plan_from_job
from .execution_plan_guard import (
    plan_requires_organizer,
    v1_plan_or_review,
)


class ImportCompletionDispatcher:
    """Dispatch completion only through the canonical durable V1 queue."""

    def __init__(
        self,
        *,
        database: Any,
        rclone_service: Any,
        category: Callable[[str], dict[str, Any]],
        enqueue_organizer: Callable[[dict[str, Any], str], dict[str, Any] | None],
        enqueue_transfer: Callable[[int], Any] | None = None,
        execution_repository: Any = None,
    ) -> None:
        self.database = database
        self.rclone_service = rclone_service
        self.category = category
        self.enqueue_organizer = enqueue_organizer
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

        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        cloud139_saved = (
            status == JOB_SUBMITTED
            and str(job.get("source_type") or "").strip().lower() == "cloud139"
            and isinstance(raw_data.get("save"), dict)
        )
        if (
            plan_requires_organizer(plan)
            and (status in {JOB_DONE, JOB_WAITING_OPENLIST, JOB_WAITING_ORGANIZER} or cloud139_saved)
        ):
            return self.enqueue_organizer(result, reason)
        return None

    def start_rclone_for_job(
        self,
        job: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        if not job:
            return None
        job_id = _job_id(job)
        plan, rejection = v1_plan_or_review(
            self.database,
            job,
            context="启动 rclone 时",
            execution_repository=self.execution_repository,
        )
        if rejection:
            return rejection
        strategy = plan_strategy(plan)
        if strategy != "rclone_copy":
            result = {
                "success": False,
                "skipped": True,
                "message": f"V1 任务策略 {strategy or 'missing'} 不应由 rclone 完成分发入口启动",
            }
            if job_id:
                self.database.add_event(job_id, EVENT_WARN, result["message"], {"reason": reason})
            return result

        if callable(self.enqueue_transfer):
            return self._enqueue_transfer_and_record({}, job, reason)
        return self._queue_unavailable(job, reason, strategy)

    def uses_rclone_staging(self, job: dict[str, Any]) -> bool:
        """Return whether a *validated* V1 plan owns rclone staging.

        This helper is used by completion/recovery code as a dispatch hint.  A
        permissive envelope reader here would let a malformed or partially
        migrated job re-enter the rclone path before the durable transfer
        worker has a chance to reject it.  Treat every validation failure as
        ``False``; callers that need the actionable rejection reason use
        :meth:`start_rclone_for_job`, which records manual review.
        """
        try:
            if self.execution_repository is not None:
                from .execution_plan_guard import require_v1_execution_plan

                plan = require_v1_execution_plan(
                    job,
                    execution_repository=self.execution_repository,
                )
            else:
                plan = validated_v1_execution_plan_from_job(job)
        except Exception:  # noqa: BLE001 - status/query helper must fail closed
            return False
        return plan_strategy(plan) == "rclone_copy"

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
            self.database.add_event(
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
            self.database.add_event(
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
