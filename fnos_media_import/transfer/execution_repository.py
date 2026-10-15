from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import (
    ErrorCode,
    IMMUTABLE_TERMINAL_PHASES,
    OPERATOR_RECOVERABLE_PHASES,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW,
    PHASE_PLANNING,
    PHASE_RETRY_WAIT,
    TERMINAL_PHASES,
)
from ..time_utils import utc_now_iso, utc_now_iso_offset

# A receipt represents an externally visible side effect.  It may only be
# cleared at the two explicit hand-off points below: after a source landing has
# become visible (the next receipt belongs to rclone), or when a bounded retry
# intentionally returns to a submission phase.  Any other disappearance is a
# stale/corrupt snapshot and must fail closed.
_RECEIPT_CLEAR_TRANSITIONS = frozenset(
    {
        ("source_waiting_visible", "transferring"),
        ("retry_wait", "source_materializing"),
        ("retry_wait", "native_submitting"),
        ("retry_wait", "transferring"),
    }
)

# The durable worker must never be able to move an execution backwards by
# writing a stale snapshot.  Keeping this table in the repository makes the
# database boundary defensive even when a caller bypasses the orchestrator.
_ALLOWED_PHASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"planning", "source_materializing", "native_submitting", "transferring", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "source_materializing": frozenset({"source_materializing", "source_waiting_visible", "native_submitting", "transferring", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "source_waiting_visible": frozenset({"source_waiting_visible", "native_submitting", "transferring", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "native_submitting": frozenset({"native_submitting", "native_waiting", "target_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "native_waiting": frozenset({"native_waiting", "target_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "transferring": frozenset({"transferring", "target_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "target_verifying": frozenset({"target_verifying", "organizing", "finalizing", "final_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "organizing": frozenset({"organizing", "finalizing", "final_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "finalizing": frozenset({"finalizing", "final_verifying", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "final_verifying": frozenset({"final_verifying", "ack_pending", "failed", "manual_review", "cancel_pending", "retry_wait", "cancelled"}),
    "ack_pending": frozenset({"ack_pending", "final_verifying", "cleanup_pending", "completed", "failed", "manual_review", "cancel_pending", "cancelled", "retry_wait"}),
    "cleanup_pending": frozenset({"cleanup_pending", "completed", "failed", "manual_review", "retry_wait"}),
    "retry_wait": frozenset({"retry_wait", "planning", "source_materializing", "native_submitting", "native_waiting", "transferring", "target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending", "cleanup_pending", "failed", "manual_review", "cancel_pending", "cancelled"}),
    "cancel_pending": frozenset({"cancel_pending", "cancelled", "failed", "manual_review", "retry_wait"}),
    "manual_review": frozenset({"manual_review", "retry_wait"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed", "retry_wait", "planning", "source_materializing", "native_submitting", "transferring", "target_verifying", "manual_review"}),
    "cancelled": frozenset({"cancelled"}),
}


class ExecutionConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransferExecution:
    job_id: int
    strategy: str
    phase: str
    snapshot: dict[str, Any]
    receipt: dict[str, Any] | None
    attempts: int
    resume_at: str | None
    organizer_task_id: int | None
    staging_verified: bool
    final_verified: bool
    app_acked: bool
    cleanup: dict[str, Any]
    last_error_code: str
    last_error_message: str
    row_version: int


class TransferExecutionRepository:
    """Durable source of truth for immutable plans and mutable transfer state."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def create(self, job_id: int, plan: dict[str, Any], *, phase: str = "planning") -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.create_with_connection(connection, job_id, plan, phase=phase)

    def create_with_connection(
        self, connection: Any, job_id: int, plan: dict[str, Any], *, phase: str = "planning"
    ) -> None:
        _validate_phase_name(phase)
        encoded = _json(plan)
        plan_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        strategy = str((plan.get("transfer") or {}).get("strategy") or "")
        now = utc_now_iso()
        connection.execute(
            "INSERT INTO import_execution_plans(job_id, schema_version, plan_json, plan_hash, created_at) "
            "VALUES (?, 1, ?, ?, ?)",
            (int(job_id), encoded, plan_hash, now),
        )
        connection.execute(
            "INSERT INTO transfer_executions(job_id, strategy, phase, snapshot_json, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (int(job_id), strategy, phase, now, now),
        )

    def plan(self, job_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT plan_json, plan_hash FROM import_execution_plans WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
        if row is None:
            return None
        encoded = str(row["plan_json"] or "")
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["plan_hash"] or ""):
            raise ExecutionConflictError("执行计划 hash 校验失败")
        value = json.loads(encoded)
        return value if isinstance(value, dict) else None

    def get(self, job_id: int) -> TransferExecution | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM transfer_executions WHERE job_id=?", (int(job_id),)
            ).fetchone()
        return _decode(row) if row is not None else None

    def save(
        self,
        execution: TransferExecution,
        snapshot: dict[str, Any],
        *,
        phase: str,
        resume_at: str | None = None,
        staging_verified: bool | None = None,
        final_verified: bool | None = None,
        app_acked: bool | None = None,
        organizer_task_id: int | None = None,
        last_error_code: str = "",
        last_error_message: str = "",
        claim_owner_id: str | None = None,
        job_status: str | None = None,
        job_error_message: str = "",
        require_job_status_transition: bool = False,
        operator_recovery: bool = False,
    ) -> TransferExecution:
        # The orchestrator emits a deliberately small runtime snapshot.  An
        # recovery history is audit evidence, not runtime state, so later
        # worker saves must not silently discard or rewrite it.
        for audit_field in ("manual_review_history", "transfer_timeout_recovery"):
            if audit_field in execution.snapshot:
                snapshot = {**snapshot, audit_field: execution.snapshot[audit_field]}
        next_phase = str(phase or "").strip()
        _validate_phase_name(next_phase)
        _validate_transition(execution.phase, next_phase)
        if execution.phase in TERMINAL_PHASES and next_phase != execution.phase:
            if execution.phase in IMMUTABLE_TERMINAL_PHASES:
                raise ExecutionConflictError(
                    f"不可变的终态执行不能迁移：{execution.phase} -> {next_phase}"
                )
            if not operator_recovery or execution.phase not in OPERATOR_RECOVERABLE_PHASES:
                raise ExecutionConflictError(
                    f"终态执行需要人工恢复：{execution.phase} -> {next_phase}"
                )
        _validate_claim_owner(execution, claim_owner_id)
        receipt_value = snapshot.get("receipt")
        receipt = receipt_value if isinstance(receipt_value, dict) else None
        cleanup = snapshot.get("cleanup") if isinstance(snapshot.get("cleanup"), dict) else {}
        _validate_receipt_fence(execution, snapshot, receipt, next_phase=next_phase)
        attempts = _next_attempts(execution, snapshot)
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE transfer_executions
                SET phase=?, snapshot_json=?, receipt_json=?, attempts=?, resume_at=?,
                    organizer_task_id=COALESCE(?, organizer_task_id),
                    staging_verified=?, final_verified=?, app_acked=?, cleanup_json=?,
                    last_error_code=?, last_error_message=?, row_version=row_version+1,
                    updated_at=?
                WHERE job_id=? AND phase=? AND row_version=?
                """,
                (
                    next_phase,
                    _json(snapshot),
                    _json(receipt) if receipt is not None else None,
                    attempts,
                    resume_at,
                    organizer_task_id,
                    int(execution.staging_verified if staging_verified is None else staging_verified),
                    int(execution.final_verified if final_verified is None else final_verified),
                    int(execution.app_acked if app_acked is None else app_acked),
                    _json(cleanup),
                    str(last_error_code or ""),
                    str(last_error_message or ""),
                    now,
                    execution.job_id,
                    execution.phase,
                    execution.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionConflictError("执行状态已被其他 Worker 更新")
            if job_status is not None:
                job_updated = _sync_job_with_connection(
                    self.database,
                    connection,
                    execution.job_id,
                    status=str(job_status),
                    error_message=str(job_error_message or ""),
                    updated_at=now,
                )
                if require_job_status_transition and not job_updated:
                    raise ExecutionConflictError(
                        "外部提交 fence 获取前任务状态已变化"
                    )
        latest = self.get(execution.job_id)
        if latest is None:
            raise ExecutionConflictError("执行状态保存后丢失")
        return latest

    def clear_resume_backoff(self, job_id: int, phase: str) -> bool:
        """Null out one phase's backoff timer so a wake-up can run immediately.

        只清 ``resume_at``：phase、snapshot 与 claim fence 仍是权威状态。调用方
        必须已确认执行正在等待的这次外部依赖（如 task-scoped rclone run）到达
        终态——指数退避在那一刻起只会掩盖完成事件（封顶 300 秒）。
        """
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE transfer_executions SET resume_at=NULL, updated_at=? "
                "WHERE job_id=? AND phase=? AND resume_at IS NOT NULL",
                (now, int(job_id), str(phase)),
            )
        return bool(cursor.rowcount)

    def list_due(self, *, now: str | None = None, limit: int = 100) -> list[TransferExecution]:
        """Return non-terminal executions that need a worker wake-up.

        A NULL resume_at means the execution is immediately runnable.  Manual
        review is intentionally excluded: it requires an explicit operator
        action and must not be auto-requeued on every process start.
        """

        if limit <= 0:
            return []
        due = str(now or utc_now_iso())
        placeholders = ",".join("?" for _ in TERMINAL_PHASES)
        terminal_args = tuple(sorted(TERMINAL_PHASES))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM transfer_executions
                WHERE phase NOT IN ({placeholders})
                  AND (resume_at IS NULL OR resume_at <= ?)
                ORDER BY COALESCE(resume_at, updated_at), job_id
                LIMIT ?
                """,
                (*terminal_args, due, int(limit)),
            ).fetchall()
        return [_decode(row) for row in rows]

    def resume_legacy_transfer_timeouts(self, *, limit: int = 100) -> int:
        """Undo only the old wall-clock cutoff, never a real unknown result.

        Keep the submitted operation and its evidence. The normal worker will
        validate the frozen plan and poll that receipt, not submit a new job.
        Queue insertion remains the scanner's responsibility: a crash between
        recovery and enqueue is repaired by the next ordinary list_due scan.
        """
        if limit <= 0:
            return 0
        recovered = 0
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT e.*, p.plan_json, p.plan_hash FROM transfer_executions e "
                "JOIN import_execution_plans p ON p.job_id=e.job_id "
                "JOIN import_jobs j ON j.id=e.job_id "
                "WHERE e.phase='manual_review' AND j.status IN ('review', 'manual_review') "
                "AND e.last_error_code=? AND e.last_error_message LIKE ? ORDER BY e.job_id",
                (ErrorCode.RESULT_UNKNOWN, "传输任务超过最大等待时间（% 秒），已停止自动轮询，请人工核对"),
            ).fetchall()
            for row in rows:
                execution = _decode(row)
                snapshot = dict(execution.snapshot)
                if not _is_legacy_transfer_timeout(execution, row["plan_json"], row["plan_hash"], now):
                    continue
                snapshot["transfer_timeout_recovery"] = {
                    "recovered_at": now,
                    "row_version": execution.row_version,
                    "review_from": snapshot["review_from"],
                    "message": execution.last_error_message,
                    "error_code": execution.last_error_code,
                    "phase_wait_started_at": snapshot.get("phase_wait_started_at", {}),
                    "receipt": execution.receipt,
                }
                snapshot.update(
                    phase=PHASE_RETRY_WAIT, retry_from="transferring", review_from="",
                    retry_ready=False, preserve_receipt_on_retry=True,
                    receipt=execution.receipt, error_code="",
                    message="已恢复因旧版时长上限暂停的传输，保留原回执继续查询",
                )
                snapshot.pop("claim", None)
                _validate_receipt_fence(execution, snapshot, execution.receipt, next_phase=PHASE_RETRY_WAIT)
                connection.execute(
                    "UPDATE transfer_executions SET phase=?, snapshot_json=?, resume_at=NULL, "
                    "last_error_code='', last_error_message='', row_version=row_version+1, updated_at=? "
                    "WHERE job_id=? AND row_version=?",
                    (PHASE_RETRY_WAIT, _json(snapshot), now, execution.job_id, execution.row_version),
                )
                if not _sync_job_with_connection(
                    self.database, connection, execution.job_id,
                    status="retry_requested", error_message="", updated_at=now,
                ):
                    raise ExecutionConflictError("传输超时恢复时任务状态已变化")
                recovered += 1
                if recovered >= limit:
                    break
        return recovered

    def claim(self, job_id: int, owner_id: str, *, lease_seconds: int = 60) -> TransferExecution | None:
        """Atomically claim an execution using its JSON snapshot.

        This is deliberately implemented without a schema change so it can be
        deployed alongside the other workspace's migration work.  A stale
        lease may be reclaimed; an active lease owned by another worker is
        returned as None.
        """

        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("缺少 owner_id")
        execution = self.get(job_id)
        if execution is None or execution.phase in TERMINAL_PHASES:
            return None
        snapshot = dict(execution.snapshot)
        claim = snapshot.get("claim") if isinstance(snapshot.get("claim"), dict) else {}
        now = utc_now_iso()
        current_owner = str(claim.get("owner_id") or "")
        lease_until = str(claim.get("lease_until") or "")
        if current_owner and current_owner != owner and lease_until > now:
            return None
        snapshot["claim"] = {
            "owner_id": owner,
            "lease_until": utc_now_iso_offset(seconds=max(1, int(lease_seconds))),
        }
        try:
            return self.save(execution, snapshot, phase=execution.phase)
        except ExecutionConflictError:
            return None

    def renew_claim(
        self, job_id: int, owner_id: str, *, lease_seconds: int = 60
    ) -> TransferExecution:
        """Extend an existing execution claim without changing its phase."""

        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("缺少 owner_id")
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM transfer_executions WHERE job_id=?", (int(job_id),)
            ).fetchone()
            if row is None:
                raise ExecutionConflictError("执行状态不存在")
            execution = _decode(row)
            if execution.phase in TERMINAL_PHASES:
                raise ExecutionConflictError("终态执行不能续租")
            snapshot = dict(execution.snapshot)
            claim = snapshot.get("claim") if isinstance(snapshot.get("claim"), dict) else {}
            if str(claim.get("owner_id") or "") != owner:
                raise ExecutionConflictError("执行 claim 已不属于当前 Worker")
            snapshot["claim"] = {
                "owner_id": owner,
                "lease_until": utc_now_iso_offset(seconds=max(1, int(lease_seconds))),
            }
            cursor = connection.execute(
                "UPDATE transfer_executions SET snapshot_json=?, row_version=row_version+1, "
                "updated_at=? WHERE job_id=? AND phase=? AND row_version=?",
                (
                    _json(snapshot),
                    now,
                    execution.job_id,
                    execution.phase,
                    execution.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionConflictError("执行状态已被其他 Worker 更新")
        latest = self.get(job_id)
        if latest is None:
            raise ExecutionConflictError("执行状态续租后丢失")
        return latest

    def reactivate(self, job_id: int) -> TransferExecution:
        execution = self.get(job_id)
        if execution is None:
            raise ExecutionConflictError("执行状态不存在")
        if execution.phase in IMMUTABLE_TERMINAL_PHASES:
            raise ExecutionConflictError(f"{execution.phase} execution cannot be recovered")
        if execution.phase == PHASE_MANUAL_REVIEW:
            raise ExecutionConflictError("manual_review 状态必须通过 recover_manual_review() 恢复")
        if execution.phase != PHASE_FAILED:
            snapshot = dict(execution.snapshot)
            snapshot.update({"retry_ready": False, "error_code": "", "message": ""})
            return self.save(
                execution,
                snapshot,
                phase=execution.phase,
                resume_at=None,
                last_error_code="",
                last_error_message="",
            )
        snapshot = dict(execution.snapshot)
        resume_phase = self._recovery_phase(
            execution,
            snapshot.get("failure_from") or snapshot.get("retry_from"),
        )
        return self._operator_recover(execution, snapshot, resume_phase)

    def recover_manual_review(
        self,
        job_id: int,
        *,
        resume_phase: str | None = None,
    ) -> TransferExecution:
        execution = self.get(job_id)
        if execution is None:
            raise ExecutionConflictError("执行状态不存在")
        if execution.phase != PHASE_MANUAL_REVIEW:
            raise ExecutionConflictError("执行不在人工审核状态")
        snapshot = dict(execution.snapshot)
        recovered_phase = self._recovery_phase(
            execution,
            resume_phase or snapshot.get("review_from"),
        )
        return self._operator_recover(execution, snapshot, recovered_phase)

    def resume_after_organizer_completion(self, job_id: int, task_id: int) -> bool:
        """Resume only a plan-bound Organizer wait, never an unknown submission.

        The completion evidence, execution recovery and queue wake-up share one
        write transaction so rollback/cancellation cannot leave a false ACK.
        Actual final file verification still belongs to the transfer worker.
        """
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM transfer_executions WHERE job_id=?", (int(job_id),)).fetchone()
            if row is None:
                return False
            execution = _decode(row)
            snapshot = dict(execution.snapshot)
            origin = snapshot.get("review_from") if execution.phase == PHASE_MANUAL_REVIEW else snapshot.get("failure_from")
            if execution.phase not in {PHASE_MANUAL_REVIEW, PHASE_FAILED} or origin != "organizing":
                return False
            if (
                int(snapshot.get("organizer_task_id") or execution.organizer_task_id or 0) != int(task_id)
                or (execution.organizer_task_id is not None and execution.organizer_task_id != int(task_id))
                or execution.final_verified or execution.app_acked
                or snapshot.get("final_verified") or snapshot.get("app_acked")
            ):
                return False
            task_row = connection.execute(
                "SELECT * FROM organizer_tasks WHERE id=? AND job_id=?", (int(task_id), int(job_id)),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT plan_json, plan_hash FROM import_execution_plans WHERE job_id=?", (int(job_id),),
            ).fetchone()
            job = connection.execute("SELECT status FROM import_jobs WHERE id=?", (int(job_id),)).fetchone()
            if task_row is None or plan_row is None or job is None:
                return False
            if str(job["status"] or "").lower() in {"cancelled", "canceled", "rejected", "unsupported"}:
                return False
            task = dict(task_row)
            evidence = _object(task.get("evidence"))
            confirmation = evidence.get("completion_confirmation") or {}
            reference = _object(task.get("raw_data")).get("execution_plan_ref") or {}
            encoded_plan = str(plan_row["plan_json"] or "")
            plan = _object(encoded_plan)
            if (
                task.get("status") not in {"done", "skipped"}
                or confirmation.get("success") is not True
                or confirmation.get("cleanup_only") or evidence.get("cleanup_only")
                or hashlib.sha256(encoded_plan.encode("utf-8")).hexdigest() != plan_row["plan_hash"]
                or reference.get("plan_hash") != plan_row["plan_hash"]
                or int(reference.get("job_id") or 0) != int(job_id)
                or reference.get("strategy") != execution.strategy
                or (plan.get("transfer") or {}).get("strategy") != execution.strategy
                or task.get("category") != plan.get("category")
                or connection.execute(
                    "SELECT 1 FROM organizer_operations WHERE task_id=? AND status NOT IN ('done', 'skipped') LIMIT 1",
                    (int(task_id),),
                ).fetchone()
            ):
                return False
            fence = snapshot.get("submission_fence") or {}
            receipt = execution.receipt or snapshot.get("receipt")
            if fence and not receipt:
                return False
            if receipt:
                snapshot["receipt"] = receipt
            attempts = dict(snapshot.get("phase_attempts") or {})
            attempts.pop("organizing", None)
            snapshot.update(
                phase=PHASE_RETRY_WAIT, retry_from="organizing", retry_ready=False,
                phase_attempts=attempts, preserve_receipt_on_retry=False,
                error_code="", message="Organizer 已完成，继续最终文件校验与 ACK",
                organizer_task_id=int(task_id),
            )
            snapshot.pop("claim", None)
            now = utc_now_iso()
            connection.execute(
                "UPDATE transfer_executions SET phase=?, snapshot_json=?, resume_at=NULL, "
                "organizer_task_id=?, last_error_code='', last_error_message='', row_version=row_version+1, updated_at=? "
                "WHERE job_id=? AND row_version=?",
                (PHASE_RETRY_WAIT, _json(snapshot), int(task_id), now, int(job_id), execution.row_version),
            )
            _sync_job_with_connection(self.database, connection, int(job_id), status="confirming", error_message="", updated_at=now)
            self.database.worker_tasks.enqueue_with_connection(
                connection, "import_transfer_execute", {"job_id": int(job_id)}, f"import-transfer:{int(job_id)}",
                max_attempts=20, reactivate_terminal=True,
            )
            return True

    def confirm_not_submitted(
        self,
        job_id: int,
        *,
        expected_row_version: int,
        admin: str,
        note: str,
    ) -> TransferExecution:
        """Release an ambiguous fence only after an explicit operator attestation.

        This does not prove that a Provider operation never happened.  The
        administrator must first check task-scoped source/destination files
        and Provider task lists; the caller warns that a mistaken confirmation
        can submit the operation twice.  Never call this from normal retries.
        """

        if type(expected_row_version) is not int or expected_row_version <= 0:
            raise ValueError("执行版本必须是正整数")
        if not isinstance(admin, str) or not admin.strip():
            raise ValueError("缺少管理员身份")
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > 1000:
            raise ValueError("请填写 1 至 1000 字的人工核对说明")
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM transfer_executions WHERE job_id=?", (int(job_id),)
            ).fetchone()
            if row is None:
                raise ExecutionConflictError("任务缺少持久执行状态")
            execution = _decode(row)
            if execution.row_version != expected_row_version:
                raise ExecutionConflictError("执行状态已变化，请刷新任务详情后重新核对")
            if execution.phase != PHASE_MANUAL_REVIEW:
                raise ExecutionConflictError("只有等待人工复核的任务可以确认未提交")
            job = connection.execute(
                "SELECT status FROM import_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if job is None or str(job["status"] or "").strip().lower() not in {"review", "manual_review"}:
                raise ExecutionConflictError("正式任务不在人工复核状态，不能重新提交")
            snapshot = dict(execution.snapshot)
            claim = snapshot.get("claim")
            if claim:
                if not isinstance(claim, dict) or not str(claim.get("owner_id") or "").strip():
                    raise ExecutionConflictError("旧执行租约信息无效，请先核对 Worker 状态")
                try:
                    lease_until = datetime.fromisoformat(str(claim.get("lease_until") or "").replace("Z", "+00:00"))
                    if lease_until.tzinfo is None:
                        lease_until = lease_until.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError) as exc:
                    raise ExecutionConflictError("旧执行租约时间无效，请先核对 Worker 状态") from exc
                if lease_until > datetime.now(timezone.utc):
                    raise ExecutionConflictError("旧 Worker 执行租约尚未结束，请等待租约结束后重新核对")
            if row["receipt_json"] is not None or snapshot.get("receipt") is not None:
                raise ExecutionConflictError("任务已有外部操作回执，不能确认为未提交；请保留回执核对结果")
            if (
                execution.staging_verified or execution.final_verified or execution.app_acked
                or any(snapshot.get(key) for key in ("target_verified", "staging_verified", "final_verified", "app_acked"))
            ):
                raise ExecutionConflictError("任务已有目标校验或入库确认记录，不能确认为未提交")
            resume_phase = str(snapshot.get("review_from") or "").strip()
            if resume_phase not in {"source_materializing", "native_submitting", "transferring"}:
                raise ExecutionConflictError("人工复核来源不是可确认未提交的阶段")
            fence = snapshot.get("submission_fence")
            if not isinstance(fence, dict) or str(fence.get("state") or "").strip().lower() not in {"prepared", "committed"}:
                raise ExecutionConflictError("任务缺少待核对的外部提交栅栏")
            fence_phase = str(fence.get("phase") or "").strip()
            if fence_phase and fence_phase != resume_phase:
                raise ExecutionConflictError("提交栅栏与人工复核阶段不一致，不能重新提交")
            history = snapshot.get("manual_review_history", [])
            if not isinstance(history, list):
                raise ExecutionConflictError("人工复核历史格式无效，已停止恢复")
            snapshot["manual_review_history"] = [
                *history,
                {
                    "action": "confirm_not_submitted",
                    "admin": admin.strip(),
                    "note": note.strip(),
                    "confirmed_at": now,
                    "previous_row_version": execution.row_version,
                    "review_from": resume_phase,
                    "submission_fence": dict(fence),
                    "previous_error": {
                        "code": execution.last_error_code,
                        "message": execution.last_error_message,
                    },
                },
            ]
            raw_attempts = snapshot.get("phase_attempts")
            phase_attempts = dict(raw_attempts) if isinstance(raw_attempts, dict) else {}
            phase_attempts.pop(resume_phase, None)
            snapshot.update({
                "phase": PHASE_RETRY_WAIT,
                "attempt": execution.attempts,
                "phase_attempts": phase_attempts,
                "retry_from": resume_phase,
                "retry_ready": False,
                "preserve_receipt_on_retry": False,
                "receipt": None,
                "error_code": "",
                "message": "管理员已确认本次操作未提交，等待重新提交",
            })
            snapshot.pop("submission_fence", None)
            snapshot.pop("claim", None)
            cursor = connection.execute(
                "UPDATE transfer_executions SET phase=?, snapshot_json=?, receipt_json=NULL, "
                "resume_at=NULL, last_error_code='', last_error_message='', "
                "row_version=row_version+1, updated_at=? "
                "WHERE job_id=? AND phase=? AND row_version=? AND receipt_json IS NULL",
                (
                    PHASE_RETRY_WAIT, _json(snapshot), now, execution.job_id,
                    PHASE_MANUAL_REVIEW, expected_row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionConflictError("执行状态已变化，人工确认未生效")
            if not _sync_job_with_connection(
                self.database, connection, execution.job_id,
                status="retry_requested", error_message="", updated_at=now,
            ):
                raise ExecutionConflictError("正式任务状态已变化，人工确认未生效")
            latest = connection.execute(
                "SELECT * FROM transfer_executions WHERE job_id=?", (execution.job_id,)
            ).fetchone()
            return _decode(latest)

    def _operator_recover(
        self,
        execution: TransferExecution,
        snapshot: dict[str, Any],
        resume_phase: str,
    ) -> TransferExecution:
        receipt_status = str((execution.receipt or {}).get("status") or "").strip().lower()
        submission_phase = resume_phase in {
            "source_materializing",
            "native_submitting",
            "transferring",
        }
        fence = snapshot.get("submission_fence")
        if (
            execution.receipt is None
            and isinstance(fence, dict)
            and str(fence.get("state") or "").strip().lower() in {"prepared", "committed"}
        ):
            raise ExecutionConflictError(
                f"提交 fence 已存在但缺少 {resume_phase} 的回执"
            )
        phase_attempts = (
            dict(snapshot.get("phase_attempts"))
            if isinstance(snapshot.get("phase_attempts"), dict)
            else {}
        )
        # Older rows may have the receipt durable in ``receipt_json`` while
        # the JSON snapshot predates that field.  Carry it forward so operator
        # recovery cannot accidentally clear an external side-effect fence.
        if execution.receipt is not None and not isinstance(snapshot.get("receipt"), dict):
            snapshot["receipt"] = dict(execution.receipt)
        preserve_receipt = bool(
            execution.receipt is not None
            and submission_phase
            and receipt_status not in {"failed", "cancelled"}
        )
        phase_attempts.pop(resume_phase, None)
        if resume_phase == "cleanup_pending" and isinstance(snapshot.get("cleanup"), dict):
            cleanup = dict(snapshot["cleanup"])
            cleanup.pop("attempt", None)
            snapshot["cleanup"] = cleanup
        snapshot.update(
            {
                "phase": PHASE_RETRY_WAIT,
                "phase_attempts": phase_attempts,
                "retry_from": resume_phase,
                "preserve_receipt_on_retry": preserve_receipt,
                "retry_ready": False,
                "error_code": "",
                "message": "",
            }
        )
        snapshot.pop("claim", None)
        return self.save(
            execution,
            snapshot,
            phase=PHASE_RETRY_WAIT,
            resume_at=None,
            last_error_code="",
            last_error_message="",
            operator_recovery=True,
        )

    @staticmethod
    def _recovery_phase(
        execution: TransferExecution,
        candidate: Any,
    ) -> str:
        phase = str(candidate or "").strip()
        if not phase:
            if execution.receipt is not None:
                raise ExecutionConflictError(
                    "执行已有回执但恢复阶段未知"
                )
            return PHASE_PLANNING
        _validate_phase_name(phase)
        if phase in TERMINAL_PHASES or phase in {PHASE_RETRY_WAIT, "cancel_pending"}:
            raise ExecutionConflictError(f"非法的人工恢复阶段：{phase}")
        return phase


def _decode(row: Any) -> TransferExecution:
    return TransferExecution(
        job_id=int(row["job_id"]),
        strategy=str(row["strategy"] or ""),
        phase=str(row["phase"] or "planning"),
        snapshot=_object(row["snapshot_json"]),
        receipt=_optional_object(row["receipt_json"]),
        attempts=int(row["attempts"] or 0),
        resume_at=str(row["resume_at"]) if row["resume_at"] else None,
        organizer_task_id=int(row["organizer_task_id"]) if row["organizer_task_id"] else None,
        staging_verified=bool(row["staging_verified"]),
        final_verified=bool(row["final_verified"]),
        app_acked=bool(row["app_acked"]),
        cleanup=_object(row["cleanup_json"]),
        last_error_code=str(row["last_error_code"] or ""),
        last_error_message=str(row["last_error_message"] or ""),
        row_version=int(row["row_version"] or 1),
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_legacy_transfer_timeout(execution: TransferExecution, encoded_plan: str, plan_hash: str, now: str) -> bool:
    snapshot = execution.snapshot
    receipt = execution.receipt
    message = execution.last_error_message
    if (
        execution.strategy not in {"rclone_copy", "relay_upload_provider"}
        or snapshot.get("phase") != PHASE_MANUAL_REVIEW
        or snapshot.get("review_from") != "transferring"
        or snapshot.get("executor_strategy") != execution.strategy
        or snapshot.get("error_code") != ErrorCode.RESULT_UNKNOWN
        or snapshot.get("message") != message
        or re.fullmatch(r"传输任务超过最大等待时间（[1-9][0-9]* 秒），已停止自动轮询，请人工核对", message) is None
        or "transfer_timeout_recovery" in snapshot
        or not isinstance(receipt, dict)
        or not str(receipt.get("operation_id") or "").strip()
        or receipt.get("status") not in {"submitted", "pending", "running", "completed"}
        or not isinstance(receipt.get("metadata"), dict)
        or (snapshot.get("receipt") is not None and snapshot["receipt"] != receipt)
        or execution.staging_verified or execution.final_verified or execution.app_acked
        or any(snapshot.get(key) for key in ("target_verified", "staging_verified", "final_verified", "app_acked"))
    ):
        return False
    if hashlib.sha256(str(encoded_plan).encode("utf-8")).hexdigest() != plan_hash:
        return False
    plan = _object(encoded_plan)
    transfer = plan.get("transfer")
    if (
        plan.get("version") != 1 or plan.get("job_id") != execution.job_id
        or not isinstance(transfer, dict) or transfer.get("strategy") != execution.strategy
    ):
        return False
    key = str(transfer.get("idempotency_key") or f"import:{execution.job_id}:{execution.strategy}")
    operation_key = f"relay-dl:{key}" if execution.strategy == "relay_upload_provider" else key
    if _explicit_operation_key(receipt) != operation_key:
        return False
    fence = snapshot.get("submission_fence")
    if fence and (
        not isinstance(fence, dict) or fence.get("state") != "committed"
        or fence.get("phase") != "transferring"
        or not (_operation_key_aliases(fence) & _operation_key_aliases(receipt))
    ):
        return False
    claim = snapshot.get("claim")
    if claim:
        if not isinstance(claim, dict):
            return False
        try:
            lease = datetime.fromisoformat(str(claim.get("lease_until") or "").replace("Z", "+00:00"))
            if lease.tzinfo is None or lease > datetime.fromisoformat(now.replace("Z", "+00:00")):
                return False
        except ValueError:
            return False
    return True


def _object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_object(value: Any) -> dict[str, Any] | None:
    return _object(value) if value else None


def _validate_phase_name(phase: str) -> None:
    if phase not in _ALLOWED_PHASE_TRANSITIONS:
        raise ExecutionConflictError(f"未知的传输执行阶段：{phase}")


def _validate_transition(current: str, next_phase: str) -> None:
    _validate_phase_name(current)
    _validate_phase_name(next_phase)
    if next_phase not in _ALLOWED_PHASE_TRANSITIONS[current]:
        raise ExecutionConflictError(f"非法的阶段迁移：{current} -> {next_phase}")


def _operation_key(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("operation_key", "idempotency_key", "run_id", "operation_id"):
        candidate = value.get(key)
        if candidate:
            return str(candidate)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        return _operation_key(metadata)
    return ""


def _explicit_operation_key(value: Any) -> str:
    """Return an idempotency key, never an opaque provider operation id.

    Provider operation ids (for example ``rclone:<job>:<run>``) identify a
    concrete run, not the application idempotency fence.  Treating them as the
    fence key makes every first receipt conflict with a prepared fence.  Only
    explicit operation/idempotency keys participate in the cross-record check.
    """

    if not isinstance(value, dict):
        return ""
    for key in ("operation_key", "idempotency_key"):
        candidate = value.get(key)
        if candidate not in (None, ""):
            return str(candidate)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        return _explicit_operation_key(metadata)
    return ""


def _operation_key_aliases(value: Any) -> set[str]:
    """Build canonical aliases for phase/purpose-qualified operation keys."""

    key = _explicit_operation_key(value)
    if not key:
        return set()
    aliases = {key}
    for prefix in ("landing:", "relay-dl:", "relay-up:"):
        if key.startswith(prefix):
            aliases.add(key[len(prefix) :])
    # Older prepared fences appended the phase to the base key.  Accept that
    # spelling while persisting the exact canonical key for new executions.
    phases = set(_ALLOWED_PHASE_TRANSITIONS)
    for phase in phases:
        suffix = ":" + phase
        if key.endswith(suffix):
            aliases.add(key[: -len(suffix)])
    return {item for item in aliases if item}


def _validate_receipt_fence(
    execution: TransferExecution,
    snapshot: dict[str, Any],
    receipt: dict[str, Any] | None,
    *,
    next_phase: str,
) -> None:
    old_key = _explicit_operation_key(execution.receipt)
    new_key = _explicit_operation_key(receipt)
    if execution.receipt is not None and receipt is None:
        transition = (execution.phase, next_phase)
        if transition not in _RECEIPT_CLEAR_TRANSITIONS:
            raise ExecutionConflictError(
                f"{execution.phase} -> {next_phase} 期间不能清除回执"
            )
    if old_key and new_key and old_key != new_key:
        raise ExecutionConflictError(
            f"任务 {execution.job_id} 的回执 operation_key 已变化：{old_key} -> {new_key}"
        )
    fence = snapshot.get("submission_fence")
    if isinstance(fence, dict):
        fence_key = _explicit_operation_key(fence)
        # A receipt without an explicit application key is still durable and
        # may only be compared by its operation id at the Provider boundary.
        # Do not reject it merely because that opaque id differs from the
        # prepared fence; the old implementation did exactly that and made the
        # first rclone receipt impossible to save.
        if new_key and fence_key and not (_operation_key_aliases(receipt) & _operation_key_aliases(fence)):
            raise ExecutionConflictError("回执与提交 fence 不匹配")


def _next_attempts(execution: TransferExecution, snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("attempt", execution.attempts)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionConflictError("执行尝试次数必须是整数") from exc
    if value < execution.attempts:
        raise ExecutionConflictError(
            f"执行尝试次数不能回退：{execution.attempts} -> {value}"
        )
    return value


def _validate_claim_owner(execution: TransferExecution, owner_id: str | None) -> None:
    if owner_id is None:
        return
    claim = execution.snapshot.get("claim") if isinstance(execution.snapshot.get("claim"), dict) else {}
    current_owner = str(claim.get("owner_id") or "").strip()
    if not current_owner:
        raise ExecutionConflictError("执行未被任何 Worker 认领")
    if current_owner != str(owner_id or "").strip():
        raise ExecutionConflictError(
            f"执行认领者不匹配：{current_owner} != {owner_id}"
        )


def _sync_job_with_connection(
    database: Any,
    connection: Any,
    job_id: int,
    *,
    status: str,
    error_message: str,
    updated_at: str,
) -> bool:
    """Keep the public job status in the same transaction as execution state.

    A concurrent cancellation/rejection is an operator decision and must never
    be overwritten by a stale Worker.  In that case the execution receipt is
    still committed (so an external side effect is not lost), while the job's
    terminal status remains authoritative for the next cancellation step.
    """

    return bool(
        database.job_commands.update_unless_status_with_connection(
            connection,
            int(job_id),
            {"cancelled", "canceled", "rejected"},
            status=status,
            error_message=error_message,
            updated_at=updated_at,
        )
    )
