from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class JobCommands(Protocol):
    def get_job(self, job_id: int) -> dict[str, Any] | None: ...
    def update_job(self, job_id: int, **updates: Any) -> None: ...
    def update_job_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        **updates: Any,
    ) -> bool: ...
    def add_event(self, job_id: int, level: str, message: str, raw_data: Any = None) -> int: ...


class CancelledTaskCleaner(Protocol):
    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict[str, Any]: ...
    def cleanup_cancelled_task(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class JobCancellationDependencies:
    jobs: JobCommands
    cleaner: CancelledTaskCleaner
    merge_raw_data: Callable[[Any, dict[str, Any]], dict[str, Any]]
    payload_bool: Callable[[dict[str, Any], str, bool], bool]
    cancelled_status: str
    worker_tasks: Any | None = None
    organizer: Any | None = None
    execution_repository: Any | None = None
    enqueue_transfer: Callable[..., Any] | None = None


class JobCancellationService:
    """Coordinates cancellation state, optional file cleanup and audit events."""

    _COMPLETED_STATUSES = frozenset({"done", "success"})
    _PROVIDER_SUBMITTING_STATUS = "provider_submitting"
    _MAX_CAS_ATTEMPTS = 4

    def __init__(self, dependencies: JobCancellationDependencies) -> None:
        self._deps = dependencies

    def cancel(
        self,
        job: dict[str, Any],
        *,
        reason: str,
        payload: dict[str, Any],
        request_item: dict[str, Any] | None,
        cleanup_default: bool,
        stop_running_default: bool,
        admin_username: str | None,
    ) -> dict[str, Any]:
        job_id = int(job["id"])
        cleanup_requested = self._deps.payload_bool(payload, "cleanup", cleanup_default)
        delete_source = self._deps.payload_bool(payload, "delete_source", True)
        delete_temp = self._deps.payload_bool(payload, "delete_temp", True)
        delete_target_partial = self._deps.payload_bool(payload, "delete_target_partial", True)
        include_title_matches = self._deps.payload_bool(payload, "include_title_matches", True)
        stop_running = self._deps.payload_bool(payload, "stop_running", stop_running_default)
        transfer_preflight = self._preflight_transfer_cancellation(job_id)
        if transfer_preflight is not None:
            if transfer_preflight.get("resume_required"):
                wakeup = self._enqueue_transfer_cancellation(job_id, reactivate=True)
                if isinstance(wakeup, dict) and wakeup.get("success") is False:
                    transfer_preflight = {
                        **transfer_preflight,
                        "message": (
                            str(transfer_preflight.get("message") or "任务已进入不可安全取消阶段")
                            + "；恢复 V1 传输任务入队失败，请立即人工检查"
                        ),
                        "retryable": True,
                    }
            return self._skipped_result(
                job=self._deps.jobs.get_job(job_id) or job,
                message=str(transfer_preflight.get("message") or "任务已进入不可安全取消阶段"),
                state_conflict=True,
                retryable=bool(transfer_preflight.get("retryable")),
            )
        claim = self._claim_cancellation(
            job_id,
            supplied_job=job,
            reason=reason,
            request_item=request_item,
            cleanup_requested=cleanup_requested,
            delete_source=delete_source,
            delete_temp=delete_temp,
            delete_target_partial=delete_target_partial,
            include_title_matches=include_title_matches,
            stop_running=stop_running,
            admin_username=admin_username,
        )
        if not claim["cancelled"]:
            return self._skipped_result(
                job=claim.get("job"),
                message=str(claim.get("message") or "任务状态已变化，取消未执行"),
                state_conflict=bool(claim.get("state_conflict")),
                retryable=bool(claim.get("retryable")),
            )

        cancelled_job = claim["job"]
        cancel_raw = claim["cancel_raw"]
        already_cancelled = bool(claim["already_cancelled"])
        durable_transfer_owned = self._is_durable_transfer_owned(job_id, cancelled_job)
        self._deps.jobs.add_event(
            job_id,
            "warn",
            f"重复取消任务：{reason}" if already_cancelled else f"任务已取消：{reason}",
            cancel_raw,
        )
        defer_transfer_cleanup = (
            cleanup_requested
            and durable_transfer_owned
            and self._should_defer_transfer_cleanup(job_id)
        )

        propagation = self._cancel_related_execution(
            cancelled_job,
            reason=reason,
        )
        transfer_wakeup = self._enqueue_transfer_cancellation(job_id, reactivate=True)
        if transfer_wakeup is not None:
            propagation["items"].append({"type": "cancel_transfer_execution", **transfer_wakeup})
            if transfer_wakeup.get("success") is False:
                propagation["warnings"].append(
                    str(transfer_wakeup.get("message") or "V1 传输取消任务入队失败")
                )

        if defer_transfer_cleanup:
            cleanup = {
                "success": not any(
                    str(item or "").strip() for item in propagation["warnings"]
                ),
                "deferred": True,
                "message": "V1 外部操作取消确认后再执行任务级清理",
                "items": [*propagation["items"]],
                "warnings": [*propagation["warnings"]],
                "errors": [],
            }
            if transfer_wakeup is None:
                cleanup["warnings"].append(
                    "V1 传输队列未配置；为安全起见未执行任何破坏性清理"
                )
                cleanup["success"] = False
        elif cleanup_requested:
            cleanup = self._deps.cleaner.cleanup_cancelled_task(
                job=cancelled_job,
                request_item=request_item,
                delete_source=delete_source,
                delete_temp=delete_temp,
                delete_target_partial=delete_target_partial,
                stop_running=stop_running,
                include_title_matches=include_title_matches,
            )
            cleanup.setdefault("items", []).extend(propagation["items"])
            cleanup.setdefault("warnings", []).extend(propagation["warnings"])
        elif durable_transfer_owned:
            cleanup = {
                "success": not any(
                    str(item or "").strip() for item in propagation["warnings"]
                ),
                "deferred": True,
                "message": "已取消任务；由 V1 Worker 确认外部操作终止，未请求文件清理",
                "items": [*propagation["items"]],
                "warnings": [*propagation["warnings"]],
                "errors": [],
            }
            if transfer_wakeup is None:
                cleanup["warnings"].append(
                    "V1 传输队列未配置；未调用旧版全局取消接口，请立即人工检查"
                )
                cleanup["success"] = False
        else:
            cancel_execution = self._cancel_execution(job_id, stop_running=stop_running)
            cleanup = {
                "success": bool(cancel_execution.get("success", True)),
                "message": "已取消并撤销任务级执行，未执行文件清理",
                "items": [{"type": "cancel_execution", **cancel_execution}, *propagation["items"]],
                "warnings": (
                    []
                    if cancel_execution.get("success", True)
                    else [cancel_execution.get("message") or "任务级执行取消失败"]
                )
                + propagation["warnings"],
                "errors": [],
            }

        self._deps.jobs.add_event(
            job_id,
            "info" if cleanup.get("success", True) else "error",
            cleanup.get("message") or "取消清理完成",
            {"cleanup": cleanup},
        )
        return {
            "job": self._deps.jobs.get_job(job_id),
            "cleanup": cleanup,
            "message": cleanup.get("message") or "已取消任务",
            "cancelled": True,
            "skipped": False,
            "state_conflict": False,
            "already_cancelled": already_cancelled,
        }

    def _claim_cancellation(
        self,
        job_id: int,
        *,
        supplied_job: dict[str, Any],
        reason: str,
        request_item: dict[str, Any] | None,
        cleanup_requested: bool,
        delete_source: bool,
        delete_temp: bool,
        delete_target_partial: bool,
        include_title_matches: bool,
        stop_running: bool,
        admin_username: str | None,
    ) -> dict[str, Any]:
        supplied_status = _normalized_status(supplied_job.get("status"))
        current = self._deps.jobs.get_job(job_id)
        state_conflict = current is None or _normalized_status((current or {}).get("status")) != supplied_status
        updater = getattr(self._deps.jobs, "update_job_if_status", None)
        if not callable(updater):
            return {
                "cancelled": False,
                "job": current,
                "state_conflict": True,
                "message": "任务存储不支持原子取消，已停止取消传播与清理",
            }

        for _attempt in range(self._MAX_CAS_ATTEMPTS):
            if not current:
                return {
                    "cancelled": False,
                    "job": None,
                    "state_conflict": True,
                    "message": "任务不存在或已被删除，取消未执行",
                }
            current_status = _normalized_status(current.get("status"))
            if current_status == self._PROVIDER_SUBMITTING_STATUS:
                return {
                    "cancelled": False,
                    "job": current,
                    "state_conflict": True,
                    "retryable": True,
                    "message": (
                        "任务正在调用外部网盘接口，当前无法确认外部任务编号并安全补偿；"
                        "本次未标记取消、未传播、未清理，请稍后在提交结果落库后重试"
                    ),
                }
            if current_status == "created" and not _has_safe_not_started_provider_fence(current):
                return {
                    "cancelled": False,
                    "job": current,
                    "state_conflict": True,
                    "retryable": True,
                    "message": (
                        "历史已创建任务缺少可证明外部网盘接口尚未调用的提交栅栏；"
                        "本次未标记取消、未传播、未清理，请先人工核对外部任务"
                    ),
                }
            if current_status in self._COMPLETED_STATUSES:
                return {
                    "cancelled": False,
                    "job": current,
                    "state_conflict": state_conflict,
                    "message": "任务已完成，取消未执行，也未传播或清理关联资源",
                }
            if not current_status:
                return {
                    "cancelled": False,
                    "job": current,
                    "state_conflict": True,
                    "message": "任务状态为空，取消未执行",
                }

            already_cancelled = current_status == _normalized_status(self._deps.cancelled_status)
            raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
            previous_cancel = raw_data.get("cancel") if isinstance(raw_data.get("cancel"), dict) else {}
            generation = _safe_generation(previous_cancel.get("generation"))
            if not already_cancelled:
                generation += 1
            generation = max(1, generation)
            cancel_raw = {
                "active": True,
                "generation": generation,
                "cancelled_by": admin_username,
                "reason": reason,
                "request_id": (request_item or {}).get("id"),
                "cleanup_requested": cleanup_requested,
                "delete_source_requested": delete_source,
                "delete_temp_requested": delete_temp,
                "delete_target_partial_requested": delete_target_partial,
                "include_title_matches_requested": include_title_matches,
                "stop_running_requested": stop_running,
            }
            updates: dict[str, Any] = {
                "raw_data": self._deps.merge_raw_data(current.get("raw_data"), {"cancel": cancel_raw}),
            }
            if not already_cancelled:
                updates.update(status=self._deps.cancelled_status, error_message=reason)
            if updater(job_id, {current_status}, **updates):
                latest = self._deps.jobs.get_job(job_id)
                if not latest or _normalized_status(latest.get("status")) != _normalized_status(
                    self._deps.cancelled_status
                ):
                    latest_status = _normalized_status((latest or {}).get("status"))
                    return {
                        "cancelled": False,
                        "job": latest,
                        "state_conflict": True,
                        "message": (
                            "任务在取消确认后已完成，已停止取消传播与清理"
                            if latest_status in self._COMPLETED_STATUSES
                            else "任务在取消确认后状态再次变化，已停止取消传播与清理"
                        ),
                    }
                return {
                    "cancelled": True,
                    "job": latest,
                    "cancel_raw": cancel_raw,
                    "already_cancelled": already_cancelled,
                    "cancelled_from_status": current_status,
                    "state_conflict": False,
                }
            state_conflict = True
            current = self._deps.jobs.get_job(job_id)

        return {
            "cancelled": False,
            "job": current,
            "state_conflict": True,
            "message": "任务状态持续变化，取消未执行，请刷新后重试",
        }

    @staticmethod
    def _skipped_result(
        *,
        job: dict[str, Any] | None,
        message: str,
        state_conflict: bool,
        retryable: bool = False,
    ) -> dict[str, Any]:
        return {
            "job": job,
            "cleanup": {
                "success": True,
                "skipped": True,
                "state_conflict": state_conflict,
                "retryable": retryable,
                "message": message,
                "items": [],
                "warnings": [],
                "errors": [],
            },
            "message": message,
            "cancelled": False,
            "skipped": True,
            "state_conflict": state_conflict,
            "retryable": retryable,
            "already_cancelled": False,
        }

    def _cancel_related_execution(
        self,
        job: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        job_id = int(job.get("id") or 0)
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        organizer_task_ids: list[int] = []

        cancel_organizer = getattr(self._deps.organizer, "cancel_job_tasks", None)
        if callable(cancel_organizer):
            try:
                result = cancel_organizer(job_id, reason=f"关联入库任务已取消：{reason}")
                organizer_task_ids = [
                    int(value)
                    for value in result.get("task_ids") or []
                    if _safe_generation(value) > 0
                ]
                items.append({"type": "cancel_organizer", **result})
                if result.get("success") is False:
                    warnings.append(result.get("message") or "Organizer 任务取消不完整")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"取消 Organizer 任务异常：{exc}")

        cancel_workers = getattr(self._deps.worker_tasks, "cancel_related", None)
        if callable(cancel_workers):
            try:
                result = cancel_workers(
                    job_id=job_id,
                    organizer_task_ids=organizer_task_ids,
                    reason=f"关联入库任务 #{job_id} 已取消：{reason}",
                )
                items.append({"type": "cancel_worker_tasks", **result})
                if result.get("success") is False:
                    warnings.append(result.get("message") or "Worker 任务取消失败")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"取消 Worker 任务异常：{exc}")

        return {"items": items, "warnings": warnings}

    def _cancel_execution(self, job_id: int, *, stop_running: bool) -> dict[str, Any]:
        cancel_job = getattr(self._deps.cleaner, "cancel_job", None)
        if not callable(cancel_job):
            return {
                "success": True,
                "skipped": True,
                "job_id": job_id,
                "message": "当前清理器未提供任务级执行取消接口",
            }
        try:
            return cancel_job(job_id, stop_running=bool(stop_running))
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "job_id": job_id,
                "message": f"取消任务级 rclone 执行异常：{exc}",
            }

    def _is_durable_transfer_owned(self, job_id: int, job: dict[str, Any]) -> bool:
        """Whether cancellation must be handled only by the V1 Worker.

        A V1 plan/row owns every provider-side receipt and its frozen account
        revision.  Calling legacy/global cancellation APIs from the command
        path would bypass those fences and can target the wrong operation.
        Repository read failures therefore fail closed.
        """

        try:
            plan_version = int(job.get("plan_version") or 0)
        except (TypeError, ValueError):
            plan_version = 0
        if plan_version == 1:
            return True

        repository = self._deps.execution_repository
        getter = getattr(repository, "get", None)
        if not callable(getter):
            return False
        try:
            return getter(job_id) is not None
        except Exception:
            return True

    def _preflight_transfer_cancellation(self, job_id: int) -> dict[str, Any] | None:
        """Reject cancellation after the durable V1 commit boundary.

        Once the final target is verified, cancelling the public job must not
        strand the execution before application ACK/source cleanup.  The V1
        state machine deliberately resumes ACK in this situation; keep that
        invariant at the admin command boundary instead of first persisting a
        contradictory ``cancelled`` job row.
        """

        repository = self._deps.execution_repository
        getter = getattr(repository, "get", None)
        if not callable(getter):
            return None
        try:
            execution = getter(job_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "message": f"无法读取 V1 传输状态，已拒绝取消：{exc}",
                "retryable": True,
            }
        if execution is None:
            return None
        phase = _normalized_status(getattr(execution, "phase", ""))
        snapshot = (
            getattr(execution, "snapshot", {})
            if isinstance(getattr(execution, "snapshot", {}), dict)
            else {}
        )
        app_acked = bool(getattr(execution, "app_acked", False) or snapshot.get("app_acked"))
        final_verified = bool(
            getattr(execution, "final_verified", False)
            or snapshot.get("final_verified")
            or snapshot.get("target_verified")
        )
        if phase == "completed":
            return {"message": "V1 传输已完成，取消未执行", "retryable": False}
        if app_acked or phase == "cleanup_pending":
            return {
                "message": "目标已完成应用 ACK，必须继续任务级来源清理，取消未执行",
                "retryable": False,
                "resume_required": True,
            }
        if final_verified or phase == "ack_pending":
            return {
                "message": "最终目标已确认，必须继续应用 ACK/清理，取消未执行",
                "retryable": False,
                "resume_required": True,
            }
        return None

    def _should_defer_transfer_cleanup(self, job_id: int) -> bool:
        """Keep destructive cleanup behind durable external-cancel confirmation."""

        repository = self._deps.execution_repository
        getter = getattr(repository, "get", None)
        if not callable(getter):
            return False
        try:
            execution = getter(job_id)
        except Exception:
            # A missing execution read must fail closed: do not fall back to
            # category-wide/legacy cleanup while the V1 ownership is unknown.
            return True
        if execution is None:
            # This method is only consulted after the job has been identified
            # as V1-owned. A missing execution row is corruption/partial
            # migration, not proof that no external operation was submitted.
            return True
        phase = _normalized_status(getattr(execution, "phase", ""))
        receipt = getattr(execution, "receipt", None)
        snapshot = getattr(execution, "snapshot", {})
        if not isinstance(receipt, dict):
            receipt = snapshot.get("receipt") if isinstance(snapshot, dict) else None
        has_receipt = isinstance(receipt, dict) and any(
            str(value or "").strip() for value in receipt.values()
        )
        if phase in {"completed", "failed", "manual_review", "cancelled"} and not has_receipt:
            return False
        return True

    def _enqueue_transfer_cancellation(
        self,
        job_id: int,
        *,
        reactivate: bool,
    ) -> dict[str, Any] | None:
        enqueue = self._deps.enqueue_transfer
        if not callable(enqueue):
            return None
        try:
            try:
                signature = inspect.signature(enqueue)
            except (TypeError, ValueError):
                signature = None
            accepts_reactivate = bool(
                signature is None
                or "reactivate" in signature.parameters
                or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_reactivate:
                result = enqueue(job_id, reactivate=bool(reactivate))
            else:
                result = enqueue(job_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "job_id": job_id,
                "message": f"V1 传输取消任务入队异常：{exc}",
            }
        if isinstance(result, dict):
            normalized = dict(result)
            normalized.setdefault("success", True)
            normalized.setdefault("job_id", job_id)
            normalized.setdefault("message", "V1 传输取消任务已入队")
            return normalized
        if result is False or result is None:
            return {
                "success": False,
                "job_id": job_id,
                "message": "V1 传输取消任务未成功入队",
            }
        return {
            "success": True,
            "job_id": job_id,
            "message": "V1 传输取消任务已入队",
            "result": result,
        }


def _safe_generation(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _has_safe_not_started_provider_fence(job: dict[str, Any]) -> bool:
    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    fence = (
        raw_data.get("provider_submission_fence")
        if isinstance(raw_data.get("provider_submission_fence"), dict)
        else {}
    )
    try:
        version = int(fence.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    return version == 1 and _normalized_status(fence.get("state")) == "not_started"
