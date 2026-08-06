"""RcloneTransferExecutor —— 跨盘 rclone_copy 执行器（§10.3）。

链路：来源 Provider 原生保存到任务级落地区 -> OpenList 可见 -> rclone 复制到目标
OpenList 路径或受控本地路径 -> 文件数/总大小校验 -> ACK -> 清理落地区。

本执行器复用现有 rclone_service 的 ``start(staging_run=...)`` copy 模式：
通过 locator_resolver 把计划的 namespace/mount/path 解析为 ``remote:path``，
并按 ``rclone_copy_run_from_job`` 的字段形态构造 copy_run，不做任何目标决策。
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
    VisibilityStatus,
    normalize_receipt,
)

__all__ = [
    "RcloneLocatorResolver",
    "RcloneTransferExecutor",
    "split_job_dir",
]


def split_job_dir(path: Any) -> tuple[str, str]:
    """把路径拆为 (根目录, job 叶子)。叶子形如 ``job-<id>`` 时返回，否则叶子为空。"""
    text = str(path or "").strip().replace("\\", "/")
    absolute = text.startswith("/")
    parts = [part for part in text.strip("/").split("/") if part]
    if parts and parts[-1].startswith("job-") and parts[-1][4:].isdigit():
        root = "/".join(parts[:-1])
        return (f"/{root}" if absolute else root), parts[-1]
    root = "/".join(parts)
    return (f"/{root}" if absolute else root), ""


_CATEGORY_SUFFIX = {
    "movie": "MOVIE",
    "tv": "TV",
    "anime": "ANIME",
    "variety": "VARIETY",
    "other": "OTHER",
}


def _category_suffix(category_key: str) -> str:
    key = str(category_key or "").strip().lower()
    return _CATEGORY_SUFFIX.get(key) or "MOVIE"


class RcloneLocatorResolver:
    """把计划的 namespace/mount/path 解析为 rclone ``remote:path`` 或受控本地路径。"""

    def remote(self, *, namespace_id: str = "", mount_id: str = "", path: str = "") -> str:
        raise NotImplementedError

    def local(self, *, namespace_id: str = "", path: str = "") -> str:
        raise NotImplementedError


class RcloneTransferExecutor:
    strategy = "rclone_copy"
    needs_source_landing = True

    def __init__(
        self,
        rclone_service_adapter: Any,
        locator_resolver: RcloneLocatorResolver,
        *,
        source_materializer: Any = None,
        visibility_poller: Any = None,
        verifier: Any = None,
        target_lister: Any = None,
    ) -> None:
        self.rclone = rclone_service_adapter
        self.locator_resolver = locator_resolver
        self.source_materializer = source_materializer
        self.visibility_poller = visibility_poller
        self.verifier = verifier
        if self.verifier is None and target_lister is not None:
            from fnos_media_import.transfer.verifier import TransferVerifier

            self.verifier = TransferVerifier(target_lister=target_lister)

    def bind(self, plan: TransferPlan) -> "RcloneTransferExecutor":
        self.plan = plan
        return self

    # -- 来源物化（native_save 到来源落地区） --
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

    # -- 传输 --
    def submit(self, existing_receipt: OperationReceipt | None = None) -> OperationReceipt:
        if existing_receipt is not None:
            return existing_receipt
        copy_run = self.build_copy_run()
        try:
            result = self.rclone.start(reason=f"transfer:{self.plan.job_id}:rclone_copy", staging_run=copy_run)
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"rclone 提交失败：{exc}", retryable=True) from exc
        if not isinstance(result, dict):
            result = {"success": bool(result)}
        if not result.get("success"):
            code = _classify_rclone_start(result)
            if code is not None:
                raise TransferError(code, str(result.get("message") or "rclone 启动被拒绝"))
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, str(result.get("message") or "rclone 启动失败"), retryable=True)
        run_id = str(result.get("run_id") or result.get("current_run_id") or "")
        status = "running"
        if result.get("completed"):
            status = "completed"
        elif result.get("queued") or result.get("already_queued"):
            status = "submitted"
        return OperationReceipt(
            operation_id=f"rclone-copy:{self.plan.job_id}" + (f":{run_id}" if run_id else ""),
            status=status,
            provider_kind="rclone",
            message=str(result.get("message") or "rclone 搬运已提交"),
            metadata={"copy_run": copy_run, "run_id": run_id},
        )

    def build_copy_run(self) -> dict[str, Any]:
        plan = self.plan
        landing = plan.source_landing
        staging = plan.destination_staging
        if landing is None or staging is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "rclone_copy 需要来源落地与目标暂存路径")
        source_root, job_dir = split_job_dir(landing["path"])
        target_root, _target_job_dir = split_job_dir(staging["path"])
        expected_job_dir = f"job-{plan.job_id}"
        if job_dir != expected_job_dir:
            raise TransferError(ErrorCode.PATH_CONFLICT, f"来源落地区不是任务级目录：{landing['path']}（需要 {expected_job_dir}）")
        source_remote = self._remote_for(landing, mount_id=plan.source_mount_id)
        is_local = staging["kind"] == "local"
        target_remote = "" if is_local else self._remote_for(staging, mount_id=plan.destination_mount_id)
        local_target = ""
        if is_local:
            if self.locator_resolver is None:
                raise TransferError(ErrorCode.INVALID_PLAN, "缺少 locator resolver，无法解析本地目标路径")
            local_target = self.locator_resolver.local(namespace_id=staging["namespace_id"], path=target_root)
        return {
            "copy_mode": True,
            "job_id": plan.job_id,
            "category": plan.category_key or "movie",
            "category_suffix": _category_suffix(plan.category_key),
            "job_dir_name": job_dir,
            "source_remote": source_remote,
            "source_root": source_root,
            "target_remote": target_remote,
            "target_root": target_root,
            "local_target": local_target,
            "destination_id": plan.destination_id,
            "delete_source_after_ack": plan.delete_source_after_ack,
        }

    def _remote_for(self, descriptor: dict[str, str], mount_id: str = "") -> str:
        if self.locator_resolver is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "缺少 locator resolver，无法解析 rclone remote")
        return self.locator_resolver.remote(
            namespace_id=descriptor.get("namespace_id", ""),
            mount_id=mount_id or descriptor.get("mount_id", ""),
            path=descriptor.get("path", ""),
        )

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            status = self.rclone.status()
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"rclone 状态查询失败：{exc}", retryable=True) from exc
        if not isinstance(status, dict):
            status = {"running": bool(status)}
        if status.get("running"):
            return OperationStatus("running", "rclone 搬运进行中")
        run_status = str(status.get("run_status") or status.get("status") or "").strip().lower()
        if run_status in {"success", "done", "completed", "skipped_existing"}:
            return OperationStatus("completed", "rclone 搬运完成")
        if run_status in {"failed", "error"}:
            return OperationStatus("failed", str(status.get("message") or "rclone 搬运失败"))
        if run_status in {"cancelled", "canceled", "stopped"}:
            return OperationStatus("cancelled", "rclone 搬运已停止")
        return OperationStatus("unknown", "rclone 运行结束但状态未知")

    def verify(self, request: Any = None) -> TransferVerifyResult:
        if self.verifier is None:
            return TransferVerifyResult("unknown", "未配置目标校验器", code=ErrorCode.RESULT_UNKNOWN)
        expected = self.plan.verification_expected()
        expected_file_count = int(expected["file_count"]) or (getattr(request, "expected_file_count", 0) or 0)
        expected_total_size = int(expected["total_size"]) or (getattr(request, "expected_total_size", 0) or 0)
        checksum = str(expected["checksum"] or (getattr(request, "checksum", "") or ""))
        descriptor = self.plan.destination_staging or self.plan.destination_final
        return self.verifier.verify(
            self.plan,
            descriptor=descriptor,
            expected_file_count=expected_file_count,
            expected_total_size=expected_total_size,
            checksum=checksum,
        )

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        cancel_op = getattr(self.rclone, "cancel", None)
        if not callable(cancel_op):
            return CancelResult("not_cancellable", "rclone 适配器不支持取消", operation_id=receipt.operation_id)
        try:
            result = cancel_op(self.plan.job_id, stop_running=True)
        except Exception as exc:  # noqa: BLE001
            return CancelResult("cancel_pending", f"rclone 取消请求异常：{exc}", operation_id=receipt.operation_id)
        if isinstance(result, dict) and result.get("success") is False:
            return CancelResult("not_cancellable", str(result.get("message") or "rclone 取消请求失败"), operation_id=receipt.operation_id)
        return CancelResult("cancel_pending", str((result or {}).get("message") or "已请求停止 rclone 搬运"), operation_id=receipt.operation_id)


def _classify_rclone_start(result: dict[str, Any]) -> str | None:
    """把 rclone 启动拒绝结果分类为可失败的错误码（不进入重试）。"""
    message = str(result.get("message") or "").lower()
    status = str(result.get("status") or "").lower()
    combined = f"{message} {status}"
    if any(token in combined for token in ("auth", "cookie", "token", "登录", "认证")):
        return ErrorCode.AUTH_EXPIRED
    if any(token in combined for token in ("cancel", "cancelled", "停止", "stopped")):
        return ErrorCode.CANCELLED
    if any(token in combined for token in ("plan", "unsupported", "不支持", "拒绝")):
        return ErrorCode.UNSUPPORTED_PLAN_VERSION
    return None
