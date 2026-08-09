from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..time_utils import utc_now_iso, utc_now_iso_offset


TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled", "manual_review"})

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
    "ack_pending": frozenset({"ack_pending", "cleanup_pending", "completed", "failed", "manual_review", "cancel_pending", "cancelled", "retry_wait"}),
    "cleanup_pending": frozenset({"cleanup_pending", "completed", "failed", "manual_review", "retry_wait"}),
    "retry_wait": frozenset({"retry_wait", "planning", "source_materializing", "native_submitting", "native_waiting", "transferring", "target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending", "cleanup_pending", "failed", "manual_review", "cancel_pending", "cancelled"}),
    "cancel_pending": frozenset({"cancel_pending", "cancelled", "failed", "manual_review", "retry_wait"}),
    "manual_review": frozenset({"manual_review"}),
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
    ) -> TransferExecution:
        next_phase = str(phase or "").strip()
        _validate_phase_name(next_phase)
        _validate_transition(execution.phase, next_phase)
        if execution.phase in TERMINAL_PHASES and next_phase != execution.phase:
            raise ExecutionConflictError(
                f"terminal execution cannot transition: {execution.phase} -> {next_phase}"
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
                        "job status changed before external submission fence was acquired"
                    )
        latest = self.get(execution.job_id)
        if latest is None:
            raise ExecutionConflictError("执行状态保存后丢失")
        return latest

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

    def claim(self, job_id: int, owner_id: str, *, lease_seconds: int = 60) -> TransferExecution | None:
        """Atomically claim an execution using its JSON snapshot.

        This is deliberately implemented without a schema change so it can be
        deployed alongside the other workspace's migration work.  A stale
        lease may be reclaimed; an active lease owned by another worker is
        returned as None.
        """

        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id is required")
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
            raise ValueError("owner_id is required")
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
        if execution.phase == "completed":
            raise ExecutionConflictError("已完成任务不能重试")
        if execution.phase in {"manual_review", "cancelled"}:
            raise ExecutionConflictError("人工复核或已取消任务不能自动重试")
        snapshot = dict(execution.snapshot)
        retry_from = str(snapshot.get("retry_from") or snapshot.get("phase") or "planning")
        snapshot.update({"phase": retry_from, "retry_ready": False, "error_code": "", "message": ""})
        return self.save(execution, snapshot, phase=retry_from, last_error_code="", last_error_message="")


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
        raise ExecutionConflictError(f"unknown transfer execution phase: {phase}")


def _validate_transition(current: str, next_phase: str) -> None:
    _validate_phase_name(current)
    _validate_phase_name(next_phase)
    if next_phase not in _ALLOWED_PHASE_TRANSITIONS[current]:
        raise ExecutionConflictError(f"illegal transfer phase transition: {current} -> {next_phase}")


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
                f"receipt cannot be cleared during {execution.phase} -> {next_phase}"
            )
    if old_key and new_key and old_key != new_key:
        raise ExecutionConflictError(
            f"receipt operation key changed for job {execution.job_id}: {old_key} -> {new_key}"
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
            raise ExecutionConflictError("receipt does not match submission fence")


def _next_attempts(execution: TransferExecution, snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("attempt", execution.attempts)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionConflictError("execution attempt must be an integer") from exc
    if value < execution.attempts:
        raise ExecutionConflictError(
            f"execution attempt cannot move backwards: {execution.attempts} -> {value}"
        )
    return value


def _validate_claim_owner(execution: TransferExecution, owner_id: str | None) -> None:
    if owner_id is None:
        return
    claim = execution.snapshot.get("claim") if isinstance(execution.snapshot.get("claim"), dict) else {}
    current_owner = str(claim.get("owner_id") or "").strip()
    if not current_owner:
        raise ExecutionConflictError("execution is not claimed by a Worker")
    if current_owner != str(owner_id or "").strip():
        raise ExecutionConflictError(
            f"execution claim owner mismatch: {current_owner} != {owner_id}"
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

    commands = getattr(database, "job_commands", None)
    previous = None
    transition_row = getattr(commands, "_transition_row", None)
    if callable(transition_row):
        previous = transition_row(connection, int(job_id))
    cursor = connection.execute(
        "UPDATE import_jobs SET status=?, error_message=?, updated_at=? "
        "WHERE id=? AND LOWER(COALESCE(status,'')) NOT IN "
        "('cancelled','canceled','rejected')",
        (status, error_message, updated_at, int(job_id)),
    )
    emitter = getattr(commands, "_emit_status_transition", None)
    if callable(emitter):
        emitter(
            connection,
            previous,
            {"status": status, "error_message": error_message, "updated_at": updated_at},
            cursor.rowcount,
        )
    return cursor.rowcount == 1
