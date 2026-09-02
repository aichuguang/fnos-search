"""Durable V1 transfer composition and worker handler."""

from __future__ import annotations

import os
import posixpath
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from ..constants import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_REVIEW,
    JOB_TRANSFERRING,
)
from ..planning.plan_validator import validate_plan
from ..services.execution_plan_guard import reject_non_v1_job, v1_plan_or_review
from . import (
    AckResult,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    OrganizeResult,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW,
    PHASE_ORGANIZING,
    PHASE_RETRY_WAIT,
    PHASE_SOURCE_MATERIALIZING,
    PHASE_NATIVE_SUBMITTING,
    PHASE_TRANSFERRING,
)
from .cleanup import CompositeFilesystemOps, LocalFilesystemOps, TransferCleanupService
from .execution_repository import ExecutionConflictError, TransferExecutionRepository
from .finalizer import TransferFinalizer
from .native_executor import NativeSourceMaterializer, NativeTransferExecutor
from .orchestrator import TransferOrchestrator
from .provider_relay_executor import ProviderRelayExecutor
from .rclone_adapter import RcloneOperationAdapter
from .rclone_executor import RcloneTransferExecutor
from .verifier import TransferVerifier


class TransferDispatchError(RuntimeError):
    pass


class TransferDispatchService:
    def __init__(
        self,
        *,
        database: Any,
        accounts_service: Any,
        target_service: Any,
        mount_service: Any,
        registry: Any,
        rclone_service: Any,
        locator_resolver: Any,
        source_record_service: Any = None,
        assignment_service: Any = None,
        execution_repository: TransferExecutionRepository | None = None,
        organizer_service: Any = None,
        relay_root: str = "/data/relay",
        local_target_root: str = "/storage-targets",
        execution_lease_seconds: int = 600,
    ) -> None:
        self.database = database
        self.accounts_service = accounts_service
        self.target_service = target_service
        self.mount_service = mount_service
        self.registry = registry
        self.rclone_service = rclone_service
        self.locator_resolver = locator_resolver
        self.source_record_service = source_record_service
        self.assignment_service = assignment_service
        self.executions = execution_repository or TransferExecutionRepository(database)
        self.organizer_service = organizer_service
        self.relay_root = os.path.realpath(relay_root)
        self.local_target_root = os.path.realpath(local_target_root)
        self.execution_lease_seconds = max(60, int(execution_lease_seconds))
        # Shared only by executors created by this dispatch composition root;
        # a fresh app/test composition must not inherit another process'
        # circuit-breaker state.
        self._halted_relay_account_ids: set[str] = set()
        local_ops = LocalFilesystemOps((self.relay_root, self.local_target_root))
        self.cleanup = TransferCleanupService(
            CompositeFilesystemOps(local_ops=local_ops),
            local_roots=(self.relay_root, self.local_target_root),
            relay_root=self.relay_root,
        )
        self.verifier = TransferVerifier(self._list_target)
        # Finalization is part of the durable V1 execution graph.  Pass the
        # repository so the finalizer can reject plans that are not the
        # immutable snapshot stored for this job; this prevents a legacy
        # completion callback from moving arbitrary/current-config paths.
        self.finalizer = TransferFinalizer(
            mount_service,
            local_root=self.local_target_root,
            execution_repository=self.executions,
        )
        self.organizer = _OrganizerAdapter(database, organizer_service)
        self.ack = _JobAckService(database, self.executions)

    def set_organizer_service(self, service: Any) -> None:
        """Atomically replace the Organizer adapter used by new worker steps."""

        self.organizer_service = service
        self.organizer = _OrganizerAdapter(self.database, service)

    def build_executors(self, plan: dict[str, Any]) -> dict[str, Any]:
        strategy = str((plan.get("transfer") or {}).get("strategy") or "")
        adapters: dict[str, Any] = {}
        for kind in {
            str((plan.get("source") or {}).get("provider_kind") or ""),
            str((plan.get("destination") or {}).get("provider_kind") or ""),
        }:
            if kind:
                adapters[kind] = self.registry.get(kind)
        materializer = NativeSourceMaterializer(adapters)
        rclone_adapter = RcloneOperationAdapter(self.rclone_service, self.locator_resolver)
        if strategy in {"native_save", "native_offline"}:
            return {strategy: NativeTransferExecutor(adapters, strategy=strategy)}
        if strategy == "rclone_copy":
            return {
                strategy: RcloneTransferExecutor(
                    rclone_adapter,
                    self.locator_resolver,
                    source_materializer=materializer,
                    visibility_poller=lambda receipt: self._source_visible(plan, receipt),
                    verifier=self.verifier,
                )
            }
        if strategy == "relay_upload_provider":
            destination = plan.get("destination") or {}
            provider_kind = str(destination.get("provider_kind") or "").strip().lower()
            destination_adapter = adapters.get(provider_kind) or self.registry.get(provider_kind)
            capabilities = destination_adapter.capabilities()
            supports_relay = (
                bool(capabilities.get("supports_relay_upload"))
                if isinstance(capabilities, dict)
                else bool(getattr(capabilities, "supports_relay_upload", False))
            )
            if not supports_relay:
                raise TransferDispatchError(
                    f"{provider_kind or '目标 Provider'} Adapter 不支持中继上传"
                )
            uploader_factory = getattr(destination_adapter, "relay_uploader_for", None)
            if not callable(uploader_factory):
                raise TransferDispatchError(
                    f"{provider_kind or '目标 Provider'} Adapter 未提供冻结账号中继上传器"
                )
            uploader = uploader_factory(
                str(destination.get("account_id") or ""),
                int(destination.get("account_revision") or 0),
            )
            if uploader is None:
                raise TransferDispatchError(
                    f"{provider_kind or '目标 Provider'} 冻结账号中继上传器不可用"
                )
            return {
                strategy: ProviderRelayExecutor(
                    rclone_adapter,
                    uploader,
                    source_materializer=materializer,
                    visibility_poller=lambda receipt: self._source_visible(plan, receipt),
                    relay_root=self.relay_root,
                    halted_account_ids=self._halted_relay_account_ids,
                )
            }
        raise TransferDispatchError(f"未知传输策略：{strategy}")

    def handle_worker(self, payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        job_id = int(payload.get("job_id") or 0)
        if job_id <= 0:
            raise TransferDispatchError("Worker 缺少 job_id")
        job = self.database.job_queries.get(job_id)
        execution = self.executions.get(job_id)
        if not isinstance(job, dict) or execution is None:
            return {"worker_outcome": "business_failed", "message": "V1 任务、计划或执行状态缺失"}

        # Queue leases prevent duplicate tasks in normal operation, but a
        # stale/replayed task can still exist after a crash.  Claim the
        # execution row as a second fence when the repository supports it.
        due_in = _seconds_until(execution.resume_at)
        if due_in > 0:
            return {
                "worker_outcome": "deferred",
                "phase": execution.phase,
                "delay_seconds": due_in,
                "message": "执行状态仍在退避等待期",
            }

        claim = getattr(self.executions, "claim", None)
        claim_owner = ""
        if callable(claim) and execution.phase not in {
            PHASE_COMPLETED,
            PHASE_FAILED,
            PHASE_MANUAL_REVIEW,
            PHASE_CANCELLED,
        }:
            claim_owner = str((_task or {}).get("owner_id") or "")
            if not claim_owner:
                claim_owner = f"transfer-task:{int((_task or {}).get('id') or 0)}"
            claimed = claim(
                job_id,
                claim_owner,
                lease_seconds=self.execution_lease_seconds,
            )
            if claimed is None:
                return {
                    "worker_outcome": "deferred",
                    "phase": execution.phase,
                    "delay_seconds": 5,
                    "message": "任务正在由其他 Worker 推进",
                }
            execution = claimed
            # Cancellation can win the job-row CAS while the transfer row is
            # being claimed.  Re-read the business row after the claim fence;
            # never submit a Provider operation from a stale pre-cancel
            # snapshot.
            job = self.database.job_queries.get(job_id) or job

        cancellation_result = self._stop_if_job_cancelled(job, execution, claim_owner)
        if cancellation_result is not None:
            return cancellation_result
        plan, rejection = v1_plan_or_review(
            self.database,
            job,
            context="V1 Worker 执行前",
            execution_repository=self.executions,
        )
        if rejection:
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        persisted_plan = self.executions.plan(job_id)
        if persisted_plan is None:
            rejection = reject_non_v1_job(
                self.database,
                job,
                context="V1 Worker 执行前",
                error="数据库缺少不可变 import_execution_plans 记录",
                execution_repository=self.executions,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        if persisted_plan != plan:
            rejection = reject_non_v1_job(
                self.database,
                job,
                context="V1 Worker 执行前",
                error="任务快照与数据库不可变执行计划不一致",
                execution_repository=self.executions,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        # The immutable plan and the durable execution row carry the strategy
        # redundantly.  Refuse to run when they disagree; otherwise a
        # corrupted/replayed execution row could make recovery diagnostics say
        # one thing while the orchestrator starts another executor.
        execution_strategy = str(getattr(execution, "strategy", "") or "").strip().lower()
        plan_strategy = str((plan.get("transfer") or {}).get("strategy") or "").strip().lower()
        if execution_strategy and plan_strategy and execution_strategy != plan_strategy:
            rejection = reject_non_v1_job(
                self.database,
                job,
                context="V1 Worker 执行前",
                error=(
                    "transfer execution strategy 与不可变 "
                    f"execution_plan 不一致：{execution_strategy} != {plan_strategy}"
                ),
                execution_repository=self.executions,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        cleanup_completed_during_retry = False
        claim_holder: dict[str, Any] | None = None
        try:
            validate_plan(
                plan,
                job,
                deps=self._validation_deps(),
            )
            orchestrator = self._orchestrator(plan)
        except Exception as exc:  # noqa: BLE001
            message = self._safe_error("V1 执行前校验/装配失败", exc)
            rejection = reject_non_v1_job(
                self.database,
                job,
                context="V1 Worker 执行前",
                error=message,
                execution_repository=self.executions,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        # ``receipt_json`` is the durable side-effect fence. Older/crashed
        # snapshots can be empty while the receipt column still records the
        # submitted operation, so hydrate it before deciding that a new
        # provider submission is safe.
        restored_snapshot = dict(getattr(execution, "snapshot", {}) or {})
        durable_receipt = self._durable_receipt(execution, restored_snapshot)
        if durable_receipt is not None:
            restored_snapshot["receipt"] = durable_receipt
        orchestrator.restore(restored_snapshot or {"phase": execution.phase})
        unsafe_phases = {
            PHASE_SOURCE_MATERIALIZING,
            PHASE_NATIVE_SUBMITTING,
            PHASE_TRANSFERRING,
        }
        if orchestrator.state.phase in unsafe_phases and orchestrator.state.receipt is None:
            fence = dict(orchestrator.state.submission_fence or {})
            if fence.get("state") == "prepared":
                orchestrator.state.review_from = orchestrator.state.phase
                orchestrator.state.phase = PHASE_MANUAL_REVIEW
                orchestrator.state.message = (
                    "外部提交 fence 已存在但 receipt 缺失，提交结果无法确认；"
                    "为避免重复转存，任务进入人工复核"
                )
            else:
                orchestrator.state.submission_fence = {
                    "state": "prepared",
                    "phase": orchestrator.state.phase,
                    # The fence key is the exact idempotency key used by the
                    # executor for this external side effect.  Appending the
                    # phase name made the first receipt fail the repository
                    # fence check (landing:/relay-dl: use explicit prefixes).
                    "operation_key": self._submission_operation_key(
                        plan,
                        orchestrator.state.phase,
                    ),
                    "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                prepared_snapshot = orchestrator.snapshot()
                prepared_snapshot["claim"] = {
                    "owner_id": claim_owner,
                    "lease_until": _after(self.execution_lease_seconds),
                }
                execution = self.executions.save(
                    execution,
                    prepared_snapshot,
                    phase=orchestrator.state.phase,
                    last_error_message="外部提交 fence 已持久化",
                    claim_owner_id=claim_owner or None,
                    job_status="provider_submitting",
                    require_job_status_transition=True,
                )
        if orchestrator.state.phase == PHASE_RETRY_WAIT:
            orchestrator.mark_retry_ready()
        claim_holder = {"execution": execution, "lost": False}
        try:
            with self._claim_heartbeat(job_id, claim_owner, claim_holder):
                result = orchestrator.step()
        except ExecutionConflictError:
            execution = claim_holder.get("execution") or execution
            if claim_holder.get("lost"):
                return self._lost_claim_result(execution.phase)
            raise
        except Exception as exc:  # noqa: BLE001
            execution = claim_holder.get("execution") or execution
            if claim_holder.get("lost"):
                return self._lost_claim_result(execution.phase)
            message = self._safe_error("V1 执行步骤异常，停止自动重试", exc)
            self._persist_manual_review(
                execution,
                message,
                claim_owner_id=claim_owner or None,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": message,
                "manual_review": True,
            }
        execution = claim_holder.get("execution") or execution
        if claim_holder.get("lost"):
            return self._lost_claim_result(execution.phase)
        if orchestrator.state.receipt is not None and orchestrator.state.submission_fence:
            # Keep the fence as a committed audit marker.  Clearing it before
            # the durable save would make a crash window indistinguishable
            # from a never-submitted operation.
            orchestrator.state.submission_fence["state"] = "committed"
            receipt = orchestrator.state.receipt
            metadata = receipt.metadata if receipt is not None else {}
            orchestrator.state.submission_fence["receipt_operation_key"] = str(
                (metadata or {}).get("operation_key")
                or (metadata or {}).get("idempotency_key")
                or receipt.operation_id
                or ""
            )
        snapshot = orchestrator.snapshot()
        snapshot.pop("claim", None)
        delay = self._delay(snapshot, result.needs_wait)
        job_status, job_error = self._job_projection(snapshot["phase"], result.message)
        try:
            self.executions.save(
                execution,
                snapshot,
                phase=snapshot["phase"],
                resume_at=_after(delay) if result.needs_wait else None,
                staging_verified=bool(snapshot.get("staging_verified")),
                final_verified=bool(snapshot.get("final_verified")),
                app_acked=bool(snapshot.get("app_acked")),
                organizer_task_id=int(snapshot.get("organizer_task_id") or 0) or None,
                last_error_code=str(snapshot.get("error_code") or ""),
                last_error_message=str(snapshot.get("message") or ""),
                claim_owner_id=claim_owner or None,
                job_status=job_status,
                job_error_message=job_error,
            )
        except ExecutionConflictError:
            # Another worker won the CAS.  Do not run the external operation
            # again; let the durable queue/scanner wake the winner's state.
            return {
                "worker_outcome": "deferred",
                "phase": snapshot.get("phase") or execution.phase,
                "delay_seconds": 5,
                "message": "执行状态已被其他 Worker 更新，放弃本次写入",
            }
        if snapshot["phase"] == PHASE_COMPLETED:
            return {"worker_outcome": "completed", "phase": PHASE_COMPLETED, "message": result.message}
        if snapshot["phase"] == PHASE_FAILED:
            return {"worker_outcome": "business_failed", "phase": PHASE_FAILED, "message": result.message}
        if snapshot["phase"] == PHASE_MANUAL_REVIEW:
            return {"worker_outcome": "completed", "phase": PHASE_MANUAL_REVIEW, "message": result.message}
        return {
            "worker_outcome": "deferred",
            "phase": snapshot["phase"],
            "delay_seconds": delay,
            "message": result.message,
        }

    @contextmanager
    def _claim_heartbeat(
        self,
        job_id: int,
        owner_id: str,
        holder: dict[str, Any],
    ):
        """Keep the transfer-execution claim alive during a blocking adapter call."""

        renew = getattr(self.executions, "renew_claim", None)
        owner = str(owner_id or "").strip()
        if not owner or not callable(renew):
            yield
            return
        stop = threading.Event()
        interval = max(1, min(60, self.execution_lease_seconds // 3))

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    holder["execution"] = renew(
                        job_id,
                        owner,
                        lease_seconds=self.execution_lease_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    holder["heartbeat_error"] = str(exc)
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"transfer-claim-{int(job_id)}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)
            latest = self.executions.get(job_id)
            if latest is None:
                holder["lost"] = True
            else:
                claim = latest.snapshot.get("claim") if isinstance(latest.snapshot, dict) else {}
                if not isinstance(claim, dict) or str(claim.get("owner_id") or "") != owner:
                    holder["lost"] = True
                else:
                    holder["execution"] = latest

    @staticmethod
    def _lost_claim_result(phase: str) -> dict[str, Any]:
        return {
            "worker_outcome": "deferred",
            "phase": str(phase or ""),
            "delay_seconds": 5,
            "message": "执行 claim 在外部调用期间发生变化；已停止写入，等待持久状态恢复",
        }

    def _stop_if_job_cancelled(
        self,
        job: dict[str, Any],
        execution: Any,
        claim_owner: str,
    ) -> dict[str, Any] | None:
        status = str(job.get("status") or "").strip().lower()
        if status not in {JOB_CANCELLED, "cancelled", "canceled", "rejected"}:
            return None
        current_phase = str(getattr(execution, "phase", "") or "").strip()
        if current_phase in {
            PHASE_COMPLETED,
            PHASE_FAILED,
            PHASE_MANUAL_REVIEW,
            PHASE_CANCELLED,
        }:
            return {
                "worker_outcome": "completed",
                "phase": current_phase,
                "message": f"任务已取消，V1 执行已处于终态：{current_phase}",
                "cancelled": current_phase == PHASE_CANCELLED,
            }
        snapshot = dict(getattr(execution, "snapshot", {}) or {})
        if bool(snapshot.get("app_acked")) or current_phase == "cleanup_pending":
            message = "任务状态已取消，但 V1 目标已 ACK；停止自动清理并转人工复核"
            self._persist_manual_review(
                execution,
                message,
                claim_owner_id=claim_owner or None,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": message,
                "manual_review": True,
                "cancelled": False,
            }
        if (
            bool(snapshot.get("final_verified") or snapshot.get("target_verified"))
            or current_phase == "ack_pending"
        ):
            message = "任务状态已取消，但最终目标已确认且 ACK 未完成；转人工复核，禁止删除来源"
            self._persist_manual_review(
                execution,
                message,
                claim_owner_id=claim_owner or None,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": message,
                "manual_review": True,
                "cancelled": False,
            }
        receipt = self._durable_receipt(execution, snapshot)
        if receipt is not None:
            return self._cancel_external_operation(job, execution, claim_owner)

        cleanup_result = self._cleanup_cancelled_artifacts(
            job,
            allow_target_partial=True,
        )
        cleanup_pending = bool(cleanup_result and cleanup_result.get("success") is False)
        if cleanup_result is not None:
            snapshot["cancel_cleanup"] = cleanup_result
            snapshot["cancel_cleanup_pending"] = cleanup_pending
        snapshot.update(
            {
                "phase": PHASE_CANCELLED,
                "message": "任务在 Provider 提交前已取消，未执行外部操作",
                "error_code": "",
            }
        )
        phase = PHASE_CANCEL_PENDING if cleanup_pending else PHASE_CANCELLED
        if cleanup_pending:
            snapshot["phase"] = phase
            snapshot["error_code"] = "CANCEL_CLEANUP_PENDING"
        message = snapshot["message"]
        try:
            self.executions.save(
                execution,
                snapshot,
                phase=phase,
                resume_at=_after(5) if cleanup_pending else None,
                last_error_code=str(snapshot.get("error_code") or ""),
                last_error_message=message,
                claim_owner_id=claim_owner or None,
            )
        except ExecutionConflictError:
            # Another cancellation/finalizer won the CAS; still do not run an
            # external operation from this worker.
            pass
        return {
            "worker_outcome": "deferred" if cleanup_pending else "completed",
            "phase": phase,
            "delay_seconds": 5 if cleanup_pending else 0,
            "message": (
                message
                if cleanup_result is None or cleanup_result.get("success", True)
                else f"{message}；任务级清理失败，需人工处理"
            ),
            "cancelled": not cleanup_pending,
            "external_operation_started": bool(receipt),
        }

    def _cancel_external_operation(
        self,
        job: dict[str, Any],
        execution: Any,
        claim_owner: str,
    ) -> dict[str, Any]:
        """Cancel/poll a submitted V1 operation without re-entering submit phases."""

        job_id = int(job.get("id") or 0)
        plan, rejection = v1_plan_or_review(
            self.database,
            job,
            context="V1 取消执行前",
            execution_repository=self.executions,
        )
        if rejection:
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": rejection["message"],
                "manual_review": True,
            }
        cleanup_completed_during_retry = False
        claim_holder: dict[str, Any] | None = None
        try:
            persisted_plan = self.executions.plan(job_id)
            if persisted_plan is None or persisted_plan != plan:
                raise TransferDispatchError("取消时任务快照与不可变执行计划不一致")
            execution_strategy = str(getattr(execution, "strategy", "") or "").strip().lower()
            plan_strategy = str((plan.get("transfer") or {}).get("strategy") or "").strip().lower()
            if not execution_strategy or execution_strategy != plan_strategy:
                raise TransferDispatchError("取消时执行策略与不可变计划不一致")
            validate_plan(plan, job, deps=self._validation_deps())
            orchestrator = self._orchestrator(plan)
            restored_snapshot = dict(getattr(execution, "snapshot", {}) or {})
            # Prefer the indexed receipt column over a missing/stale snapshot.
            # Otherwise a submitted operation could be mistaken for a
            # pre-submit cancellation and its source could be deleted.
            durable_receipt = self._durable_receipt(execution, restored_snapshot)
            if durable_receipt is not None:
                restored_snapshot["receipt"] = durable_receipt
            if restored_snapshot.get("cancel_cleanup_pending"):
                cleanup_result = self._cleanup_cancelled_artifacts(
                    job,
                    allow_target_partial=True,
                )
                if cleanup_result is not None:
                    restored_snapshot["cancel_cleanup"] = cleanup_result
                    if cleanup_result.get("success") is False:
                        restored_snapshot["phase"] = PHASE_CANCEL_PENDING
                        restored_snapshot["message"] = "取消已确认，但任务级清理仍失败，等待再次重试"
                        restored_snapshot["error_code"] = "CANCEL_CLEANUP_PENDING"
                        orchestrator.restore(restored_snapshot)
                        snapshot = orchestrator.snapshot()
                        snapshot.pop("claim", None)
                        self.executions.save(
                            execution,
                            snapshot,
                            phase=PHASE_CANCEL_PENDING,
                            resume_at=_after(5),
                            last_error_code="CANCEL_CLEANUP_PENDING",
                            last_error_message=str(snapshot["message"]),
                            claim_owner_id=claim_owner or None,
                        )
                        return {
                            "worker_outcome": "deferred",
                            "phase": PHASE_CANCEL_PENDING,
                            "delay_seconds": 5,
                            "message": str(snapshot["message"]),
                            "cancelled": True,
                            "external_operation_started": True,
                        }
                    restored_snapshot["cancel_cleanup_pending"] = False
                    restored_snapshot["cancel_cleanup_completed"] = True
                    cleanup_completed_during_retry = True
            orchestrator.restore(restored_snapshot)
            claim_holder = {"execution": execution, "lost": False}
            with self._claim_heartbeat(job_id, claim_owner, claim_holder):
                if orchestrator.state.phase == PHASE_CANCEL_PENDING:
                    result = orchestrator.step()
                    pending = bool(result.needs_wait and orchestrator.state.phase == PHASE_CANCEL_PENDING)
                    message = str(result.message or orchestrator.state.message or "等待取消确认")
                    external_completed = "外部操作已完成" in message
                else:
                    cancel_result = orchestrator.cancel(direct=True)
                    pending = cancel_result.status == "cancel_pending"
                    message = str(cancel_result.message or orchestrator.state.message or "取消请求已处理")
                    external_completed = "外部操作已完成" in message
                    if cancel_result.status == "resume_ack":
                        raise TransferDispatchError(
                            "最终目标已确认但任务行已取消，无法自动恢复 ACK；需要人工复核"
                        )
            execution = claim_holder.get("execution") or execution
            if claim_holder.get("lost"):
                return self._lost_claim_result(execution.phase)
        except Exception as exc:  # noqa: BLE001
            if claim_holder is not None:
                execution = claim_holder.get("execution") or execution
                if claim_holder.get("lost"):
                    return self._lost_claim_result(execution.phase)
            message = self._safe_error("V1 外部操作取消失败，转人工复核", exc)
            self._persist_manual_review(
                execution,
                message,
                claim_owner_id=claim_owner or None,
            )
            return {
                "worker_outcome": "completed",
                "phase": PHASE_MANUAL_REVIEW,
                "message": message,
                "manual_review": True,
            }

        snapshot = orchestrator.snapshot()
        snapshot.pop("claim", None)
        phase = str(snapshot.get("phase") or PHASE_CANCEL_PENDING)
        cleanup_result = None
        if (
            not pending
            and phase == PHASE_CANCELLED
            and not cleanup_completed_during_retry
        ):
            cleanup_result = (
                {
                    "success": True,
                    "skipped": True,
                    "message": (
                        "外部操作已完成但目标尚未通过 V1 校验；"
                        "保留源端、临时文件与目标残留等待人工核对"
                    ),
                    "items": [],
                    "warnings": [],
                    "errors": [],
                }
                if external_completed
                else self._cleanup_cancelled_artifacts(
                    job,
                    allow_target_partial=True,
                )
            )
            if cleanup_result is not None:
                snapshot["cancel_cleanup"] = cleanup_result
                if cleanup_result.get("rejected"):
                    snapshot["review_from"] = phase
                    snapshot["phase"] = PHASE_MANUAL_REVIEW
                    phase = PHASE_MANUAL_REVIEW
                    message = (
                        f"{message}；任务级清理被拒绝，已转人工复核："
                        f"{cleanup_result.get('message') or '未知原因'}"
                    )
                elif cleanup_result.get("success") is False:
                    snapshot["cancel_cleanup_pending"] = True
                    snapshot["phase"] = PHASE_CANCEL_PENDING
                    phase = PHASE_CANCEL_PENDING
                    pending = True
                    message = f"{message}；任务级清理失败，等待重试"
                    snapshot["error_code"] = "CANCEL_CLEANUP_PENDING"
        elif pending:
            snapshot["cancel_cleanup_pending"] = bool(snapshot.get("cancel_cleanup_pending"))
        if pending:
            snapshot["error_code"] = "CANCEL_RESULT_PENDING"
            if snapshot.get("cancel_cleanup_pending"):
                snapshot["error_code"] = "CANCEL_CLEANUP_PENDING"
            snapshot["message"] = message
        try:
            self.executions.save(
                execution,
                snapshot,
                phase=phase,
                resume_at=_after(5) if pending else None,
                last_error_code=str(snapshot.get("error_code") or ""),
                last_error_message=message,
                claim_owner_id=claim_owner or None,
            )
        except ExecutionConflictError:
            return {
                "worker_outcome": "deferred",
                "phase": phase,
                "delay_seconds": 5,
                "message": "取消状态已被其他 Worker 更新，等待重新核对",
                "cancelled": True,
            }
        if pending:
            return {
                "worker_outcome": "deferred",
                "phase": PHASE_CANCEL_PENDING,
                "delay_seconds": 5,
                "message": message,
                "cancelled": True,
                "external_operation_started": True,
            }
        return {
            "worker_outcome": "completed",
            "phase": phase,
            "message": message,
            "cancelled": phase == PHASE_CANCELLED,
            "external_operation_started": True,
        }

    def _cleanup_cancelled_artifacts(
        self,
        job: dict[str, Any],
        *,
        allow_target_partial: bool,
    ) -> dict[str, Any] | None:
        """Run only task-scoped V1 cleanup after cancellation is confirmed."""

        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        cancel = raw_data.get("cancel") if isinstance(raw_data.get("cancel"), dict) else {}
        if not bool(cancel.get("active")) or not bool(cancel.get("cleanup_requested")):
            return None
        cleanup = getattr(self.rclone_service, "cleanup_cancelled_task", None)
        if not callable(cleanup):
            return {
                "success": True,
                "skipped": True,
                "message": "当前 V1 策略未提供任务级取消清理器",
                "items": [],
                "warnings": [],
                "errors": [],
            }
        try:
            result = cleanup(
                job=job,
                delete_source=bool(cancel.get("delete_source_requested", True)),
                delete_temp=bool(cancel.get("delete_temp_requested", True)),
                delete_target_partial=(
                    bool(cancel.get("delete_target_partial_requested", True))
                    and bool(allow_target_partial)
                ),
                stop_running=False,
                include_title_matches=bool(
                    cancel.get(
                        "include_title_matches_requested",
                        cancel.get("include_title_matches", True),
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"任务级取消清理异常：{exc}",
                "items": [],
                "warnings": [],
                "errors": [str(exc)],
            }
        if isinstance(result, dict):
            normalized = dict(result)
            # Missing success is an indeterminate cleanup result. Fail closed
            # so the durable execution stays in ``cancel_pending`` instead of
            # pretending destructive cleanup completed.
            normalized.setdefault("success", False)
            normalized.setdefault("items", [])
            normalized.setdefault("warnings", [])
            normalized.setdefault("errors", [])
            return normalized
        return {
            "success": bool(result),
            "message": "任务级取消清理完成" if result else "任务级取消清理未确认",
            "items": [],
            "warnings": [],
            "errors": [],
        }

    @staticmethod
    def _durable_receipt(
        execution: Any,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a non-empty receipt, preferring the indexed DB column.

        A receipt is the external side-effect fence. Treat even an incomplete
        non-empty receipt conservatively: it must never be downgraded to the
        pre-submit cleanup path merely because ``snapshot_json`` is stale.
        """

        value = getattr(execution, "receipt", None)
        if not (
            isinstance(value, dict)
            and any(str(item or "").strip() for item in value.values())
        ):
            value = (snapshot or {}).get("receipt")
        if not (
            isinstance(value, dict)
            and any(str(item or "").strip() for item in value.values())
        ):
            return None
        return dict(value)

    @staticmethod
    def _safe_error(prefix: str, exc: Exception) -> str:
        text = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
        # Avoid persisting full URLs, cookies, or arbitrary provider payloads
        # in the job error column.  The trace remains in server logs.
        for marker in ("password=", "cookie=", "token=", "Authorization:"):
            if marker.lower() in text.lower():
                text = text[: text.lower().index(marker.lower())] + "[redacted]"
                break
        return f"{prefix}：{text[:800] or exc.__class__.__name__}"

    def _persist_manual_review(
        self,
        execution: Any,
        message: str,
        *,
        claim_owner_id: str | None = None,
    ) -> None:
        if execution is None or str(getattr(execution, "phase", "")) == PHASE_MANUAL_REVIEW:
            return
        snapshot = dict(getattr(execution, "snapshot", {}) or {})
        review_from = str(snapshot.get("phase") or getattr(execution, "phase", "") or "planning")
        snapshot.update(
            {
                "phase": PHASE_MANUAL_REVIEW,
                "review_from": review_from,
                "message": message,
                "error_code": "RESULT_UNKNOWN",
            }
        )
        try:
            self.executions.save(
                execution,
                snapshot,
                phase=PHASE_MANUAL_REVIEW,
                last_error_code="RESULT_UNKNOWN",
                last_error_message=message,
                claim_owner_id=claim_owner_id,
                job_status=JOB_REVIEW,
                job_error_message=message,
            )
        except Exception:
            return

    def dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """Synchronous single-step entry used only by tests and diagnostics."""
        result = self.handle_worker({"job_id": int(job.get("id") or 0)}, {})
        return {"success": result.get("worker_outcome") != "business_failed", **result}

    def _orchestrator(self, plan: dict[str, Any]) -> TransferOrchestrator:
        return TransferOrchestrator(
            plan,
            self.build_executors(plan),
            verifier=self.verifier,
            cleanup=self.cleanup,
            ack_service=self.ack,
            organizer_service=self.organizer,
            finalizer_service=self.finalizer,
        )

    def _validation_deps(self) -> Any:
        from ..planning.plan_validator import PlanValidationDeps

        return PlanValidationDeps(
            accounts_service=self.accounts_service,
            target_service=self.target_service,
            mount_service=self.mount_service,
            registry=self.registry,
            source_record_service=self.source_record_service,
            assignment_service=self.assignment_service,
            config={"FNOS_LOCAL_TARGET_ROOT": self.local_target_root},
        )

    def _source_visible(self, plan: dict[str, Any], _receipt: Any) -> Any:
        bindings = plan.get("bindings") if isinstance(plan.get("bindings"), dict) else {}
        paths = plan.get("paths") if isinstance(plan.get("paths"), dict) else {}
        landing = paths.get("source_landing") if isinstance(paths.get("source_landing"), dict) else {}
        mount_id = str(bindings.get("source_mount_id") or "")
        mount_revision = int(bindings.get("source_mount_revision") or 0)
        path = str(landing.get("path") or "")
        if not mount_id or not path:
            return "unknown"
        try:
            if not self._mount_matches_frozen(
                mount_id,
                mount_revision,
                expected_namespace=str(landing.get("namespace_id") or ""),
                expected_connection_id=str(landing.get("connection_id") or ""),
                expected_connection_revision=int(landing.get("connection_revision") or 0),
                expected_storage_identity_id=str(landing.get("storage_identity_id") or ""),
                expected_identity_root=str(landing.get("identity_root") or ""),
            ):
                return "unknown"
            result = self.mount_service.list_path(
                mount_id,
                path,
                revision=mount_revision,
            )
        except Exception:  # noqa: BLE001 - visibility retry is bounded by the state machine
            return "pending"
        if result.get("unknown"):
            return "unknown"
        return "visible" if result.get("exists") else "pending"

    def _list_target(self, descriptor: dict[str, str]) -> dict[str, Any]:
        namespace = str(descriptor.get("namespace_id") or "")
        path = str(descriptor.get("path") or "")
        if namespace.startswith("openlist:"):
            parts = namespace.split(":", 2)
            if len(parts) != 3:
                return {"unknown": True, "message": "OpenList namespace 非法"}
            mount_id = str(descriptor.get("mount_id") or "").strip()
            revision = int(descriptor.get("mount_revision") or 0)
            if not mount_id or mount_id != parts[2] or revision <= 0 or not self._mount_matches_frozen(
                mount_id,
                revision,
                expected_namespace=namespace,
                expected_connection_id=str(descriptor.get("connection_id") or ""),
                expected_connection_revision=int(descriptor.get("connection_revision") or 0),
                expected_storage_identity_id=str(descriptor.get("storage_identity_id") or ""),
                expected_identity_root=str(descriptor.get("identity_root") or ""),
            ):
                return {"unknown": True, "message": "OpenList 目标缺少或无法解析冻结挂载修订"}
            return self.mount_service.list_path(mount_id, path, revision=revision)
        if namespace.startswith("local:"):
            real = os.path.realpath(path)
            if os.path.commonpath([real, self.local_target_root]) != self.local_target_root:
                return {"unknown": True, "message": "本地目标越界"}
            if not os.path.exists(real):
                return {"exists": False}
            count = 0
            size = 0
            for current, _dirs, files in os.walk(real):
                for name in files:
                    count += 1
                    size += os.path.getsize(os.path.join(current, name))
            return {"exists": True, "file_count": count, "total_size": size}
        return {"unknown": True, "message": "原生目标必须由 Provider 或 Organizer 校验"}

    def _mount_matches_frozen(
        self,
        mount_id: str,
        revision: int,
        *,
        expected_namespace: str = "",
        expected_connection_id: str = "",
        expected_connection_revision: int = 0,
        expected_storage_identity_id: str = "",
        expected_identity_root: str = "",
    ) -> bool:
        """Check the immutable mount revision before using a live client."""

        if not mount_id or revision <= 0:
            return False
        getter = getattr(self.mount_service, "get_revision", None)
        if not callable(getter):
            return False
        frozen = getter(mount_id, revision)
        if not isinstance(frozen, dict):
            return False
        if expected_namespace:
            namespace = str(frozen.get("namespace_id") or "")
            if namespace and namespace != expected_namespace:
                return False
        if expected_connection_id and str(frozen.get("connection_id") or "") != expected_connection_id:
            return False
        if expected_connection_revision and int(frozen.get("connection_revision") or 0) != int(expected_connection_revision):
            return False
        if expected_storage_identity_id and str(frozen.get("storage_identity_id") or "") != expected_storage_identity_id:
            return False
        if expected_identity_root:
            frozen_root = posixpath.normpath("/" + str(frozen.get("identity_root") or "").strip("/"))
            expected_root = posixpath.normpath("/" + str(expected_identity_root).strip("/"))
            if frozen_root != expected_root:
                return False
        return bool(frozen.get("enabled", True))

    @staticmethod
    def _delay(snapshot: dict[str, Any], needs_wait: bool) -> int:
        if not needs_wait:
            return 0
        phase_attempts = snapshot.get("phase_attempts")
        phase = str(snapshot.get("retry_from") or snapshot.get("phase") or "")
        try:
            attempt = int((phase_attempts or {}).get(phase) or 0) if isinstance(phase_attempts, dict) else 0
        except (TypeError, ValueError):
            attempt = 0
        return min(300, max(2, 5 * (2**attempt)))

    @staticmethod
    def _job_projection(phase: str, message: str) -> tuple[str, str]:
        if phase == PHASE_COMPLETED:
            status = JOB_DONE
        elif phase == PHASE_FAILED:
            status = JOB_FAILED
        elif phase == PHASE_MANUAL_REVIEW:
            status = JOB_REVIEW
        elif phase == PHASE_CANCELLED:
            status = JOB_CANCELLED
        elif phase == PHASE_ORGANIZING:
            status = JOB_ORGANIZING
        else:
            status = JOB_TRANSFERRING
        return status, message if status in {JOB_FAILED, JOB_REVIEW} else ""

    @staticmethod
    def _submission_operation_key(plan: dict[str, Any], phase: str) -> str:
        """Return the exact external-operation key for a phase.

        The durable submission fence and the receipt metadata must use one
        canonical key.  Provider-native materialization and relay download
        deliberately carry a namespaced key so their receipts cannot be
        confused with the later transfer/upload operation.
        """

        base = str((plan.get("transfer") or {}).get("idempotency_key") or "").strip()
        strategy = str((plan.get("transfer") or {}).get("strategy") or "").strip().lower()
        if not base:
            return ""
        if phase == PHASE_SOURCE_MATERIALIZING:
            return f"landing:{base}"
        if phase == PHASE_TRANSFERRING and strategy == "relay_upload_provider":
            return f"relay-dl:{base}"
        return base


class _JobAckService:
    def __init__(self, database: Any, executions: Any = None) -> None:
        self.database = database
        self.executions = executions

    def ack(self, plan: Any) -> AckResult:
        job = self.database.job_queries.get(plan.job_id)
        if not job:
            return AckResult(False, "ACK 时任务不存在")
        if str(job.get("status") or "").lower() in {"cancelled", "canceled", "rejected"}:
            return AckResult(False, "任务已取消或拒绝，禁止 ACK")

        # ACK is an application-level commit fence.  It must refer to the
        # immutable plan stored for this job and may not be satisfied merely
        # because a job row exists.
        if self.executions is not None:
            try:
                frozen = self.executions.plan(int(plan.job_id))
            except Exception as exc:  # noqa: BLE001
                return AckResult(False, f"无法读取冻结执行计划：{exc}")
            plan_snapshot = getattr(plan, "raw", None)
            if not isinstance(frozen, dict) or not isinstance(plan_snapshot, dict) or frozen != plan_snapshot:
                return AckResult(False, "ACK 计划快照与冻结计划不一致")
            execution = self.executions.get(int(plan.job_id))
            if execution is None or not bool(execution.final_verified):
                return AckResult(False, "最终目录尚未完成校验，禁止 ACK")
        return AckResult(True, "应用 ACK 完成")


class _OrganizerAdapter:
    def __init__(self, database: Any, service: Any) -> None:
        self.database = database
        self.service = service

    def organize(self, plan: Any, *, organizer_task_id: int | None = None) -> OrganizeResult:
        if self.service is None:
            return OrganizeResult("failed", "Organizer 服务未装配", retryable=False)
        frozen_task_id = int(organizer_task_id or 0)
        with self.database.connect() as connection:
            if frozen_task_id > 0:
                row = connection.execute(
                    "SELECT id, status, error_message FROM organizer_tasks WHERE id=? AND job_id=?",
                    (frozen_task_id, plan.job_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id, status, error_message FROM organizer_tasks WHERE job_id=? ORDER BY id DESC LIMIT 1",
                    (plan.job_id,),
                ).fetchone()
        if row is None:
            job = self.database.job_queries.get(plan.job_id) or {}
            result = self.service.enqueue_from_completed_directory(
                job=job,
                category_label=str(job.get("category_label") or ""),
                root_path=str((plan.organizer_path or {}).get("path") or ""),
                payload={"v1_transfer": True},
                trigger_type="v1_transfer",
            )
            if not result.get("success") or result.get("skipped"):
                return OrganizeResult("failed", str(result.get("message") or "Organizer 创建失败"), retryable=False)
            task = result.get("task") if isinstance(result.get("task"), dict) else {}
            task_id = int(
                result.get("organizer_task_id")
                or result.get("task_id")
                or task.get("id")
                or 0
            )
            return OrganizeResult("pending", "Organizer 已创建", task_id=task_id)
        status = str(row["status"] or "").lower()
        if status in {"done", "completed", "applied", "success"}:
            with self.database.connect() as connection:
                mappings = connection.execute(
                    "SELECT target_path FROM organizer_mappings WHERE task_id=? ORDER BY target_path",
                    (int(row["id"]),),
                ).fetchall()
            final_path = _common_target_root(
                [str(item["target_path"] or "") for item in mappings]
            )
            if not final_path:
                return OrganizeResult(
                    "unknown",
                    "Organizer 已完成但缺少最终 target_path 映射",
                    retryable=False,
                    task_id=int(row["id"]),
                )
            return OrganizeResult(
                "done",
                "Organizer 完成",
                task_id=int(row["id"]),
                final_path=final_path,
            )
        if status in {"failed", "cancelled", "error"}:
            return OrganizeResult("failed", str(row["error_message"] or "Organizer 失败"), retryable=False, task_id=int(row["id"]))
        if status in {"review", "manual_review"}:
            return OrganizeResult("unknown", "Organizer 等待人工复核", retryable=False, task_id=int(row["id"]))
        return OrganizeResult("pending", "Organizer 进行中", task_id=int(row["id"]))


def _after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).isoformat(timespec="seconds")


def _seconds_until(value: str | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        due = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    remaining = (due - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining + 0.999))


def _common_target_root(paths: list[str]) -> str:
    normalized = [
        str(path or "").replace("\\", "/").strip()
        for path in paths
        if str(path or "").strip()
    ]
    if not normalized:
        return ""
    parents = [posixpath.dirname(path) or "/" for path in normalized]
    try:
        return posixpath.commonpath(parents)
    except ValueError:
        return ""
