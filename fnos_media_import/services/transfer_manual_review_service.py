from __future__ import annotations

from typing import Any, Callable

from ..transfer.execution_repository import ExecutionConflictError, TransferExecutionRepository


_RESUBMISSION_WARNING = "此操作会重新提交转存；如果人工核对有误，可能产生重复文件或重复任务。"
_QUEUE_UNCONFIRMED_MESSAGE = "人工确认已保存，但未确认任务仍在可执行的 Worker 队列中；请刷新任务状态并检查 Worker，勿重复确认"
_NON_RUNNABLE_JOB_STATUSES = frozenset({
    "cancelled", "canceled", "rejected", "done", "success", "completed", "failed", "unsupported", "review", "manual_review",
})


class TransferManualReviewService:
    """A separate, explicit recovery command; ordinary retry remains fail-closed."""

    def __init__(
        self,
        *,
        database: Any,
        executions: TransferExecutionRepository,
        validate_plan: Callable[[dict[str, Any], dict[str, Any]], None],
        enqueue_transfer: Callable[[int], Any],
    ) -> None:
        self.database = database
        self.executions = executions
        self.validate_plan = validate_plan
        self.enqueue_transfer = enqueue_transfer

    def confirm_not_submitted(
        self,
        job_id: int,
        payload: dict[str, Any],
        *,
        admin: str,
    ) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict) or payload.get("action") != "confirm_not_submitted":
            return self._failure("请选择受支持的人工复核操作", 400)
        if payload.get("confirmed") is not True:
            return self._failure("请先核对来源暂存目录、目标目录和网盘任务列表，明确确认没有本次文件及进行中任务", 400)
        version = payload.get("expected_row_version")
        if type(version) is not int or version <= 0:
            return self._failure("执行版本必须是正整数，请刷新任务详情", 400)
        note = payload.get("note")
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > 1000:
            return self._failure("请填写 1 至 1000 字的人工核对说明", 400)
        if not isinstance(admin, str) or not admin.strip():
            return self._failure("需要已登录的管理员身份", 403)
        job = self.database.job_queries.get(job_id)
        if not isinstance(job, dict):
            return self._failure("任务不存在", 404)
        if str(job.get("status") or "").strip().lower() not in {"review", "manual_review"}:
            return self._failure("正式任务不在人工复核状态，不能重新提交", 409)
        try:
            plan = self.executions.plan(job_id)
            if not plan:
                return self._failure("任务缺少 V1 不可变执行计划，不能重新提交", 409)
            self.validate_plan(plan, job)
        except Exception as exc:  # noqa: BLE001 - no state changes before validation succeeds
            return self._failure(f"冻结执行计划校验未通过：{exc}", 409)
        try:
            execution = self.executions.confirm_not_submitted(
                job_id,
                expected_row_version=version,
                admin=admin,
                note=note,
            )
        except ExecutionConflictError as exc:
            return self._failure(str(exc), 409)
        except ValueError as exc:
            return self._failure(str(exc), 400)
        result = {
            "job_id": int(job_id),
            "recovered": True,
            "phase": execution.phase,
            "row_version": execution.row_version,
            "action": "confirm_not_submitted",
            "warning": _RESUBMISSION_WARNING,
        }
        try:
            queued = self.enqueue_transfer(job_id)
            if not self._queue_accepted(job_id, queued):
                raise RuntimeError("Worker 队列未确认接收")
        except Exception:  # noqa: BLE001 - the acknowledgement is already committed
            return {
                **result,
                "success": False,
                "queued": False,
                "message": _QUEUE_UNCONFIRMED_MESSAGE,
            }, 503
        return {
            **result,
            "success": True,
            "queued": True,
            "message": "人工确认已保存，原任务已重新入队，将重新提交转存",
        }, 200

    def _queue_accepted(self, job_id: int, queued: Any) -> bool:
        if isinstance(queued, dict):
            if queued.get("success") is not True or queued.get("queued") is not True:
                return False
            if "worker_task_id" in queued:
                task_id = queued["worker_task_id"]
                if type(task_id) is not int or task_id <= 0:
                    return False
                getter = getattr(getattr(self.database, "worker_tasks", None), "get", None)
                if not callable(getter):
                    return False
                task = getter(task_id)
                if not isinstance(task, dict) or task.get("id") != task_id:
                    return False
                payload = task.get("payload")
                if (
                    str(task.get("status") or "") not in {"pending", "running"}
                    or task.get("task_type") != "import_transfer_execute"
                    or task.get("idempotency_key") != f"import-transfer:{int(job_id)}"
                    or not isinstance(payload, dict)
                    or type(payload.get("job_id")) is not int
                    or payload["job_id"] != job_id
                ):
                    return False
        elif queued is not True:
            return False
        # A cancellation can commit after the recovery transaction but before
        # enqueue returns. Never describe that terminal job as queued to retry.
        current_job = self.database.job_queries.get(job_id)
        return bool(
            isinstance(current_job, dict)
            and str(current_job.get("status") or "").strip().lower()
            not in _NON_RUNNABLE_JOB_STATUSES
        )

    @staticmethod
    def _failure(message: str, status: int) -> tuple[dict[str, Any], int]:
        return {"success": False, "message": message}, status
