"""Durable V1 transfer composition and worker handler."""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..constants import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_REVIEW,
    JOB_TRANSFERRING,
)
from ..planning.plan_validator import validate_plan
from ..planning.job_plan import compute_persisted_plan_hash
from ..services.execution_plan_guard import reject_non_v1_job, v1_plan_or_review
from . import (
    AckResult,
    ErrorCode,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    OrganizeResult,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_FINALIZING,
    PHASE_MANUAL_REVIEW,
    PHASE_ORGANIZING,
    PHASE_PLANNING,
    PHASE_RETRY_WAIT,
    PHASE_SOURCE_MATERIALIZING,
    PHASE_NATIVE_SUBMITTING,
    PHASE_TARGET_VERIFYING,
    PHASE_TRANSFERRING,
    TransferError,
    TransferVerifyResult,
)
from .cleanup import (
    CompositeFilesystemOps,
    LocalFilesystemOps,
    OpenListFilesystemOps,
    TransferCleanupService,
)
from .execution_repository import ExecutionConflictError, TransferExecutionRepository
from .finalizer import TransferFinalizer
from .job_gate import TransferJobGate
from .native_executor import NativeSourceMaterializer, NativeTransferExecutor
from .orchestrator import TransferOrchestrator, TransferStepResult
from .provider_relay_executor import ProviderRelayExecutor
from .rclone_adapter import RcloneOperationAdapter
from .rclone_executor import RcloneTransferExecutor, build_managed_copy_run
from .verifier import TransferVerifier


_LOGGER = logging.getLogger(__name__)

# The job gate protects only the phase that consumes download/upload/disk
# resources.  Waiting phases (native_waiting, source_waiting_visible,
# retry_wait) and organizer/final verification deliberately release it so a
# slow external wait cannot become a head-of-line block for every later job.
_TRANSFER_GATE_PHASES = frozenset(
    {
        PHASE_PLANNING,
        PHASE_SOURCE_MATERIALIZING,
        PHASE_NATIVE_SUBMITTING,
        PHASE_TRANSFERRING,
        PHASE_TARGET_VERIFYING,
        PHASE_FINALIZING,
    }
)


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
        upload_log_bridge: Callable[[int, str], None] | None = None,
        relay_root: str = "/data/relay",
        local_target_root: str = "/storage-targets",
        execution_lease_seconds: int = 600,
        source_visibility_timeout_seconds: int | None = None,
        native_wait_timeout_seconds: int | None = None,
        transfer_wait_timeout_seconds: int | None = None,
        cancel_timeout_seconds: int | None = None,
        ensure_remote_confirmation: Callable[[], Any] | None = None,
        transfer_gate: Any | None = None,
        transfer_gate_lease_seconds: int = 900,
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
        self.transfer_gate = (
            transfer_gate
            if transfer_gate is not None
            else TransferJobGate(
                getattr(database, "scheduler_leases", None),
                ttl_seconds=transfer_gate_lease_seconds,
            )
        )
        self.organizer_service = organizer_service
        self.upload_log_bridge = upload_log_bridge
        self.ensure_remote_confirmation = ensure_remote_confirmation
        self.relay_root = os.path.realpath(relay_root)
        self.local_target_root = os.path.realpath(local_target_root)
        self.execution_lease_seconds = max(60, int(execution_lease_seconds))
        self.source_visibility_timeout_seconds = _configured_seconds(
            source_visibility_timeout_seconds,
            "TRANSFER_SOURCE_VISIBLE_TIMEOUT_SECONDS",
            1800,
            60,
            7 * 24 * 3600,
        )
        self.native_wait_timeout_seconds = _configured_seconds(
            native_wait_timeout_seconds,
            "TRANSFER_NATIVE_WAIT_TIMEOUT_SECONDS",
            7 * 24 * 3600,
            60,
            90 * 24 * 3600,
        )
        self.transfer_wait_timeout_seconds = _configured_seconds(
            transfer_wait_timeout_seconds,
            "TRANSFER_WAIT_TIMEOUT_SECONDS",
            3 * 3600,
            60,
            30 * 24 * 3600,
        )
        self.cancel_timeout_seconds = _configured_seconds(
            cancel_timeout_seconds,
            "TRANSFER_CANCEL_TIMEOUT_SECONDS",
            3600,
            60,
            7 * 24 * 3600,
        )
        # Shared only by executors created by this dispatch composition root;
        # a fresh app/test composition must not inherit another process'
        # circuit-breaker state. Entries include Provider/account/revision;
        # late failures from old plans cannot stop newly authenticated work.
        self._halted_relay_account_ids: set[str] = set()
        local_ops = LocalFilesystemOps((self.relay_root, self.local_target_root))
        self.cleanup = TransferCleanupService(
            CompositeFilesystemOps(
                openlist_ops=OpenListFilesystemOps(mount_service),
                local_ops=local_ops,
            ),
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
                    copy_manifest_verifier=self._verify_rclone_copy_manifest,
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
            job_id = int(plan.get("job_id") or 0)
            return {
                strategy: ProviderRelayExecutor(
                    rclone_adapter,
                    uploader,
                    source_materializer=materializer,
                    visibility_poller=lambda receipt: self._source_visible(plan, receipt),
                    relay_root=self.relay_root,
                    halted_account_ids=self._halted_relay_account_ids,
                    progress_sink=self._upload_progress_bridge(job_id),
                    event_sink=self._job_event_sink(job_id),
                    cancel_requested=lambda: self._relay_cancel_requested(job_id),
                )
            }
        raise TransferDispatchError(f"未知传输策略：{strategy}")

    def _relay_cancel_requested(self, job_id: int) -> bool:
        job = self.database.job_queries.get(job_id)
        return not isinstance(job, dict) or str(job.get("status") or "").lower() in {"cancelled", "canceled", "rejected"}

    def _upload_progress_bridge(self, job_id: int) -> Any:
        """上传进度行送进 rclone 实时日志流（与下载阶段同一页面）。

        桥接回调由组装根注入：分体部署时经 Worker 控制通道转发到 Worker
        的日志缓冲；桥接失败静默丢弃，绝不拖累上传主流程。
        """
        bridge = self.upload_log_bridge
        if job_id <= 0 or bridge is None:
            return None

        def sink(message: str) -> None:
            try:
                bridge(job_id, message)
            except Exception:  # noqa: BLE001
                pass

        return sink

    def _job_event_sink(self, job_id: int) -> Any:
        """上传里程碑（开始/完成/失败）写任务事件时间线；高频进度不进这里。"""

        if job_id <= 0:
            return None
        job_commands = getattr(self.database, "job_commands", None)
        add_event = getattr(job_commands, "add_event", None)
        if not callable(add_event):
            return None

        def sink(level: str, message: str) -> None:
            try:
                add_event(job_id, level, message, None)
            except Exception:  # noqa: BLE001
                pass

        return sink

    def _publish_transfer_phase(self, job_id: int, phase: str, message: str = "") -> None:
        """Publish committed job milestones, separate from rclone process success."""
        bridge = getattr(self, "upload_log_bridge", None)
        if not callable(bridge):
            return
        try:
            if phase == PHASE_COMPLETED:
                job = self.database.job_queries.get(job_id)
                if not isinstance(job, dict) or str(job.get("status") or "") not in {JOB_DONE, "success", "completed"}:
                    return
            labels = {
                "planning": "准备入库", "source_materializing": "来源转存",
                "source_waiting_visible": "等待来源可见", "native_submitting": "提交网盘任务",
                "native_waiting": "等待网盘任务完成", "transferring": "跨盘搬运",
                "target_verifying": "校验上传结果", "organizing": "整理文件",
                "finalizing": "归档文件", "final_verifying": "最终文件校验",
                "ack_pending": "确认入库结果", "cleanup_pending": "清理中继文件",
                "completed": "入库完成", "failed": "入库失败", "manual_review": "等待人工复核",
                "cancelled": "任务已取消", "cancel_pending": "等待取消确认", "retry_wait": "原任务等待重试",
            }
            if phase not in labels:
                return
            detail = self._safe_error(labels[phase], RuntimeError(message)) if message else labels[phase]
            bridge(job_id, f"入库阶段：{phase}；{detail}")
        except Exception:  # noqa: BLE001 - live feedback cannot change a committed business result
            pass

    def handle_worker(self, payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        """Run one durable worker step under the single-transfer-job gate."""

        job_id = int(payload.get("job_id") or 0)
        result: dict[str, Any] | None = None
        try:
            result = self._handle_worker(payload, _task)
            return result
        finally:
            # Every early return above still goes through this release.  If
            # the execution is still in a gated phase the helper keeps the
            # lease (and refreshes it on the next worker step); once it reaches
            # organizing or a terminal/waiting phase the next job may start.
            result_phase = str((result or {}).get("phase") or "")
            self._release_transfer_gate_if_idle(job_id, result_phase=result_phase)

    def _handle_worker(self, payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
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
            self._release_transfer_gate(job_id)
            return {
                "worker_outcome": "deferred",
                "phase": execution.phase,
                "delay_seconds": due_in,
                "message": "执行状态仍在退避等待期",
            }

        # A job-level admission gate, not the worker thread count, is what
        # makes the pipeline one transfer job at a time.  Do this before
        # claiming the execution row: a queued job must not hold a claim while
        # it waits for the active job to finish its download/upload phase.
        if execution.phase in _TRANSFER_GATE_PHASES:
            acquired, active_job_id = self._acquire_transfer_gate(job_id)
            if not acquired:
                detail = (
                    f"前一个入库任务 #{active_job_id} 尚未完成下载/上传"
                    if active_job_id > 0
                    else "传输队列锁暂时不可用"
                )
                return {
                    "worker_outcome": "deferred",
                    "phase": execution.phase,
                    "delay_seconds": 15,
                    "message": f"{detail}，当前任务排队等待",
                }
        else:
            # Organizer/final-verification/recovery phases do not consume the
            # transfer slot; release it immediately for the next queued job.
            self._release_transfer_gate(job_id)

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

        loaded_phase = str(execution.phase)
        cancellation_result = self._stop_if_job_cancelled(job, execution, claim_owner)
        if cancellation_result is not None:
            self._publish_transfer_phase(job_id, str(cancellation_result.get("phase") or ""), str(cancellation_result.get("message") or ""))
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
        if loaded_phase == PHASE_RETRY_WAIT:
            self._publish_transfer_phase(job_id, PHASE_RETRY_WAIT, "保留原任务记录，从暂停阶段继续")
        unsafe_phases = {
            PHASE_SOURCE_MATERIALIZING,
            PHASE_NATIVE_SUBMITTING,
            PHASE_TRANSFERRING,
        }
        preflight_result: TransferStepResult | None = None
        if orchestrator.state.phase in unsafe_phases and orchestrator.state.receipt is None:
            fence = dict(orchestrator.state.submission_fence or {})
            if fence.get("state") == "prepared" or (
                orchestrator.state.phase == PHASE_TRANSFERRING and bool(fence)
            ):
                # Old committed landing markers are not proof that a later
                # copy never started. Only the new explicit hand-off separates
                # source evidence from the current submission fence.
                previous_message = str(
                    orchestrator.state.message or getattr(execution, "last_error_message", "") or ""
                ).strip()
                orchestrator.state.review_from = orchestrator.state.phase
                orchestrator.state.phase = PHASE_MANUAL_REVIEW
                orchestrator.state.error_code = ErrorCode.RESULT_UNKNOWN
                orchestrator.state.submission_preflight = {}
                orchestrator.state.message = (
                    "外部提交 fence 已存在但 receipt 缺失，提交结果无法确认；"
                    "为避免重复转存，任务进入人工复核"
                )
                if previous_message and previous_message not in {"外部提交 fence 已持久化", "计划校验通过", "开始重试"}:
                    orchestrator.state.message += "；" + self._safe_error("此前记录", RuntimeError(previous_message))
                self._record_submission_diagnostic(
                    job_id, orchestrator.state.review_from, orchestrator.state.message,
                )
            else:
                if orchestrator.state.phase == PHASE_TRANSFERRING:
                    preflight_result = self._prepare_transfer_submission(orchestrator, plan, job_id)
                if orchestrator.state.phase in unsafe_phases:
                    # A not-started preflight record applies only before this
                    # fence. It must not survive an actual submission attempt.
                    orchestrator.state.submission_preflight = {}
                    orchestrator.state.submission_fence = {
                        "state": "prepared",
                        "phase": orchestrator.state.phase,
                        "operation_key": self._submission_operation_key(plan, orchestrator.state.phase),
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
        if preflight_result is None and orchestrator.state.phase == PHASE_RETRY_WAIT:
            orchestrator.mark_retry_ready()
        step_phase = orchestrator.state.phase
        claim_holder = {"execution": execution, "lost": False}
        try:
            with self._claim_heartbeat(job_id, claim_owner, claim_holder):
                # A newly scheduled preflight backoff must be persisted, not
                # immediately marked ready and consumed by this same wake-up.
                result = preflight_result if preflight_result is not None else orchestrator.step()
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
        if (
            step_phase in unsafe_phases
            and orchestrator.state.phase == PHASE_RETRY_WAIT
            and orchestrator.state.receipt is None
            and orchestrator.state.submission_fence.get("state") == "prepared"
        ):
            # Record the first submission error while it is still available;
            # retry_wait itself only moves state and must not emit it again.
            self._record_submission_diagnostic(
                job_id, step_phase,
                self._safe_error("提交未获得回执，已保留防重复栅栏", RuntimeError(result.message)),
            )
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
        except ExecutionConflictError as exc:
            # This also covers receipt/fence validation errors, not just CAS
            # contention. Preserve the real cause without resubmitting.
            message = self._safe_error("执行状态保存失败，已停止本次推进", exc)
            self._record_submission_diagnostic(job_id, snapshot.get("phase") or execution.phase, message)
            return {
                "worker_outcome": "deferred",
                "phase": snapshot.get("phase") or execution.phase,
                "delay_seconds": 5,
                "message": message,
            }
        if snapshot["phase"] != loaded_phase:
            self._publish_transfer_phase(job_id, snapshot["phase"], result.message)
        if snapshot["phase"] in {PHASE_MANUAL_REVIEW, PHASE_FAILED} and (
            snapshot.get("review_from") if snapshot["phase"] == PHASE_MANUAL_REVIEW else snapshot.get("failure_from")
        ) == PHASE_ORGANIZING:
            # Organizer may finish after our waiting-review read but before
            # this save. Reconcile that completion after persisting the park
            # so neither ordering can lose the only recovery wake-up.
            resume = getattr(self.executions, "resume_after_organizer_completion", None)
            if callable(resume) and resume(job_id, int(snapshot.get("organizer_task_id") or 0)):
                return {
                    "worker_outcome": "deferred", "phase": PHASE_RETRY_WAIT,
                    "delay_seconds": 0, "message": "Organizer 已完成，继续最终文件校验与 ACK",
                }
        if snapshot["phase"] == PHASE_COMPLETED:
            return {"worker_outcome": "completed", "phase": PHASE_COMPLETED, "message": result.message}
        if snapshot["phase"] == PHASE_CANCELLED:
            return {"worker_outcome": "completed", "phase": PHASE_CANCELLED, "message": result.message, "cancelled": True}
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

    def _prepare_transfer_submission(
        self, orchestrator: TransferOrchestrator, plan: dict[str, Any], job_id: int,
    ) -> TransferStepResult | None:
        """Confirm Remote setup and preflight before any copy can be submitted."""
        confirmation_retryable = False
        try:
            confirm = getattr(self, "ensure_remote_confirmation", None)
            if callable(confirm):
                try:
                    confirm()
                except Exception as exc:  # noqa: BLE001 - remote setup must complete before any copy submission
                    try:
                        status_code = int(getattr(exc, "status_code", 0) or 0)
                    except (TypeError, ValueError):
                        status_code = 0
                    confirmation_retryable = getattr(exc, "retryable", False) is True or 500 <= status_code < 600
                    raise TransferError(
                        ErrorCode.OPENLIST_UNAVAILABLE,
                        f"托管 OpenList Remote 自动确认失败：{exc}",
                    ) from exc
            orchestrator.prepare_submission()
        except Exception as exc:  # noqa: BLE001 - this boundary cannot submit a transfer
            code = exc.code if isinstance(exc, TransferError) else ErrorCode.INVALID_PLAN
            message = self._safe_error("搬运前置校验未通过，尚未启动本次搬运", exc)
            orchestrator.state.submission_preflight = {
                "version": 1,
                "job_id": job_id,
                "phase": PHASE_TRANSFERRING,
                "state": "not_started",
                "operation_key": self._submission_operation_key(plan, PHASE_TRANSFERRING),
                "error_code": code,
                "message": message,
                "retryable": confirmation_retryable,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._record_submission_diagnostic(job_id, PHASE_TRANSFERRING, message)
            if confirmation_retryable:
                return orchestrator._handle_failure(
                    TransferError(code, message, retryable=True),
                    PHASE_TRANSFERRING,
                    exhausted_to_manual_review=True,
                )
            return orchestrator._to_manual_review(message, review_from=PHASE_TRANSFERRING, error_code=code)
        return None

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
                gate = getattr(self, "transfer_gate", None)
                renew_gate = getattr(gate, "renew", None)
                if callable(renew_gate):
                    try:
                        holder["gate_owned"] = bool(renew_gate(job_id))
                    except Exception as exc:  # noqa: BLE001
                        holder["gate_heartbeat_error"] = str(exc)
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

    def _acquire_transfer_gate(self, job_id: int) -> tuple[bool, int]:
        gate = getattr(self, "transfer_gate", None)
        if gate is None:
            return True, int(job_id)
        self._release_stale_transfer_gate()
        try:
            acquired, active = gate.try_acquire(int(job_id))
        except Exception as exc:  # noqa: BLE001 - a queue gate must fail closed
            _LOGGER.warning("transfer job gate acquire failed job_id=%s error=%s", job_id, exc)
            return False, 0
        return bool(acquired), max(0, int(active or 0))

    def _release_stale_transfer_gate(self) -> None:
        """Clear a lease left behind by a cancelled/crashed previous holder."""

        gate = getattr(self, "transfer_gate", None)
        active_method = getattr(gate, "active_job_id", None)
        if not callable(active_method):
            return
        try:
            active = int(active_method() or 0)
        except Exception:  # noqa: BLE001 - diagnostics only
            return
        if active <= 0:
            return
        try:
            execution = self.executions.get(active)
        except Exception:  # noqa: BLE001 - cannot prove the holder is stale
            return
        phase = str(getattr(execution, "phase", "") or "") if execution is not None else ""
        stale = execution is None or phase not in _TRANSFER_GATE_PHASES
        if not stale:
            try:
                job = self.database.job_queries.get(active)
            except Exception:  # noqa: BLE001
                job = None
            if isinstance(job, dict):
                status = str(job.get("status") or "").strip().lower()
                stale = status in {
                    JOB_DONE,
                    JOB_FAILED,
                    JOB_CANCELLED,
                    JOB_REVIEW,
                }
        if stale:
            self._release_transfer_gate(active)

    def _release_transfer_gate(self, job_id: int) -> None:
        gate = getattr(self, "transfer_gate", None)
        if gate is None or int(job_id or 0) <= 0:
            return
        try:
            gate.release(int(job_id))
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the step result
            _LOGGER.warning("transfer job gate release failed job_id=%s error=%s", job_id, exc)

    def _release_transfer_gate_if_idle(
        self,
        job_id: int,
        *,
        result_phase: str = "",
    ) -> None:
        """Release the gate once the durable execution leaves transfer phases."""

        normalized = int(job_id or 0)
        if normalized <= 0 or getattr(self, "transfer_gate", None) is None:
            return
        if result_phase and result_phase not in _TRANSFER_GATE_PHASES:
            self._release_transfer_gate(normalized)
            return
        try:
            job = self.database.job_queries.get(normalized)
        except Exception:  # noqa: BLE001 - the step result is already decided
            job = None
        if isinstance(job, dict):
            status = str(job.get("status") or "").strip().lower()
            if status in {
                JOB_DONE,
                JOB_FAILED,
                JOB_CANCELLED,
                JOB_REVIEW,
            }:
                self._release_transfer_gate(normalized)
                return
        try:
            execution = self.executions.get(normalized)
        except Exception:  # noqa: BLE001 - the step result is already decided
            return
        phase = str(getattr(execution, "phase", "") or "") if execution is not None else ""
        if execution is None or phase not in _TRANSFER_GATE_PHASES:
            self._release_transfer_gate(normalized)

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
        fence = snapshot.get("submission_fence")
        if isinstance(fence, dict) and fence.get("state") in {"prepared", "committed"}:
            message = "外部提交已有 fence 但回执缺失，无法确认取消；保留来源、缓存与目标文件并转人工复核"
            self._persist_manual_review(execution, message, claim_owner_id=claim_owner or None)
            return {
                "worker_outcome": "completed", "phase": PHASE_MANUAL_REVIEW,
                "message": message, "manual_review": True, "cancelled": False,
            }

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
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(exc or "")).strip()
        # Log only a short diagnostic, never provider payloads or a traceback
        # that could carry URLs, share passwords, cookies or bearer tokens.
        text = re.sub(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+|magnet:\?[^\s<>\"']+", "[redacted-url]", text)
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[redacted-auth]", text)
        secret = re.search(
            r"(?i)\b(?:password|passwd|pwd(?:_id)?|cookie|set-cookie|authorization|"
            r"(?:access|refresh|auth|session)[_-]?token|token|secret|credentials?|"
            r"api[_-]?key|source_url|share_url|url|uri)[\"']?\s*[:=]",
            text,
        )
        if secret is not None:
            text = text[:secret.start()] + "[redacted]"
        return f"{prefix}：{text[:800] or exc.__class__.__name__}"

    def _record_submission_diagnostic(self, job_id: int, phase: str, message: str) -> None:
        phase = str(phase or "")
        safe_phase = phase if re.fullmatch(r"[a-z_]{1,64}", phase) else "unknown"
        safe_message = self._safe_error("V1 提交诊断", RuntimeError(message))
        _LOGGER.warning("job_id=%s phase=%s %s", int(job_id), safe_phase, safe_message)
        add_event = getattr(getattr(self.database, "job_commands", None), "add_event", None)
        if callable(add_event):
            try:
                add_event(job_id, "warn", safe_message, {"phase": safe_phase, "diagnostic": "submission"})
            except Exception:  # noqa: BLE001 - logging must not change side-effect decisions
                pass

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
        self._publish_transfer_phase(int(execution.job_id), PHASE_MANUAL_REVIEW, message)

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
            source_visibility_timeout_seconds=self.source_visibility_timeout_seconds,
            native_wait_timeout_seconds=self.native_wait_timeout_seconds,
            transfer_wait_timeout_seconds=self.transfer_wait_timeout_seconds,
            cancel_timeout_seconds=self.cancel_timeout_seconds,
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

    def _rclone_copy_manifest(self, plan: Any, receipt: Any, *, final: bool = False) -> dict[str, Any]:
        """Resolve the callback inventory only for this successful frozen run."""
        from ..services.rclone_service import RcloneService

        metadata = getattr(receipt, "metadata", None)
        if plan.strategy != "rclone_copy" or not isinstance(metadata, dict):
            raise ValueError("缺少 rclone 回执")
        run_id = int(metadata.get("run_id") or 0)
        if (
            run_id <= 0 or metadata.get("operation_key") != plan.idempotency_key
            or (metadata.get("job_id") is not None and int(metadata["job_id"]) != plan.job_id)
        ):
            raise ValueError("rclone 回执与冻结操作不一致")
        if self.executions.plan(plan.job_id) != plan.raw:
            raise ValueError("暂存核验计划与持久快照不一致")
        job = self.database.job_queries.get(plan.job_id) or {}
        if not job or str(job.get("status") or "").lower() in {"cancelled", "canceled", "rejected"}:
            raise ValueError("暂存核验关联任务不存在或已取消")
        run = self.database.rclone.get_rclone_run(run_id) or {}
        if (
            int(run.get("job_id") or 0) != plan.job_id
            or run.get("operation_key") != plan.idempotency_key
            or run.get("status") != "success"
            or run.get("exit_code") != 0
        ):
            raise ValueError("rclone 运行尚未成功、已取消或不属于本任务")
        expected = RcloneService._validated_copy_run(build_managed_copy_run(plan, self.locator_resolver))
        actual = RcloneService._validated_copy_run(run.get("copy_descriptor"))
        if not expected or actual != expected:
            raise ValueError("rclone 运行路径与冻结复制描述符不一致")
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        manifest = raw_data.get("rclone_staging_manifest")
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("缺少持久化 rclone 文件清单")
        run_ids = manifest.get("run_ids")
        if not isinstance(run_ids, list) or run_id not in run_ids or manifest.get("last_run_id") != run_id:
            raise ValueError("rclone 文件清单不属于本次成功运行")
        source_root = f"/{expected['source_root'].strip('/')}/{expected['job_dir_name']}"
        if manifest.get("job_root") != source_root:
            raise ValueError("rclone 文件清单来源根路径不一致")
        paths = manifest.get("source_paths")
        if not isinstance(paths, list) or not paths or manifest.get("expected_file_count") != len(paths):
            raise ValueError("rclone 文件清单为空或数量不一致")
        seen: set[str] = set()
        files: list[dict[str, Any]] = []
        staging = plan.destination_staging or {}
        destination = plan.destination_final if final else staging
        if not destination or destination.get("namespace_id") != staging.get("namespace_id"):
            raise ValueError("rclone 核验目标不属于冻结复制存储")
        target_root = str(destination.get("path") or "").rstrip("/")
        if not target_root:
            raise ValueError("rclone 核验目标路径缺失")
        for path in paths:
            if (
                not isinstance(path, str) or not path.startswith(source_root + "/")
                or any(part in {".", "..", ""} for part in path[1:].split("/"))
                or any(ord(char) < 32 for char in path) or "\\" in path
                or path in seen
            ):
                raise ValueError("rclone 文件清单含重复、越界或非法路径")
            seen.add(path)
            relative = path[len(source_root) + 1:]
            files.append({"path": f"{target_root}/{relative}", "size": None})
        return {
            "run_id": run_id, "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "manifest": manifest, "files": files, "destination": destination,
        }

    def _verify_rclone_copy_manifest(self, plan: Any, receipt: Any, *, final: bool = False) -> TransferVerifyResult:
        try:
            evidence = self._rclone_copy_manifest(plan, receipt, final=final)
            result = self.verifier.verify_manifest(descriptor=evidence["destination"], files=evidence["files"])
            if result.status == "ok" and self._rclone_copy_manifest(plan, receipt, final=final) != evidence:
                raise ValueError("rclone 暂存核验期间运行或清单发生变化")
            return result
        except Exception as exc:  # noqa: BLE001 - unproven copies must not reach Organizer/ACK
            return TransferVerifyResult("unknown", f"rclone 暂存清单核验失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)

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
            if descriptor.get("include_file_manifest"):
                client, mount = self.mount_service.client_for_mount(mount_id, revision=revision)
                absolute = self.mount_service.resolve_mount_absolute_path(mount, path)
                if not client.exists(absolute):
                    return {"exists": False, "files": []}
                return {
                    "exists": True,
                    "files": [
                        item for item in client.list_dir(absolute)
                        if isinstance(item, dict)
                        and str(item.get("path") or "") != absolute
                        and posixpath.dirname(str(item.get("path") or "")) == absolute
                    ],
                }
            return self.mount_service.list_path(mount_id, path, revision=revision)
        if namespace.startswith("local:"):
            real = os.path.realpath(path)
            if os.path.commonpath([real, self.local_target_root]) != self.local_target_root:
                return {"unknown": True, "message": "本地目标越界"}
            if not os.path.exists(real):
                return {"exists": False}
            if descriptor.get("include_file_manifest"):
                with os.scandir(real) as entries:
                    return {
                        "exists": True,
                        "files": [
                            {"name": item.name, "is_dir": not item.is_file(follow_symlinks=False),
                             "size": item.stat(follow_symlinks=False).st_size}
                            for item in entries
                        ],
                    }
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

    def _final_manifest(self, plan: Any, task_id: int) -> dict[str, Any]:
        """Read completed, job/plan-bound evidence; never infer it from a directory."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, job_id, status, revision, category, raw_data, evidence FROM organizer_tasks WHERE id=? AND job_id=?",
                (task_id, plan.job_id),
            ).fetchone()
            if row is None:
                raise ValueError("缺少本任务关联的 Organizer 完成记录")
            task = dict(row)
            if str(task["status"] or "") not in {"done", "completed", "applied", "success", "skipped"}:
                raise ValueError("Organizer 尚未完成或已被取消/回滚")
            raw = json.loads(task["raw_data"] or "{}")
            evidence = json.loads(task["evidence"] or "{}")
            reference = raw.get("execution_plan_ref") or {}
            if (
                reference.get("plan_hash") != compute_persisted_plan_hash(plan.raw)
                or int(reference.get("job_id") or 0) != plan.job_id
                or str(reference.get("strategy") or "") != plan.strategy
                or str(task["category"] or "") != plan.category_key
            ):
                raise ValueError("Organizer 完成记录与冻结任务计划不一致")
            confirmation = evidence.get("completion_confirmation") or {}
            if confirmation.get("success") is not True or confirmation.get("cleanup_only") or evidence.get("cleanup_only"):
                raise ValueError("Organizer 缺少入库文件完成确认")
            if connection.execute(
                "SELECT 1 FROM organizer_operations WHERE task_id=? AND status NOT IN ('done', 'skipped') LIMIT 1",
                (task_id,),
            ).fetchone():
                raise ValueError("Organizer 仍有未完成的文件操作")
            mappings = [dict(item) for item in connection.execute(
                "SELECT m.id, m.status, m.source_path, m.target_path, f.id AS source_file_id, f.size "
                "FROM organizer_mappings m LEFT JOIN organizer_files f "
                "ON f.id=m.file_id AND f.task_id=m.task_id AND f.path=m.source_path "
                "WHERE m.task_id=? ORDER BY m.id",
                (task_id,),
            ).fetchall()]
        if any(item["status"] not in {"ready", "skipped_existing", "delete_ad", "skipped"} for item in mappings):
            raise ValueError("Organizer 映射尚未全部确认")
        retained = [item for item in mappings if item["status"] in {"ready", "skipped_existing"}]
        if not retained or any(not item["source_file_id"] or not item["target_path"] for item in retained):
            raise ValueError("Organizer 缺少可关联源文件的最终映射")
        confirmed = confirmation.get("confirmed_paths")
        if not isinstance(confirmed, list) or not confirmed or not set(confirmed).issubset({item["target_path"] for item in retained}):
            raise ValueError("Organizer 最终映射与完成确认不一致")
        return {
            "task_id": task_id, "revision": task["revision"], "status": task["status"],
            "confirmation": confirmation, "mappings": mappings,
            "files": [{"path": item["target_path"], "size": item["size"]} for item in retained],
        }

    def verify_final(self, plan: Any, *, organizer_task_id: int, verifier: Any) -> TransferVerifyResult:
        verify = getattr(verifier, "verify_manifest", None)
        if not callable(verify):
            return TransferVerifyResult("unknown", "未配置任务级最终文件核验器", code=ErrorCode.RESULT_UNKNOWN)
        try:
            task_id = int(organizer_task_id or 0)
            manifest = self._final_manifest(plan, task_id)
            result = verify(descriptor=plan.organizer_path, files=manifest["files"])
            # A rollback or revised mapping can win while remote reads are in
            # flight. Re-read the evidence before allowing ACK/source cleanup.
            if result.status == "ok" and self._final_manifest(plan, task_id) != manifest:
                raise ValueError("Organizer 最终映射在核验期间发生变化")
            return result
        except Exception as exc:  # noqa: BLE001 - stale/missing evidence is not completion
            return TransferVerifyResult("unknown", f"Organizer 最终文件核验失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)

    def organize(self, plan: Any, *, organizer_task_id: int | None = None) -> OrganizeResult:
        if self.service is None:
            return OrganizeResult("failed", "Organizer 服务未装配", retryable=False)
        frozen_task_id = int(organizer_task_id or 0)
        with self.database.connect() as connection:
            if frozen_task_id > 0:
                row = connection.execute(
                    "SELECT id, status, error_message, evidence FROM organizer_tasks WHERE id=? AND job_id=?",
                    (frozen_task_id, plan.job_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id, status, error_message, evidence FROM organizer_tasks WHERE job_id=? ORDER BY id DESC LIMIT 1",
                    (plan.job_id,),
                ).fetchone()
        if row is None:
            job = self.database.job_queries.get(plan.job_id) or {}
            result = self.service.enqueue_from_completed_directory(
                job=job,
                category_label=str(job.get("category_label") or ""),
                root_path=str((plan.organizer_path or {}).get("path") or ""),
                payload={
                    "v1_transfer": True,
                    "scan_filters": _plan_scan_filters(plan, job),
                },
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
        if status in {"done", "completed", "applied", "success", "skipped"}:
            evidence = dict(row).get("evidence")
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (ValueError, TypeError):
                    evidence = {}
            evidence = evidence if isinstance(evidence, dict) else {}
            confirmation = evidence.get("completion_confirmation")
            confirmation = confirmation if isinstance(confirmation, dict) else {}
            if evidence.get("cleanup_only") is True or (
                confirmation.get("cleanup_only") is True and confirmation.get("success") is True
            ):
                return OrganizeResult(
                    "done",
                    "广告清理完成，没有保留的视频文件，本次未入库",
                    task_id=int(row["id"]),
                    cleanup_only=True,
                )
            with self.database.connect() as connection:
                mappings = connection.execute(
                    "SELECT target_path FROM organizer_mappings WHERE task_id=? AND status IN ('ready', 'skipped_existing') ORDER BY target_path",
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
                "Organizer 已完成（跳过整理）" if status == "skipped" else "Organizer 完成",
                task_id=int(row["id"]),
                final_path=final_path,
            )
        if status in {"failed", "cancelled", "error"}:
            return OrganizeResult("failed", str(row["error_message"] or "Organizer 失败"), retryable=False, task_id=int(row["id"]))
        if status in {"review", "manual_review", "waiting_review"}:
            return OrganizeResult("unknown", "Organizer 等待人工复核", retryable=False, task_id=int(row["id"]))
        return OrganizeResult("pending", "Organizer 进行中", task_id=int(row["id"]))


def _plan_scan_filters(plan: Any, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build scan_filters for the Organizer task from the frozen V1 plan.

    Native strategies land files directly on the category root.  Without a
    file manifest the Organizer refuses to recursively scan the whole
    category directory (correct fail-closed behaviour).  The plan's frozen
    selection snapshot is the authoritative manifest of what this job
    submitted, so it feeds expected_names/expected_paths here.

    Only category-root organizer paths receive filters: job-scoped roots
    (job-<id>) are already scanned exhaustively and a hand-built manifest
    might not match their verified layout.  File-mode
    native saves land flat on the category root, so every path is
    root/<name>; ``expected_count`` must equal the path count or the
    Organizer discards the manifest as incomplete.

    Folder selections are excluded because their nested contents are
    unknown at submit time.  For those jobs the rclone worker has already
    persisted the actual per-file manifest (callback_service stores it in
    ``raw_data.rclone_staging_manifest``), and the relay upload preserves
    that relative layout at the destination — so the manifest maps
    1:1 onto precise expected_paths at Organizer time.  Relay jobs that
    froze a native staging directory have a job-scoped Organizer root and
    never reach the manifest mapping below.
    """

    raw = getattr(plan, "raw", None)
    if not isinstance(raw, dict):
        return {}
    organizer_path = str((getattr(plan, "organizer_path", None) or {}).get("path") or "").strip()
    job_id = int(getattr(plan, "job_id", 0) or 0)
    if not organizer_path or job_id <= 0:
        return {}
    leaf = organizer_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if leaf.casefold() == f"job-{job_id}".casefold():
        return {}
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    selection = source.get("selection") if isinstance(source.get("selection"), dict) else {}
    names = _selection_manifest_names(selection)
    if not names:
        provider_selection = (
            selection.get("provider_selection")
            if isinstance(selection.get("provider_selection"), dict)
            else {}
        )
        for key in ("cloud139_selection", "tianyi_selection", "quark_selection", "generic_selection"):
            candidate = provider_selection.get(key)
            if isinstance(candidate, dict):
                names.extend(_selection_manifest_names(candidate))
    if not names:
        return _manifest_scan_filters(job or {}, organizer_path)
    root = "/" + organizer_path.replace("\\", "/").strip("/")
    expected_names: list[str] = []
    for name in names:
        if name not in expected_names:
            expected_names.append(name)
    expected_paths = [f"{root}/{name}" for name in expected_names]
    return {
        "expected_names": expected_names,
        "expected_paths": expected_paths,
        "expected_count": len(expected_paths),
    }


def _manifest_scan_filters(job: dict[str, Any], organizer_root: str) -> dict[str, Any]:
    """Derive precise expected_paths from the persisted rclone file manifest.

    文件夹形态的保存在提交时无法展开嵌套内容，selection 清单为空，Organizer
    会在分类根上拒绝无清单扫描。但中继下载前 rclone worker 已把真实文件清单
    回调固化到 ``raw_data.rclone_staging_manifest.source_paths``；job 根下的
    相对结构与上传到分类根后的布局一致（移动云上传按相对目录逐文件落盘），
    剥掉 job 根前缀即可得到精确 expected_paths。
    """

    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    manifest = (
        raw_data.get("rclone_staging_manifest")
        if isinstance(raw_data.get("rclone_staging_manifest"), dict)
        else {}
    )
    source_paths = (
        manifest.get("source_paths")
        if isinstance(manifest.get("source_paths"), list)
        else []
    )
    if not source_paths:
        return {}
    job_root = _normalize_manifest_filter_path(manifest.get("job_root"))
    job_root_folded = job_root.casefold()
    root = "/" + str(organizer_root or "").replace("\\", "/").strip("/")
    expected_paths: list[str] = []
    seen: set[str] = set()
    for value in source_paths:
        normalized = _normalize_manifest_filter_path(value)
        if not normalized:
            continue
        rel = normalized
        folded = normalized.casefold()
        if job_root and (folded == job_root_folded or folded.startswith(f"{job_root_folded}/")):
            rel = normalized[len(job_root) :].strip("/")
        if not rel:
            continue
        path = f"{root}/{rel}"
        if path.casefold() not in seen:
            seen.add(path.casefold())
            expected_paths.append(path)
    if not expected_paths:
        return {}
    return {
        "expected_paths": expected_paths,
        "expected_count": len(expected_paths),
    }


def _normalize_manifest_filter_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def _selection_manifest_names(selection: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("selected_files", "files", "selected_items"):
        values = selection.get(key) if isinstance(selection.get(key), list) else []
        for item in values:
            if not isinstance(item, dict):
                continue
            if bool(item.get("is_dir") or item.get("directory") or item.get("type") in {"dir", "folder"}):
                continue
            name = str(item.get("name") or item.get("fileName") or "").strip()
            if name:
                names.append(name)
    return names


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


def _configured_seconds(
    value: int | None,
    env_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    candidate: Any = value
    if candidate is None:
        candidate = os.getenv(env_name, str(default))
    try:
        parsed = int(candidate)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
