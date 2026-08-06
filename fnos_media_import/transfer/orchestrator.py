"""TransferOrchestrator —— 按 v4 冻结计划驱动 §13.1 状态机的编排器。

职责：
- planning 阶段校验计划与策略约束（磁链->六盘、同源原生、relay 只到移动云、rclone 不到移动云）。
- 按策略选择执行器，驱动 来源物化 -> 可见性等待 -> 传输 -> 目标校验 -> 整理 -> ACK -> 清理。
- 幂等：保存 OperationReceipt；未知结果进入 manual_review，绝不重复创建原生转存（§13.3）。
- 取消：区分「未提交直接取消 / 已提交请求取消 / 结果未知 cancel_pending /
  目标已确认但 ACK 未完成（保留来源，恢复 ACK/清理，不重复传输）」。
- 重试：有界指数退避（RetryPolicy），只对可重试错误分类（§18.1）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from fnos_media_import.transfer import (
    AckResult,
    CancelResult,
    ErrorCode,
    OperationReceipt,
    OrganizeResult,
    PHASE_ACK_PENDING,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    PHASE_CLEANUP_PENDING,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW,
    PHASE_NATIVE_SUBMITTING,
    PHASE_NATIVE_WAITING,
    PHASE_ORGANIZING,
    PHASE_PLANNING,
    PHASE_RETRY_WAIT,
    PHASE_SOURCE_MATERIALIZING,
    PHASE_SOURCE_WAITING_VISIBLE,
    PHASE_TARGET_VERIFYING,
    PHASE_TRANSFERRING,
    TERMINAL_PHASES,
    TransferError,
    TransferPlan,
    VisibilityStatus,
    normalize_operation_status,
    normalize_receipt,
)

__all__ = [
    "RetryPolicy",
    "TransferState",
    "TransferStepResult",
    "TransferOrchestrator",
]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 5.0
    max_delay_seconds: float = 300.0


@dataclass
class TransferState:
    phase: str = PHASE_PLANNING
    receipt: OperationReceipt | None = None
    attempt: int = 0
    retry_from: str = ""
    retry_ready: bool = False
    target_verified: bool = False
    app_acked: bool = False
    message: str = ""
    error_code: str = ""
    executor_strategy: str = ""
    cleanup: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransferStepResult:
    phase: str
    message: str = ""
    finished: bool = False
    needs_wait: bool = False


def _visibility_to_status(value: Any) -> VisibilityStatus:
    if isinstance(value, VisibilityStatus):
        return value
    if isinstance(value, bool):
        return VisibilityStatus.visible if value else VisibilityStatus.pending
    text = str(value or "").strip().lower()
    if text in {"visible", "ok", "true", "ready", "yes", "1"}:
        return VisibilityStatus.visible
    if text in {"missing", "gone", "absent", "not_found"}:
        return VisibilityStatus.missing
    if text in {"unknown", "error"}:
        return VisibilityStatus.unknown
    return VisibilityStatus.pending


class TransferOrchestrator:
    """按 §13.1 状态机驱动单一 v4 冻结计划。"""

    def __init__(
        self,
        plan: dict[str, Any] | TransferPlan,
        executors: dict[str, Any],
        *,
        verifier: Any = None,
        cleanup: Any = None,
        ack_service: Any = None,
        organizer_service: Any = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.plan = plan if isinstance(plan, TransferPlan) else TransferPlan.from_dict(plan)
        self.executors = dict(executors or {})
        self.verifier = verifier
        self.cleanup = cleanup
        self.ack_service = ack_service
        self.organizer_service = organizer_service
        self.retry_policy = retry_policy or RetryPolicy()
        self.state = TransferState()

    # ------------------------------------------------------------------
    # 状态快照 / 恢复（应用层持久化 phase + receipt 以支持重启续跑）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.state.phase,
            "message": self.state.message,
            "error_code": self.state.error_code,
            "attempt": self.state.attempt,
            "retry_from": self.state.retry_from,
            "retry_ready": self.state.retry_ready,
            "target_verified": self.state.target_verified,
            "app_acked": self.state.app_acked,
            "executor_strategy": self.state.executor_strategy,
            "receipt": asdict(self.state.receipt) if self.state.receipt is not None else None,
            "cleanup": dict(self.state.cleanup),
        }

    def restore(self, state: dict[str, Any] | TransferState | None) -> "TransferOrchestrator":
        if isinstance(state, TransferState):
            self.state = state
            return self
        raw = dict(state or {})
        self.state.phase = str(raw.get("phase") or PHASE_PLANNING)
        self.state.message = str(raw.get("message") or "")
        self.state.error_code = str(raw.get("error_code") or "")
        try:
            self.state.attempt = int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            self.state.attempt = 0
        self.state.retry_from = str(raw.get("retry_from") or "")
        self.state.retry_ready = bool(raw.get("retry_ready"))
        self.state.target_verified = bool(raw.get("target_verified"))
        self.state.app_acked = bool(raw.get("app_acked"))
        self.state.executor_strategy = str(raw.get("executor_strategy") or "")
        self.state.cleanup = dict(raw.get("cleanup") or {})
        receipt = raw.get("receipt")
        if isinstance(receipt, dict):
            try:
                self.state.receipt = OperationReceipt(
                    operation_id=str(receipt.get("operation_id") or ""),
                    status=str(receipt.get("status") or "unknown"),
                    provider_kind=str(receipt.get("provider_kind") or ""),
                    message=str(receipt.get("message") or ""),
                    external_ref=str(receipt.get("external_ref") or ""),
                    error_code=str(receipt.get("error_code") or ""),
                    metadata=dict(receipt.get("metadata") or {}),
                )
            except Exception:
                self.state.receipt = None
        else:
            self.state.receipt = None
        return self

    def mark_retry_ready(self) -> None:
        self.state.retry_ready = True

    # ------------------------------------------------------------------
    # 主驱动
    # ------------------------------------------------------------------
    def run(self, max_steps: int = 100) -> TransferStepResult:
        result: TransferStepResult | None = None
        for _ in range(max(1, max_steps)):
            result = self.step()
            if result.finished:
                return result
        if result is None:
            result = TransferStepResult(self.state.phase, "未执行任何步骤", finished=True)
        return result

    def step(self) -> TransferStepResult:
        phase = self.state.phase
        if phase in TERMINAL_PHASES:
            return TransferStepResult(phase, self.state.message or f"任务已处于终态：{phase}", finished=True)

        if phase == PHASE_PLANNING:
            return self._step_planning()
        if phase == PHASE_SOURCE_MATERIALIZING:
            return self._step_source_materializing()
        if phase == PHASE_SOURCE_WAITING_VISIBLE:
            return self._step_source_waiting_visible()
        if phase == PHASE_NATIVE_SUBMITTING:
            return self._step_native_submitting()
        if phase == PHASE_NATIVE_WAITING:
            return self._step_native_waiting()
        if phase == PHASE_TRANSFERRING:
            return self._step_transferring()
        if phase == PHASE_TARGET_VERIFYING:
            return self._step_target_verifying()
        if phase == PHASE_ORGANIZING:
            return self._step_organizing()
        if phase == PHASE_ACK_PENDING:
            return self._step_ack()
        if phase == PHASE_CLEANUP_PENDING:
            return self._step_cleanup()
        if phase == PHASE_RETRY_WAIT:
            return self._step_retry_wait()
        if phase == PHASE_CANCEL_PENDING:
            return self._step_cancel_pending()
        if phase == PHASE_MANUAL_REVIEW:
            return TransferStepResult(PHASE_MANUAL_REVIEW, self.state.message or "等待人工复核", finished=True, needs_wait=True)
        # 未知阶段：保守失败关闭
        return self._fail(TransferError(ErrorCode.INVALID_PLAN, f"未知阶段：{phase}"))

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------
    def _step_planning(self) -> TransferStepResult:
        strategy = self.plan.strategy
        error = self._validate_plan()
        if error is not None:
            return self._fail(error)
        executor = self.executors.get(strategy)
        if executor is None:
            return self._fail(
                TransferError(ErrorCode.NO_EXECUTOR, f"策略 {strategy} 没有可用执行器，失败关闭")
            )
        if getattr(executor, "plan", None) is None and hasattr(executor, "bind"):
            executor.bind(self.plan)
        else:
            executor.plan = self.plan
        self.state.executor_strategy = strategy
        if getattr(executor, "needs_source_landing", False):
            self.state.phase = PHASE_SOURCE_MATERIALIZING
        else:
            self.state.phase = PHASE_NATIVE_SUBMITTING
        return TransferStepResult(self.state.phase, "计划校验通过", finished=False)

    def _validate_plan(self) -> TransferError | None:
        plan = self.plan
        source_form = plan.source_form
        strategy = plan.strategy
        source_kind = plan.source_provider_kind
        dest_kind = plan.destination_provider_kind
        if not strategy:
            return TransferError(ErrorCode.INVALID_PLAN, "计划缺少传输策略")
        if source_form in {"magnet", "torrent"}:
            if strategy != "native_offline" or dest_kind != "sixpan":
                return TransferError(
                    ErrorCode.MAGNET_TARGET_RESTRICTED,
                    "磁链/种子只能保存到六盘（native_offline），不展示、不接受、不降级",
                )
            return None
        if strategy == "native_offline":
            return TransferError(ErrorCode.INVALID_PLAN, "native_offline 只适用于磁链/种子到六盘")
        if strategy == "native_save":
            if source_form != "share":
                return TransferError(ErrorCode.INVALID_PLAN, "native_save 只适用于分享来源")
            if source_kind != dest_kind:
                return TransferError(
                    ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                    "native_save 要求来源与目标同 Provider，失败不降级为跨盘",
                )
            return None
        if strategy == "relay_upload_mobile":
            if dest_kind != "mobile":
                return TransferError(ErrorCode.INVALID_PLAN, "relay_upload_mobile 只允许目标为移动云")
            return None
        if strategy == "rclone_copy":
            if dest_kind == "mobile":
                return TransferError(ErrorCode.INVALID_PLAN, "rclone_copy 不允许直接写入移动云目标")
            return None
        return TransferError(ErrorCode.INVALID_PLAN, f"未知策略：{strategy}")

    # ------------------------------------------------------------------
    # source_materializing
    # ------------------------------------------------------------------
    def _step_source_materializing(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "来源物化阶段缺少执行器"))
        if self.state.receipt is not None:
            # 已提交（重启续跑）：跳过重复物化，直接等待可见
            self.state.phase = PHASE_SOURCE_WAITING_VISIBLE
            return TransferStepResult(PHASE_SOURCE_WAITING_VISIBLE, "来源物化已提交，继续等待可见", finished=False)
        try:
            receipt = executor.materialize_source()
        except TransferError as exc:
            return self._handle_failure(exc, PHASE_SOURCE_MATERIALIZING)
        except Exception as exc:  # noqa: BLE001
            return self._handle_failure(
                TransferError(ErrorCode.NETWORK_TRANSIENT, f"来源物化异常：{exc}", retryable=True),
                PHASE_SOURCE_MATERIALIZING,
            )
        receipt = normalize_receipt(receipt, provider_kind=self.plan.source_provider_kind)
        self.state.receipt = receipt
        self.state.message = receipt.message
        if receipt.status == "failed":
            return self._handle_failure(self._receipt_error(receipt), PHASE_SOURCE_MATERIALIZING)
        if receipt.status == "unknown":
            return self._to_manual_review("来源物化结果未知，不重复创建")
        self.state.phase = PHASE_SOURCE_WAITING_VISIBLE
        return TransferStepResult(PHASE_SOURCE_WAITING_VISIBLE, "来源已物化，等待 OpenList 可见", finished=False)

    # ------------------------------------------------------------------
    # source_waiting_visible
    # ------------------------------------------------------------------
    def _step_source_waiting_visible(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "可见性等待阶段缺少执行器"))
        poller = getattr(executor, "wait_source_visible", None)
        if not callable(poller):
            self.state.phase = PHASE_TRANSFERRING
            return TransferStepResult(PHASE_TRANSFERRING, "无可见性检查，直接传输", finished=False)
        try:
            visibility = poller(self.state.receipt)
        except TransferError as exc:
            return self._handle_failure(exc, PHASE_SOURCE_WAITING_VISIBLE)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"OpenList 可见性检查异常：{exc}")
        status = _visibility_to_status(visibility)
        if status == VisibilityStatus.visible:
            self.state.phase = PHASE_TRANSFERRING
            # 物化 receipt 已完成使命：清空，让 transferring 阶段提交新的传输操作。
            # 否则 transferring 会把「来源物化 receipt」误当作传输 receipt 去轮询。
            self.state.receipt = None
            return TransferStepResult(PHASE_TRANSFERRING, "来源 OpenList 可见且稳定", finished=False)
        if status == VisibilityStatus.missing:
            return self._handle_failure(
                TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, "来源落地区不可见或已丢失"),
                PHASE_SOURCE_WAITING_VISIBLE,
            )
        if status == VisibilityStatus.unknown:
            return self._to_manual_review("来源落地区可见性未知")
        return self._handle_failure(
            TransferError(ErrorCode.OPERATION_PENDING, "来源未稳定可见，等待后重试", retryable=True),
            PHASE_SOURCE_WAITING_VISIBLE,
        )

    # ------------------------------------------------------------------
    # native_submitting / native_waiting
    # ------------------------------------------------------------------
    def _step_native_submitting(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "原生提交阶段缺少执行器"))
        if self.state.receipt is not None:
            # 幂等续跑：已有 receipt，不重复创建原生转存
            self.state.phase = PHASE_NATIVE_WAITING
            return TransferStepResult(PHASE_NATIVE_WAITING, "原生任务已提交，继续轮询", finished=False)
        try:
            receipt = executor.submit()
        except TransferError as exc:
            return self._handle_failure(exc, PHASE_NATIVE_SUBMITTING)
        except Exception as exc:  # noqa: BLE001
            return self._handle_failure(
                TransferError(ErrorCode.NETWORK_TRANSIENT, f"原生提交异常：{exc}", retryable=True),
                PHASE_NATIVE_SUBMITTING,
            )
        receipt = normalize_receipt(receipt, provider_kind=self.plan.source_provider_kind)
        self.state.receipt = receipt
        self.state.message = receipt.message
        if receipt.status == "failed":
            return self._handle_failure(self._receipt_error(receipt), PHASE_NATIVE_SUBMITTING)
        if receipt.status == "unknown":
            return self._to_manual_review("原生提交结果未知，不重复创建转存")
        self.state.phase = PHASE_NATIVE_WAITING
        return TransferStepResult(PHASE_NATIVE_WAITING, "原生任务已提交", finished=False)

    def _step_native_waiting(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "原生轮询阶段缺少执行器"))
        receipt = self.state.receipt
        if receipt is None:
            # 结果未知且没有可轮询凭证：人工复核，禁止重复提交
            return self._to_manual_review("原生任务 receipt 缺失，结果未知，禁止重复提交")
        try:
            status = executor.poll(receipt)
        except TransferError as exc:
            return self._handle_failure(exc, PHASE_NATIVE_WAITING)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"原生轮询异常：{exc}")
        status = normalize_operation_status(status)
        if status.status == "completed":
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "原生任务完成，进入目标校验", finished=False)
        if status.status == "cancelled":
            self.state.phase = PHASE_CANCELLED
            return TransferStepResult(PHASE_CANCELLED, "原生任务已取消", finished=True)
        if status.status == "unknown":
            return self._to_manual_review("原生任务状态未知，进入人工复核，不重复提交")
        if status.status == "failed":
            error = TransferError(
                status.error_code or ErrorCode.NETWORK_TRANSIENT,
                status.message or "原生任务失败",
                retryable=self._code_retryable(status.error_code),
            )
            return self._handle_failure(error, PHASE_NATIVE_WAITING)
        return TransferStepResult(PHASE_NATIVE_WAITING, status.message or "原生任务进行中", finished=True, needs_wait=True)

    # ------------------------------------------------------------------
    # transferring
    # ------------------------------------------------------------------
    def _step_transferring(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "传输阶段缺少执行器"))
        receipt = self.state.receipt
        if receipt is None:
            try:
                receipt = executor.submit()
            except TransferError as exc:
                return self._handle_failure(exc, PHASE_TRANSFERRING)
            except Exception as exc:  # noqa: BLE001
                return self._handle_failure(
                    TransferError(ErrorCode.NETWORK_TRANSIENT, f"传输提交异常：{exc}", retryable=True),
                    PHASE_TRANSFERRING,
                )
            receipt = normalize_receipt(receipt, provider_kind=getattr(executor, "strategy", "") or "")
            self.state.receipt = receipt
            self.state.message = receipt.message
            if receipt.status == "failed":
                return self._handle_failure(self._receipt_error(receipt), PHASE_TRANSFERRING)
            if receipt.status == "unknown":
                return self._to_manual_review("传输提交结果未知，保留来源")
            return TransferStepResult(PHASE_TRANSFERRING, receipt.message or "传输已启动", finished=True, needs_wait=True)
        try:
            status = executor.poll(receipt)
        except TransferError as exc:
            return self._handle_failure(exc, PHASE_TRANSFERRING)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"传输轮询异常：{exc}")
        status = normalize_operation_status(status)
        if status.status == "completed":
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "传输完成，进入目标校验", finished=False)
        if status.status == "cancelled":
            self.state.phase = PHASE_CANCELLED
            return TransferStepResult(PHASE_CANCELLED, "传输已取消", finished=True)
        if status.status == "unknown":
            return self._to_manual_review("传输结果未知，保留来源，进入人工复核")
        if status.status == "failed":
            error = TransferError(
                status.error_code or ErrorCode.NETWORK_TRANSIENT,
                status.message or "传输失败",
                retryable=self._code_retryable(status.error_code),
            )
            return self._handle_failure(error, PHASE_TRANSFERRING)
        return TransferStepResult(PHASE_TRANSFERRING, status.message or "传输进行中", finished=True, needs_wait=True)

    # ------------------------------------------------------------------
    # target_verifying
    # ------------------------------------------------------------------
    def _step_target_verifying(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "目标校验阶段缺少执行器"))
        verifier = self.verifier if self.verifier is not None else getattr(executor, "verify", None)
        if verifier is None:
            return self._to_manual_review("未配置目标校验器，无法确认目标")
        try:
            verify_call = getattr(verifier, "verify", None)
            if callable(verify_call):
                result = verify_call(self.plan)
            elif callable(verifier):
                result = verifier()
            else:
                return self._to_manual_review("目标校验器不可调用")
        except TransferError as exc:
            if exc.code == ErrorCode.RESULT_UNKNOWN:
                return self._to_manual_review(exc.message or "目标校验结果未知")
            return self._handle_failure(exc, PHASE_TARGET_VERIFYING)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"目标校验异常：{exc}")
        result = self._normalize_verify_result(result)
        if result.status == "ok":
            self.state.target_verified = True
            self.state.message = "目标校验通过"
            if self.plan.organizer_enabled:
                self.state.phase = PHASE_ORGANIZING
            else:
                self.state.phase = PHASE_ACK_PENDING
            return TransferStepResult(self.state.phase, "目标校验通过", finished=False)
        if result.status == "unknown":
            return self._to_manual_review(result.message or "目标校验结果未知，保留来源")
        # missing / mismatch：目标未稳定，进入验证重试，不清理（§13.3）
        return self._handle_failure(
            TransferError(
                ErrorCode.VERIFY_MISMATCH,
                result.message or "目标文件缺失或大小/文件数不符",
                retryable=True,
            ),
            PHASE_TARGET_VERIFYING,
        )

    # ------------------------------------------------------------------
    # organizing / ack / cleanup
    # ------------------------------------------------------------------
    def _step_organizing(self) -> TransferStepResult:
        if self.organizer_service is None:
            self.state.phase = PHASE_ACK_PENDING
            return TransferStepResult(PHASE_ACK_PENDING, "无需整理，进入 ACK", finished=False)
        try:
            result = self.organizer_service.organize(self.plan)
        except Exception as exc:  # noqa: BLE001
            result = OrganizeResult("failed", f"整理异常：{exc}")
        result = self._normalize_organize_result(result)
        if result.status == "done":
            self.state.phase = PHASE_ACK_PENDING
            return TransferStepResult(PHASE_ACK_PENDING, "整理完成，进入 ACK", finished=False)
        if result.status == "pending":
            return TransferStepResult(PHASE_ORGANIZING, result.message or "整理进行中", finished=True, needs_wait=True)
        if result.status == "unknown":
            return self._to_manual_review(result.message or "整理结果未知")
        return self._handle_failure(
            TransferError(ErrorCode.NETWORK_TRANSIENT, result.message or "整理失败", retryable=result.retryable),
            PHASE_ORGANIZING,
        )

    def _step_ack(self) -> TransferStepResult:
        if not self.state.target_verified:
            # 防御：ACK 前必须已确认目标
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "目标未确认，回到校验", finished=False)
        if self.ack_service is None:
            ack = AckResult(acked=True)
        else:
            try:
                ack = self.ack_service.ack(self.plan)
            except Exception as exc:  # noqa: BLE001
                ack = AckResult(acked=False, message=f"ACK 异常：{exc}")
        ack = self._normalize_ack_result(ack)
        self.state.app_acked = bool(ack.acked)
        self.state.message = ack.message
        if ack.acked:
            if self.plan.delete_source_after_ack:
                self.state.phase = PHASE_CLEANUP_PENDING
            else:
                self.state.phase = PHASE_COMPLETED
            return TransferStepResult(self.state.phase, "ACK 完成", finished=False)
        # 目标已确认但 ACK 未完成：保留来源，恢复 ACK/清理，不重复传输（§13.3）
        return TransferStepResult(PHASE_ACK_PENDING, ack.message or "应用 ACK 未完成，保留来源", finished=True, needs_wait=True)

    def _step_cleanup(self) -> TransferStepResult:
        if self.cleanup is None:
            self.state.phase = PHASE_COMPLETED
            self.state.message = "完成（无清理服务）"
            return TransferStepResult(PHASE_COMPLETED, self.state.message, finished=True)
        try:
            result = self.cleanup.cleanup(
                self.plan,
                target_verified=self.state.target_verified,
                app_acked=self.state.app_acked,
            )
        except Exception as exc:  # noqa: BLE001
            result = type(
                "CleanupResult",
                (),
                {"ok": False, "reason": f"清理异常：{exc}", "to_dict": lambda self: {"ok": False, "reason": str(exc)}},
            )()
        ok = bool(getattr(result, "ok", True))
        reason = str(getattr(result, "reason", "") or getattr(result, "message", "") or "")
        if hasattr(result, "to_dict"):
            try:
                self.state.cleanup = result.to_dict()
            except Exception:
                self.state.cleanup = {"ok": ok}
        else:
            self.state.cleanup = {"ok": ok, "reason": reason}
        self.state.phase = PHASE_COMPLETED
        if ok:
            self.state.message = "完成，任务级来源落地与本地缓存已清理"
        else:
            # 清理失败不回滚已完成入库，记录告警并可重试（§13.3）
            self.state.message = f"完成（清理警告，可重试）：{reason or '清理失败'}"
        return TransferStepResult(PHASE_COMPLETED, self.state.message, finished=True)

    # ------------------------------------------------------------------
    # retry_wait / cancel_pending
    # ------------------------------------------------------------------
    def _step_retry_wait(self) -> TransferStepResult:
        if not self.state.retry_ready:
            return TransferStepResult(PHASE_RETRY_WAIT, "等待重试退避", finished=True, needs_wait=True)
        self.state.retry_ready = False
        retry_from = self.state.retry_from or PHASE_PLANNING
        self.state.phase = retry_from
        if self._retry_resets_receipt(retry_from):
            self.state.receipt = None
        return TransferStepResult(retry_from, "开始重试", finished=False)

    def _step_cancel_pending(self) -> TransferStepResult:
        receipt = self.state.receipt
        executor = self._executor()
        if receipt is not None and executor is not None and callable(getattr(executor, "poll", None)):
            try:
                status = executor.poll(receipt)
                if getattr(status, "status", "") == "cancelled":
                    self.state.phase = PHASE_CANCELLED
                    self.state.message = "取消已确认"
                    return TransferStepResult(PHASE_CANCELLED, "取消已确认", finished=True)
            except Exception:  # noqa: BLE001
                pass
        return TransferStepResult(PHASE_CANCEL_PENDING, "等待取消确认", finished=True, needs_wait=True)

    # ------------------------------------------------------------------
    # 取消（§13.1）
    # ------------------------------------------------------------------
    def cancel(self, *, direct: bool = False) -> CancelResult:
        phase = self.state.phase
        if phase in TERMINAL_PHASES:
            return CancelResult("already_final", f"任务已处于终态：{phase}", phase=phase)
        if phase == PHASE_PLANNING:
            self.state.phase = PHASE_CANCELLED
            self.state.message = "计划阶段直接取消"
            return CancelResult("cancelled", "计划阶段直接取消", phase=phase)
        # 目标已确认但 ACK 未完成：保留来源，恢复 ACK/清理，不重复传输
        if self.state.target_verified and not self.state.app_acked and phase in {
            PHASE_ACK_PENDING,
            PHASE_ORGANIZING,
            PHASE_TARGET_VERIFYING,
        }:
            return CancelResult(
                "resume_ack",
                "目标已确认但未完成 ACK，保留来源并恢复 ACK/清理，不重复传输",
                phase=phase,
            )
        receipt = self.state.receipt
        if receipt is None:
            # 尚未向 Provider/rclone 提交：可直接取消
            self.state.phase = PHASE_CANCELLED
            self.state.message = f"{phase} 阶段尚未提交，直接取消"
            return CancelResult("cancelled", f"{phase} 阶段尚未提交，直接取消", phase=phase)
        if receipt.status == "unknown":
            # 已提交但结果未知：进入 cancel_pending，不能假装失败后重试
            self.state.phase = PHASE_CANCEL_PENDING
            self.state.message = "操作结果未知，进入取消确认，不假失败重试"
            return CancelResult(
                "cancel_pending",
                "操作结果未知，进入取消确认，不假失败重试",
                operation_id=receipt.operation_id,
                phase=phase,
            )
        executor = self._executor()
        if direct and executor is not None and callable(getattr(executor, "poll", None)) and phase in {
            PHASE_SOURCE_MATERIALIZING,
            PHASE_SOURCE_WAITING_VISIBLE,
            PHASE_NATIVE_WAITING,
            PHASE_TRANSFERRING,
        }:
            try:
                completed_status = executor.poll(receipt)
                if getattr(completed_status, "status", "") == "completed":
                    self.state.phase = PHASE_CANCELLED
                    return CancelResult(
                        "cancelled",
                        "操作已完成但未通过目标校验，取消任务（目标可能残留文件，不做清理）",
                        operation_id=receipt.operation_id,
                        phase=phase,
                    )
            except Exception:  # noqa: BLE001
                pass
        if executor is None:
            self.state.phase = PHASE_CANCEL_PENDING
            return CancelResult("cancel_pending", "执行器缺失，无法确认取消", phase=phase)
        cancel_op = getattr(executor, "cancel", None)
        if not callable(cancel_op):
            self.state.phase = PHASE_CANCEL_PENDING
            return CancelResult(
                "cancel_pending",
                "执行器不支持取消，结果未知",
                operation_id=receipt.operation_id,
                phase=phase,
            )
        try:
            result = cancel_op(receipt)
        except Exception as exc:  # noqa: BLE001
            self.state.phase = PHASE_CANCEL_PENDING
            return CancelResult(
                "cancel_pending",
                f"取消请求异常：{exc}",
                operation_id=receipt.operation_id,
                phase=phase,
            )
        result = self._normalize_cancel_result(result)
        if result.status == "cancelled":
            self.state.phase = PHASE_CANCELLED
            self.state.message = result.message or "已确认取消"
            return CancelResult("cancelled", result.message or "已确认取消", operation_id=receipt.operation_id, phase=phase)
        self.state.phase = PHASE_CANCEL_PENDING
        self.state.message = result.message or "已请求取消，等待确认"
        return CancelResult(
            "cancel_pending",
            result.message or "已请求取消，等待确认",
            operation_id=receipt.operation_id,
            phase=phase,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _executor(self) -> Any:
        if not self.state.executor_strategy:
            return None
        executor = self.executors.get(self.state.executor_strategy)
        if executor is None:
            return None
        # 恢复（restore）状态下执行器可能尚未绑定计划；惰性绑定保证轮询/取消可用。
        if getattr(executor, "plan", None) is None:
            if hasattr(executor, "bind"):
                executor.bind(self.plan)
            else:
                executor.plan = self.plan
        return executor

    def _fail(self, error: TransferError) -> TransferStepResult:
        self.state.phase = PHASE_FAILED
        self.state.message = error.message
        self.state.error_code = error.code
        return TransferStepResult(PHASE_FAILED, error.message, finished=True)

    def _to_manual_review(self, message: str) -> TransferStepResult:
        self.state.phase = PHASE_MANUAL_REVIEW
        self.state.message = message
        self.state.error_code = ErrorCode.RESULT_UNKNOWN
        return TransferStepResult(PHASE_MANUAL_REVIEW, message, finished=True, needs_wait=True)

    def _handle_failure(self, error: TransferError, retry_from: str) -> TransferStepResult:
        self.state.attempt += 1
        self.state.message = error.message
        self.state.error_code = error.code
        if not error.retryable:
            self.state.phase = PHASE_FAILED
            return TransferStepResult(PHASE_FAILED, error.message, finished=True)
        if self.state.attempt >= self.retry_policy.max_attempts:
            self.state.phase = PHASE_FAILED
            self.state.message = f"重试次数耗尽：{error.message}"
            return TransferStepResult(PHASE_FAILED, self.state.message, finished=True)
        self.state.phase = PHASE_RETRY_WAIT
        self.state.retry_from = retry_from
        self.state.retry_ready = False
        return TransferStepResult(PHASE_RETRY_WAIT, error.message, finished=True, needs_wait=True)

    @staticmethod
    def _code_retryable(code: str) -> bool:
        from fnos_media_import.transfer import RETRYABLE_ERROR_CODES

        if not code:
            return True
        return code in RETRYABLE_ERROR_CODES

    def _receipt_error(self, receipt: OperationReceipt) -> TransferError:
        code = receipt.error_code or ErrorCode.NETWORK_TRANSIENT
        return TransferError(
            code,
            receipt.message or "操作失败",
            details=receipt.metadata,
            phase=self.state.phase,
        )

    @staticmethod
    def _retry_resets_receipt(phase: str) -> bool:
        return phase in {PHASE_SOURCE_MATERIALIZING, PHASE_NATIVE_SUBMITTING, PHASE_TRANSFERRING}

    @staticmethod
    def _normalize_verify_result(value: Any) -> Any:
        from fnos_media_import.transfer import TransferVerifyResult

        if isinstance(value, TransferVerifyResult):
            return value
        if isinstance(value, dict):
            text = str(value.get("status") or "").strip().lower()
            status = "ok" if text in {"ok", "success", "verified"} else ("missing" if text in {"missing", "mismatch", "fail", "failed"} else "unknown")
            return TransferVerifyResult(
                status=status,
                message=str(value.get("message") or ""),
                file_count=int(value.get("file_count") or 0),
                total_size=int(value.get("total_size") or 0),
                code=str(value.get("code") or ""),
            )
        text = str(value or "").strip().lower()
        if text in {"ok", "success", "verified"}:
            return TransferVerifyResult("ok")
        if text in {"missing", "mismatch", "fail", "failed"}:
            return TransferVerifyResult("missing", text)
        return TransferVerifyResult("unknown", "无法识别的校验结果")

    @staticmethod
    def _normalize_organize_result(value: Any) -> OrganizeResult:
        if isinstance(value, OrganizeResult):
            return value
        if isinstance(value, dict):
            return OrganizeResult(
                status=str(value.get("status") or "pending"),
                message=str(value.get("message") or ""),
                retryable=bool(value.get("retryable", True)),
            )
        text = str(value or "").strip().lower()
        if text in {"done", "success", "completed"}:
            return OrganizeResult("done")
        if text in {"failed", "error"}:
            return OrganizeResult("failed", text)
        if text in {"unknown"}:
            return OrganizeResult("unknown", text)
        return OrganizeResult("pending", text or "整理进行中")

    @staticmethod
    def _normalize_ack_result(value: Any) -> AckResult:
        if isinstance(value, AckResult):
            return value
        if isinstance(value, dict):
            return AckResult(acked=bool(value.get("acked", value.get("success", False))), message=str(value.get("message") or ""))
        return AckResult(acked=bool(value))

    @staticmethod
    def _normalize_cancel_result(value: Any) -> CancelResult:
        if isinstance(value, CancelResult):
            return value
        if isinstance(value, dict):
            text = str(value.get("status") or "").strip().lower()
            if text in {"cancelled", "canceled", "confirmed"}:
                status = "cancelled"
            elif text in {"resume_ack"}:
                status = "resume_ack"
            else:
                status = "cancel_pending"
            return CancelResult(status, str(value.get("message") or ""), str(value.get("operation_id") or ""))
        return CancelResult("cancel_pending", str(value or ""))
