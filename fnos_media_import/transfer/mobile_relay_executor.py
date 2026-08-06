"""MobileRelayExecutor —— 其他来源 -> 移动云中继上传（relay_upload_mobile，§10.4）。

链路：来源原生保存到落地区 -> rclone 下载到 /data/relay/job-<id> -> 移动云 API 上传
（CmccUploader）-> 目录结果校验 -> ACK -> 清理落地区与本地缓存。

移动云大文件通过 OpenList 直传存在卡 100% 的问题，因此跨盘写移动云唯一例外链路为
「本地中继 + 移动云上传 API」。rclone 只负责下载；移动云 Adapter 负责秒传、完整上传、
认证错误与结果确认。

认证失效：停止同账号后续上传任务并在后台生成账号告警，不进行盲重试（§10.4）。
"""

from __future__ import annotations

import posixpath
from typing import Any

from fnos_media_import.cmcc.auth import CmccAuthConfigError, CmccAuthExpired
from fnos_media_import.cmcc.client import CmccApiError
from fnos_media_import.cmcc.uploader import CmccUploadResult, CmccUploader
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

__all__ = [
    "DownloadStatus",
    "MobileRelayExecutor",
    "CmccMobileUploader",
]


class DownloadStatus:
    __slots__ = ("status", "message", "error_code")

    def __init__(self, status: str, message: str = "", error_code: str = "") -> None:
        self.status = status  # completed | running | failed | unknown
        self.message = message
        self.error_code = error_code


class MobileRelayExecutor:
    strategy = "relay_upload_mobile"
    needs_source_landing = True

    def __init__(
        self,
        rclone_adapter: Any,
        mobile_uploader: Any,
        *,
        source_materializer: Any = None,
        visibility_poller: Any = None,
        relay_root: str = "/data/relay",
        alert_service: Any = None,
    ) -> None:
        self.rclone_adapter = rclone_adapter
        self.mobile_uploader = mobile_uploader
        self.source_materializer = source_materializer
        self.visibility_poller = visibility_poller
        self.relay_root = str(relay_root or "/data/relay")
        self.alert_service = alert_service
        self.halted_account_ids: set[str] = set()

    def bind(self, plan: TransferPlan) -> "MobileRelayExecutor":
        self.plan = plan
        return self

    def _relay_dir(self) -> str:
        return f"{self.relay_root.rstrip('/')}/job-{self.plan.job_id}"

    # -- 来源物化 --
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

    # -- 传输：rclone 下载 -> 移动云上传 --
    def submit(self, existing_receipt: OperationReceipt | None = None) -> OperationReceipt:
        if existing_receipt is not None:
            return existing_receipt
        landing = self.plan.source_landing
        if landing is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "relay_upload_mobile 需要来源落地区")
        local_dir = self._relay_dir()
        try:
            receipt = self.rclone_adapter.download(
                source=landing,
                local_dir=local_dir,
                idempotency_key=f"relay-dl:{self.plan.idempotency_key}",
            )
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"中继下载提交失败：{exc}", retryable=True) from exc
        return normalize_receipt(receipt, provider_kind="rclone")

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        step = str((receipt.metadata or {}).get("step") or "download")
        if step == "download":
            try:
                status = self._check_download(receipt)
            except TransferError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"中继下载状态查询失败：{exc}", retryable=True) from exc
            if status.status == "completed":
                outcome = self._upload()
                if outcome.status == "auth_expired":
                    self._on_auth_expired()
                    return OperationStatus(
                        "failed",
                        outcome.message or "移动云认证已过期，暂停同账号上传任务",
                        error_code=ErrorCode.AUTH_EXPIRED,
                    )
                if outcome.status in {"done", "completed"}:
                    return OperationStatus(
                        "completed",
                        outcome.message or "移动云上传完成",
                        expected_file_count=outcome.file_count,
                        completed_file_count=outcome.file_count,
                        total_size=outcome.total_size,
                    )
                if outcome.status == "unknown":
                    return OperationStatus("unknown", outcome.message or "移动云上传结果未知")
                if outcome.status == "failed":
                    return OperationStatus(
                        "failed",
                        outcome.message or "移动云上传失败",
                        error_code=outcome.error_code or ErrorCode.NETWORK_TRANSIENT,
                    )
                return OperationStatus("running", outcome.message or "移动云上传进行中")
            if status.status == "failed":
                return OperationStatus(
                    "failed",
                    status.message or "中继下载失败",
                    error_code=status.error_code or ErrorCode.NETWORK_TRANSIENT,
                )
            if status.status == "unknown":
                return OperationStatus("unknown", status.message or "中继下载结果未知")
            return OperationStatus("running", status.message or "中继下载进行中")
        # upload step（同步上传器一般直接返回终态；此处防御处理）
        check = getattr(self.mobile_uploader, "check_upload", None)
        if callable(check):
            outcome = normalize_upload_outcome(check(receipt))
            if outcome.status in {"done", "completed"}:
                return OperationStatus("completed", outcome.message or "移动云上传完成", total_size=outcome.total_size)
            if outcome.status == "auth_expired":
                self._on_auth_expired()
                return OperationStatus("failed", outcome.message or "移动云认证已过期", error_code=ErrorCode.AUTH_EXPIRED)
            if outcome.status == "failed":
                return OperationStatus("failed", outcome.message or "移动云上传失败", error_code=outcome.error_code or ErrorCode.NETWORK_TRANSIENT)
            return OperationStatus("running", outcome.message or "移动云上传进行中")
        return OperationStatus("running", "移动云上传进行中")

    def _check_download(self, receipt: OperationReceipt) -> DownloadStatus:
        check = getattr(self.rclone_adapter, "check", None)
        if not callable(check):
            return DownloadStatus("completed", "下载适配器无状态检查，按完成处理")
        value = check(receipt)
        if isinstance(value, DownloadStatus):
            return value
        if isinstance(value, dict):
            text = str(value.get("status") or "").strip().lower()
            status = "completed" if text in {"completed", "done", "success"} else ("failed" if text in {"failed", "error"} else ("unknown" if text == "unknown" else "running"))
            return DownloadStatus(status, str(value.get("message") or ""), str(value.get("error_code") or ""))
        if isinstance(value, str):
            text = value.strip().lower()
            status = "completed" if text in {"completed", "done", "success"} else ("failed" if text in {"failed", "error"} else ("unknown" if text == "unknown" else "running"))
            return DownloadStatus(status, value)
        return DownloadStatus("running", "中继下载进行中")

    def _upload(self) -> UploadOutcome:
        local_dir = self._relay_dir()
        target = self.plan.destination_staging or {"namespace_id": "cmcc:api", "path": "/", "kind": "openlist"}
        try:
            outcome = self.mobile_uploader.upload_local_dir(
                local_dir=local_dir,
                target_locator=target,
                idempotency_key=f"relay-up:{self.plan.idempotency_key}",
            )
        except CmccAuthExpired:
            return UploadOutcome("auth_expired", "移动云认证已过期")
        except TransferError as exc:
            return UploadOutcome("failed", exc.message, error_code=exc.code)
        except Exception as exc:  # noqa: BLE001
            return UploadOutcome("failed", f"移动云上传异常：{exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
        return normalize_upload_outcome(outcome)

    def _on_auth_expired(self) -> None:
        account_id = self.plan.destination_account_id or self.plan.destination_id
        self.halted_account_ids.add(account_id)
        if self.alert_service is not None:
            try:
                self.alert_service(
                    account_id=account_id,
                    code=ErrorCode.AUTH_EXPIRED,
                    message="移动云认证已过期，已暂停该账号后续上传任务",
                )
            except Exception:  # noqa: BLE001
                pass

    def verify(self, request: Any = None) -> TransferVerifyResult:
        verify_target = getattr(self.mobile_uploader, "verify_target", None)
        if not callable(verify_target):
            return TransferVerifyResult("unknown", "移动云上传器不支持目标校验", code=ErrorCode.RESULT_UNKNOWN)
        expected = self.plan.verification_expected()
        target = self.plan.destination_staging or {"namespace_id": "cmcc:api", "path": "/", "kind": "openlist"}
        try:
            value = verify_target(
                target_locator=target,
                expected_file_count=int(expected["file_count"]),
                expected_total_size=int(expected["total_size"]),
            )
        except CmccAuthExpired:
            return TransferVerifyResult("unknown", "移动云认证已过期，无法确认目标", code=ErrorCode.RESULT_UNKNOWN)
        except Exception as exc:  # noqa: BLE001
            return TransferVerifyResult("unknown", f"移动云目标校验失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)
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
        return CancelResult("cancel_pending", "已请求停止中继下载/上传", operation_id=receipt.operation_id)


class CmccMobileUploader:
    """把现有 CmccUploader 包装为 relay 链路的移动云上传器。

    对 /data/relay/job-<id> 下每个文件调用 CmccUploader.upload_file，随后读取
    目标目录清单做文件数与总大小校验。认证过期以 CmccAuthExpired / auth_expired
    显式返回，不做盲重试。
    """

    def __init__(
        self,
        uploader: CmccUploader | None = None,
        *,
        target_parent_path: str = "",
        progress: Any = None,
    ) -> None:
        self.uploader = uploader or CmccUploader()
        self.target_parent_path = str(target_parent_path or "")
        self.progress = progress

    def _list_local_files(self, local_dir: str) -> list[tuple[str, int]]:
        import os

        root = str(local_dir or "")
        if not root or not os.path.isdir(root):
            return []
        result: list[tuple[str, int]] = []
        for current, _dirs, files in os.walk(root):
            for name in sorted(files):
                full = os.path.join(current, name)
                rel = posixpath.normpath(os.path.relpath(full, root))
                if rel.startswith("..") or rel == ".":
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                result.append((full, size))
        return result

    def upload_local_dir(
        self,
        *,
        local_dir: str,
        target_locator: dict[str, str],
        idempotency_key: str = "",
        progress: Any = None,
    ) -> UploadOutcome:
        del idempotency_key
        files = self._list_local_files(local_dir)
        if not files:
            return UploadOutcome("failed", f"中继目录为空或不存在：{local_dir}", error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE)
        target_path = str(target_locator.get("path") or self.target_parent_path or "")
        uploaded: list[CmccUploadResult] = []
        for full_path, _size in files:
            try:
                result = self.uploader.upload_file(
                    local_file=full_path,
                    target_parent_path=target_path,
                    target_name=posixpath.basename(full_path),
                    progress=progress or self.progress,
                )
            except CmccAuthExpired:
                return UploadOutcome("auth_expired", "移动云认证已过期")
            except CmccAuthConfigError as exc:
                return UploadOutcome("failed", f"移动云认证配置错误：{exc}", error_code=ErrorCode.AUTH_CONFIG)
            except CmccApiError as exc:
                return UploadOutcome(
                    "auth_expired" if exc.auth_expired else "failed",
                    f"移动云上传失败：{exc}",
                    error_code=ErrorCode.AUTH_EXPIRED if exc.auth_expired else ErrorCode.NETWORK_TRANSIENT,
                )
            except Exception as exc:  # noqa: BLE001
                return UploadOutcome("failed", f"移动云上传异常：{exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
            uploaded.append(result)
            if not result.success:
                return UploadOutcome(
                    "failed",
                    f"移动云上传未完成：{result.message}",
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
        total_size = sum(int(item.size or 0) for item in uploaded)
        return UploadOutcome("done", "移动云上传完成", file_count=len(uploaded), total_size=total_size)

    def verify_target(
        self,
        *,
        target_locator: dict[str, str],
        expected_file_count: int,
        expected_total_size: int,
    ) -> TransferVerifyResult:
        target_path = str(target_locator.get("path") or self.target_parent_path or "")
        parent_id = self._resolve_readonly(target_path)
        if not parent_id:
            return TransferVerifyResult("missing", f"移动云目标目录不存在：{target_path}", code=ErrorCode.VERIFY_MISMATCH)
        try:
            items = self.uploader.client.list_dir(parent_id)
        except CmccAuthExpired:
            return TransferVerifyResult("unknown", "移动云认证已过期，无法确认目标", code=ErrorCode.RESULT_UNKNOWN)
        except CmccApiError as exc:
            return TransferVerifyResult(
                "unknown" if exc.auth_expired else "unknown",
                f"移动云目录读取失败：{exc}",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        files = [item for item in items if not _item_is_dir(item)]
        count = len(files)
        total_size = sum(int(item.get("size") or 0) for item in files)
        problems: list[str] = []
        if expected_file_count > 0 and count != expected_file_count:
            problems.append(f"文件数 {count} != {expected_file_count}")
        if expected_total_size > 0 and total_size != expected_total_size:
            problems.append(f"总大小 {total_size} != {expected_total_size}")
        if problems:
            return TransferVerifyResult(
                "missing",
                "; ".join(problems),
                file_count=count,
                total_size=total_size,
                expected_file_count=expected_file_count,
                expected_total_size=expected_total_size,
                code=ErrorCode.VERIFY_MISMATCH,
            )
        return TransferVerifyResult(
            "ok",
            "移动云目标校验通过",
            file_count=count,
            total_size=total_size,
            expected_file_count=expected_file_count,
            expected_total_size=expected_total_size,
        )

    def _resolve_readonly(self, cloud_path: str) -> str:
        current = "/"
        for segment in str(cloud_path or "").strip("/").split("/"):
            if not segment:
                continue
            try:
                exists = self.uploader.client.check_exists(parent_file_id=current, file_name=segment)
            except CmccAuthExpired:
                raise
            except Exception:  # noqa: BLE001
                return ""
            if not exists.get("exist") or not exists.get("file_id"):
                return ""
            current = str(exists["file_id"])
        return current


def _item_is_dir(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_dir") is True or item.get("isDir") is True or item.get("dir") is True:
        return True
    type_text = str(item.get("file_type") or item.get("fileType") or item.get("type") or "").strip().lower()
    return type_text in {"dir", "folder", "catalog", "directory"}
