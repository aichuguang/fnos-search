"""V1.0 传输执行器与编排（Transfer Orchestrator + Executors）包。

本包只执行 v4 冻结执行计划（docs/V1.0多链路设计文档.md §12-§13）：
四种策略 native_save / native_offline / rclone_copy / relay_upload_mobile，
由 TransferOrchestrator 驱动 §13.1 状态机，执行器只负责各自的链路操作。

约束（§0 / §13.3）：
- 原生转存失败绝不降级为跨盘。
- 磁链只能保存到六盘（native_offline）。
- relay_upload_mobile 只允许目标为移动云。
- 来源落地与本地缓存在「目标校验成功 + 应用 ACK」之前绝不删除。
- Provider 返回未知结果时进入人工复核，绝不重复创建原生转存。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from fnos_media_import.constants import (
    JOB_DONE,
    JOB_FAILED,
    JOB_SUBMITTED,
    JOB_WAITING_TRANSFER,
)
from fnos_media_import.domain.models import (
    EXECUTION_PLAN_VERSION,
    DestinationDescriptor,
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    ProviderCapabilities,
    RcloneLocator,
    SourceDescriptor,
)

__all__ = [
    "EXECUTION_PLAN_VERSION",
    "PHASE_PLANNING",
    "PHASE_SOURCE_MATERIALIZING",
    "PHASE_SOURCE_WAITING_VISIBLE",
    "PHASE_NATIVE_SUBMITTING",
    "PHASE_NATIVE_WAITING",
    "PHASE_TRANSFERRING",
    "PHASE_TARGET_VERIFYING",
    "PHASE_ORGANIZING",
    "PHASE_ACK_PENDING",
    "PHASE_CLEANUP_PENDING",
    "PHASE_COMPLETED",
    "PHASE_RETRY_WAIT",
    "PHASE_MANUAL_REVIEW",
    "PHASE_CANCEL_PENDING",
    "PHASE_FAILED",
    "PHASE_CANCELLED",
    "ErrorCode",
    "RETRYABLE_ERROR_CODES",
    "TransferError",
    "OperationReceipt",
    "OperationStatus",
    "CancelResult",
    "TransferVerifyResult",
    "UploadOutcome",
    "AckResult",
    "OrganizeResult",
    "VisibilityStatus",
    "TransferPlan",
    "normalize_receipt",
    "normalize_operation_status",
    "normalize_verify_result",
    "normalize_upload_outcome",
    "receipt_from_import_result",
    "NativeSaveRequest",
    "NativeOfflineRequest",
]


# ---------------------------------------------------------------------------
# §13.1 状态机阶段
# ---------------------------------------------------------------------------
PHASE_PLANNING = "planning"
PHASE_SOURCE_MATERIALIZING = "source_materializing"
PHASE_SOURCE_WAITING_VISIBLE = "source_waiting_visible"
PHASE_NATIVE_SUBMITTING = "native_submitting"
PHASE_NATIVE_WAITING = "native_waiting"
PHASE_TRANSFERRING = "transferring"
PHASE_TARGET_VERIFYING = "target_verifying"
PHASE_ORGANIZING = "organizing"
PHASE_ACK_PENDING = "ack_pending"
PHASE_CLEANUP_PENDING = "cleanup_pending"
PHASE_COMPLETED = "completed"
PHASE_RETRY_WAIT = "retry_wait"
PHASE_MANUAL_REVIEW = "manual_review"
PHASE_CANCEL_PENDING = "cancel_pending"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"

# 终态
TERMINAL_PHASES = frozenset({PHASE_COMPLETED, PHASE_FAILED, PHASE_CANCELLED})

# 需要等外部事件（轮询 / 退避 / 人工）的等待阶段
WAITING_PHASES = frozenset(
    {
        PHASE_NATIVE_WAITING,
        PHASE_SOURCE_WAITING_VISIBLE,
        PHASE_TRANSFERRING,
        PHASE_RETRY_WAIT,
        PHASE_MANUAL_REVIEW,
        PHASE_CANCEL_PENDING,
        PHASE_ACK_PENDING,
        PHASE_ORGANIZING,
    }
)


# ---------------------------------------------------------------------------
# §18.1 错误分类
# ---------------------------------------------------------------------------
class ErrorCode:
    AUTH_CONFIG = "AUTH_CONFIG"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SHARE_INVALID = "SHARE_INVALID"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_TRANSIENT = "NETWORK_TRANSIENT"
    OPERATION_PENDING = "OPERATION_PENDING"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    PATH_CONFLICT = "PATH_CONFLICT"
    CANCELLED = "CANCELLED"
    NATIVE_TRANSFER_UNAVAILABLE = "NATIVE_TRANSFER_UNAVAILABLE"
    MAGNET_TARGET_RESTRICTED = "MAGNET_TARGET_RESTRICTED"
    SOURCE_LANDING_UNAVAILABLE = "SOURCE_LANDING_UNAVAILABLE"
    OPENLIST_UNAVAILABLE = "OPENLIST_UNAVAILABLE"
    UNSUPPORTED_PLAN_VERSION = "UNSUPPORTED_PLAN_VERSION"
    VERIFY_MISMATCH = "VERIFY_MISMATCH"
    INVALID_PLAN = "INVALID_PLAN"
    NO_EXECUTOR = "NO_EXECUTOR"
    CLEANUP_FAILED = "CLEANUP_FAILED"


# 允许有界重试的错误；其余错误（认证失效、分享失效、结果未知、配置错误）不盲重试。
RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.NETWORK_TRANSIENT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.OPERATION_PENDING,
        ErrorCode.VERIFY_MISMATCH,
        ErrorCode.SOURCE_LANDING_UNAVAILABLE,
        ErrorCode.OPENLIST_UNAVAILABLE,
        ErrorCode.CLEANUP_FAILED,
    }
)


class TransferError(RuntimeError):
    """执行器 / 编排器统一错误，携带 §18.1 分类码与是否可重试。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        phase: str = "",
        details: dict[str, Any] | None = None,
        account_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = str(message)
        if retryable is None:
            retryable = code in RETRYABLE_ERROR_CODES
        self.retryable = bool(retryable)
        self.phase = phase
        self.details = dict(details or {})
        self.account_id = str(account_id or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "phase": self.phase,
            "details": self.details,
            "account_id": self.account_id,
        }


# ---------------------------------------------------------------------------
# 通用结果类型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OperationReceipt:
    """执行器提交后返回的稳定操作凭证（§13.2 幂等身份）。"""

    operation_id: str
    status: str  # submitted | running | completed | failed | unknown | cancelled
    provider_kind: str = ""
    message: str = ""
    external_ref: str = ""
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationStatus:
    status: str  # completed | running | failed | unknown | cancelled
    message: str = ""
    error_code: str = ""
    expected_file_count: int = 0
    completed_file_count: int = 0
    total_size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    # cancelled | cancel_pending | resume_ack | not_cancellable | already_final
    status: str
    message: str = ""
    operation_id: str = ""
    phase: str = ""


@dataclass(frozen=True)
class TransferVerifyResult:
    status: str  # ok | missing | unknown
    message: str = ""
    file_count: int = 0
    total_size: int = 0
    expected_file_count: int = 0
    expected_total_size: int = 0
    code: str = ""


@dataclass(frozen=True)
class UploadOutcome:
    """移动云中继上传结果。"""

    status: str  # done | running | failed | auth_expired | unknown
    message: str = ""
    file_count: int = 0
    total_size: int = 0
    error_code: str = ""


@dataclass(frozen=True)
class AckResult:
    """应用层 ACK：只有 ACK 完成后才允许清理来源落地。"""

    acked: bool
    message: str = ""


@dataclass(frozen=True)
class OrganizeResult:
    status: str  # done | pending | failed | unknown
    message: str = ""
    retryable: bool = True


class VisibilityStatus(str, Enum):
    visible = "visible"
    pending = "pending"
    missing = "missing"
    unknown = "unknown"


def _visibility_from_text(value: Any) -> VisibilityStatus:
    text = str(value or "").strip().lower()
    if text in {"visible", "ok", "true", "ready", "yes", "1"}:
        return VisibilityStatus.visible
    if text in {"missing", "gone", "absent", "not_found", "none"}:
        return VisibilityStatus.missing
    if text in {"unknown", "error", "failed"}:
        return VisibilityStatus.unknown
    return VisibilityStatus.pending


# ---------------------------------------------------------------------------
# v4 冻结执行计划模型（§12.3）
# ---------------------------------------------------------------------------
_JOB_DIR_RE = re.compile(r"^job-([1-9][0-9]*)$")


def _norm_plan_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    parts = [part for part in text.strip("/").split("/") if part and part != "."]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


class TransferPlan:
    """只读 v4 计划封装。非 v4 计划在构造时即拒绝（§12.1）。"""

    def __init__(self, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            raise TransferError(ErrorCode.UNSUPPORTED_PLAN_VERSION, "执行计划必须是 v4 冻结计划")
        try:
            version = int(raw.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version != EXECUTION_PLAN_VERSION:
            raise TransferError(
                ErrorCode.UNSUPPORTED_PLAN_VERSION,
                f"系统只执行 v4 计划，收到 version={raw.get('version')!r}",
            )
        try:
            job_id = int(raw.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if job_id <= 0:
            raise TransferError(ErrorCode.UNSUPPORTED_PLAN_VERSION, f"计划缺少有效 job_id: {raw.get('job_id')!r}")
        self.raw = raw
        self.version = version
        self.job_id = job_id
        self.created_at = str(raw.get("created_at") or "")
        self.source = dict(raw.get("source") or {})
        self.destination = dict(raw.get("destination") or {})
        self.transfer = dict(raw.get("transfer") or {})
        self.bindings = dict(raw.get("bindings") or {})
        self.paths = dict(raw.get("paths") or {})
        self.verification = dict(raw.get("verification") or {})
        self.postprocess = dict(raw.get("postprocess") or {})
        self.category_key = str(raw.get("category") or raw.get("category_key") or "")
        self.target_category = raw.get("target_category") if isinstance(raw.get("target_category"), dict) else None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferPlan":
        return cls(raw)

    # ---- source ----
    @property
    def source_provider_kind(self) -> str:
        return str(self.source.get("provider_kind") or "")

    @property
    def source_form(self) -> str:
        return str(self.source.get("source_form") or "share")

    @property
    def canonical_source_id(self) -> str:
        return str(self.source.get("canonical_source_id") or "")

    @property
    def source_account_id(self) -> str:
        return str(self.source.get("account_id") or "")

    @property
    def source_account_revision(self) -> int:
        try:
            return int(self.source.get("account_revision") or 0)
        except (TypeError, ValueError):
            return 0

    # ---- destination ----
    @property
    def destination_id(self) -> str:
        return str(self.destination.get("id") or "")

    @property
    def destination_provider_kind(self) -> str:
        return str(self.destination.get("provider_kind") or "")

    @property
    def destination_target_revision(self) -> int:
        try:
            return int(self.destination.get("target_revision") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def destination_account_id(self) -> str:
        return str(self.destination.get("account_id") or "")

    @property
    def destination_account_revision(self) -> int:
        try:
            return int(self.destination.get("account_revision") or 0)
        except (TypeError, ValueError):
            return 0

    # ---- transfer ----
    @property
    def strategy(self) -> str:
        return str(self.transfer.get("strategy") or "")

    @property
    def idempotency_key(self) -> str:
        return str(self.transfer.get("idempotency_key") or f"import:{self.job_id}:{self.strategy}")

    @property
    def delete_source_after_ack(self) -> bool:
        return bool(self.transfer.get("delete_source_after_ack", True))

    # ---- bindings ----
    @property
    def source_mount_id(self) -> str:
        return str(self.bindings.get("source_mount_id") or "")

    @property
    def destination_mount_id(self) -> str:
        return str(self.bindings.get("destination_mount_id") or "")

    # ---- postprocess ----
    @property
    def organizer_enabled(self) -> bool:
        return bool(self.postprocess.get("organizer_enabled", False))

    @property
    def media_refresh_enabled(self) -> bool:
        return bool(self.postprocess.get("media_refresh_enabled", False))

    # ---- paths ----
    @property
    def job_dir_name(self) -> str:
        return f"job-{self.job_id}"

    def path(self, key: str) -> dict[str, str] | None:
        value = self.paths.get(key)
        if not isinstance(value, dict):
            return None
        namespace_id = str(value.get("namespace_id") or "")
        path = _norm_plan_path(value.get("path"))
        kind = "local" if str(namespace_id).lower().startswith("local:") else "openlist"
        return {"namespace_id": namespace_id, "path": path, "kind": kind}

    @property
    def source_landing(self) -> dict[str, str] | None:
        return self.path("source_landing")

    @property
    def destination_staging(self) -> dict[str, str] | None:
        return self.path("destination_staging")

    @property
    def destination_final(self) -> dict[str, str] | None:
        return self.path("destination_final")

    @property
    def organizer_path(self) -> dict[str, str] | None:
        return self.path("organizer")

    @property
    def local_temp(self) -> dict[str, str] | None:
        return self.path("local_temp")

    # ---- descriptors ----
    @property
    def source_descriptor(self) -> SourceDescriptor:
        return SourceDescriptor(
            provider_kind=self.source_provider_kind,
            source_form=self.source_form,
            canonical_source_id=self.canonical_source_id,
            account_scope=str(self.source.get("account_scope") or "default"),
            selected_items_hash=str(self.source.get("selection_hash") or ""),
        )

    @property
    def destination_descriptor(self) -> DestinationDescriptor:
        return DestinationDescriptor(
            destination_id=self.destination_id,
            provider_kind=self.destination_provider_kind,
            account_scope=str(self.destination.get("account_scope") or None) or None,
            target_revision=self.destination_target_revision,
        )

    def native_locator(self) -> NativeLocator:
        if self.target_category and isinstance(self.target_category.get("native_locator"), dict):
            nl = self.target_category["native_locator"]
            return NativeLocator(kind=str(nl.get("kind") or "folder_id"), value=str(nl.get("value") or ""))
        return NativeLocator(kind="folder_id", value=self.destination_id)

    def verification_expected(self) -> dict[str, int | str]:
        minimum = self.verification.get("minimum") if isinstance(self.verification.get("minimum"), list) else []
        return {
            "file_count": int(self.verification.get("expected_file_count") or 0),
            "total_size": int(self.verification.get("expected_total_size") or 0),
            "checksum": str(self.verification.get("checksum") or ""),
            "minimum": minimum,
        }


# ---------------------------------------------------------------------------
# 原生提交请求（§6.1 Adapter 协议）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NativeSaveRequest:
    idempotency_key: str
    source: SourceDescriptor
    destination: DestinationDescriptor
    locator: NativeLocator
    account_id: str
    account_revision: int
    category_key: str = ""


@dataclass(frozen=True)
class NativeOfflineRequest:
    idempotency_key: str
    source: SourceDescriptor
    destination: DestinationDescriptor
    locator: NativeLocator
    account_id: str
    account_revision: int
    save_path: str = ""


# ---------------------------------------------------------------------------
# 结果归一化
# ---------------------------------------------------------------------------
def _safe_raw(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {"raw": str(value)[:2000]}


def normalize_receipt(value: Any, *, provider_kind: str = "") -> OperationReceipt:
    """把 Adapter / 执行器返回的任意形态结果归一化为 OperationReceipt。"""
    if isinstance(value, OperationReceipt):
        return value
    if isinstance(value, dict):
        status_text = str(
            value.get("status")
            or value.get("result")
            or value.get("state")
            or value.get("code")
            or "submitted"
        ).strip().lower()
        operation_id = str(
            value.get("operation_id")
            or value.get("operationId")
            or value.get("id")
            or value.get("task_id")
            or value.get("taskId")
            or value.get("external_task_id")
            or ""
        ).strip()
        error_code = str(value.get("error_code") or value.get("errorCode") or "").strip()
        message = str(value.get("message") or value.get("msg") or "").strip()
        if status_text in {"failed", "fail", "error", "cancelled", "canceled"}:
            status = "failed"
        elif status_text in {"completed", "done", "success", "finished", "verified"}:
            status = "completed"
        elif status_text in {"running", "processing", "pending", "queued", "in_progress", "uploading", "downloading"}:
            status = "running"
        elif status_text in {"unknown", "unclear"}:
            status = "unknown"
        else:
            status = "submitted"
        return OperationReceipt(
            operation_id=operation_id,
            status=status,
            provider_kind=provider_kind or str(value.get("provider_kind") or ""),
            message=message,
            external_ref=str(value.get("external_ref") or operation_id),
            error_code=error_code,
            metadata=_safe_raw(value),
        )
    if isinstance(value, str):
        return OperationReceipt(operation_id=value, status="submitted", provider_kind=provider_kind)
    raise TransferError(ErrorCode.INVALID_PLAN, f"无法识别操作结果类型：{type(value).__name__}")


def receipt_from_import_result(result: Any, *, provider_kind: str) -> OperationReceipt:
    """把现有 ImportResult（importers/base.py）归一化为 OperationReceipt。"""
    status_text = str(getattr(result, "status", "") or "").strip().lower()
    success = bool(getattr(result, "success", False))
    operation_id = str(getattr(result, "external_task_id", "") or "").strip()
    message = str(getattr(result, "message", "") or "")
    if not success or status_text in {"failed", "fail", "error"}:
        return OperationReceipt(
            operation_id=operation_id,
            status="failed",
            provider_kind=provider_kind,
            message=message,
            external_ref=operation_id,
            metadata={"target_path": str(getattr(result, "target_path", "") or ""), "raw": _safe_raw(getattr(result, "raw_data", {}))},
        )
    if status_text in {"done", "success", "completed", "skipped_existing", "finished", "verified"}:
        return OperationReceipt(
            operation_id=operation_id,
            status="completed",
            provider_kind=provider_kind,
            message=message,
            external_ref=operation_id,
            metadata={"target_path": str(getattr(result, "target_path", "") or ""), "raw": _safe_raw(getattr(result, "raw_data", {}))},
        )
    if status_text in {"submitted", "running", "processing", "pending", "waiting_transfer", "waiting_openlist", "waiting_organizer", JOB_WAITING_TRANSFER, JOB_SUBMITTED}:
        return OperationReceipt(
            operation_id=operation_id,
            status="submitted",
            provider_kind=provider_kind,
            message=message,
            external_ref=operation_id,
            metadata={"target_path": str(getattr(result, "target_path", "") or ""), "raw": _safe_raw(getattr(result, "raw_data", {}))},
        )
    return OperationReceipt(
        operation_id=operation_id,
        status="unknown",
        provider_kind=provider_kind,
        message=message,
        external_ref=operation_id,
        metadata={"target_path": str(getattr(result, "target_path", "") or ""), "raw": _safe_raw(getattr(result, "raw_data", {}))},
    )


def normalize_operation_status(value: Any) -> OperationStatus:
    if isinstance(value, OperationStatus):
        return value
    if isinstance(value, dict):
        status_text = str(value.get("status") or value.get("state") or value.get("result") or "").strip().lower()
        error_code = str(value.get("error_code") or value.get("errorCode") or "").strip()
        message = str(value.get("message") or value.get("msg") or "").strip()
        if status_text in {"completed", "done", "success", "finished", "verified"}:
            status = "completed"
        elif status_text in {"failed", "fail", "error"}:
            status = "failed"
        elif status_text in {"cancelled", "canceled", "stopped"}:
            status = "cancelled"
        elif status_text in {"unknown", "unclear"}:
            status = "unknown"
        else:
            status = "running"
        return OperationStatus(
            status=status,
            message=message,
            error_code=error_code,
            expected_file_count=int(value.get("expected_file_count") or 0),
            completed_file_count=int(value.get("completed_file_count") or 0),
            total_size=int(value.get("total_size") or 0),
            metadata=_safe_raw(value),
        )
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"completed", "done", "success"}:
            return OperationStatus("completed")
        if text in {"failed", "fail", "error"}:
            return OperationStatus("failed")
        if text in {"cancelled", "canceled"}:
            return OperationStatus("cancelled")
        if text in {"unknown"}:
            return OperationStatus("unknown")
        return OperationStatus("running")
    return OperationStatus("unknown", "无法识别的操作状态")


def normalize_verify_result(value: Any) -> TransferVerifyResult:
    if isinstance(value, TransferVerifyResult):
        return value
    if isinstance(value, dict):
        status_text = str(value.get("status") or "").strip().lower()
        if status_text in {"ok", "success", "verified"}:
            status = "ok"
        elif status_text in {"missing", "mismatch", "fail", "failed"}:
            status = "missing"
        else:
            status = "unknown"
        return TransferVerifyResult(
            status=status,
            message=str(value.get("message") or ""),
            file_count=int(value.get("file_count") or 0),
            total_size=int(value.get("total_size") or 0),
            expected_file_count=int(value.get("expected_file_count") or 0),
            expected_total_size=int(value.get("expected_total_size") or 0),
            code=str(value.get("code") or ""),
        )
    text = str(value or "").strip().lower()
    if text in {"ok", "success", "verified"}:
        return TransferVerifyResult("ok")
    if text in {"missing", "mismatch", "fail", "failed"}:
        return TransferVerifyResult("missing", text)
    return TransferVerifyResult("unknown", "无法识别的校验结果")


def normalize_upload_outcome(value: Any) -> UploadOutcome:
    if isinstance(value, UploadOutcome):
        return value
    if isinstance(value, dict):
        status_text = str(value.get("status") or value.get("result") or value.get("state") or "").strip().lower()
        if status_text in {"done", "completed", "success", "verified"}:
            status = "done"
        elif status_text in {"auth_expired", "auth_failed", "unauthorized"}:
            status = "auth_expired"
        elif status_text in {"failed", "fail", "error"}:
            status = "failed"
        elif status_text in {"unknown"}:
            status = "unknown"
        else:
            status = "running"
        return UploadOutcome(
            status=status,
            message=str(value.get("message") or ""),
            file_count=int(value.get("file_count") or 0),
            total_size=int(value.get("total_size") or 0),
            error_code=str(value.get("error_code") or ""),
        )
    text = str(value or "").strip().lower()
    if text in {"done", "completed", "success"}:
        return UploadOutcome("done")
    if text in {"auth_expired"}:
        return UploadOutcome("auth_expired")
    if text in {"failed", "error"}:
        return UploadOutcome("failed")
    if text in {"unknown"}:
        return UploadOutcome("unknown")
    return UploadOutcome("running")


# 协议类型：源物化 / 可见性 / 上传 / ACK / 整理
SourceMaterializer = Any  # Callable[[TransferPlan], OperationReceipt]
VisibilityPoller = Any  # Callable[[OperationReceipt], VisibilityStatus | bool]
MobileUploader = Any
AckService = Any
OrganizerService = Any
TargetLister = Any
FilesystemOps = Any


# 延迟导入子模块，避免循环依赖；共享类型在导入子模块前已全部定义。
from .cleanup import (  # noqa: E402,F401
    CleanupItem,
    CleanupResult,
    CompositeFilesystemOps,
    LocalFilesystemOps,
    TransferCleanupService,
)
from .verifier import ListedFiles, TransferVerifier, VerifyRequest  # noqa: E402,F401
from .native_executor import (  # noqa: E402,F401
    LegacyNativeAdapter,
    NativeSourceMaterializer,
    NativeTransferExecutor,
)
from .rclone_executor import RcloneTransferExecutor  # noqa: E402,F401
from .mobile_relay_executor import CmccMobileUploader, MobileRelayExecutor  # noqa: E402,F401
from .orchestrator import TransferOrchestrator  # noqa: E402,F401
