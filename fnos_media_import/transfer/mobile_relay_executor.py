"""MobileRelayExecutor —— 其他来源 -> 移动云中继上传（relay_upload_mobile，§10.4）。

链路：来源原生保存到落地区 -> rclone 下载到 /data/relay/job-<id> -> 移动云 API 上传
（CmccUploader）-> 目录结果校验 -> ACK -> 清理落地区与本地缓存。

移动云大文件通过 OpenList 直传存在卡 100% 的问题，因此跨盘写移动云唯一例外链路为
「本地中继 + 移动云上传 API」。rclone 只负责下载；移动云 Adapter 负责秒传、完整上传、
认证错误与结果确认。

认证失效：停止同账号后续上传任务并在后台生成账号告警，不进行盲重试（§10.4）。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import posixpath
import tempfile
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

    _GLOBAL_HALTED_ACCOUNT_IDS: set[str] = set()

    def __init__(
        self,
        rclone_adapter: Any,
        mobile_uploader: Any,
        *,
        source_materializer: Any = None,
        visibility_poller: Any = None,
        relay_root: str | None = None,
        alert_service: Any = None,
        halted_account_ids: set[str] | None = None,
    ) -> None:
        self.rclone_adapter = rclone_adapter
        self.mobile_uploader = mobile_uploader
        self.source_materializer = source_materializer
        self.visibility_poller = visibility_poller
        # Do not silently select a provider root.  Composition Root should
        # inject this value; a plan may also carry an explicit task-local path
        # and is checked against the injected root below.
        self.relay_root = self._normalize_root(relay_root)
        self.alert_service = alert_service
        # The dispatch service owns the circuit set and injects it into each
        # short-lived executor.  Direct callers (and isolated jobs/tests) get
        # an instance-local set, while a production dispatch service still
        # preserves the account-level halt across durable worker steps without
        # leaking state between independent compositions.
        self.halted_account_ids = (
            halted_account_ids
            if isinstance(halted_account_ids, set)
            else set()
        )

    def bind(self, plan: TransferPlan) -> "MobileRelayExecutor":
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

    def _relay_dir(self) -> str:
        if getattr(self, "plan", None) is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "relay executor is not bound to a plan")
        expected_name = f"job-{int(self.plan.job_id)}"
        frozen = self.plan.local_temp
        frozen_path = ""
        if isinstance(frozen, dict):
            # Planner freezes relay cache descriptors as ``kind=relay`` to
            # distinguish them from user-configured local storage.  The
            # namespace remains local (``local:relay``); older hand-built V1
            # fixtures may omit ``kind`` and are treated as local.  Never
            # accept a remote/native namespace here because this directory is
            # used as a host filesystem path by rclone.
            kind = str(frozen.get("kind") or "local").strip().lower()
            if kind not in {"local", "relay"}:
                raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp must be a local/relay target")
            namespace_id = str(frozen.get("namespace_id") or "").strip().lower()
            if namespace_id and not namespace_id.startswith("local:"):
                raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp must use a local namespace")
            frozen_path = self._normalize_root(frozen.get("path"))

        if self.relay_root:
            candidate = f"{self.relay_root}/{expected_name}"
            if frozen_path and self._normalize_root(frozen_path) != candidate:
                raise TransferError(
                    ErrorCode.INVALID_PLAN,
                    "frozen relay local_temp does not match the configured relay root",
                )
            return candidate

        # An executor created without an injected root may only use a fully
        # explicit plan path.  It must still be task-scoped; there is no
        # fallback to /data/relay or to a provider root.
        if not frozen_path:
            raise TransferError(ErrorCode.INVALID_PLAN, "relay_root or plan.local_temp is required")
        parts = [part for part in frozen_path.split("/") if part]
        if not parts or parts[-1] != expected_name:
            raise TransferError(ErrorCode.INVALID_PLAN, f"relay local_temp must end with {expected_name}")
        if any(part in {".", ".."} for part in parts):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay local_temp contains traversal segments")
        return frozen_path

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
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"中继下载提交失败：{exc}", retryable=True) from exc
        receipt = normalize_receipt(receipt, provider_kind="rclone")
        receipt.metadata.setdefault("step", "download")
        receipt.metadata.setdefault("operation_key", operation_key)
        receipt.metadata.setdefault("idempotency_key", operation_key)
        receipt.metadata.setdefault("local_dir", local_dir)
        return receipt

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        if not isinstance(receipt, OperationReceipt):
            return OperationStatus("unknown", "relay receipt is invalid", error_code=ErrorCode.RESULT_UNKNOWN)
        step = str((receipt.metadata or {}).get("step") or "download").strip().lower()
        if step == "download":
            try:
                status = self._check_download(receipt)
            except TransferError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"中继下载状态查询失败：{exc}", retryable=True) from exc
            if status.status == "completed":
                outcome = self._upload(receipt)
                return self._upload_status(outcome)
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
        if step == "upload":
            return self._poll_upload(receipt)
        return OperationStatus("unknown", f"unknown relay receipt step: {step}", error_code=ErrorCode.RESULT_UNKNOWN)


    def _check_download(self, receipt: OperationReceipt) -> DownloadStatus:
        check = getattr(self.rclone_adapter, "check", None)
        if not callable(check):
            return DownloadStatus("unknown", "rclone adapter does not expose a scoped download status check", ErrorCode.RESULT_UNKNOWN)
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

    def _upload(self, receipt: OperationReceipt) -> UploadOutcome:
        local_dir = self._relay_dir()
        target = self._destination_target()
        metadata = receipt.metadata
        operation_key = f"relay-up:{self.plan.idempotency_key}"
        account_id = self.plan.destination_account_id or self.plan.destination_id
        if account_id in self.halted_account_ids:
            return UploadOutcome("auth_expired", "移动云账号已被熔断，等待管理员重新检测")
        # Set the fence before calling the external API.  The orchestrator
        # persists this mutable metadata with the receipt.
        metadata["step"] = "upload"
        metadata.setdefault("upload_idempotency_key", operation_key)
        fence = metadata.setdefault("upload_submission_fence", {})
        if isinstance(fence, dict):
            fence.setdefault("operation_key", operation_key)
            fence.setdefault("state", "prepared")
        previous = normalize_upload_outcome(metadata.get("upload_outcome")) if metadata.get("upload_outcome") else None
        if previous is not None and previous.status in {"done", "completed", "auth_expired", "failed", "unknown"}:
            return previous
        try:
            outcome = self.mobile_uploader.upload_local_dir(
                local_dir=local_dir,
                target_locator=target,
                idempotency_key=operation_key,
            )
        except CmccAuthExpired:
            outcome = UploadOutcome("auth_expired", "mobile account authorization expired")
        except TransferError as exc:
            outcome = UploadOutcome("failed", exc.message, error_code=exc.code)
        except Exception as exc:  # noqa: BLE001
            outcome = UploadOutcome("failed", f"mobile upload exception: {exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
        normalized = normalize_upload_outcome(outcome)
        metadata["upload_outcome"] = _upload_outcome_dict(normalized)
        if isinstance(fence, dict):
            fence["state"] = normalized.status
        return normalized

    def _poll_upload(self, receipt: OperationReceipt) -> OperationStatus:
        metadata = receipt.metadata
        stored = metadata.get("upload_outcome")
        outcome = normalize_upload_outcome(stored) if stored else None
        check = getattr(self.mobile_uploader, "check_upload", None)
        if (outcome is None or outcome.status == "running") and callable(check):
            try:
                outcome = normalize_upload_outcome(check(receipt))
            except Exception as exc:  # noqa: BLE001
                return OperationStatus("unknown", f"mobile upload status query failed: {exc}", error_code=ErrorCode.RESULT_UNKNOWN)
            metadata["upload_outcome"] = _upload_outcome_dict(outcome)
        if outcome is None:
            return OperationStatus(
                "unknown",
                "upload receipt has no durable outcome or scoped checker",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        return self._upload_status(outcome)

    def _upload_status(self, outcome: UploadOutcome) -> OperationStatus:
        if outcome.status in {"done", "completed"}:
            return OperationStatus(
                "completed",
                outcome.message or "mobile upload completed",
                expected_file_count=outcome.file_count,
                completed_file_count=outcome.file_count,
                total_size=outcome.total_size,
            )
        if outcome.status == "auth_expired":
            self._on_auth_expired()
            return OperationStatus("failed", outcome.message or "mobile account authorization expired", error_code=ErrorCode.AUTH_EXPIRED)
        if outcome.status == "failed":
            return OperationStatus("failed", outcome.message or "mobile upload failed", error_code=outcome.error_code or ErrorCode.NETWORK_TRANSIENT)
        if outcome.status == "unknown":
            return OperationStatus("unknown", outcome.message or "mobile upload result is unknown", error_code=ErrorCode.RESULT_UNKNOWN)
        return OperationStatus("running", outcome.message or "mobile upload is running")

    def _destination_target(self) -> dict[str, str]:
        """Return the frozen final destination for the relay upload.

        Relay is a two-leg data plane (source landing -> local temp -> mobile
        final).  It deliberately has no destination staging directory; using
        one here would reintroduce the removed legacy contract.
        """
        target = self.plan.destination_final
        if not isinstance(target, dict):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay_upload_mobile requires an explicit destination_final")
        namespace_id = str(target.get("namespace_id") or "").strip()
        path = str(target.get("path") or "").strip()
        if not namespace_id or not path:
            raise TransferError(ErrorCode.INVALID_PLAN, "destination_final must include namespace_id and path")
        if not namespace_id.lower().startswith("native:"):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay destination_final must use a native namespace")
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
                    message="移动云认证已过期，已暂停该账号后续上传任务",
                )
            except Exception:  # noqa: BLE001
                pass

    def verify(self, request: Any = None) -> TransferVerifyResult:
        verify_target = getattr(self.mobile_uploader, "verify_target", None)
        if not callable(verify_target):
            return TransferVerifyResult("unknown", "移动云上传器不支持目标校验", code=ErrorCode.RESULT_UNKNOWN)
        expected = self.plan.verification_expected()
        try:
            target = self._destination_target()
        except TransferError as exc:
            return TransferVerifyResult("unknown", exc.message, code=exc.code)

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

    def _list_local_files(self, local_dir: str) -> list[tuple[str, str, int]]:
        root = str(local_dir or "")
        if not root or not os.path.isdir(root):
            return []
        root_real = os.path.realpath(root)
        result: list[tuple[str, str, int]] = []
        for current, _dirs, files in os.walk(root_real):
            for name in sorted(files):
                # These are executor/uploader bookkeeping files, never source
                # content.  Uploading them was a subtle relay corruption bug.
                if name.startswith(".fnos-relay-upload-") or name.endswith(".fnos-cmcc-upload.json"):
                    continue
                full = os.path.realpath(os.path.join(current, name))
                try:
                    if os.path.commonpath((root_real, full)) != root_real:
                        continue
                    size = os.path.getsize(full)
                except (OSError, ValueError):
                    continue
                rel = os.path.relpath(full, root_real).replace(os.sep, "/")
                rel = posixpath.normpath(rel)
                if rel.startswith("../") or rel in {"..", "."}:
                    continue
                result.append((full, rel, int(size)))
        return result

    def upload_local_dir(
        self,
        *,
        local_dir: str,
        target_locator: dict[str, str],
        idempotency_key: str = "",
        progress: Any = None,
    ) -> UploadOutcome:
        operation_key = str(idempotency_key or "").strip()
        if not operation_key:
            return UploadOutcome("failed", "mobile relay upload requires an idempotency key", error_code=ErrorCode.INVALID_PLAN)
        if not isinstance(target_locator, dict):
            return UploadOutcome("failed", "mobile relay upload requires an explicit target locator", error_code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        target_parent_id = str(
            target_locator.get("parent_file_id")
            or target_locator.get("parentFileId")
            or ""
        ).strip()
        if not target_path and not target_parent_id:
            return UploadOutcome("failed", "mobile relay target path or parent id is required", error_code=ErrorCode.INVALID_PLAN)

        files = self._list_local_files(local_dir)
        if not files:
            return UploadOutcome("failed", f"relay directory is empty or missing: {local_dir}", error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE)

        manifest_path = self._operation_manifest_path(local_dir, operation_key)
        manifest = self._read_operation_manifest(manifest_path)
        target_fingerprint = json.dumps(
            {"path": target_path, "parent_file_id": target_parent_id},
            ensure_ascii=False,
            sort_keys=True,
        )
        fresh_manifest = (
            manifest.get("idempotency_key") != operation_key
            or manifest.get("target_fingerprint") != target_fingerprint
        )
        if fresh_manifest:
            manifest = {
                "version": 1,
                "idempotency_key": operation_key,
                "target_fingerprint": target_fingerprint,
                "status": "uploading",
                "files": {},
            }
        elif str(manifest.get("status") or "").lower() == "completed":
            records = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
            return UploadOutcome(
                "done",
                "mobile upload already reconciled from durable manifest",
                file_count=sum(1 for item in records.values() if isinstance(item, dict) and item.get("success")),
                total_size=sum(int(item.get("size") or 0) for item in records.values() if isinstance(item, dict) and item.get("success")),
            )
        elif str(manifest.get("status") or "").lower() == "uploading":
            # The previous process may have crashed after the Provider call
            # but before recording its response.  Do not submit the same
            # operation again; an operator/provider reconcile is required.
            return UploadOutcome("unknown", "移动云上传 fence 存在但结果未知")
        records = manifest.setdefault("files", {})
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法持久化移动云上传 fence", error_code=ErrorCode.RESULT_UNKNOWN)
        uploaded: list[CmccUploadResult] = []
        callback = progress or self.progress
        for full_path, relative_path, size in files:
            entry = records.get(relative_path)
            if isinstance(entry, dict) and entry.get("success") and int(entry.get("size") or -1) == size:
                uploaded.append(_cmcc_result_from_dict(entry.get("result"), relative_path, size))
                continue
            file_key = f"{operation_key}:file:{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:24]}"
            kwargs: dict[str, Any] = {
                "local_file": full_path,
                "target_parent_file_id": target_parent_id,
                "target_parent_path": target_path,
                "target_name": posixpath.basename(relative_path),
                "target_relative_dir": posixpath.dirname(relative_path) if posixpath.dirname(relative_path) != "." else "",
                "progress": callback,
            }
            try:
                result = self._call_upload_file(kwargs, file_key)
            except CmccAuthExpired:
                return UploadOutcome("auth_expired", "mobile account authorization expired")
            except CmccAuthConfigError as exc:
                return UploadOutcome("failed", f"mobile credential configuration error: {exc}", error_code=ErrorCode.AUTH_CONFIG)
            except CmccApiError as exc:
                return UploadOutcome(
                    "auth_expired" if exc.auth_expired else "failed",
                    f"mobile upload failed: {exc}",
                    error_code=ErrorCode.AUTH_EXPIRED if exc.auth_expired else ErrorCode.NETWORK_TRANSIENT,
                )
            except TransferError as exc:
                return UploadOutcome("failed", exc.message, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                return UploadOutcome("failed", f"mobile upload exception: {exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
            result = _coerce_cmcc_result(result, relative_path, size)
            records[relative_path] = {"success": bool(result.success), "size": size, "result": result.to_dict()}
            manifest["status"] = "uploading"
            if not self._write_operation_manifest(manifest_path, manifest):
                return UploadOutcome("unknown", "无法持久化移动云逐文件回执", error_code=ErrorCode.RESULT_UNKNOWN)
            if not result.success:
                return UploadOutcome(
                    "failed",
                    f"mobile upload incomplete: {result.message}",
                    file_count=len(uploaded),
                    total_size=sum(int(item.size or 0) for item in uploaded),
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
            uploaded.append(result)

        manifest["status"] = "completed"
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法持久化移动云完成回执", error_code=ErrorCode.RESULT_UNKNOWN)
        total_size = sum(int(item.size or 0) for item in uploaded)
        return UploadOutcome("done", "mobile upload completed", file_count=len(uploaded), total_size=total_size)

    def _call_upload_file(self, kwargs: dict[str, Any], file_key: str) -> Any:
        method = getattr(self.uploader, "upload_file", None)
        if not callable(method):
            raise TransferError(ErrorCode.AUTH_CONFIG, "mobile uploader does not expose upload_file")
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if "target_relative_dir" not in params and not accepts_kwargs:
            raise TransferError(ErrorCode.INVALID_PLAN, "mobile uploader must support target_relative_dir")
        if "idempotency_key" in params or accepts_kwargs:
            kwargs["idempotency_key"] = file_key
        if not accepts_kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key in params}
        return method(**kwargs)

    @staticmethod
    def _operation_manifest_path(local_dir: str, operation_key: str) -> str:
        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(os.path.realpath(local_dir), f".fnos-relay-upload-{digest}.json")

    @staticmethod
    def _read_operation_manifest(path: str) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write_operation_manifest(path: str, value: dict[str, Any]) -> bool:
        directory = os.path.dirname(path) or "."
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".fnos-relay-upload-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return True
        except OSError:
            try:
                if "temp_path" in locals() and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
            return False

    def _list_all_files(self, parent_id: str) -> list[dict[str, Any]]:
        """Recursively enumerate a Mobile Cloud directory when possible."""

        client = self.uploader.client
        for method_name in ("list_tree", "walk_dir", "list_recursive"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    value = method(parent_id)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
                except Exception:
                    # Fall back to the conservative recursive list below.
                    pass
        result: list[dict[str, Any]] = []
        queue: list[tuple[str, int]] = [(str(parent_id), 0)]
        visited: set[str] = set()
        while queue and len(result) < 10000:
            current, depth = queue.pop(0)
            if current in visited or depth > 32:
                continue
            visited.add(current)
            entries = client.list_dir(current)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if _item_is_dir(item):
                    child = str(item.get("file_id") or item.get("fileId") or item.get("id") or "")
                    if child:
                        queue.append((child, depth + 1))
                else:
                    result.append(item)
        return result


    def verify_target(
        self,
        *,
        target_locator: dict[str, str],
        expected_file_count: int,
        expected_total_size: int,
    ) -> TransferVerifyResult:
        if not isinstance(target_locator, dict):
            return TransferVerifyResult("unknown", "mobile relay target locator is missing", code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        parent_id = str(target_locator.get("parent_file_id") or target_locator.get("parentFileId") or "").strip()
        if not target_path and not parent_id:
            return TransferVerifyResult("unknown", "mobile relay target path or parent id is required", code=ErrorCode.INVALID_PLAN)
        if not parent_id:
            parent_id = self._resolve_readonly(target_path)
        if not parent_id:
            return TransferVerifyResult("missing", f"移动云目标目录不存在：{target_path}", code=ErrorCode.VERIFY_MISMATCH)
        try:
            items = self._list_all_files(parent_id)
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



def _upload_outcome_dict(outcome: UploadOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "message": outcome.message,
        "file_count": int(outcome.file_count or 0),
        "total_size": int(outcome.total_size or 0),
        "error_code": outcome.error_code,
    }


def _coerce_cmcc_result(value: Any, relative_path: str, size: int) -> CmccUploadResult:
    if isinstance(value, CmccUploadResult):
        return value
    if isinstance(value, dict):
        payload = dict(value)
        return CmccUploadResult(
            bool(payload.get("success")),
            str(payload.get("status") or ("done" if payload.get("success") else "failed")),
            str(payload.get("message") or ""),
            mode=str(payload.get("mode") or ""),
            file_id=str(payload.get("file_id") or payload.get("fileId") or ""),
            upload_id=str(payload.get("upload_id") or payload.get("uploadId") or ""),
            parent_file_id=str(payload.get("parent_file_id") or payload.get("parentFileId") or ""),
            target_name=str(payload.get("target_name") or posixpath.basename(relative_path)),
            size=int(payload.get("size") or size),
            sha256=str(payload.get("sha256") or ""),
            manifest_path=str(payload.get("manifest_path") or ""),
            raw=dict(payload.get("raw") or {}),
        )
    return CmccUploadResult(False, "failed", f"unsupported mobile upload result: {type(value).__name__}", target_name=relative_path, size=size)


def _cmcc_result_from_dict(value: Any, relative_path: str, size: int) -> CmccUploadResult:
    return _coerce_cmcc_result(value if isinstance(value, dict) else {}, relative_path, size)


def _item_is_dir(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_dir") is True or item.get("isDir") is True or item.get("dir") is True:
        return True
    type_text = str(item.get("file_type") or item.get("fileType") or item.get("type") or "").strip().lower()
    return type_text in {"dir", "folder", "catalog", "directory"}
