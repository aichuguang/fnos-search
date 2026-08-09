"""NativeTransferExecutor —— 同源原生转存（native_save）与六盘磁链离线（native_offline）。

- ``native_save``：来源与目标同 Provider 的原生转存，直接写目标任务级 staging/final，不下载本地。
- ``native_offline``：磁链/种子 -> 六盘离线（唯一合法磁链链路）。
- Provider 声明但未实现的原生能力返回 NATIVE_TRANSFER_UNAVAILABLE，绝不静默降级为跨盘（§0 / §2.2）。
- 提交使用稳定的幂等键（§13.2）；Provider 返回未知结果时不重复创建。
"""

from __future__ import annotations

import inspect
from typing import Any

from fnos_media_import.domain.models import (
    NativeLocator,
    ProviderCapabilities,
)
from fnos_media_import.providers.manifests import BUILTIN_MANIFESTS
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


def _call_verify_operation(operation: Any, request: Any, receipt: OperationReceipt | None = None) -> Any:
    """调用 Provider 校验方法，并兼容旧的单参数 Adapter。

    V1 的正式协议是 ``verify_native_result(VerifyRequest)``。LegacyNativeAdapter
    还需要一个冻结的 OperationReceipt 才能向旧 importer 查询任务，因此它
    可以声明可选的 ``receipt=`` 关键字。通过签名探测而不是捕获任意
    TypeError，避免 Provider 内部真正的 TypeError 被重复调用。
    """

    if receipt is None:
        return operation(request)
    try:
        signature = inspect.signature(operation)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters.values()
        accepts_receipt = "receipt" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        if accepts_receipt:
            return operation(request, receipt=receipt)
    return operation(request)


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

    def verify(
        self,
        request: Any = None,
        *,
        receipt: OperationReceipt | None = None,
        descriptor: dict[str, Any] | None = None,
        expected_file_count: int | None = None,
        expected_total_size: int | None = None,
        checksum: str | None = None,
    ) -> TransferVerifyResult:
        """校验原生转存结果。

        ``VerifyRequest`` 是 V1 对外的唯一校验请求类型。旧调用方曾把
        ``plan`` 和一组关键字直接传给此方法，导致 ``TypeError`` 并把所有
        原生任务误送入 manual_review；这里保留关键字形态作为输入兼容，
        但在调用 Provider 前始终收敛成 ``VerifyRequest``。

        LegacyNativeAdapter 还可通过可选 ``receipt`` 接收冻结的外部任务
        凭证，用于适配现有 importer 的任务查询接口。receipt 只作为适配
        信息传递，不会触发新的提交。
        """

        adapter = self._adapter()
        verify_op = getattr(adapter, "verify_native_result", None)
        if not callable(verify_op):
            return TransferVerifyResult("unknown", "Provider 不支持原生结果校验", code=ErrorCode.RESULT_UNKNOWN)

        active_receipt = receipt
        # 旧编排器曾直接把 OperationReceipt 传给 executor.verify；接受它
        # 作为恢复兼容输入，但 Provider 仍优先收到正式 VerifyRequest。
        if isinstance(request, OperationReceipt):
            active_receipt = request
            request = None
        verify_request = request if self._is_verify_request(request) else self._default_verify_request(
            descriptor=descriptor,
            expected_file_count=expected_file_count,
            expected_total_size=expected_total_size,
            checksum=checksum,
        )
        try:
            value = _call_verify_operation(verify_op, verify_request, active_receipt)
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

    @staticmethod
    def _is_verify_request(value: Any) -> bool:
        # 延迟导入，避免 transfer.verifier -> transfer 包的循环导入。
        from fnos_media_import.transfer.verifier import VerifyRequest

        return isinstance(value, VerifyRequest)

    def _default_verify_request(
        self,
        *,
        descriptor: dict[str, Any] | None = None,
        expected_file_count: int | None = None,
        expected_total_size: int | None = None,
        checksum: str | None = None,
    ) -> Any:
        from fnos_media_import.transfer.verifier import VerifyRequest

        expected = self.plan.verification_expected()
        if expected_file_count is None:
            expected_file_count = int(expected["file_count"])
        if expected_total_size is None:
            expected_total_size = int(expected["total_size"])
        if checksum is None:
            checksum = str(expected["checksum"])
        return VerifyRequest(
            source=self.plan.source_descriptor,
            destination=self.plan.destination_descriptor,
            locator=self.plan.native_locator(),
            expected_file_count=int(expected_file_count),
            expected_total_size=int(expected_total_size),
            checksum=str(checksum),
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


class LegacyNativeAdapter:
    """把现有 Importer 包装为 V1 Provider Adapter。

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
        supports_operation_polling: bool | None = None,
        supports_native_verify: bool | None = None,
        source_loader: Any = None,
        category_for_request: Any = None,
        importer_factory: Any = None,
        account_importer_factory: Any = None,
        source_record_service: Any = None,
    ) -> None:
        self.importer = importer
        self.kind = str(provider_kind)
        self._supports_native_save = bool(supports_native_save)
        self._supports_native_offline = bool(supports_native_offline)
        self.source_loader = source_loader
        self.category_for_request = category_for_request
        self.importer_factory = importer_factory
        self.account_importer_factory = account_importer_factory
        self.source_record_service = source_record_service
        # The built-in importers predate the V1 Adapter protocol.  Quark and
        # Tianyi expose the lifecycle methods directly, while Mobile and
        # SixPan expose lower-level task query APIs.  Infer lifecycle support
        # only when a real backend is present; callers can still explicitly
        # disable it with ``False``.  This prevents a composition root from
        # accidentally advertising a permanently-running native operation
        # merely because it forgot two constructor flags.
        inferred_polling = self._has_poll_backend(importer)
        self._supports_operation_polling = (
            inferred_polling
            if supports_operation_polling is None
            else bool(supports_operation_polling)
        )
        self._supports_native_verify = (
            inferred_polling or self._callable_attr(importer, "verify_native_result")
            if supports_native_verify is None
            else bool(supports_native_verify)
        )
        self.manifest = BUILTIN_MANIFESTS[self.kind]

    def capabilities(self, account: Any = None) -> ProviderCapabilities:
        # Manifest 是能力上限，构造器参数是本适配器在当前部署中真正启用
        # 的能力。不能直接把 manifest 原样返回，否则六盘等 importer 没有
        # poll/verify 实现时仍会被 Planner 当成健康的原生 Provider。
        caps = self.manifest.capabilities
        runtime_importer = self.importer
        if runtime_importer is None and isinstance(account, dict):
            try:
                runtime_importer = self._build_importer(account)
            except Exception:  # noqa: BLE001
                runtime_importer = None
        materialize_available = self._has_materialize_backend(runtime_importer)
        polling_available = self._has_poll_backend(runtime_importer)
        verify_available = polling_available or self._callable_attr(
            runtime_importer, "verify_native_result"
        )
        return ProviderCapabilities(
            supports_search=caps.supports_search,
            supports_share_inspection=caps.supports_share_inspection,
            supports_native_save=caps.supports_native_save and self._supports_native_save,
            supports_native_offline=caps.supports_native_offline and self._supports_native_offline,
            supports_operation_polling=caps.supports_operation_polling and self._supports_operation_polling and polling_available,
            supports_native_verify=caps.supports_native_verify and self._supports_native_verify and verify_available,
            supports_source_materialization=caps.supports_source_materialization and materialize_available,
            supports_relay_upload=caps.supports_relay_upload,
            supports_rclone_dest=caps.supports_rclone_dest,
            supports_direct_final=caps.supports_direct_final,
            supports_quota=caps.supports_quota,
        )

    def validate_account_config(self, config: dict[str, Any], credentials: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for field in self.manifest.account_schema.get("fields", []):
            if not field.get("required"):
                continue
            container = credentials if field.get("secret") else config
            if not str(container.get(field["name"]) or "").strip():
                errors.append(f"缺少必填字段：{field['label']}")
        return errors

    def test_account(self, account: dict[str, Any]) -> Any:
        from ..domain.models import AccountTestResult

        importer = self._build_account_importer(account)
        probe = getattr(importer, "probe", None) or getattr(importer, "check_configuration", None)
        if not callable(probe):
            return AccountTestResult(False, message="Provider 未实现真实账号检测")
        result = probe()
        ok = bool(
            getattr(result, "ok", None)
            if not isinstance(result, dict)
            else result.get("ok") or result.get("success")
        )
        message = str(getattr(result, "message", "") if not isinstance(result, dict) else result.get("message") or "")
        details = getattr(result, "details", {}) if not isinstance(result, dict) else result.get("details")
        details = details if isinstance(details, dict) else {}
        identity = str(details.get("identity_hint") or "")
        return AccountTestResult(ok, identity_hint=identity, message=message)

    def _build_account_importer(self, account: dict[str, Any]) -> Any:
        """Build a probe client from candidate credentials, never global config."""

        config = dict(account.get("config") or {})
        credentials = dict(account.get("credentials") or {})
        values = {**config, **credentials}
        if callable(self.account_importer_factory):
            return self.account_importer_factory(values)
        importer_type = type(self.importer) if self.importer is not None else None
        if importer_type is None:
            raise TransferError(ErrorCode.AUTH_CONFIG, "Provider 未配置账号检测工厂")
        if self.kind == "mobile":
            return importer_type(config, cmcc_config=values)
        return importer_type(values)

    def inspect_source(self, account: dict[str, Any], locator: dict[str, Any]) -> dict[str, Any]:
        importer = self._build_importer(account)
        check = getattr(importer, "check_share", None)
        if not callable(check):
            raise TransferError(ErrorCode.SHARE_INVALID, "Provider 未实现分享检查")
        ok, data = check(str(locator.get("url") or ""), password=str(locator.get("password") or ""))
        if not ok:
            raise TransferError(ErrorCode.SHARE_INVALID, str((data or {}).get("message") or "分享失效"))
        return data if isinstance(data, dict) else {"ok": True}

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
        importer = self._importer_for_request(request)
        if self.kind == "tianyi" and request.locator and request.locator.kind == "folder_path":
            root_folder_id, separator, relative_path = save_path.partition("::")
            if not separator or not root_folder_id or not relative_path:
                raise TransferError(
                    ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                    "天翼云保存目录定位器无效",
                )
            save_path = importer._ensure_folder_path(root_folder_id, relative_path)
        if self.kind == "mobile":
            result = importer.import_resource(
                title=title, share_url=share_url, save_path=save_path, password=password,
                category_key=request.category_key, category=category, native_selection=selection,
            )
        elif self.kind == "quark":
            result = importer.import_resource(
                title=title, share_url=share_url, save_path=save_path, password=password,
                category_key=request.category_key, category=category, quark_selection=selection,
            )
        elif self.kind == "tianyi":
            result = importer.import_resource(
                title=title, share_url=share_url, save_path=save_path, password=password,
                category_key=request.category_key, category=category, native_selection=selection,
            )
        else:
            result = importer.import_resource(
                title=title, share_url=share_url, save_path=save_path, password=password,
                category_key=request.category_key, category=category,
            )
        if isinstance(result, ImportResult):
            receipt = receipt_from_import_result(result, provider_kind=self.kind)
        else:
            receipt = normalize_receipt(result, provider_kind=self.kind)
        return self._freeze_account(receipt, request)

    def native_offline(self, request: NativeOfflineRequest) -> OperationReceipt:
        if not self._supports_native_offline:
            raise TransferError(ErrorCode.NATIVE_TRANSFER_UNAVAILABLE, f"{self.kind} 不支持原生离线")
        category = self._category(request)
        source_url = str(request.source.canonical_source_id or "")
        password = ""
        source_payload: dict[str, Any] = {}
        if self.source_record_service is not None and request.source.source_record_id:
            source_payload = self.source_record_service.resolve(
                request.source.source_record_id, request.source.source_record_revision
            )
            source_url = str(source_payload.get("url") or source_url)
            password = str(source_payload.get("password") or "")
        importer = self._importer_for_request(request)
        selection = _selection_payload(source_payload or request.source)
        # SixPan calls this field ``ignore_files``.  Keep the complete frozen
        # selection in the request while translating only at this Provider
        # boundary; no planner branch is needed for future Providers.
        ignore_files = selection.get("ignore_files")
        kwargs: dict[str, Any] = {
            "title": str(request.source.canonical_source_id or "磁链任务"),
            "source_url": source_url,
            "category": category,
            "password": password,
            "save_path": request.save_path,
            "ignore_files": list(ignore_files) if isinstance(ignore_files, (list, tuple)) else [],
        }
        result = _call_supported(importer.import_resource, kwargs)
        if isinstance(result, ImportResult):
            receipt = receipt_from_import_result(result, provider_kind=self.kind)
        else:
            receipt = normalize_receipt(result, provider_kind=self.kind)
        return self._freeze_account(receipt, request)

    # -- 轮询 --
    def poll_operation(self, receipt: OperationReceipt) -> OperationStatus:
        if not self._supports_operation_polling:
            return OperationStatus(
                "unknown",
                "Provider 未启用原生任务轮询，无法确认操作结果",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        importer = self.importer
        if callable(self.importer_factory):
            importer = self.importer_factory(
                str((receipt.metadata or {}).get("account_id") or ""),
                int((receipt.metadata or {}).get("account_revision") or 0),
            )
        poll_op = getattr(importer, "poll_operation", None)
        if callable(poll_op):
            return normalize_operation_status(poll_op(receipt))

        # SixPan's maintained importer exposes ``find_task`` + ``task_state``
        # rather than the V1 poll method.  Translate that lower-level API at
        # this boundary so the Planner can truthfully advertise native_offline
        # without teaching the core about a Provider-specific task format.
        if self.kind == "sixpan":
            finder = getattr(importer, "find_task", None)
            state_for = getattr(importer, "task_state", None)
            if callable(finder) and callable(state_for):
                task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
                if not task_id:
                    return OperationStatus(
                        "unknown",
                        "六盘任务 receipt 缺少 task_id",
                        error_code=ErrorCode.RESULT_UNKNOWN,
                    )
                task = finder(task_id)
                if not isinstance(task, dict):
                    return OperationStatus(
                        "unknown",
                        "六盘任务未找到，无法确认离线结果",
                        error_code=ErrorCode.RESULT_UNKNOWN,
                    )
                state = state_for(task)
                status = str(getattr(state, "status", "") or "").strip().lower()
                message = str(getattr(state, "message", "") or "")
                if status == "completed" or bool(getattr(state, "completed", False)):
                    return OperationStatus("completed", message or "六盘离线任务完成")
                if status == "failed" or bool(getattr(state, "failed", False)):
                    return OperationStatus("failed", message or "六盘离线任务失败")
                if status == "running":
                    return OperationStatus("running", message or "六盘离线任务进行中")
                return OperationStatus(
                    "unknown",
                    message or "六盘离线任务状态未知",
                    error_code=ErrorCode.RESULT_UNKNOWN,
                )

        # Mobile's official client exposes the task endpoint on ``_cmcc``.
        # A task response is authoritative for submission completion; final
        # file visibility is still checked by the subsequent V1 verify /
        # Organizer gates.
        if self.kind == "mobile":
            client_factory = getattr(importer, "_cmcc", None)
            task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
            if callable(client_factory) and task_id:
                task = client_factory().get_task(task_id)
                text = str(
                    (task or {}).get("status")
                    or (task or {}).get("taskStatus")
                    or (task or {}).get("state")
                    or ""
                ).strip().lower()
                message = str((task or {}).get("message") or (task or {}).get("msg") or "")
                if text in {"success", "done", "2", "completed", "finished"}:
                    return OperationStatus("completed", message or "移动云原生转存完成")
                if text in {"failed", "fail", "3", "error", "cancelled", "canceled"}:
                    return OperationStatus("failed", message or "移动云原生转存失败")
                if text in {"queued", "pending", "running", "processing", "0", "1"}:
                    return OperationStatus("running", message or "移动云原生转存进行中")
                return OperationStatus(
                    "unknown",
                    message or f"移动云任务状态未知：{text or 'empty'}",
                    error_code=ErrorCode.RESULT_UNKNOWN,
                )

        return OperationStatus(
            "unknown",
            "Importer 未实现原生任务轮询，无法确认操作结果",
            error_code=ErrorCode.RESULT_UNKNOWN,
        )

    def can_native_transfer(self, source: Any, destination: Any, account: Any = None) -> Any:
        from ..planning import CapabilityDecision

        if not self._supports_native_save:
            return CapabilityDecision(
                available=False,
                reason=f"{self.kind} 同源原生转存未实现，不降级为跨盘",
                code="NATIVE_TRANSFER_UNAVAILABLE",
            )
        source_kind = str(getattr(source, "provider_kind", None) or "").strip().lower()
        if source_kind != self.kind:
            return CapabilityDecision(available=False, reason="非同源目标，不适用原生转存", code="NOT_SAME_PROVIDER")
        return CapabilityDecision(available=True)

    def materialize_source(self, request: NativeSaveRequest) -> OperationReceipt:
        title, share_url, password, selection = self._load_source(request)
        importer = self._importer_for_request(request)
        materialize = getattr(importer, "materialize_resource", None)
        if callable(materialize):
            result = materialize(
                title=title,
                share_url=share_url,
                password=password,
                root_locator={"kind": request.locator.kind, "value": request.locator.value},
                landing_path=request.landing_path,
                category_key=request.category_key,
                selection=selection,
            )
            receipt = (
                receipt_from_import_result(result, provider_kind=self.kind)
                if isinstance(result, ImportResult)
                else normalize_receipt(result, provider_kind=self.kind)
            )
            return self._freeze_account(receipt, request)

        if request.locator.kind not in {"path", "provider_path"}:
            raise TransferError(
                ErrorCode.SOURCE_LANDING_UNAVAILABLE,
                f"{self.kind} 物化接口不支持 {request.locator.kind} 原生定位器",
            )
        base = str(request.locator.value or "").rstrip("/")
        suffix = str(request.landing_path or "").strip("/")
        path_request = NativeSaveRequest(
            idempotency_key=request.idempotency_key,
            source=request.source,
            destination=request.destination,
            locator=NativeLocator(kind="provider_path", value=f"{base}/{suffix}" if suffix else base),
            account_id=request.account_id,
            account_revision=request.account_revision,
            category_key=request.category_key,
            landing_path=request.landing_path,
        )
        return self.native_save(path_request)

    def relay_uploader(self) -> Any:
        if self.kind != "mobile":
            return None
        from ..cmcc.uploader import CmccUploader

        return CmccUploader

    def relay_uploader_for(self, account_id: str, revision: int) -> Any:
        if self.kind != "mobile":
            return None
        from ..cmcc.uploader import CmccUploader
        from .mobile_relay_executor import CmccMobileUploader

        importer = self.importer_factory(account_id, revision) if callable(self.importer_factory) else self.importer
        return CmccMobileUploader(CmccUploader(client=importer._cmcc()))

    # -- 结果校验 --
    def verify_native_result(
        self,
        request: Any,
        *,
        receipt: OperationReceipt | None = None,
    ) -> TransferVerifyResult:
        """执行 V1 VerifyRequest 校验并适配旧 importer。

        旧 importer 的 ``verify_native_result`` 接收 OperationReceipt，而
        V1 Adapter 契约接收 VerifyRequest。NativeTransferExecutor 会同时
        传入二者；这里明确完成协议翻译。缺少 verify/poll 实现时返回
        unknown，绝不返回 running。
        """

        operation_receipt = receipt if isinstance(receipt, OperationReceipt) else None
        if isinstance(request, OperationReceipt):
            operation_receipt = request
        importer = self.importer
        if operation_receipt is not None and callable(self.importer_factory):
            importer = self.importer_factory(
                str((operation_receipt.metadata or {}).get("account_id") or ""),
                int((operation_receipt.metadata or {}).get("account_revision") or 0),
            )
        verify_op = getattr(importer, "verify_native_result", None)
        if self._supports_native_verify and callable(verify_op):
            # Legacy importer 的真实签名是 verify(receipt)，而不是 V1
            # VerifyRequest。若没有 receipt（例如直接调用 Adapter），
            # 传入 request 仍允许未来的 V1 importer 自己实现正式协议。
            value = verify_op(operation_receipt if operation_receipt is not None else request)
            return normalize_verify_result(value)

        if operation_receipt is not None:
            # 没有 verify 时最多使用可靠的 poll 完成态作为结果；poll 缺失
            # 或返回 running/unknown 均进入 unknown，避免永久等待。
            status = self.poll_operation(operation_receipt)
            if status.status == "completed":
                return TransferVerifyResult("ok", "Provider 已确认原生任务完成")
            if status.status == "failed":
                return TransferVerifyResult(
                    "missing",
                    status.message or "原生任务失败",
                    code=ErrorCode.VERIFY_MISMATCH,
                )
        return TransferVerifyResult("unknown", "Provider 不支持原生结果校验", code=ErrorCode.RESULT_UNKNOWN)

    # -- 取消 --
    def cancel_operation(self, receipt: OperationReceipt) -> CancelResult:
        importer = self.importer
        if callable(self.importer_factory):
            metadata = receipt.metadata if isinstance(receipt, OperationReceipt) else {}
            try:
                importer = self.importer_factory(
                    str((metadata or {}).get("account_id") or ""),
                    int((metadata or {}).get("account_revision") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                return CancelResult(
                    "cancel_pending",
                    f"无法加载冻结账号以取消操作：{exc}",
                    operation_id=getattr(receipt, "operation_id", ""),
                )
        cancel_op = getattr(importer, "cancel_operation", None)
        if callable(cancel_op):
            value = cancel_op(receipt)
        elif self.kind == "sixpan":
            client = getattr(importer, "client", None)
            delete_tasks = getattr(client, "delete_offline_tasks", None)
            task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
            if not callable(delete_tasks) or not task_id:
                return CancelResult("not_cancellable", "六盘任务缺少可用取消接口", operation_id=receipt.operation_id)
            delete_tasks([task_id], delete_files=False)
            return CancelResult(
                "cancel_pending",
                "六盘取消请求已提交，等待任务状态确认",
                operation_id=receipt.operation_id,
            )
        else:
            return CancelResult("not_cancellable", "Provider 不支持取消，结果未知", operation_id=receipt.operation_id)
        if isinstance(value, CancelResult):
            return value
        return CancelResult("cancel_pending", "已请求取消，等待确认", operation_id=receipt.operation_id)

    # -- 内部 --
    def _load_source(self, request: NativeSaveRequest) -> tuple[str, str, str, dict[str, Any]]:
        if self.source_record_service is not None and request.source.source_record_id:
            loaded = self.source_record_service.resolve(
                request.source.source_record_id, request.source.source_record_revision
            )
            return (
                str(loaded.get("title") or request.source.canonical_source_id),
                str(loaded.get("url") or ""),
                str(loaded.get("password") or ""),
                _selection_payload(loaded),
            )
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
        return request.source.canonical_source_id, request.source.canonical_source_id, "", _selection_payload(request.source)

    def _importer_for_request(self, request: Any) -> Any:
        if not callable(self.importer_factory):
            return self.importer
        return self.importer_factory(request.account_id, request.account_revision)

    def _build_importer(self, account: dict[str, Any]) -> Any:
        factory = self.importer_factory
        if not callable(factory):
            return self.importer
        try:
            return factory(account)
        except TypeError:
            return factory(account.get("account_id"), account.get("revision"))

    def _has_materialize_backend(self, importer: Any) -> bool:
        if importer is None:
            return callable(getattr(self, "importer_factory", None)) and self._supports_native_save
        if self._callable_attr(importer, "materialize_resource"):
            return True
        # Mobile can materialize by native-saving into the frozen landing
        # locator; ``materialize_source`` performs that protocol translation.
        return self.kind == "mobile" and self._supports_native_save

    def _has_poll_backend(self, importer: Any) -> bool:
        if importer is None:
            return bool(
                callable(getattr(self, "importer_factory", None))
                and (self._supports_native_save or self._supports_native_offline)
            )
        if self._callable_attr(importer, "poll_operation"):
            return True
        if self.kind == "sixpan":
            return self._callable_attr(importer, "find_task") and self._callable_attr(
                importer, "task_state"
            )
        if self.kind == "mobile":
            return self._callable_attr(importer, "_cmcc")
        return False

    @staticmethod
    def _callable_attr(value: Any, name: str) -> bool:
        try:
            return callable(getattr(value, name, None))
        except Exception:  # noqa: BLE001 - capability probing must be side-effect free
            return False

    @staticmethod
    def _freeze_account(receipt: OperationReceipt, request: Any) -> OperationReceipt:
        return OperationReceipt(
            operation_id=receipt.operation_id,
            status=receipt.status,
            provider_kind=receipt.provider_kind,
            message=receipt.message,
            external_ref=receipt.external_ref,
            error_code=receipt.error_code,
            metadata={
                **dict(receipt.metadata or {}),
                "account_id": request.account_id,
                "account_revision": request.account_revision,
            },
        )

    def _category(self, request: Any) -> dict[str, Any]:
        if callable(self.category_for_request):
            try:
                category = self.category_for_request(request)
                if isinstance(category, dict):
                    return category
            except Exception:  # noqa: BLE001
                pass
        return {"label": request.category_key or "未分类"}


def _selection_payload(value: Any) -> dict[str, Any]:
    """Return a flat, provider-neutral selection payload for an Adapter.

    ``SourceDescriptor.selection`` and encrypted source snapshots serialize
    the same fields in slightly different wrappers.  Normalize them once at
    the Provider boundary so provider importers receive ``selected_items`` /
    ``ignore_files`` and their provider-specific selection without leaking
    credentials into the request.
    """

    if value is None:
        return {}
    nested: Any = None
    if isinstance(value, dict):
        nested = value.get("selection")
        if nested is None:
            nested = value.get("selection_snapshot")
    else:
        nested = getattr(value, "selection", None)
        if nested is not None and hasattr(nested, "to_dict"):
            try:
                nested = nested.to_dict()
            except Exception:  # noqa: BLE001
                nested = None
    result: dict[str, Any] = dict(nested) if isinstance(nested, dict) else {}
    if isinstance(value, dict):
        for key in ("selected_items", "ignore_files", "provider_selection", "selection_hash", "hash"):
            if key in value and value.get(key) is not None:
                result[key] = value.get(key)
    else:
        selection = getattr(value, "selection", None)
        if selection is not None:
            for key in ("selected_items", "ignore_files", "provider_selection", "selection_hash"):
                candidate = getattr(selection, key, None)
                if candidate is not None:
                    result[key] = candidate
    provider_selection = result.get("provider_selection")
    if isinstance(provider_selection, dict):
        # Preserve provider-specific keys at the top level for importers that
        # expect e.g. ``quark_selection`` rather than a nested map.
        for key, candidate in provider_selection.items():
            if key not in result and isinstance(candidate, dict):
                result[key] = candidate
            if isinstance(candidate, dict):
                for field_name, field_value in candidate.items():
                    result.setdefault(str(field_name), field_value)
    return result


def _call_supported(method: Any, kwargs: dict[str, Any]) -> Any:
    """Call a Provider method without hiding an internal TypeError."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**kwargs)
    params = signature.parameters
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in params.values()
    )
    if accepts_kwargs:
        return method(**kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in params}
    return method(**filtered)


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
