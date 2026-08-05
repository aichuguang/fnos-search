from __future__ import annotations

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
    sixpan_importer: Callable[[], Any] | None = None


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
        stop_running = self._deps.payload_bool(payload, "stop_running", stop_running_default)
        claim = self._claim_cancellation(
            job_id,
            supplied_job=job,
            reason=reason,
            request_item=request_item,
            cleanup_requested=cleanup_requested,
            delete_source=delete_source,
            delete_temp=delete_temp,
            delete_target_partial=delete_target_partial,
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
        self._deps.jobs.add_event(
            job_id,
            "warn",
            f"重复取消任务：{reason}" if already_cancelled else f"任务已取消：{reason}",
            cancel_raw,
        )

        propagation = self._cancel_related_execution(
            cancelled_job,
            reason=reason,
            cancelled_from_status=str(claim.get("cancelled_from_status") or ""),
        )

        if cleanup_requested:
            cleanup = self._deps.cleaner.cleanup_cancelled_task(
                job=cancelled_job,
                request_item=request_item,
                delete_source=delete_source,
                delete_temp=delete_temp,
                delete_target_partial=delete_target_partial,
                stop_running=stop_running,
                include_title_matches=self._deps.payload_bool(payload, "include_title_matches", True),
            )
            cleanup.setdefault("items", []).extend(propagation["items"])
            cleanup.setdefault("warnings", []).extend(propagation["warnings"])
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
        cancelled_from_status: str,
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

        if self._should_cancel_sixpan(job, cancelled_from_status=cancelled_from_status):
            try:
                importer = self._deps.sixpan_importer() if callable(self._deps.sixpan_importer) else None
                delete_tasks = getattr(importer, "delete_offline_tasks", None)
                if not callable(delete_tasks):
                    delete_tasks = getattr(getattr(importer, "client", None), "delete_offline_tasks", None)
                if not callable(delete_tasks):
                    warnings.append("六盘适配器未提供离线任务取消接口")
                else:
                    external_task_id = str(job.get("external_task_id") or "").strip()
                    response = delete_tasks([external_task_id], delete_files=False)
                    explicit_failure = isinstance(response, dict) and response.get("success") is False
                    items.append(
                        {
                            "type": "cancel_sixpan_task",
                            "success": not explicit_failure,
                            "external_task_id": external_task_id,
                            "delete_files": False,
                            "response": response,
                        }
                    )
                    if explicit_failure:
                        warnings.append("六盘离线任务取消接口返回失败")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"取消六盘离线任务异常：{exc}")

        return {"items": items, "warnings": warnings}

    @staticmethod
    def _should_cancel_sixpan(job: dict[str, Any], *, cancelled_from_status: str) -> bool:
        if _normalized_status(cancelled_from_status) != "submitted":
            return False
        if not str(job.get("external_task_id") or "").strip():
            return False
        source_type = str(job.get("source_type") or "").strip().lower()
        route = str(job.get("target_route") or "").strip().lower()
        return source_type in {"magnet", "torrent", "bt"} or route == "sixpan_offline"

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
