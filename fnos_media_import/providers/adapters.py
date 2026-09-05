"""Built-in V1 Provider adapters.

The adapters in this module are the only bridge between an immutable V1
execution plan and the provider-specific importer clients.  Every execution
request is resolved from the frozen ``account_id`` and ``account_revision``;
there is deliberately no active-account or process-wide configuration
fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.models import AccountTestResult, NativeLocator, ProviderCapabilities
from ..importers.base import ImportResult
from ..transfer import (
    CancelResult,
    ErrorCode,
    NativeOfflineRequest,
    NativeSaveRequest,
    OperationReceipt,
    OperationStatus,
    TransferError,
    TransferVerifyResult,
    normalize_operation_status,
    normalize_receipt,
    normalize_verify_result,
    receipt_from_import_result,
)
from ..transfer.verifier import VerifyRequest
from ..time_utils import utc_now_iso
from .manifests import (
    MOBILE_MANIFEST,
    QUARK_MANIFEST,
    SIXPAN_MANIFEST,
    TIANYI_MANIFEST,
)


ImporterFactory = Callable[[str, int], Any]
AccountImporterFactory = Callable[[dict[str, Any]], Any]


class _FrozenImporterAdapter:
    """Shared protocol translation for the four built-in providers."""

    kind = ""
    manifest = QUARK_MANIFEST
    supports_native_save = False
    supports_native_offline = False

    def __init__(
        self,
        *,
        importer_factory: ImporterFactory,
        account_importer_factory: AccountImporterFactory,
        source_record_service: Any,
    ) -> None:
        if not callable(importer_factory):
            raise TypeError("importer_factory 必须可调用")
        if not callable(account_importer_factory):
            raise TypeError("account_importer_factory 必须可调用")
        if source_record_service is None:
            raise TypeError("缺少 source_record_service")
        self._importer_factory = importer_factory
        self._account_importer_factory = account_importer_factory
        self._source_records = source_record_service

    def capabilities(self, account: dict[str, Any] | None = None) -> ProviderCapabilities:
        del account
        return self.manifest.capabilities

    def validate_account_config(
        self,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        for field in self.manifest.account_schema.get("fields", []):
            if not field.get("required"):
                continue
            container = credentials if field.get("secret") else config
            if not str(container.get(field["name"]) or "").strip():
                errors.append(f"缺少必填字段：{field['label']}")
        return errors

    def test_account(self, account: dict[str, Any]) -> AccountTestResult:
        importer = self._build_account_importer(account)
        probe = getattr(importer, "probe", None) or getattr(
            importer, "check_configuration", None
        )
        if not callable(probe):
            return AccountTestResult(False, message="Provider 未实现真实账号检测")
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001
            return AccountTestResult(False, message=f"Provider 账号检测失败：{exc}")
        if isinstance(result, dict):
            ok = bool(result.get("ok") or result.get("success"))
            message = str(result.get("message") or "")
            details = result.get("details")
        else:
            ok = bool(getattr(result, "ok", False))
            message = str(getattr(result, "message", "") or "")
            details = getattr(result, "details", {})
        details = details if isinstance(details, dict) else {}
        return AccountTestResult(
            ok,
            identity_hint=str(details.get("identity_hint") or ""),
            message=message,
        )

    def inspect_source(
        self,
        account: dict[str, Any],
        locator: dict[str, Any],
    ) -> dict[str, Any]:
        importer = self._build_account_importer(account)
        check = getattr(importer, "check_share", None)
        if not callable(check):
            raise TransferError(ErrorCode.SHARE_INVALID, "Provider 未实现分享检查")
        url = str(locator.get("url") or "")
        password = str(locator.get("password") or "")
        value = check(share_url=url, password=password)
        if not isinstance(value, tuple) or len(value) != 2:
            raise TransferError(ErrorCode.SHARE_INVALID, "Provider 分享检查返回格式无效")
        ok, data = value
        if not ok:
            message = str((data or {}).get("message") or "分享失效") if isinstance(data, dict) else "分享失效"
            raise TransferError(ErrorCode.SHARE_INVALID, message)
        return dict(data or {}) if isinstance(data, dict) else {"ok": True}

    def can_native_transfer(self, source: Any, destination: Any, account: Any) -> Any:
        del account
        from ..planning import CapabilityDecision

        source_kind = str(_field(source, "provider_kind") or "").strip().lower()
        destination_kind = str(
            _field(destination, "provider_kind") or ""
        ).strip().lower()
        if not self.supports_native_save:
            return CapabilityDecision(
                False,
                f"{self.kind} 不支持同源原生转存",
                "NATIVE_TRANSFER_UNAVAILABLE",
            )
        if source_kind != self.kind or (
            destination_kind and destination_kind != self.kind
        ):
            return CapabilityDecision(
                False,
                "非同源目标，不适用原生转存",
                "NOT_SAME_PROVIDER",
            )
        return CapabilityDecision(True)

    def native_save(self, request: NativeSaveRequest) -> OperationReceipt:
        if not self.supports_native_save:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                f"{self.kind} 不支持同源原生转存",
            )
        title, share_url, password, selection = self._load_source(request)
        importer = self._importer_for_request(request)
        save_path = self._save_path(importer, request)
        kwargs: dict[str, Any] = {
            "title": title,
            "share_url": share_url,
            "save_path": save_path,
            "password": password,
            "category_key": request.category_key,
            "category": {"label": request.category_key or "未分类"},
        }
        selection_name = self._selection_argument()
        if selection_name:
            kwargs[selection_name] = selection
        result = importer.import_resource(**kwargs)
        return self._freeze_receipt(result, request)

    def native_offline(self, request: NativeOfflineRequest) -> OperationReceipt:
        if not self.supports_native_offline:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                f"{self.kind} 不支持原生离线",
            )
        source = self._source_payload(request)
        selection = _selection_payload(source)
        ignore_files = selection.get("ignore_files")
        importer = self._importer_for_request(request)
        result = importer.import_resource(
            **{
                "title": str(source.get("title") or request.source.canonical_source_id or "磁链任务"),
                "source_url": str(source.get("url") or request.source.canonical_source_id or ""),
                "category": {"label": "未分类"},
                "password": str(source.get("password") or ""),
                "save_path": request.save_path,
                "ignore_files": list(ignore_files)
                if isinstance(ignore_files, (list, tuple))
                else [],
            }
        )
        return self._freeze_receipt(result, request)

    def materialize_source(self, request: NativeSaveRequest) -> OperationReceipt:
        title, share_url, password, selection = self._load_source(request)
        importer = self._importer_for_request(request)
        materialize = getattr(importer, "materialize_resource", None)
        if callable(materialize):
            result = materialize(
                **{
                    "title": title,
                    "share_url": share_url,
                    "password": password,
                    "root_locator": {
                        "kind": request.locator.kind,
                        "value": request.locator.value,
                    },
                    "landing_path": request.landing_path,
                    "category_key": request.category_key,
                    "selection": selection,
                }
            )
            return self._freeze_receipt(result, request)
        return self._materialize_without_importer_method(request)

    def poll_operation(self, receipt: OperationReceipt) -> OperationStatus:
        importer = self._importer_for_receipt(receipt)
        poll = getattr(importer, "poll_operation", None)
        if not callable(poll):
            return self._poll_provider_task(importer, receipt)
        return normalize_operation_status(poll(receipt))

    def verify_native_result(self, request: VerifyRequest) -> TransferVerifyResult:
        if not isinstance(request, VerifyRequest):
            raise TypeError("verify_native_result 需要 VerifyRequest 参数")
        receipt = request.operation
        if not isinstance(receipt, OperationReceipt):
            return TransferVerifyResult(
                "unknown",
                "原生校验请求缺少冻结操作凭证",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        importer = self._importer_for_receipt(receipt)
        verify = getattr(importer, "verify_native_result", None)
        if callable(verify):
            return normalize_verify_result(verify(request))
        status = self._poll_provider_task(importer, receipt)
        if status.status == "completed":
            return TransferVerifyResult("ok", status.message or "Provider 已确认原生任务完成")
        if status.status in {"failed", "cancelled"}:
            return TransferVerifyResult(
                "missing",
                status.message or "Provider 原生任务未完成",
                code=ErrorCode.VERIFY_MISMATCH,
            )
        return TransferVerifyResult(
            "unknown",
            status.message or "Provider 无法确认原生任务结果",
            code=ErrorCode.RESULT_UNKNOWN,
        )

    def cancel_operation(self, receipt: OperationReceipt) -> CancelResult:
        importer = self._importer_for_receipt(receipt)
        cancel = getattr(importer, "cancel_operation", None)
        if callable(cancel):
            return _normalize_cancel(cancel(receipt), receipt)
        return self._cancel_provider_task(importer, receipt)

    def relay_uploader(self) -> Any:
        return None

    def relay_uploader_for(self, account_id: str, revision: int) -> Any:
        del account_id, revision
        return None

    def folder_browser_for(self, account_id: str, revision: int) -> Any:
        importer = self._importer_factory(account_id, revision)
        return importer if callable(getattr(importer, "list_folders", None)) else None

    def _build_account_importer(self, account: dict[str, Any]) -> Any:
        config = dict(account.get("config") or {})
        credentials = dict(account.get("credentials") or {})
        return self._account_importer_factory({**config, **credentials})

    def _importer_for_request(self, request: Any) -> Any:
        account_id = str(getattr(request, "account_id", "") or "").strip()
        revision = int(getattr(request, "account_revision", 0) or 0)
        if not account_id or revision <= 0:
            raise TransferError(
                ErrorCode.AUTH_CONFIG,
                "执行计划缺少冻结 Provider 账号 ID 或 revision",
            )
        return self._importer_factory(account_id, revision)

    def _importer_for_receipt(self, receipt: OperationReceipt) -> Any:
        metadata = dict(receipt.metadata or {})
        account_id = str(metadata.get("account_id") or "").strip()
        revision = int(metadata.get("account_revision") or 0)
        if not account_id or revision <= 0:
            raise TransferError(
                ErrorCode.AUTH_CONFIG,
                "原生操作凭证缺少冻结 Provider 账号 ID 或 revision",
            )
        return self._importer_factory(account_id, revision)

    def _source_payload(self, request: Any) -> dict[str, Any]:
        source_record_id = str(request.source.source_record_id or "").strip()
        source_revision = int(request.source.source_record_revision or 0)
        if not source_record_id or source_revision <= 0:
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "执行计划缺少冻结来源记录 ID 或 revision",
            )
        payload = self._source_records.resolve(source_record_id, source_revision)
        if not isinstance(payload, dict):
            raise TransferError(ErrorCode.INVALID_PLAN, "冻结来源记录格式无效")
        return dict(payload)

    def _load_source(
        self, request: NativeSaveRequest
    ) -> tuple[str, str, str, dict[str, Any]]:
        payload = self._source_payload(request)
        share_url = str(payload.get("url") or "").strip()
        if not share_url:
            raise TransferError(ErrorCode.INVALID_PLAN, "冻结来源记录缺少分享地址")
        return (
            str(payload.get("title") or request.source.canonical_source_id),
            share_url,
            str(payload.get("password") or ""),
            _selection_payload(payload),
        )

    def _save_path(self, importer: Any, request: NativeSaveRequest) -> str:
        del importer
        return str(request.locator.value or request.destination.destination_id)

    def _selection_argument(self) -> str:
        return "native_selection"

    def _materialize_without_importer_method(
        self, request: NativeSaveRequest
    ) -> OperationReceipt:
        raise TransferError(
            ErrorCode.SOURCE_LANDING_UNAVAILABLE,
            f"{self.kind} 未实现来源物化",
        )

    def _poll_provider_task(
        self, importer: Any, receipt: OperationReceipt
    ) -> OperationStatus:
        del importer, receipt
        return OperationStatus(
            "unknown",
            "Provider 未实现原生任务轮询",
            error_code=ErrorCode.RESULT_UNKNOWN,
        )

    def _cancel_provider_task(
        self, importer: Any, receipt: OperationReceipt
    ) -> CancelResult:
        del importer
        return CancelResult(
            "not_cancellable",
            "Provider 不支持取消",
            operation_id=receipt.operation_id,
        )

    def _freeze_receipt(self, result: Any, request: Any) -> OperationReceipt:
        receipt = (
            receipt_from_import_result(result, provider_kind=self.kind)
            if isinstance(result, ImportResult)
            else normalize_receipt(result, provider_kind=self.kind)
        )
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
                "submitted_at": str(
                    (receipt.metadata or {}).get("submitted_at")
                    or utc_now_iso()
                ),
            },
        )


class QuarkProviderAdapter(_FrozenImporterAdapter):
    kind = "quark"
    manifest = QUARK_MANIFEST
    supports_native_save = True

    def _selection_argument(self) -> str:
        return "quark_selection"


class TianyiProviderAdapter(_FrozenImporterAdapter):
    kind = "tianyi"
    manifest = TIANYI_MANIFEST
    supports_native_save = True

    def _save_path(self, importer: Any, request: NativeSaveRequest) -> str:
        value = str(request.locator.value or "")
        if request.locator.kind != "folder_path":
            return value
        root_folder_id, separator, relative_path = value.partition("::")
        if not separator or not root_folder_id or not relative_path:
            raise TransferError(
                ErrorCode.NATIVE_TRANSFER_UNAVAILABLE,
                "天翼云保存目录定位器无效",
            )
        return importer._ensure_folder_path(root_folder_id, relative_path)

    def relay_uploader(self) -> Any:
        from ..transfer.tianyi_uploader import TianyiCloudUploader

        return TianyiCloudUploader

    def relay_uploader_for(self, account_id: str, revision: int) -> Any:
        from ..transfer.tianyi_uploader import TianyiCloudUploader

        return TianyiCloudUploader(self._importer_factory(account_id, revision))

    def folder_browser_for(self, account_id: str, revision: int) -> Any:
        return self.relay_uploader_for(account_id, revision)


class MobileProviderAdapter(_FrozenImporterAdapter):
    kind = "mobile"
    manifest = MOBILE_MANIFEST
    supports_native_save = True

    def _materialize_without_importer_method(
        self, request: NativeSaveRequest
    ) -> OperationReceipt:
        if request.locator.kind not in {"path", "provider_path"}:
            raise TransferError(
                ErrorCode.SOURCE_LANDING_UNAVAILABLE,
                f"mobile 物化接口不支持 {request.locator.kind} 原生定位器",
            )
        base = str(request.locator.value or "").rstrip("/")
        suffix = str(request.landing_path or "").strip("/")
        return self.native_save(
            NativeSaveRequest(
                idempotency_key=request.idempotency_key,
                source=request.source,
                destination=request.destination,
                locator=NativeLocator(
                    kind="provider_path",
                    value=f"{base}/{suffix}" if suffix else base,
                ),
                account_id=request.account_id,
                account_revision=request.account_revision,
                category_key=request.category_key,
                landing_path=request.landing_path,
            )
        )

    def _poll_provider_task(
        self, importer: Any, receipt: OperationReceipt
    ) -> OperationStatus:
        task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
        client_factory = getattr(importer, "_cmcc", None)
        if not callable(client_factory) or not task_id:
            return super()._poll_provider_task(importer, receipt)
        try:
            task = client_factory().get_task(task_id)
        except Exception as exc:  # noqa: BLE001
            # 分享批量转存任务（createOuterLinkBatchOprTask 返回的 sk* 任务号）
            # 无法走 /task/get：该端点对任何任务号都返回
            # 系统服务调用错误(00010014)。改用目标目录探测转存结果。
            return self._poll_by_target_listing(importer, receipt, exc)
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

    def _poll_by_target_listing(
        self, importer: Any, receipt: OperationReceipt, cause: Exception
    ) -> OperationStatus:
        metadata = dict(getattr(receipt, "metadata", {}) or {})
        raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
        parent_id = str(raw.get("target_parent_file_id") or "").strip()
        expected = self._expected_target_names(raw)
        if not parent_id or not expected:
            return OperationStatus(
                "unknown",
                f"移动云任务状态查询失败，且缺少可探测的目标目录信息：{cause}",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        client_factory = getattr(importer, "_cmcc", None)
        if not callable(client_factory):
            return OperationStatus(
                "unknown",
                f"移动云任务状态查询失败，目标目录探测不可用：{cause}",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        try:
            items = client_factory().list_dir(parent_id)
        except Exception as exc:  # noqa: BLE001
            return OperationStatus(
                "unknown",
                f"移动云任务状态查询失败，目标目录探测也失败：{exc}",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        actual = {
            str(item.get("name") or item.get("fileName") or "").strip()
            for item in items
            if isinstance(item, dict)
        }
        if all(name in actual for name in expected):
            return OperationStatus("completed", "移动云原生转存完成（目标目录已出现保存内容）")
        return OperationStatus("running", "移动云原生转存进行中（目标目录尚未出现保存内容）")

    @staticmethod
    def _expected_target_names(raw: dict[str, Any]) -> list[str]:
        # 落盘到 target_parent_file_id 下的子项名 = 转存勾选的分享条目名
        # （勾选文件夹时是该文件夹的原名，如「抓丨特丨务」；整分享默认
        # 保存时 selection 里带分享根条目）。resource_name 只是目标目录
        # 自身的名字，已经体现在 target_parent_file_id 里，不会作为子项
        # 出现，不能拿来当期望名。
        selection = raw.get("selection") if isinstance(raw.get("selection"), dict) else {}
        names: list[str] = []
        for key in ("selected_files", "selected_folders"):
            for item in selection.get(key) or []:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("fileName") or "").strip()
                    if name:
                        names.append(name)
        if names:
            return names
        plan = raw.get("directory_plan") if isinstance(raw.get("directory_plan"), dict) else {}
        if plan.get("resource_dir_applied"):
            return []
        name = str(plan.get("resource_name") or "").strip()
        return [name] if name else []

    def relay_uploader(self) -> Any:
        from ..transfer.mobile_relay_executor import CmccMobileUploader

        return CmccMobileUploader

    def relay_uploader_for(self, account_id: str, revision: int) -> Any:
        from ..cmcc.uploader import CmccUploader
        from ..transfer.mobile_relay_executor import CmccMobileUploader

        importer = self._importer_factory(account_id, revision)
        return CmccMobileUploader(CmccUploader(client=importer._cmcc()))


class SixPanProviderAdapter(_FrozenImporterAdapter):
    kind = "sixpan"
    manifest = SIXPAN_MANIFEST
    supports_native_offline = True

    def can_native_transfer(self, source: Any, destination: Any, account: Any) -> Any:
        del source, destination, account
        from ..planning import CapabilityDecision

        return CapabilityDecision(False, "六盘只支持 native_offline", "NATIVE_SAVE_UNSUPPORTED")

    def _poll_provider_task(
        self, importer: Any, receipt: OperationReceipt
    ) -> OperationStatus:
        finder = getattr(importer, "find_task", None)
        state_for = getattr(importer, "task_state", None)
        task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
        if not callable(finder) or not callable(state_for) or not task_id:
            return super()._poll_provider_task(importer, receipt)
        metadata = receipt.metadata if isinstance(receipt.metadata, dict) else {}
        submitted_at = str(metadata.get("submitted_at") or "").strip()
        try:
            submitted_timeout = max(0, int(getattr(importer, "submitted_timeout_seconds", 0) or 0))
        except (TypeError, ValueError):
            submitted_timeout = 0
        if submitted_timeout and submitted_at:
            try:
                started = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - started).total_seconds() >= submitted_timeout:
                    return OperationStatus(
                        "unknown",
                        f"六盘离线任务超过最大等待时间（{submitted_timeout} 秒）",
                        error_code=ErrorCode.RESULT_UNKNOWN,
                    )
            except (TypeError, ValueError):
                pass
        task = finder(task_id)
        if not isinstance(task, dict):
            missing_count = int(metadata.get("missing_polls") or 0) + 1
            metadata["missing_polls"] = missing_count
            try:
                missing_limit = max(1, int(getattr(importer, "task_missing_poll_limit", 5) or 5))
            except (TypeError, ValueError):
                missing_limit = 5
            if missing_count < missing_limit:
                return OperationStatus("running", f"六盘任务暂未出现在任务列表（第 {missing_count} 次）")
            return OperationStatus(
                "unknown",
                "六盘任务多次未找到，无法确认离线结果",
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
        unknown_count = int(metadata.get("unknown_polls") or 0) + 1
        metadata["unknown_polls"] = unknown_count
        try:
            unknown_limit = max(1, int(getattr(importer, "task_unknown_poll_limit", 5) or 5))
        except (TypeError, ValueError):
            unknown_limit = 5
        if unknown_count < unknown_limit:
            return OperationStatus("running", message or f"六盘离线任务状态暂不明确（第 {unknown_count} 次）")
        return OperationStatus(
            "unknown",
            message or "六盘离线任务状态多次未知",
            error_code=ErrorCode.RESULT_UNKNOWN,
        )

    def _cancel_provider_task(
        self, importer: Any, receipt: OperationReceipt
    ) -> CancelResult:
        delete_tasks = getattr(importer, "delete_offline_tasks", None)
        if not callable(delete_tasks):
            client = getattr(importer, "client", None)
            delete_tasks = getattr(client, "delete_offline_tasks", None)
        task_id = str(receipt.operation_id or receipt.external_ref or "").strip()
        if not callable(delete_tasks) or not task_id:
            return super()._cancel_provider_task(importer, receipt)
        delete_tasks([task_id], delete_files=False)
        return CancelResult(
            "cancel_pending",
            "六盘取消请求已提交，等待任务状态确认",
            operation_id=receipt.operation_id,
        )


def _selection_payload(value: Any) -> dict[str, Any]:
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
    result = dict(nested) if isinstance(nested, dict) else {}
    if isinstance(value, dict):
        for key in (
            "selected_items",
            "ignore_files",
            "provider_selection",
            "selection_hash",
            "hash",
        ):
            if key in value and value.get(key) is not None:
                result[key] = value.get(key)
    provider_selection = result.get("provider_selection")
    if isinstance(provider_selection, dict):
        for key, candidate in provider_selection.items():
            if key not in result and isinstance(candidate, dict):
                result[key] = candidate
            if isinstance(candidate, dict):
                for field_name, field_value in candidate.items():
                    result.setdefault(str(field_name), field_value)
    return result


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _normalize_cancel(value: Any, receipt: OperationReceipt) -> CancelResult:
    if isinstance(value, CancelResult):
        return value
    if isinstance(value, dict):
        status = str(value.get("status") or "").strip().lower()
        message = str(value.get("message") or "")
        if status in {"cancelled", "canceled", "confirmed"}:
            return CancelResult("cancelled", message, operation_id=receipt.operation_id)
        if status in {"not_cancellable", "unsupported"}:
            return CancelResult(
                "not_cancellable", message, operation_id=receipt.operation_id
            )
    return CancelResult(
        "cancel_pending",
        "已请求取消，等待确认",
        operation_id=receipt.operation_id,
    )


__all__ = [
    "MobileProviderAdapter",
    "QuarkProviderAdapter",
    "SixPanProviderAdapter",
    "TianyiProviderAdapter",
]
