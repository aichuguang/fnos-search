"""NativeTransferExecutor —— 同源原生转存（native_save）与六盘磁链离线（native_offline）。

- ``native_save``：来源与目标同 Provider 的原生转存，直接写目标任务级 staging/final，不下载本地。
- ``native_offline``：磁链/种子 -> 六盘离线（唯一合法磁链链路）。
- 天翼等未实现的 Adapter 返回 NATIVE_TRANSFER_UNAVAILABLE，绝不降级为跨盘（§0 / §2.2）。
- 提交使用稳定的幂等键（§13.2）；Provider 返回未知结果时不重复创建。
"""

from __future__ import annotations

from typing import Any

from fnos_media_import.domain.models import (
    NativeLocator,
    ProviderCapabilities,
)
from fnos_media_import.importers.base import ImportResult
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
    receipt_from_import_result,
)

__all__ = [
    "NativeTransferExecutor",
    "NativeSourceMaterializer",
    "LegacyNativeAdapter",
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
                supports_relay_upload=_caps_to_bool(caps.get("supports_relay_upload")),
            )
    return ProviderCapabilities()


def _poll_adapter(receipt: OperationReceipt, adapter: Any) -> OperationStatus:
    poll_op = getattr(adapter, "poll_operation", None)
    if not callable(poll_op):
        if receipt.status == "completed":
            return OperationStatus("completed", receipt.message)
        return OperationStatus("running", "Provider 不支持轮询，按进行中处理")
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
        locator = self.plan.native_locator()
        idempotency_key = self.plan.idempotency_key
        try:
            if self.strategy == "native_offline":
                request = NativeOfflineRequest(
                    idempotency_key=idempotency_key,
                    source=self.plan.source_descriptor,
                    destination=self.plan.destination_descriptor,
                    locator=locator,
                    account_id=self.plan.source_account_id,
                    account_revision=self.plan.source_account_revision,
                    save_path=(self.plan.destination_staging or {}).get("path", "") or "",
                )
                receipt = adapter.native_offline(request)
            else:
                request = NativeSaveRequest(
                    idempotency_key=idempotency_key,
                    source=self.plan.source_descriptor,
                    destination=self.plan.destination_descriptor,
                    locator=locator,
                    account_id=self.plan.source_account_id,
                    account_revision=self.plan.source_account_revision,
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
        return normalize_receipt(receipt, provider_kind=self.plan.source_provider_kind)

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        adapter = self.provider_adapters.get(receipt.provider_kind) or self._adapter()
        try:
            return _poll_adapter(receipt, adapter)
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"原生任务轮询失败：{exc}", retryable=True) from exc

    def verify(self, request: Any = None) -> TransferVerifyResult:
        adapter = self._adapter()
        verify_op = getattr(adapter, "verify_native_result", None)
        if not callable(verify_op):
            return TransferVerifyResult("unknown", "Provider 不支持原生结果校验", code=ErrorCode.RESULT_UNKNOWN)
        verify_request = request or self._default_verify_request()
        try:
            value = verify_op(verify_request)
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

    def _default_verify_request(self) -> Any:
        from fnos_media_import.transfer.verifier import VerifyRequest

        expected = self.plan.verification_expected()
        return VerifyRequest(
            source=self.plan.source_descriptor,
            destination=self.plan.destination_descriptor,
            locator=self.plan.native_locator(),
            expected_file_count=int(expected["file_count"]),
            expected_total_size=int(expected["total_size"]),
            checksum=str(expected["checksum"]),
        )


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
        native_save = getattr(adapter, "native_save", None)
        if not callable(native_save):
            raise TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, f"{plan.source_provider_kind} 不支持原生保存到落地区")
        request = NativeSaveRequest(
            idempotency_key=f"landing:{plan.idempotency_key}",
            source=plan.source_descriptor,
            destination=plan.destination_descriptor,
            locator=NativeLocator(kind="landing_path", value=landing["path"]),
            account_id=plan.source_account_id,
            account_revision=plan.source_account_revision,
            category_key=plan.category_key,
        )
        try:
            receipt = native_save(request)
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
        return normalize_receipt(receipt, provider_kind=plan.source_provider_kind)


class LegacyNativeAdapter:
    """把现有 Importer（Cloud139Importer / QuarkImporter / SixPanOfflineImporter）包装为 v4 Adapter。

    只做协议翻译，不修改原 importer。真实账号的分享 URL / 选择集由可选的
    ``source_loader`` 提供；未提供时使用 canonical_source_id 作为分享来源。
    """

    def __init__(
        self,
        importer: Any,
        provider_kind: str,
        *,
        supports_native_save: bool = False,
        supports_native_offline: bool = False,
        supports_operation_polling: bool = False,
        supports_native_verify: bool = False,
        source_loader: Any = None,
        category_for_request: Any = None,
    ) -> None:
        self.importer = importer
        self.kind = str(provider_kind)
        self._supports_native_save = bool(supports_native_save)
        self._supports_native_offline = bool(supports_native_offline)
        self._supports_operation_polling = bool(supports_operation_polling)
        self._supports_native_verify = bool(supports_native_verify)
        self.source_loader = source_loader
        self.category_for_request = category_for_request

    def capabilities(self, account: Any = None) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_native_save=self._supports_native_save,
            supports_native_offline=self._supports_native_offline,
            supports_operation_polling=self._supports_operation_polling,
            supports_native_verify=self._supports_native_verify,
        )

    # -- 提交 --
    def native_save(self, request: NativeSaveRequest) -> OperationReceipt:
        if not self._supports_native_save:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                f"{self.kind} 同源原生转存未实现，不降级为跨盘",
            )
        title, share_url, password, selection = self._load_source(request)
        category = self._category(request)
        save_path = str((request.locator.value if request.locator else "") or request.destination.destination_id)
        result = self.importer.import_resource(
            title=title,
            share_url=share_url,
            save_path=save_path,
            password=password,
            category_key=request.category_key,
            category=category,
            native_selection=selection if self.kind == "cloud139" else None,
            quark_selection=selection if self.kind == "quark" else None,
        )
        if isinstance(result, ImportResult):
            return receipt_from_import_result(result, provider_kind=self.kind)
        return normalize_receipt(result, provider_kind=self.kind)

    def native_offline(self, request: NativeOfflineRequest) -> OperationReceipt:
        if not self._supports_native_offline:
            raise TransferError(ErrorCode.NATIVE_TRANSFER_UNAVAILABLE, f"{self.kind} 不支持原生离线")
        category = self._category(request)
        result = self.importer.import_resource(
            title=str(request.source.canonical_source_id or "磁链任务"),
            source_url=str(request.source.canonical_source_id or ""),
            category=category,
            save_path=request.save_path,
        )
        if isinstance(result, ImportResult):
            return receipt_from_import_result(result, provider_kind=self.kind)
        return normalize_receipt(result, provider_kind=self.kind)

    # -- 轮询 --
    def poll_operation(self, receipt: OperationReceipt) -> OperationStatus:
        if not self._supports_operation_polling:
            return OperationStatus("running", "Provider 不支持轮询，按进行中处理")
        poll_op = getattr(self.importer, "poll_operation", None)
        if not callable(poll_op):
            return OperationStatus("running", "Provider 不支持轮询，按进行中处理")
        return normalize_operation_status(poll_op(receipt))

    def can_native_transfer(self, source: Any, destination: Any, account: Any = None) -> Any:
        from ..planning import CapabilityDecision

        if not self._supports_native_save:
            return CapabilityDecision(
                available=False,
                reason=f"{self.kind} 同源原生转存未实现，不降级为跨盘",
                code="NATIVE_TRANSFER_UNAVAILABLE",
            )
        source_kind = str(getattr(source, "provider_kind", None) or "").strip().lower()
        destination_kind = str(getattr(destination, "provider_kind", None) or "").strip().lower()
        if source_kind != self.kind or destination_kind != self.kind:
            return CapabilityDecision(available=False, reason="非同源目标，不适用原生转存", code="NOT_SAME_PROVIDER")
        return CapabilityDecision(available=True)

    def relay_uploader(self) -> Any:
        if self.kind != "mobile":
            return None
        from ..cmcc.uploader import CmccUploader

        return CmccUploader

    # -- 结果校验 --
    def verify_native_result(self, request: Any) -> TransferVerifyResult:
        verify_op = getattr(self.importer, "verify_native_result", None)
        if not callable(verify_op):
            return TransferVerifyResult("unknown", "Provider 不支持原生结果校验", code=ErrorCode.RESULT_UNKNOWN)
        value = verify_op(request)
        return normalize_verify_result(value)

    # -- 取消 --
    def cancel_operation(self, receipt: OperationReceipt) -> CancelResult:
        cancel_op = getattr(self.importer, "cancel_operation", None)
        if not callable(cancel_op):
            return CancelResult("not_cancellable", "Provider 不支持取消，结果未知", operation_id=receipt.operation_id)
        value = cancel_op(receipt)
        if isinstance(value, CancelResult):
            return value
        return CancelResult("cancel_pending", "已请求取消，等待确认", operation_id=receipt.operation_id)

    # -- 内部 --
    def _load_source(self, request: NativeSaveRequest) -> tuple[str, str, str, dict[str, Any]]:
        if callable(self.source_loader):
            try:
                loaded = self.source_loader(request)
                if isinstance(loaded, dict):
                    return (
                        str(loaded.get("title") or request.source.canonical_source_id),
                        str(loaded.get("share_url") or request.source.canonical_source_id),
                        str(loaded.get("password") or ""),
                        dict(loaded.get("selection") or {}),
                    )
            except Exception:  # noqa: BLE001
                pass
        return request.source.canonical_source_id, request.source.canonical_source_id, "", {}

    def _category(self, request: Any) -> dict[str, Any]:
        if callable(self.category_for_request):
            try:
                category = self.category_for_request(request)
                if isinstance(category, dict):
                    return category
            except Exception:  # noqa: BLE001
                pass
        return {"label": request.category_key or "未分类"}
