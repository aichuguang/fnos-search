"""NativeTransferExecutor —— 同源原生转存（native_save）与六盘磁链离线（native_offline）。

- ``native_save``：来源与目标同 Provider 的原生转存，直接写目标任务级 staging/final，不下载本地。
- ``native_offline``：磁链/种子 -> 六盘离线（唯一合法磁链链路）。
- Provider 声明但未实现的原生能力返回 NATIVE_TRANSFER_UNAVAILABLE，绝不静默降级为跨盘（§0 / §2.2）。
- 提交使用稳定的幂等键（§13.2）；Provider 返回未知结果时不重复创建。
"""

from __future__ import annotations

from typing import Any

from fnos_media_import.domain.models import (
    ProviderCapabilities,
)
from fnos_media_import.transfer import (
    CancelResult,
    ErrorCode,
    NativeOfflineRequest,
    NativeSaveRequest,
    OperationReceipt,
    OperationStatus,
    TransferError,
    TransferPlan,
    TransferVerifyResult,
    normalize_operation_status,
    normalize_receipt,
    normalize_verify_result,
)

__all__ = [
    "NativeTransferExecutor",
    "NativeSourceMaterializer",
]


def _caps_to_bool(value: Any) -> bool:
    return bool(value)


def _load_capabilities(adapter: Any, provider_kind: str) -> ProviderCapabilities:
    caps_call = getattr(adapter, "capabilities", None)
    if callable(caps_call):
        try:
            caps = caps_call(account=None)
        except Exception:  # noqa: BLE001
            caps = None
        if isinstance(caps, ProviderCapabilities):
            return caps
        if isinstance(caps, dict):
            return ProviderCapabilities(
                supports_search=_caps_to_bool(caps.get("supports_search")),
                supports_share_inspection=_caps_to_bool(caps.get("supports_share_inspection")),
                supports_native_save=_caps_to_bool(caps.get("supports_native_save")),
                supports_native_offline=_caps_to_bool(caps.get("supports_native_offline")),
                supports_operation_polling=_caps_to_bool(caps.get("supports_operation_polling")),
                supports_native_verify=_caps_to_bool(caps.get("supports_native_verify")),
                supports_source_materialization=_caps_to_bool(caps.get("supports_source_materialization")),
                supports_relay_upload=_caps_to_bool(caps.get("supports_relay_upload")),
                supports_rclone_dest=_caps_to_bool(caps.get("supports_rclone_dest")),
                supports_direct_final=_caps_to_bool(caps.get("supports_direct_final")),
                supports_quota=_caps_to_bool(caps.get("supports_quota")),
            )
    return ProviderCapabilities()


def _poll_adapter(receipt: OperationReceipt, adapter: Any) -> OperationStatus:
    poll_op = getattr(adapter, "poll_operation", None)
    if not callable(poll_op):
        if receipt.status == "completed":
            return OperationStatus("completed", receipt.message)
        # 不得把「没有轮询实现」伪装成 running。这样会让 durable
        # execution 永久停在 native_waiting/transferring，且没有任何可
        # 观测的失败状态。未知能力必须 fail-closed，交给编排器进入
        # manual_review。
        return OperationStatus(
            "unknown",
            "Provider 未实现原生任务轮询，无法确认操作结果",
            error_code=ErrorCode.RESULT_UNKNOWN,
        )
    value = poll_op(receipt)
    return normalize_operation_status(value)


class NativeTransferExecutor:
    """同源原生转存 / 六盘离线执行器。构造时固定 strategy（native_save 或 native_offline）。"""

    strategy: str = "native_save"
    needs_source_landing = False

    def __init__(
        self,
        provider_adapters: dict[str, Any],
        *,
        strategy: str = "native_save",
    ) -> None:
        self.provider_adapters = dict(provider_adapters or {})
        self.strategy = strategy

    def bind(self, plan: TransferPlan) -> "NativeTransferExecutor":
        self.plan = plan
        return self

    def _adapter(self) -> Any:
        provider_kind = self.plan.source_provider_kind
        adapter = self.provider_adapters.get(provider_kind)
        if adapter is None:
            raise TransferError(ErrorCode.INVALID_PLAN, f"缺少 Provider Adapter：{provider_kind}")
        return adapter

    def _validate_strategy(self) -> None:
        plan = self.plan
        if plan.strategy != self.strategy:
            raise TransferError(ErrorCode.INVALID_PLAN, f"执行器策略 {self.strategy} 与计划策略 {plan.strategy} 不一致")
        if self.strategy == "native_offline":
            if plan.source_form not in {"magnet", "torrent"}:
                raise TransferError(ErrorCode.MAGNET_TARGET_RESTRICTED, "native_offline 只接受磁链/种子")
            if plan.destination_provider_kind != "sixpan":
                raise TransferError(ErrorCode.MAGNET_TARGET_RESTRICTED, "磁链只能保存到六盘，不接受其他目标")
            return
        if self.strategy == "native_save":
            if plan.source_form != "share":
                raise TransferError(ErrorCode.INVALID_PLAN, "native_save 只适用于分享来源")
            if plan.source_provider_kind != plan.destination_provider_kind:
                raise TransferError(
                    ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                    "同源转存要求来源与目标 Provider 一致，失败不降级为跨盘",
                )
            return

    def submit(self, existing_receipt: OperationReceipt | None = None) -> OperationReceipt:
        if existing_receipt is not None:
            return existing_receipt
        self._validate_strategy()
        adapter = self._adapter()
        caps = _load_capabilities(adapter, self.plan.source_provider_kind)
        if self.strategy == "native_save" and not caps.supports_native_save:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                f"{self.plan.source_provider_kind} 不支持同源原生转存，不降级为跨盘",
            )
        if self.strategy == "native_offline" and not caps.supports_native_offline:
            raise TransferError(ErrorCode.NATIVE_TRANSFER_UNAVAILABLE, "六盘不支持原生离线")
        # 提交目录优先取冻结的原生入库暂存目录（分类父目录 / _入库暂存 / 分类名 /
        # job-N）；没有暂存目录时 native_submit_locator() 退回分类定位器。
        locator = self.plan.native_submit_locator()
        idempotency_key = self.plan.idempotency_key
        try:
            if self.strategy == "native_offline":
                request = NativeOfflineRequest(
                    idempotency_key=idempotency_key,
                    source=self.plan.source_descriptor,
                    destination=self.plan.destination_descriptor,
                    locator=locator,
                    account_id=self.plan.destination_account_id,
                    account_revision=self.plan.destination_account_revision,
                    save_path=locator.value,
                )
                receipt = adapter.native_offline(request)
            else:
                request = NativeSaveRequest(
                    idempotency_key=idempotency_key,
                    source=self.plan.source_descriptor,
                    destination=self.plan.destination_descriptor,
                    locator=locator,
                    account_id=self.plan.destination_account_id,
                    account_revision=self.plan.destination_account_revision,
                    category_key=self.plan.category_key,
                )
                receipt = adapter.native_save(request)
        except TransferError:
            raise
        except NotImplementedError as exc:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                f"{self.plan.source_provider_kind} 同源原生转存未实现，不降级为跨盘",
                details={"reason": str(exc)},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"原生提交失败：{exc}",
                retryable=True,
            ) from exc
        normalized = normalize_receipt(receipt, provider_kind=self.plan.source_provider_kind)
        return _receipt_with_operation_key(normalized, idempotency_key)

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        adapter = self.provider_adapters.get(receipt.provider_kind) or self._adapter()
        try:
            return _poll_adapter(receipt, adapter)
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"原生任务轮询失败：{exc}", retryable=True) from exc

    def verify(self, request: Any) -> TransferVerifyResult:
        """Verify a native result using the sole V1 ``VerifyRequest`` contract."""

        from fnos_media_import.transfer.verifier import VerifyRequest

        if not isinstance(request, VerifyRequest):
            raise TypeError("NativeTransferExecutor.verify requires VerifyRequest")
        adapter = self._adapter()
        verify_op = getattr(adapter, "verify_native_result", None)
        if not callable(verify_op):
            return TransferVerifyResult("unknown", "Provider 不支持原生结果校验", code=ErrorCode.RESULT_UNKNOWN)
        try:
            value = verify_op(request)
        except Exception as exc:  # noqa: BLE001
            return TransferVerifyResult("unknown", f"原生结果校验失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)
        return normalize_verify_result(value)

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        adapter = self.provider_adapters.get(receipt.provider_kind) or self._adapter()
        cancel_op = getattr(adapter, "cancel_operation", None)
        if not callable(cancel_op):
            return CancelResult("not_cancellable", "Provider 不支持取消，结果未知", operation_id=receipt.operation_id)
        try:
            value = cancel_op(receipt)
        except Exception as exc:  # noqa: BLE001
            return CancelResult("cancel_pending", f"取消请求异常：{exc}", operation_id=receipt.operation_id)
        if isinstance(value, CancelResult):
            return value
        if isinstance(value, dict):
            text = str(value.get("status") or "").strip().lower()
            if text in {"cancelled", "canceled", "confirmed"}:
                return CancelResult("cancelled", str(value.get("message") or ""), operation_id=receipt.operation_id)
            if text in {"not_cancellable", "unsupported"}:
                return CancelResult("not_cancellable", str(value.get("message") or ""), operation_id=receipt.operation_id)
        return CancelResult("cancel_pending", "已请求取消，等待确认", operation_id=receipt.operation_id)

class NativeSourceMaterializer:
    """跨盘/中继链路的来源物化器：调用来源 Provider 原生保存到任务级落地区（§10.3）。"""

    def __init__(self, provider_adapters: dict[str, Any]) -> None:
        self.provider_adapters = dict(provider_adapters or {})

    def __call__(self, plan: TransferPlan) -> OperationReceipt:
        adapter = self.provider_adapters.get(plan.source_provider_kind)
        if adapter is None:
            raise TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, f"缺少来源 Provider Adapter：{plan.source_provider_kind}")
        landing = plan.source_landing
        if landing is None:
            raise TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, "计划缺少来源落地区路径")
        materialize = getattr(adapter, "materialize_source", None)
        if not callable(materialize):
            raise TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, f"{plan.source_provider_kind} 不支持来源物化")
        request = NativeSaveRequest(
            idempotency_key=f"landing:{plan.idempotency_key}",
            source=plan.source_descriptor,
            destination=plan.destination_descriptor,
            locator=plan.source_native_locator,
            account_id=plan.source_account_id,
            account_revision=plan.source_account_revision,
            category_key=plan.category_key,
            landing_path=landing["path"],
        )
        try:
            receipt = materialize(request)
        except TransferError:
            raise
        except NotImplementedError as exc:
            raise TransferError(
                ErrorCode.SOURCE_LANDING_UNAVAILABLE,
                f"{plan.source_provider_kind} 来源落地区物化未实现",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"来源物化失败：{exc}",
                retryable=True,
            ) from exc
        return _receipt_with_operation_key(
            normalize_receipt(receipt, provider_kind=plan.source_provider_kind),
            f"landing:{plan.idempotency_key}",
        )


def _receipt_with_operation_key(receipt: OperationReceipt, operation_key: str) -> OperationReceipt:
    """Attach a durable idempotency fence to every native receipt."""

    key = str(operation_key or "").strip()
    metadata = dict(receipt.metadata or {})
    if key:
        metadata.setdefault("operation_key", key)
        metadata.setdefault("idempotency_key", key)
    if not str(receipt.operation_id or "").strip() and receipt.status not in {"completed", "failed", "cancelled"}:
        raise TransferError(
            ErrorCode.RESULT_UNKNOWN,
            "Provider 原生提交未返回可恢复的 operation_id，拒绝继续轮询",
        )
    return OperationReceipt(
        operation_id=receipt.operation_id or key,
        status=receipt.status,
        provider_kind=receipt.provider_kind,
        message=receipt.message,
        external_ref=receipt.external_ref or receipt.operation_id or key,
        error_code=receipt.error_code,
        metadata=metadata,
    )
