"""Provider-neutral relay transfer executor.

The relay data plane is shared by every Provider that advertises
``supports_relay_upload``::

    Provider source -> OpenList landing -> rclone local cache
    -> destination Provider uploader -> verify -> ACK -> cleanup

Only the final uploader is Provider-specific.  The durable phase model,
idempotency fences, local-cache containment and cleanup semantics live here so
adding another upload-capable Provider does not require another orchestrator.
"""

from __future__ import annotations

from typing import Any

from fnos_media_import.transfer import (
    CancelResult,
    ErrorCode,
    OperationReceipt,
    OperationStatus,
    TransferError,
    TransferPlan,
    TransferVerifyResult,
    UploadOutcome,
    VisibilityStatus,
    normalize_receipt,
    normalize_upload_outcome,
    normalize_verify_result,
)

__all__ = ["DownloadStatus", "ProviderRelayExecutor"]


class DownloadStatus:
    __slots__ = ("status", "message", "error_code")

    def __init__(self, status: str, message: str = "", error_code: str = "") -> None:
        self.status = status
        self.message = message
        self.error_code = error_code


class ProviderRelayExecutor:
    """Execute the common local-relay pipeline for one destination uploader."""

    strategy = "relay_upload_provider"
    needs_source_landing = True

    def __init__(
        self,
        rclone_adapter: Any,
        provider_uploader: Any,
        *,
        source_materializer: Any = None,
        visibility_poller: Any = None,
        relay_root: str | None = None,
        alert_service: Any = None,
        halted_account_ids: set[str] | None = None,
    ) -> None:
        self.rclone_adapter = rclone_adapter
        self.provider_uploader = provider_uploader
        self.source_materializer = source_materializer
        self.visibility_poller = visibility_poller
        self.relay_root = self._normalize_root(relay_root)
        self.alert_service = alert_service
        self.halted_account_ids = halted_account_ids if isinstance(halted_account_ids, set) else set()

    def bind(self, plan: TransferPlan) -> "ProviderRelayExecutor":
        self.plan = plan
        bind = getattr(self.rclone_adapter, "bind", None)
        if callable(bind):
            bind(plan)
        return self

    @staticmethod
    def _normalize_root(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        while "//" in text:
            text = text.replace("//", "/")
        if not text:
            return ""
        return text.rstrip("/") or "/"

    def _provider_kind(self) -> str:
        configured = str(getattr(self.provider_uploader, "provider_kind", "") or "").strip().lower()
        planned = str(getattr(self.plan, "destination_provider_kind", "") or "").strip().lower()
        if configured and planned and configured != planned:
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "relay uploader Provider 与冻结目标不一致",
            )
        return configured or planned or "provider"

    def _provider_label(self) -> str:
        return str(
            getattr(self.provider_uploader, "display_name", "")
            or getattr(self.provider_uploader, "provider_name", "")
            or self._provider_kind()
        ).strip()

    def _relay_dir(self) -> str:
        if getattr(self, "plan", None) is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "relay 执行器未绑定计划")
        expected_name = f"job-{int(self.plan.job_id)}"
        frozen = self.plan.local_temp
        frozen_path = ""
        if isinstance(frozen, dict):
            kind = str(frozen.get("kind") or "local").strip().lower()
            if kind not in {"local", "relay"}:
                raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp 必须是 local/relay 目标")
            namespace_id = str(frozen.get("namespace_id") or "").strip().lower()
            if namespace_id and not namespace_id.startswith("local:"):
                raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp 必须使用 local 命名空间")
            frozen_path = self._normalize_root(frozen.get("path"))

        if self.relay_root:
            candidate = f"{self.relay_root}/{expected_name}"
            if frozen_path and self._normalize_root(frozen_path) != candidate:
                raise TransferError(
                    ErrorCode.INVALID_PLAN,
                    "冻结的 relay local_temp 与配置的 relay 根目录不一致",
                )
            return candidate

        if not frozen_path:
            raise TransferError(ErrorCode.INVALID_PLAN, "缺少 relay_root 或 plan.local_temp")
        parts = [part for part in frozen_path.split("/") if part]
        if not parts or parts[-1] != expected_name:
            raise TransferError(ErrorCode.INVALID_PLAN, f"relay local_temp 必须以 {expected_name} 结尾")
        if any(part in {".", ".."} for part in parts):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp 包含路径穿越段")
        return frozen_path

    def materialize_source(self) -> OperationReceipt:
        if self.source_materializer is None:
            raise TransferError(ErrorCode.SOURCE_LANDING_UNAVAILABLE, "缺少来源物化器")
        receipt = self.source_materializer(self.plan)
        return normalize_receipt(receipt, provider_kind=self.plan.source_provider_kind)

    def wait_source_visible(self, receipt: OperationReceipt) -> VisibilityStatus:
        if self.visibility_poller is None:
            return VisibilityStatus.visible
        value = self.visibility_poller(receipt)
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

    def submit(self, existing_receipt: OperationReceipt | None = None) -> OperationReceipt:
        if existing_receipt is not None:
            return existing_receipt
        landing = self.plan.source_landing
        if landing is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "Provider relay 需要来源落地区")
        local_dir = self._relay_dir()
        operation_key = f"relay-dl:{self.plan.idempotency_key}"
        try:
            receipt = self.rclone_adapter.download(
                source=landing,
                local_dir=local_dir,
                idempotency_key=operation_key,
            )
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"中继下载提交失败：{exc}",
                retryable=True,
            ) from exc
        receipt = normalize_receipt(receipt, provider_kind="rclone")
        receipt.metadata.setdefault("step", "download")
        receipt.metadata.setdefault("operation_key", operation_key)
        receipt.metadata.setdefault("idempotency_key", operation_key)
        receipt.metadata.setdefault("local_dir", local_dir)
        return receipt

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        if not isinstance(receipt, OperationReceipt):
            return OperationStatus("unknown", "relay 回执无效", error_code=ErrorCode.RESULT_UNKNOWN)
        step = str((receipt.metadata or {}).get("step") or "download").strip().lower()
        if step == "download":
            try:
                status = self._check_download(receipt)
            except TransferError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TransferError(
                    ErrorCode.NETWORK_TRANSIENT,
                    f"中继下载状态查询失败：{exc}",
                    retryable=True,
                ) from exc
            if status.status == "completed":
                return self._upload_status(self._upload(receipt))
            if status.status == "failed":
                return OperationStatus(
                    "failed",
                    status.message or "中继下载失败",
                    error_code=status.error_code or ErrorCode.NETWORK_TRANSIENT,
                )
            if status.status == "unknown":
                return OperationStatus("unknown", status.message or "中继下载结果未知")
            return OperationStatus("running", status.message or "中继下载进行中")
        if step == "upload":
            return self._poll_upload(receipt)
        return OperationStatus(
            "unknown",
            f"未知的 relay 回执步骤：{step}",
            error_code=ErrorCode.RESULT_UNKNOWN,
        )

    def _check_download(self, receipt: OperationReceipt) -> DownloadStatus:
        check = getattr(self.rclone_adapter, "check", None)
        if not callable(check):
            return DownloadStatus(
                "unknown",
                "rclone 适配器未提供限定的下载状态查询",
                ErrorCode.RESULT_UNKNOWN,
            )
        value = check(receipt)
        if isinstance(value, DownloadStatus):
            return value
        if isinstance(value, dict):
            text = str(value.get("status") or "").strip().lower()
            status = self._normalized_status(text)
            return DownloadStatus(
                status,
                str(value.get("message") or ""),
                str(value.get("error_code") or ""),
            )
        if isinstance(value, str):
            return DownloadStatus(self._normalized_status(value), value)
        return DownloadStatus("running", "中继下载进行中")

    @staticmethod
    def _normalized_status(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"completed", "done", "success"}:
            return "completed"
        if text in {"failed", "error"}:
            return "failed"
        if text == "unknown":
            return "unknown"
        return "running"

    def _upload(self, receipt: OperationReceipt) -> UploadOutcome:
        local_dir = self._relay_dir()
        target = self._destination_target()
        metadata = receipt.metadata
        operation_key = f"relay-up:{self.plan.idempotency_key}"
        account_id = self.plan.destination_account_id or self.plan.destination_id
        if account_id in self.halted_account_ids:
            return UploadOutcome(
                "auth_expired",
                f"{self._provider_label()}账号已被熔断，等待管理员重新检测",
            )
        metadata["step"] = "upload"
        metadata.setdefault("upload_idempotency_key", operation_key)
        fence = metadata.setdefault("upload_submission_fence", {})
        if isinstance(fence, dict):
            fence.setdefault("operation_key", operation_key)
            fence.setdefault("state", "prepared")
        previous = (
            normalize_upload_outcome(metadata.get("upload_outcome"))
            if metadata.get("upload_outcome")
            else None
        )
        if previous is not None and previous.status in {
            "done",
            "completed",
            "auth_expired",
            "failed",
            "unknown",
        }:
            return previous
        try:
            outcome = self.provider_uploader.upload_local_dir(
                local_dir=local_dir,
                target_locator=target,
                idempotency_key=operation_key,
            )
        except TransferError as exc:
            outcome = UploadOutcome("failed", exc.message, error_code=exc.code)
        except Exception as exc:  # noqa: BLE001
            outcome = UploadOutcome(
                "failed",
                f"{self._provider_label()}上传异常：{exc}",
                error_code=ErrorCode.NETWORK_TRANSIENT,
            )
        normalized = normalize_upload_outcome(outcome)
        metadata["upload_outcome"] = _upload_outcome_dict(normalized)
        if isinstance(fence, dict):
            fence["state"] = normalized.status
        return normalized

    def _poll_upload(self, receipt: OperationReceipt) -> OperationStatus:
        metadata = receipt.metadata
        stored = metadata.get("upload_outcome")
        outcome = normalize_upload_outcome(stored) if stored else None
        check = getattr(self.provider_uploader, "check_upload", None)
        if (outcome is None or outcome.status == "running") and callable(check):
            try:
                outcome = normalize_upload_outcome(check(receipt))
            except Exception as exc:  # noqa: BLE001
                return OperationStatus(
                    "unknown",
                    f"{self._provider_label()}上传状态查询失败：{exc}",
                    error_code=ErrorCode.RESULT_UNKNOWN,
                )
            metadata["upload_outcome"] = _upload_outcome_dict(outcome)
        if outcome is None:
            return OperationStatus(
                "unknown",
                "上传回执缺少持久结果，也没有限定的状态查询器",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        return self._upload_status(outcome)

    def _upload_status(self, outcome: UploadOutcome) -> OperationStatus:
        label = self._provider_label()
        if outcome.status in {"done", "completed"}:
            return OperationStatus(
                "completed",
                outcome.message or f"{label}上传完成",
                expected_file_count=outcome.file_count,
                completed_file_count=outcome.file_count,
                total_size=outcome.total_size,
            )
        if outcome.status == "auth_expired":
            self._on_auth_expired()
            return OperationStatus(
                "failed",
                outcome.message or f"{label}账号认证已过期",
                error_code=ErrorCode.AUTH_EXPIRED,
            )
        if outcome.status == "failed":
            return OperationStatus(
                "failed",
                outcome.message or f"{label}上传失败",
                error_code=outcome.error_code or ErrorCode.NETWORK_TRANSIENT,
            )
        if outcome.status == "unknown":
            return OperationStatus(
                "unknown",
                outcome.message or f"{label}上传结果未知",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        return OperationStatus("running", outcome.message or f"{label}上传进行中")

    def _destination_target(self) -> dict[str, str]:
        target = self.plan.destination_final
        if not isinstance(target, dict):
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "Provider relay 需要明确的 destination_final",
            )
        namespace_id = str(target.get("namespace_id") or "").strip()
        path = str(target.get("path") or "").strip()
        if not namespace_id or not path:
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "destination_final 必须包含 namespace_id 和 path",
            )
        if not namespace_id.lower().startswith("native:"):
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "relay destination_final 必须使用 native 命名空间",
            )
        return dict(target)

    def _on_auth_expired(self) -> None:
        account_id = self.plan.destination_account_id or self.plan.destination_id
        already_halted = account_id in self.halted_account_ids
        self.halted_account_ids.add(account_id)
        if self.alert_service is not None and not already_halted:
            try:
                self.alert_service(
                    account_id=account_id,
                    code=ErrorCode.AUTH_EXPIRED,
                    message=f"{self._provider_label()}认证已过期，已暂停该账号后续上传任务",
                )
            except Exception:  # noqa: BLE001
                pass

    def verify(self, request: Any = None) -> TransferVerifyResult:
        del request
        verify_target = getattr(self.provider_uploader, "verify_target", None)
        if not callable(verify_target):
            return TransferVerifyResult(
                "unknown",
                f"{self._provider_label()}上传器不支持目标校验",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        expected = self.plan.verification_expected()
        try:
            target = self._destination_target()
        except TransferError as exc:
            return TransferVerifyResult("unknown", exc.message, code=exc.code)

        kwargs = {
            "target_locator": target,
            "expected_file_count": int(expected["file_count"]),
            "expected_total_size": int(expected["total_size"]),
            "idempotency_key": f"relay-up:{self.plan.idempotency_key}",
            "local_dir": self._relay_dir(),
        }
        try:
            value = verify_target(**kwargs)
        except TransferError as exc:
            return TransferVerifyResult("unknown", exc.message, code=exc.code)
        except Exception as exc:  # noqa: BLE001
            return TransferVerifyResult(
                "unknown",
                f"{self._provider_label()}目标校验失败：{exc}",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        return normalize_verify_result(value)

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        cancel_dl = getattr(self.rclone_adapter, "cancel", None)
        if callable(cancel_dl):
            try:
                value = cancel_dl(receipt)
                if isinstance(value, CancelResult) and value.status == "cancelled":
                    return value
            except Exception:  # noqa: BLE001
                pass
        return CancelResult(
            "cancel_pending",
            "已请求停止中继下载/上传",
            operation_id=receipt.operation_id,
        )


def _upload_outcome_dict(outcome: UploadOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "message": outcome.message,
        "file_count": int(outcome.file_count or 0),
        "total_size": int(outcome.total_size or 0),
        "error_code": outcome.error_code,
    }
