from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PublicImportJobResult:
    result: dict[str, Any]
    job: dict[str, Any]
    public_status: str
    bound_request: dict[str, Any] | None
    bind_outcome: str
    rclone_start: dict[str, Any] | None


class PublicImportJobCoordinator:
    """Creates an import job and atomically associates it with a guest request."""

    def __init__(
        self,
        *,
        import_service: Callable[[], Any],
        submission_service: Callable[[], Any],
        runtime_revision: Callable[[], int],
        executor_id: Callable[[], str],
        start_rclone: Callable[[dict[str, Any], str], dict[str, Any] | None],
        public_status: Callable[[str], str],
        safe_result: Callable[[dict[str, Any]], dict[str, Any]],
        warn: Callable[[str, tuple[Any, ...]], None],
        cancel_unbound_job: Callable[..., dict[str, Any] | None] | None = None,
        worker_dispatcher: Any | None = None,
    ) -> None:
        self.import_service = import_service
        self.submission_service = submission_service
        self.runtime_revision = runtime_revision
        self.executor_id = executor_id
        self.start_rclone = start_rclone
        self.public_status = public_status
        self.safe_result = safe_result
        self.warn = warn
        self.cancel_unbound_job = cancel_unbound_job
        self.worker_dispatcher = worker_dispatcher

    def execute(
        self,
        *,
        guest_request_id: int,
        request_token: str,
        submit_payload: dict[str, Any],
        request_updates: dict[str, Any] | None = None,
    ) -> PublicImportJobResult:
        if self.worker_dispatcher:
            queued = self.worker_dispatcher.public_import_create(
                guest_request_id=guest_request_id,
                request_token=request_token,
                submit_payload=submit_payload,
                request_updates=request_updates,
            )
            if queued:
                queued_request = None
                mark_queued = getattr(self.submission_service(), "mark_worker_queued", None)
                if callable(mark_queued):
                    queued_request = mark_queued(
                        guest_request_id,
                        worker_task_id=int(queued.get("worker_task_id") or 0),
                        task_type=str(queued.get("task_type") or "public_import_create"),
                        request_updates=request_updates,
                    )
                return PublicImportJobResult(
                    result=queued,
                    job={},
                    public_status="处理中",
                    bound_request=queued_request,
                    bind_outcome="queued",
                    rclone_start=None,
                )
        return self.execute_inline(
            guest_request_id=guest_request_id,
            request_token=request_token,
            submit_payload=submit_payload,
            request_updates=request_updates,
        )

    def execute_inline(
        self,
        *,
        guest_request_id: int,
        request_token: str,
        submit_payload: dict[str, Any],
        mark_failure: bool = True,
        request_updates: dict[str, Any] | None = None,
        compensation_retry_job_id: int = 0,
    ) -> PublicImportJobResult:
        try:
            compensation_retry_job_id = max(0, int(compensation_retry_job_id or 0))
        except (TypeError, ValueError):
            compensation_retry_job_id = 0
        payload = dict(submit_payload)
        payload["idempotency_key"] = f"guest-request:{request_token}"
        payload["config_revision"] = payload.get("config_revision") or self.runtime_revision()
        payload["executor_id"] = payload.get("executor_id") or self.executor_id()
        submissions = self.submission_service()
        execution_allowed = getattr(submissions, "worker_execution_allowed", None)
        compensation_request = None
        if callable(execution_allowed):
            allowed, reason, current_request = execution_allowed(guest_request_id)
            if not allowed:
                if _request_job_id(current_request) > 0:
                    return PublicImportJobResult(
                        result={
                            "success": True,
                            "worker_outcome": "completed",
                            "already_bound": True,
                            "terminal": True,
                            "message": reason,
                        },
                        job={},
                        public_status=str(
                            (current_request or {}).get("public_status") or "处理中"
                        ),
                        bound_request=current_request,
                        bind_outcome="existing",
                        rclone_start=None,
                    )
                if not compensation_retry_job_id:
                    try:
                        existing_job = self._existing_idempotent_job(
                            str(payload["idempotency_key"])
                        )
                    except Exception as exc:  # noqa: BLE001
                        return self._retryable_recovery_failure(
                            result={"success": False, "cancelled": False},
                            job={},
                            request_item=current_request,
                            message=f"访客请求已取消，但同 key 正式任务查询失败：{exc}",
                            bind_outcome="cancelled_job_lookup_failed",
                        )
                    if existing_job:
                        return self._cancelled_request_existing_job(
                            job=existing_job,
                            request_item=current_request,
                            reason=reason,
                        )
                    return PublicImportJobResult(
                        result={
                            "success": False,
                            "worker_outcome": "business_failed",
                            "cancelled": True,
                            "terminal": True,
                            "message": reason,
                        },
                        job={},
                        public_status=str((current_request or {}).get("public_status") or "已取消"),
                        bound_request=current_request,
                        bind_outcome="cancelled",
                        rclone_start=None,
                    )
                compensation_request = current_request
        try:
            imports = self.import_service()
            result = imports.create_import_job(payload)
            job = result.get("job") if isinstance(result.get("job"), dict) else {}
            expected_idempotency_key = str(payload["idempotency_key"])
            owned_by_request = (
                str(job.get("idempotency_key") or "") == expected_idempotency_key
            )
            newly_created = result.get("created") is True and owned_by_request
            idempotent_retry = result.get("created") is False and owned_by_request
            manageable_by_request = newly_created or idempotent_retry
            manual_provider_review = bool(result.get("manual_review"))
            try:
                created_job_id = int(job.get("id") or 0)
            except (TypeError, ValueError):
                created_job_id = 0
            if compensation_retry_job_id and (
                not manageable_by_request or created_job_id != compensation_retry_job_id
            ):
                return self._retryable_recovery_failure(
                    result=result,
                    job=job,
                    request_item=compensation_request,
                    message="补偿重试未找回原正式任务，已停止对其他任务执行取消",
                    bind_outcome="compensation_recovery_identity_conflict",
                )

            current_provider_status = _normalized_status(job.get("status"))
            if idempotent_retry and current_provider_status == "provider_submitting":
                manual_provider_review = True
                result = {
                    **result,
                    "success": False,
                    "worker_outcome": "business_failed",
                    "terminal": True,
                    "manual_review": True,
                    "provider_submission_ambiguous": True,
                    "message": (
                        "网盘提交已开始但本地尚未确认结果；为避免重复转存或离线下载，"
                        "不会自动重提，请到对应网盘核对后人工处理"
                    ),
                }

            if (
                idempotent_retry
                and current_provider_status == "created"
                and not _has_safe_not_started_provider_fence(job)
            ):
                manual_provider_review = True
                result = {
                    **result,
                    "success": False,
                    "worker_outcome": "business_failed",
                    "terminal": True,
                    "manual_review": True,
                    "provider_submission_ambiguous": True,
                    "message": (
                        "历史任务停留在已创建状态，但缺少可证明网盘接口尚未调用的版本化栅栏；"
                        "为避免重复提交，已停止自动恢复并等待人工核对"
                    ),
                }

            if compensation_retry_job_id and not manual_provider_review:
                return self._compensate_unbound_job(
                    result=result,
                    job=job,
                    request_item=compensation_request,
                    reason="访客提交已取消，重试撤销原正式任务",
                    bind_outcome="cancelled",
                    rclone_start=None,
                )

            if not manual_provider_review:
                fenced = self._post_creation_execution_fence(
                    execution_allowed=execution_allowed,
                    guest_request_id=guest_request_id,
                    result=result,
                    job=job,
                    manageable_by_request=manageable_by_request,
                    rclone_start=None,
                )
                if fenced is not None:
                    return fenced

            # Only jobs carrying the versioned ``not_started`` fence are safe
            # to replay from ``created``.  The retry service validates and
            # atomically claims that fence before any provider call.  Legacy
            # ``created`` rows have ambiguous provider history and therefore
            # return a manual-review result instead of being resubmitted.
            if (
                idempotent_retry
                and current_provider_status == "created"
                and not manual_provider_review
            ):
                retry_job = getattr(imports, "retry_job", None)
                if not callable(retry_job):
                    return self._retryable_recovery_failure(
                        result=result,
                        job=job,
                        request_item=None,
                        message="幂等任务停留在创建状态，但当前入库服务不支持恢复提交",
                        bind_outcome="provider_recovery_unavailable",
                    )
                try:
                    recovered = retry_job(_positive_job_id(job))
                except Exception as exc:  # noqa: BLE001
                    return self._retryable_recovery_failure(
                        result=result,
                        job=job,
                        request_item=None,
                        message=f"幂等任务恢复提交异常：{exc}",
                        bind_outcome="provider_recovery_failed",
                    )
                if not isinstance(recovered, dict):
                    return self._retryable_recovery_failure(
                        result=result,
                        job=job,
                        request_item=None,
                        message="幂等任务恢复提交未返回有效结果",
                        bind_outcome="provider_recovery_failed",
                    )
                recovery_requires_review = bool(
                    recovered.get("manual_review")
                    or recovered.get("provider_submission_ambiguous")
                )
                result = {
                    **recovered,
                    # Preserve the creation contract of this execution.  The
                    # provider retry helpers return ``created=True`` because
                    # they historically describe provider submission, not a
                    # new import_jobs row.
                    "created": False,
                    "idempotent_recovered": not recovery_requires_review,
                    "idempotent_recovery_blocked": recovery_requires_review,
                }
                manual_provider_review = manual_provider_review or recovery_requires_review
                job = result.get("job") if isinstance(result.get("job"), dict) else job
                owned_by_request = (
                    str(job.get("idempotency_key") or "") == expected_idempotency_key
                )
                if not owned_by_request:
                    return self._retryable_recovery_failure(
                        result=result,
                        job=job,
                        request_item=None,
                        message="幂等任务恢复提交返回了不属于当前请求的任务，已停止绑定和分发",
                        bind_outcome="provider_recovery_identity_conflict",
                    )
                manageable_by_request = owned_by_request
                if _normalized_status(job.get("status")) == "created" and not manual_provider_review:
                    return self._retryable_recovery_failure(
                        result=result,
                        job=job,
                        request_item=None,
                        message="幂等任务恢复提交后仍停留在创建状态，稍后重试",
                        bind_outcome="provider_recovery_pending",
                    )
            if not manual_provider_review:
                fenced = self._post_creation_execution_fence(
                    execution_allowed=execution_allowed,
                    guest_request_id=guest_request_id,
                    result=result,
                    job=job,
                    manageable_by_request=manageable_by_request,
                    rclone_start=None,
                )
                if fenced is not None:
                    return fenced

            # A ``created=False`` result can mean either an exact replay of
            # this guest request or reuse of a same-resource job owned by
            # somebody else.  Only the persisted idempotency key proves
            # ownership.  ``start_rclone`` must remain job-idempotent because
            # an earlier attempt may have started dispatch and crashed before
            # binding the guest request.
            manual_provider_review = bool(
                manual_provider_review
                or result.get("manual_review")
                or result.get("provider_submission_ambiguous")
                or _normalized_status(job.get("status")) == "provider_submitting"
            )
            if manual_provider_review:
                result = {
                    **result,
                    "success": False,
                    "worker_outcome": "business_failed",
                    "terminal": True,
                    "manual_review": True,
                }
            rclone_start = (
                self.start_rclone(result, f"public_submit:{request_token}")
                if manageable_by_request and not manual_provider_review
                else None
            )
            status = (
                "等待人工核对网盘提交状态"
                if manual_provider_review
                else self.public_status(str(job.get("status") or "created"))
            )
            bind_outcome, bound_request = submissions.bind_import_job(
                guest_request_id,
                public_status=status,
                job=job,
                safe_result=self.safe_result(result),
                rclone_start=rclone_start,
                success=bool(result.get("success", True)),
                request_updates=request_updates,
            )
            if bind_outcome != "bound":
                self.warn(
                    "guest request job bind skipped: request_id=%s outcome=%s job_id=%s",
                    (guest_request_id, bind_outcome, job.get("id")),
                )
            if (
                manageable_by_request
                and not manual_provider_review
                and bind_outcome in {"state_conflict", "conflict", "missing"}
            ):
                return self._compensate_unbound_job(
                    result=result,
                    job=job,
                    request_item=bound_request,
                    reason=f"正式任务创建后访客提交无法绑定（{bind_outcome}），撤销未绑定任务",
                    bind_outcome=bind_outcome,
                    rclone_start=rclone_start,
                )
            return PublicImportJobResult(
                result=result,
                job=job,
                public_status=status,
                bound_request=bound_request,
                bind_outcome=bind_outcome,
                rclone_start=rclone_start,
            )
        except Exception as exc:
            if mark_failure:
                failed_request = submissions.mark_import_failed(guest_request_id, error=str(exc))
                if failed_request is None:
                    self.warn(
                        "guest request failure transition skipped: request_id=%s",
                        (guest_request_id,),
                    )
            raise

    def _existing_idempotent_job(self, idempotency_key: str) -> dict[str, Any] | None:
        imports = self.import_service()
        getter = getattr(imports, "get_job_by_idempotency_key", None)
        if not callable(getter):
            return None
        job = getter(idempotency_key)
        if not isinstance(job, dict):
            return None
        if str(job.get("idempotency_key") or "") != str(idempotency_key or ""):
            return None
        return job

    def _cancelled_request_existing_job(
        self,
        *,
        job: dict[str, Any],
        request_item: dict[str, Any] | None,
        reason: str,
    ) -> PublicImportJobResult:
        status = _normalized_status(job.get("status"))
        ambiguous = status == "provider_submitting" or (
            status == "created" and not _has_safe_not_started_provider_fence(job)
        )
        result = {
            "success": False,
            "created": False,
            "orphan_job_recovered": True,
            "job_id": job.get("id"),
        }
        if ambiguous:
            return PublicImportJobResult(
                result={
                    **result,
                    "worker_outcome": "business_failed",
                    "terminal": True,
                    "cancelled": False,
                    "manual_review": True,
                    "provider_submission_ambiguous": True,
                    "message": (
                        "访客请求已取消，但发现同一请求的网盘提交状态无法安全确认；"
                        "未把正式任务标记为已取消，请人工核对外部网盘任务"
                    ),
                },
                job=job,
                public_status="已取消（网盘提交待人工核对）",
                bound_request=request_item,
                bind_outcome="manual_review",
                rclone_start=None,
            )
        return self._compensate_unbound_job(
            result=result,
            job=job,
            request_item=request_item,
            reason=reason or "访客请求已取消，撤销未绑定的正式任务",
            bind_outcome="cancelled",
            rclone_start=None,
        )

    def _post_creation_execution_fence(
        self,
        *,
        execution_allowed: Any,
        guest_request_id: int,
        result: dict[str, Any],
        job: dict[str, Any],
        manageable_by_request: bool,
        rclone_start: dict[str, Any] | None,
    ) -> PublicImportJobResult | None:
        if not callable(execution_allowed):
            return None
        allowed, reason, current_request = execution_allowed(guest_request_id)
        if allowed:
            return None
        current_job_id = int((current_request or {}).get("job_id") or 0)
        created_job_id = int(job.get("id") or 0)
        same_owned_job = bool(
            manageable_by_request
            and created_job_id
            and current_job_id == created_job_id
        )
        request_status = _normalized_status((current_request or {}).get("status"))
        if same_owned_job and request_status not in {"cancelled", "rejected", "unsupported"}:
            # Another execution may have observed ``provider_submitting`` and
            # bound this exact idempotent job while the original provider call
            # was still in flight.  A later authoritative provider result must
            # still pass through the job-idempotent rclone dispatcher; otherwise
            # the early binding can strand a successful job in waiting_transfer.
            return None
        if not manageable_by_request or same_owned_job:
            return PublicImportJobResult(
                result=result,
                job=job,
                public_status=str(
                    (current_request or {}).get("public_status")
                    or self.public_status(str(job.get("status") or "created"))
                ),
                bound_request=current_request,
                bind_outcome="existing",
                rclone_start=rclone_start,
            )
        return self._compensate_unbound_job(
            result=result,
            job=job,
            request_item=current_request,
            reason=reason or "访客提交状态已变化，撤销刚创建的正式任务",
            bind_outcome="cancelled",
            rclone_start=rclone_start,
        )

    def _compensate_unbound_job(
        self,
        *,
        result: dict[str, Any],
        job: dict[str, Any],
        request_item: dict[str, Any] | None,
        reason: str,
        bind_outcome: str,
        rclone_start: dict[str, Any] | None,
    ) -> PublicImportJobResult:
        public_status = str((request_item or {}).get("public_status") or "已取消")
        if not job.get("id"):
            return self._compensation_failure_result(
                result=result,
                job=job,
                request_item=request_item,
                reason=reason,
                failure_message="创建结果缺少正式任务编号",
                bind_outcome=bind_outcome,
                rclone_start=rclone_start,
                public_status=public_status,
            )
        if not callable(self.cancel_unbound_job):
            return self._compensation_failure_result(
                result=result,
                job=job,
                request_item=request_item,
                reason=reason,
                failure_message="未配置未绑定任务撤销器",
                bind_outcome=bind_outcome,
                rclone_start=rclone_start,
                public_status=public_status,
            )
        compensation_error = ""
        try:
            compensation = self.cancel_unbound_job(
                job,
                reason=reason,
                request_item=request_item,
            )
        except Exception as exc:  # noqa: BLE001
            compensation = None
            compensation_error = str(exc) or exc.__class__.__name__
        cancelled_job = (
            compensation.get("job")
            if isinstance(compensation, dict) and isinstance(compensation.get("job"), dict)
            else job
        )
        if not (isinstance(compensation, dict) and compensation.get("cancelled") is True):
            latest_status = _normalized_status(cancelled_job.get("status"))
            compensation_message = str(
                (compensation or {}).get("message") if isinstance(compensation, dict) else ""
            ).strip()
            if latest_status in {"done", "success"}:
                return PublicImportJobResult(
                    result={
                        **result,
                        "success": True,
                        "worker_outcome": "completed",
                        "completed_without_cancel": True,
                        "cancelled": False,
                        "retryable": False,
                        "terminal": True,
                        "message": compensation_message
                        or "正式任务已完成，未再执行取消补偿",
                    },
                    job=cancelled_job,
                    public_status=public_status,
                    bound_request=request_item,
                    bind_outcome=bind_outcome,
                    rclone_start=rclone_start,
                )
            failure_message = compensation_error or compensation_message or "任务取消补偿未成功"
            return self._compensation_failure_result(
                result=result,
                job=cancelled_job,
                request_item=request_item,
                reason=reason,
                failure_message=failure_message,
                bind_outcome=bind_outcome,
                rclone_start=rclone_start,
                public_status=public_status,
            )
        return PublicImportJobResult(
            result={
                **result,
                "success": False,
                "worker_outcome": "business_failed",
                "cancelled": True,
                "terminal": True,
                "message": reason,
            },
            job=cancelled_job,
            public_status=public_status,
            bound_request=request_item,
            bind_outcome=bind_outcome,
            rclone_start=rclone_start,
        )

    @staticmethod
    def _compensation_failure_result(
        *,
        result: dict[str, Any],
        job: dict[str, Any],
        request_item: dict[str, Any] | None,
        reason: str,
        failure_message: str,
        bind_outcome: str,
        rclone_start: dict[str, Any] | None,
        public_status: str,
    ) -> PublicImportJobResult:
        return PublicImportJobResult(
            result={
                **result,
                "success": False,
                "worker_outcome": "retryable",
                "retryable": True,
                "compensation_failed": True,
                "cancelled": False,
                "terminal": False,
                "message": f"{reason}；{failure_message}",
            },
            job=job,
            public_status=public_status,
            bound_request=request_item,
            bind_outcome=bind_outcome,
            rclone_start=rclone_start,
        )

    @staticmethod
    def _retryable_recovery_failure(
        *,
        result: dict[str, Any],
        job: dict[str, Any],
        request_item: dict[str, Any] | None,
        message: str,
        bind_outcome: str,
    ) -> PublicImportJobResult:
        return PublicImportJobResult(
            result={
                **result,
                "success": False,
                "worker_outcome": "retryable",
                "retryable": True,
                "recovery_failed": True,
                "terminal": False,
                "message": message,
            },
            job=job,
            public_status=str((request_item or {}).get("public_status") or "处理中"),
            bound_request=request_item,
            bind_outcome=bind_outcome,
            rclone_start=None,
        )


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _positive_job_id(job: dict[str, Any]) -> int:
    try:
        job_id = int(job.get("id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    if job_id <= 0:
        raise ValueError("幂等任务缺少有效任务编号，无法恢复提交")
    return job_id


def _request_job_id(request_item: dict[str, Any] | None) -> int:
    try:
        job_id = int((request_item or {}).get("job_id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    return max(0, job_id)


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
    return version == 1 and str(fence.get("state") or "").strip().lower() == "not_started"
