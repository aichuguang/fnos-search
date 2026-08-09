"""TransferOrchestrator —— 按 V1 冻结计划驱动 §13.1 状态机的编排器。

职责：
- planning 阶段校验计划与策略约束（磁链->六盘、同源原生、Provider relay 只到移动云/天翼、rclone 不直接写入移动云/天翼）。
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
    PHASE_FINALIZING,
    PHASE_FINAL_VERIFYING,
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


_RELAY_UPLOAD_STRATEGIES = frozenset({"relay_upload_provider"})


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
    # ``attempt`` is a monotonic audit counter.  Retry budgets are scoped to
    # the phase that failed so a transient source visibility problem cannot
    # permanently consume the budget of a later transfer/verification phase.
    phase_attempts: dict[str, int] = field(default_factory=dict)
    retry_from: str = ""
    retry_ready: bool = False
    target_verified: bool = False
    staging_verified: bool = False
    final_verified: bool = False
    app_acked: bool = False
    message: str = ""
    error_code: str = ""
    executor_strategy: str = ""
    cleanup: dict[str, Any] = field(default_factory=dict)
    submission_fence: dict[str, Any] = field(default_factory=dict)
    organizer_task_id: int = 0
    organizer_final_path: str = ""


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
    """按 §13.1 状态机驱动单一 V1 冻结计划。"""

    def __init__(
        self,
        plan: dict[str, Any] | TransferPlan,
        executors: dict[str, Any],
        *,
        verifier: Any = None,
        cleanup: Any = None,
        ack_service: Any = None,
        organizer_service: Any = None,
        finalizer_service: Any = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.plan = plan if isinstance(plan, TransferPlan) else TransferPlan.from_dict(plan)
        self.executors = dict(executors or {})
        self.verifier = verifier
        self.cleanup = cleanup
        self.ack_service = ack_service
        self.organizer_service = organizer_service
        self.finalizer_service = finalizer_service
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
            "phase_attempts": dict(self.state.phase_attempts),
            "retry_from": self.state.retry_from,
            "retry_ready": self.state.retry_ready,
            "target_verified": self.state.target_verified,
            "staging_verified": self.state.staging_verified,
            "final_verified": self.state.final_verified,
            "app_acked": self.state.app_acked,
            "executor_strategy": self.state.executor_strategy,
            "receipt": asdict(self.state.receipt) if self.state.receipt is not None else None,
            "cleanup": dict(self.state.cleanup),
            "submission_fence": dict(self.state.submission_fence),
            "organizer_task_id": self.state.organizer_task_id,
            "organizer_final_path": self.state.organizer_final_path,
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
        raw_phase_attempts = raw.get("phase_attempts")
        if isinstance(raw_phase_attempts, dict):
            parsed_phase_attempts: dict[str, int] = {}
            for key, value in raw_phase_attempts.items():
                phase_key = str(key or "").strip()
                if not phase_key:
                    continue
                try:
                    parsed_value = int(value or 0)
                except (TypeError, ValueError):
                    parsed_value = 0
                if parsed_value > 0:
                    parsed_phase_attempts[phase_key] = parsed_value
            self.state.phase_attempts = parsed_phase_attempts
        else:
            self.state.phase_attempts = {}
        self.state.retry_from = str(raw.get("retry_from") or "")
        self.state.retry_ready = bool(raw.get("retry_ready"))
        self.state.target_verified = bool(raw.get("target_verified"))
        self.state.staging_verified = bool(raw.get("staging_verified"))
        self.state.final_verified = bool(raw.get("final_verified"))
        self.state.app_acked = bool(raw.get("app_acked"))
        self.state.executor_strategy = str(raw.get("executor_strategy") or "")
        self.state.cleanup = dict(raw.get("cleanup") or {})
        self.state.submission_fence = dict(raw.get("submission_fence") or {})
        try:
            self.state.organizer_task_id = int(raw.get("organizer_task_id") or 0)
        except (TypeError, ValueError):
            self.state.organizer_task_id = 0
        self.state.organizer_final_path = str(raw.get("organizer_final_path") or "")
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
        if phase == PHASE_FINALIZING:
            return self._step_finalizing()
        if phase == PHASE_FINAL_VERIFYING:
            return self._step_final_verifying()
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
        if strategy in _RELAY_UPLOAD_STRATEGIES:
            if source_kind == dest_kind:
                return TransferError(ErrorCode.INVALID_PLAN, "同 Provider 传输必须使用 native_save")
            return None
        if strategy == "rclone_copy":
            if dest_kind in {"mobile", "tianyi"}:
                return TransferError(
                    ErrorCode.INVALID_PLAN,
                    "rclone_copy 不允许直接写入移动云或天翼目标",
                )
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
        # ``pending`` is a healthy asynchronous state, not a failed Provider
        # operation.  Waiting for a large landing must not consume the shared
        # retry budget or eventually turn into a hard failure merely because
        # several visibility polls were required.
        self.state.message = "来源未稳定可见，等待后继续轮询"
        self.state.error_code = ErrorCode.OPERATION_PENDING
        return TransferStepResult(
            PHASE_SOURCE_WAITING_VISIBLE,
            self.state.message,
            finished=True,
            needs_wait=True,
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
        if receipt.status == "completed":
            # Providers are allowed to complete synchronously.  Do not invent
            # a task id and enter a polling phase when the durable receipt
            # already proves completion.
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "原生任务已同步完成，进入目标校验", finished=False)
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
        if receipt.status == "completed":
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "原生任务已完成，进入目标校验", finished=False)
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
            if receipt.status == "completed":
                self.state.phase = PHASE_TARGET_VERIFYING
                return TransferStepResult(PHASE_TARGET_VERIFYING, "传输已同步完成，进入目标校验", finished=False)
            return TransferStepResult(PHASE_TRANSFERRING, receipt.message or "传输已启动", finished=True, needs_wait=True)
        if receipt.status == "completed":
            self.state.phase = PHASE_TARGET_VERIFYING
            return TransferStepResult(PHASE_TARGET_VERIFYING, "传输已完成，进入目标校验", finished=False)
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
    def _verify_request(self, descriptor: dict[str, Any] | None) -> Any:
        """构造 V1 原生校验请求。

        Provider 原生校验统一接收 ``VerifyRequest``；路径 descriptor 仍由
        编排器冻结计划决定，不能再以 ``plan + 一组散落关键字`` 调用原生
        executor。Native executor 使用 plan 的 native locator，并把冻结
        operation receipt 作为正式校验请求的一部分传入。
        """

        from fnos_media_import.transfer.verifier import VerifyRequest

        expected = self.plan.verification_expected()
        return VerifyRequest(
            source=self.plan.source_descriptor,
            destination=self.plan.destination_descriptor,
            locator=self.plan.native_locator(),
            expected_file_count=int(expected["file_count"]),
            expected_total_size=int(expected["total_size"]),
            checksum=str(expected["checksum"]),
            operation=self.state.receipt,
        )

    def _invoke_verifier(self, verifier: Any, *, descriptor: dict[str, Any], native_target: bool) -> Any:
        """按统一协议调用目标校验器。

        通用文件系统校验器保留现有 ``verify(plan, **kwargs)`` 契约；原生
        校验器严格且仅收到包含冻结 operation 的 VerifyRequest。
        """

        object_verify = getattr(verifier, "verify", None)
        verify_call = object_verify
        if not callable(verify_call) and callable(verifier):
            verify_call = verifier
        if not callable(verify_call):
            raise TypeError("目标校验器不可调用")

        if native_target:
            request = self._verify_request(descriptor)
            return verify_call(request)

        expected = self.plan.verification_expected()
        if callable(object_verify):
            return verify_call(
                self.plan,
                descriptor=descriptor,
                expected_file_count=int(expected["file_count"]),
                expected_total_size=int(expected["total_size"]),
                checksum=str(expected["checksum"]),
            )
        # 裸 callable 的 V1 兼容约定：传入冻结计划，而不是 receipt/None。
        return verify_call(self.plan)

    def _step_target_verifying(self) -> TransferStepResult:
        executor = self._executor()
        if executor is None:
            return self._fail(TransferError(ErrorCode.NO_EXECUTOR, "目标校验阶段缺少执行器"))
        # Relay uploads write directly to the mobile Provider final root.  A
        # stale/hand-built plan may still carry a native destination_staging
        # alias; never let that alias select the wrong verification target.
        descriptor = (
            self.plan.destination_final
            if self.plan.strategy in _RELAY_UPLOAD_STRATEGIES
            else self.plan.destination_staging or self.plan.destination_final
        )
        native_target = self._is_native_descriptor(descriptor)
        verifier = getattr(executor, "verify", None) if native_target else self.verifier
        if verifier is None:
            verifier = getattr(executor, "verify", None)
        if verifier is None:
            return self._to_manual_review("未配置目标校验器，无法确认目标")
        try:
            result = self._invoke_verifier(verifier, descriptor=descriptor, native_target=native_target)
        except TransferError as exc:
            if exc.code == ErrorCode.RESULT_UNKNOWN:
                return self._to_manual_review(exc.message or "目标校验结果未知")
            return self._handle_failure(exc, PHASE_TARGET_VERIFYING)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"目标校验异常：{exc}")
        result = self._normalize_verify_result(result)
        if result.status == "ok":
            self.state.staging_verified = True
            self.state.message = "暂存校验通过"
            if self.plan.organizer_enabled:
                if self.organizer_service is None:
                    return self._to_manual_review("Organizer 已启用但未装配，禁止跳过整理")
                self.state.phase = PHASE_ORGANIZING
            elif self.plan.strategy in _RELAY_UPLOAD_STRATEGIES or self.plan.destination_staging is None:
                self.state.phase = PHASE_FINAL_VERIFYING
            else:
                self.state.phase = PHASE_FINALIZING
            return TransferStepResult(self.state.phase, "暂存校验通过", finished=False)
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
            return self._to_manual_review("Organizer 已启用但未装配，禁止跳过整理")
        try:
            result = self._invoke_organizer()
        except Exception as exc:  # noqa: BLE001
            result = OrganizeResult("failed", f"整理异常：{exc}")
        result = self._normalize_organize_result(result)
        if result.task_id:
            self.state.organizer_task_id = result.task_id
        if result.final_path:
            self.state.organizer_final_path = result.final_path
        if result.status == "done":
            self.state.phase = PHASE_FINAL_VERIFYING
            return TransferStepResult(PHASE_FINAL_VERIFYING, "整理完成，进入最终校验", finished=False)
        if result.status == "pending":
            return TransferStepResult(PHASE_ORGANIZING, result.message or "整理进行中", finished=True, needs_wait=True)
        if result.status == "unknown":
            return self._to_manual_review(result.message or "整理结果未知")
        return self._handle_failure(
            TransferError(ErrorCode.NETWORK_TRANSIENT, result.message or "整理失败", retryable=result.retryable),
            PHASE_ORGANIZING,
        )

    def _step_finalizing(self) -> TransferStepResult:
        if self.plan.strategy in _RELAY_UPLOAD_STRATEGIES:
            # Relay has no staging -> final archive operation.  This guard also
            # repairs a restored snapshot produced by an older planner without
            # invoking TransferFinalizer against a native namespace.
            self.state.phase = PHASE_FINAL_VERIFYING
            return TransferStepResult(PHASE_FINAL_VERIFYING, "中继已写入最终目录，跳过归档", finished=False)
        if self.finalizer_service is None:
            return self._to_manual_review("未装配 staging 到 final 的归档服务")
        try:
            value = self.finalizer_service.finalize(self.plan)
        except Exception as exc:  # noqa: BLE001
            return self._handle_failure(
                TransferError(ErrorCode.NETWORK_TRANSIENT, f"归档异常：{exc}", retryable=True),
                PHASE_FINALIZING,
            )
        if isinstance(value, dict):
            status = str(value.get("status") or "").lower()
            retryable = bool(value.get("retryable", True))
            failure_message = str(value.get("message") or "归档到最终目录失败")
            failure_code = str(value.get("code") or ErrorCode.NETWORK_TRANSIENT)
        else:
            status = str(getattr(value, "status", None) or value or "").lower()
            retryable = True
            failure_message = "归档到最终目录失败"
            failure_code = ErrorCode.NETWORK_TRANSIENT
        if status in {"done", "completed", "ok", "success", "true"} or value is True:
            self.state.phase = PHASE_FINAL_VERIFYING
            return TransferStepResult(PHASE_FINAL_VERIFYING, "已归档到最终目录", finished=False)
        if status in {"pending", "running", "submitted"}:
            return TransferStepResult(PHASE_FINALIZING, "归档进行中", finished=True, needs_wait=True)
        return self._handle_failure(
            TransferError(
                failure_code,
                failure_message,
                retryable=retryable,
            ),
            PHASE_FINALIZING,
        )

    def _step_final_verifying(self) -> TransferStepResult:
        if self.plan.organizer_enabled:
            organizer = self.plan.organizer_path or {}
            descriptor = (
                {
                    **organizer,
                    "path": self.state.organizer_final_path,
                }
                if self.state.organizer_final_path
                else None
            )
        else:
            descriptor = self.plan.destination_final
        if descriptor is None:
            return self._to_manual_review("缺少 Organizer 最终映射或最终目录，无法完成入库")
        native_target = self._is_native_descriptor(descriptor)
        executor = self._executor()
        verifier = getattr(executor, "verify", None) if native_target and executor else self.verifier
        if verifier is None:
            return self._to_manual_review("未配置最终目录校验器")
        try:
            result = self._invoke_verifier(verifier, descriptor=descriptor, native_target=native_target)
        except Exception as exc:  # noqa: BLE001
            return self._to_manual_review(f"最终目录校验异常：{exc}")
        result = self._normalize_verify_result(result)
        if result.status == "ok":
            self.state.final_verified = True
            self.state.target_verified = True
            self.state.phase = PHASE_ACK_PENDING
            return TransferStepResult(PHASE_ACK_PENDING, "最终目录校验通过", finished=False)
        if result.status == "unknown":
            return self._to_manual_review(result.message or "最终目录状态未知")
        return self._handle_failure(
            TransferError(ErrorCode.VERIFY_MISMATCH, result.message or "最终目录校验失败", retryable=True),
            PHASE_FINAL_VERIFYING,
        )

    def _step_ack(self) -> TransferStepResult:
        if not self.state.final_verified:
            # V1 ACK is gated by the final directory verification, not merely
            # the staging/target check.  A restored legacy snapshot may only
            # have target_verified; it is sent back through the final gate.
            self.state.phase = PHASE_FINAL_VERIFYING
            return TransferStepResult(PHASE_FINAL_VERIFYING, "最终目录未确认，回到最终校验", finished=False)
        if self.ack_service is None:
            return self._to_manual_review("未装配应用 ACK 服务，禁止完成任务")
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
            return self._to_manual_review("未装配清理服务，禁止跳过任务级落地清理")
        previous_cleanup = dict(self.state.cleanup or {})
        previous_attempt = previous_cleanup.get("attempt")
        try:
            result = self.cleanup.cleanup(
                self.plan,
                target_verified=self.state.target_verified,
                app_acked=self.state.app_acked,
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"清理异常：{exc}"
            return self._handle_cleanup_failure(reason, retryable=True)

        if isinstance(result, dict):
            ok = bool(result.get("ok", False))
            retryable = bool(result.get("retryable", False))
            reason = str(result.get("reason") or result.get("message") or "")
        else:
            ok = bool(getattr(result, "ok", True))
            retryable = bool(getattr(result, "retryable", False))
            reason = str(getattr(result, "reason", "") or getattr(result, "message", "") or "")
        if hasattr(result, "to_dict"):
            try:
                self.state.cleanup = result.to_dict()
            except Exception:
                self.state.cleanup = {"ok": ok, "retryable": retryable, "reason": reason}
        else:
            self.state.cleanup = {
                **(dict(result) if isinstance(result, dict) else {}),
                "ok": ok,
                "retryable": retryable,
                "reason": reason,
            }
        if previous_attempt is not None:
            self.state.cleanup.setdefault("attempt", previous_attempt)
        if not ok:
            return self._handle_cleanup_failure(reason or "清理失败", retryable=retryable)

        self.state.error_code = ""
        self.state.phase = PHASE_COMPLETED
        if ok:
            self.state.message = "完成，任务级来源落地与本地缓存已清理"
        return TransferStepResult(PHASE_COMPLETED, self.state.message, finished=True)

    def _handle_cleanup_failure(self, reason: str, *, retryable: bool) -> TransferStepResult:
        """把清理失败保留在 durable cleanup 阶段，禁止伪装 completed。"""

        previous = self.state.cleanup if isinstance(self.state.cleanup, dict) else {}
        try:
            attempt = int(previous.get("attempt") or 0) + 1
        except (TypeError, ValueError):
            attempt = 1
        self.state.cleanup = {
            **previous,
            "ok": False,
            "warning": False,
            "retryable": bool(retryable),
            "attempt": attempt,
            "reason": str(reason or "清理失败"),
        }
        self.state.error_code = ErrorCode.CLEANUP_FAILED
        self.state.message = f"清理失败：{reason or '未知错误'}"
        if not retryable or attempt >= max(1, int(self.retry_policy.max_attempts)):
            self.state.phase = PHASE_FAILED
            self.state.message = f"清理失败且无法继续重试：{reason or '未知错误'}"
            return TransferStepResult(PHASE_FAILED, self.state.message, finished=True)
        # 等待下一次 durable worker 唤醒；来源仍保留，不能进入 completed。
        self.state.phase = PHASE_CLEANUP_PENDING
        return TransferStepResult(PHASE_CLEANUP_PENDING, self.state.message, finished=True, needs_wait=True)

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
                status = normalize_operation_status(executor.poll(receipt))
                normalized = status.status
                if normalized == "cancelled":
                    self.state.phase = PHASE_CANCELLED
                    self.state.message = "取消已确认"
                    return TransferStepResult(PHASE_CANCELLED, "取消已确认", finished=True)
                if normalized in {"completed", "failed"}:
                    self.state.phase = PHASE_CANCELLED
                    self.state.message = (
                        "外部操作已完成，任务按取消终止；目标可能存在残留，禁止自动删除"
                        if normalized == "completed"
                        else "外部操作已失败，取消终止已确认"
                    )
                    return TransferStepResult(
                        PHASE_CANCELLED,
                        self.state.message,
                        finished=True,
                    )
                if normalized in {"running", "submitted"}:
                    cancel_op = getattr(executor, "cancel", None)
                    if callable(cancel_op):
                        cancellation = self._normalize_cancel_result(cancel_op(receipt))
                        if cancellation.status == "cancelled":
                            self.state.phase = PHASE_CANCELLED
                            self.state.message = cancellation.message or "取消已确认"
                            return TransferStepResult(
                                PHASE_CANCELLED,
                                self.state.message,
                                finished=True,
                            )
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
                completed_status = normalize_operation_status(executor.poll(receipt))
                if completed_status.status == "completed":
                    self.state.phase = PHASE_CANCELLED
                    return CancelResult(
                        "cancelled",
                        "操作已完成但未通过目标校验，取消任务（目标可能残留文件，不做清理）",
                        operation_id=receipt.operation_id,
                        phase=phase,
                    )
                if completed_status.status in {"cancelled", "failed"}:
                    self.state.phase = PHASE_CANCELLED
                    self.state.message = (
                        "取消已确认"
                        if completed_status.status == "cancelled"
                        else "外部操作已失败，取消终止已确认"
                    )
                    return CancelResult(
                        "cancelled",
                        self.state.message,
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
    @staticmethod
    def _is_native_descriptor(descriptor: dict[str, Any] | None) -> bool:
        if not isinstance(descriptor, dict):
            return False
        kind = str(descriptor.get("kind") or "").strip().lower()
        if kind:
            return kind == "native"
        return str(descriptor.get("namespace_id") or "").strip().lower().startswith("native:")

    def _invoke_organizer(self) -> Any:
        """Pass the frozen Organizer task id through the formal V1 contract."""

        method = getattr(self.organizer_service, "organize", None)
        if not callable(method):
            raise TypeError("Organizer 服务不可调用")
        task_id = int(self.state.organizer_task_id or 0)
        return method(self.plan, organizer_task_id=task_id or None)

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
        phase_key = str(retry_from or self.state.phase or PHASE_PLANNING)
        phase_attempt = int(self.state.phase_attempts.get(phase_key, 0) or 0) + 1
        self.state.phase_attempts[phase_key] = phase_attempt
        self.state.message = error.message
        self.state.error_code = error.code
        if not error.retryable:
            self.state.phase = PHASE_FAILED
            return TransferStepResult(PHASE_FAILED, error.message, finished=True)
        if phase_attempt >= self.retry_policy.max_attempts:
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
                task_id=int(value.get("task_id") or value.get("organizer_task_id") or 0),
                final_path=str(value.get("final_path") or ""),
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
