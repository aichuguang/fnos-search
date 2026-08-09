"""RcloneTransferExecutor —— 跨盘 rclone_copy 执行器（§10.3）。

链路：来源 Provider 原生保存到任务级落地区 -> OpenList 可见 -> rclone 复制到目标
OpenList 路径或受控本地路径 -> 文件数/总大小校验 -> ACK -> 清理落地区。

本执行器复用现有 rclone_service 的 ``start(staging_run=...)`` copy 模式：
通过 locator_resolver 把计划的 namespace/mount/path 解析为 ``remote:path``，
并按 ``rclone_copy_run_from_job`` 的字段形态构造 copy_run，不做任何目标决策。
"""

from __future__ import annotations

import posixpath
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
    "build_managed_copy_run",
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

_RELAY_UPLOAD_STRATEGIES = frozenset({"relay_upload_provider"})


def _category_suffix(category_key: str) -> str:
    key = str(category_key or "").strip().lower()
    return _CATEGORY_SUFFIX.get(key) or "MOVIE"


def build_managed_copy_run(plan: Any, locator_resolver: Any) -> dict[str, Any]:
    """Build the shell descriptor from one immutable V1 plan.

    OpenList namespace strings are identities, not rclone remote names.  This
    function asks the managed resolver to validate the frozen mount and
    connection revisions, resolves ``remote:/absolute/path/job-N``, then
    separates the remote, task root and job leaf expected by the shell worker.
    A caller-supplied remote/root is never accepted as execution truth.
    """

    if locator_resolver is None or not callable(
        getattr(locator_resolver, "resolve_frozen_remote", None)
    ):
        raise TransferError(
            ErrorCode.OPENLIST_UNAVAILABLE,
            "V1 rclone requires a managed frozen-revision locator resolver",
        )
    raw = _raw_plan(plan)
    transfer = _mapping(raw.get("transfer"))
    strategy = str(transfer.get("strategy") or "").strip().lower()
    if strategy != "rclone_copy" and strategy not in _RELAY_UPLOAD_STRATEGIES:
        return {}
    job_id = _positive_int(raw.get("job_id"))
    if job_id <= 0:
        raise TransferError(ErrorCode.INVALID_PLAN, "V1 rclone 计划缺少有效 job_id")
    paths = _mapping(raw.get("paths"))
    bindings = _mapping(raw.get("bindings"))
    source = _mapping(paths.get("source_landing"))
    target_name = "destination_staging" if strategy == "rclone_copy" else "local_temp"
    target = _mapping(paths.get(target_name))
    if not source or not target:
        raise TransferError(
            ErrorCode.INVALID_PLAN,
            f"V1 {strategy} 缺少 source_landing 或 {target_name}",
        )

    source_value = _resolve_openlist_plan_path(
        locator_resolver,
        source,
        bindings,
        role="source",
    )
    source_remote, source_path = _split_resolved_remote(locator_resolver, source_value)
    source_root, source_leaf = split_job_dir(source_path)
    expected_leaf = f"job-{job_id}"
    if source_leaf != expected_leaf:
        raise TransferError(
            ErrorCode.PATH_CONFLICT,
            f"来源落地区不是当前任务目录：{source_path}（需要 {expected_leaf}）",
        )

    target_kind = _path_kind(target)
    target_remote = ""
    target_root = ""
    local_target = ""
    if target_kind == "openlist":
        target_value = _resolve_openlist_plan_path(
            locator_resolver,
            target,
            bindings,
            role="destination",
        )
        target_remote, target_path = _split_resolved_remote(locator_resolver, target_value)
        target_root, target_leaf = split_job_dir(target_path)
        if target_leaf != expected_leaf:
            raise TransferError(
                ErrorCode.PATH_CONFLICT,
                f"目标暂存目录不是当前任务目录：{target_path}（需要 {expected_leaf}）",
            )
    elif target_kind in {"local", "relay"}:
        target_root, target_leaf = split_job_dir(target.get("path"))
        if target_leaf != expected_leaf:
            raise TransferError(
                ErrorCode.PATH_CONFLICT,
                f"本地目标不是当前任务目录：{target.get('path')}（需要 {expected_leaf}）",
            )
        local_target = _resolve_local_plan_root(
            locator_resolver,
            target,
            target_root,
            relay=strategy in _RELAY_UPLOAD_STRATEGIES,
        )
        target_root = local_target
    else:
        raise TransferError(
            ErrorCode.INVALID_PLAN,
            f"V1 rclone 目标 kind 不受支持：{target_kind or 'missing'}",
        )

    category = str(raw.get("category") or "").strip().lower()
    suffix = _CATEGORY_SUFFIX.get(category)
    if not suffix:
        raise TransferError(ErrorCode.INVALID_PLAN, "V1 rclone 分类无效")
    destination = _mapping(raw.get("destination"))
    return {
        "copy_mode": True,
        "relay_mode": strategy in _RELAY_UPLOAD_STRATEGIES,
        "job_id": job_id,
        "category": category,
        "category_suffix": suffix,
        "job_dir_name": expected_leaf,
        "source_remote": source_remote,
        "source_root": source_root,
        "target_remote": target_remote,
        "target_root": target_root,
        "local_target": local_target,
        "destination_id": str(destination.get("id") or "").strip(),
        "delete_source_after_ack": bool(transfer.get("delete_source_after_ack", True)),
    }


def _raw_plan(plan: Any) -> dict[str, Any]:
    if isinstance(plan, dict):
        return plan
    raw = getattr(plan, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _path_kind(descriptor: dict[str, Any]) -> str:
    declared = str(descriptor.get("kind") or "").strip().lower()
    if declared:
        return declared
    namespace = str(descriptor.get("namespace_id") or "").strip().lower()
    if namespace == "local:relay":
        return "relay"
    if namespace.startswith("local:"):
        return "local"
    if namespace.startswith("openlist:"):
        return "openlist"
    return ""


def _binding_identity(bindings: dict[str, Any], role: str) -> dict[str, Any]:
    prefix = "source" if role == "source" else "destination"
    return {
        "mount_id": str(bindings.get(f"{prefix}_mount_id") or "").strip(),
        "mount_revision": _positive_int(bindings.get(f"{prefix}_mount_revision")),
        "namespace_id": str(
            bindings.get(f"{prefix}_mount_namespace_id") or ""
        ).strip(),
        "connection_id": str(
            bindings.get(f"{prefix}_connection_id") or ""
        ).strip(),
        "connection_revision": _positive_int(
            bindings.get(f"{prefix}_connection_revision")
        ),
        "storage_identity_id": str(
            bindings.get(f"{prefix}_mount_storage_identity_id") or ""
        ).strip(),
        "identity_root": _normalize_identity_root(
            bindings.get(f"{prefix}_mount_identity_root")
        ),
    }


def _normalize_identity_root(value: Any) -> str:
    """Normalize the frozen storage identity root before exact comparison.

    Mount revisions and path descriptors may serialize the same root as
    ``media``, ``/media`` or ``/media/``.  Those spellings represent one
    immutable identity and must compare equally, while traversal and control
    characters remain invalid rather than being silently folded.
    """

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if "://" in text or any(ord(ch) < 32 for ch in text):
        raise TransferError(ErrorCode.INVALID_PLAN, "冻结 identity_root 格式无效")
    if ".." in text.strip("/").split("/"):
        raise TransferError(ErrorCode.INVALID_PLAN, "冻结 identity_root 不允许越级")
    return posixpath.normpath("/" + text.strip("/"))


def _resolve_openlist_plan_path(
    resolver: Any,
    descriptor: dict[str, Any],
    bindings: dict[str, Any],
    *,
    role: str,
) -> str:
    if _path_kind(descriptor) != "openlist":
        raise TransferError(ErrorCode.INVALID_PLAN, f"{role} 路径不是 OpenList")
    identity = _binding_identity(bindings, role)
    required = {
        "mount_id": identity["mount_id"],
        "mount_revision": identity["mount_revision"],
        "namespace_id": identity["namespace_id"],
        "connection_id": identity["connection_id"],
        "connection_revision": identity["connection_revision"],
        "storage_identity_id": identity["storage_identity_id"],
        "identity_root": identity["identity_root"],
    }
    missing = [key for key, value in required.items() if value in (None, "", 0)]
    if missing:
        raise TransferError(
            ErrorCode.INVALID_PLAN,
            f"{role} 路径缺少冻结 mount 身份：{','.join(missing)}",
        )
    descriptor_checks = {
        "mount_id": str(descriptor.get("mount_id") or "").strip(),
        "mount_revision": _positive_int(descriptor.get("mount_revision")),
        "namespace_id": str(descriptor.get("namespace_id") or "").strip(),
        "connection_id": str(descriptor.get("connection_id") or "").strip(),
        "connection_revision": _positive_int(descriptor.get("connection_revision")),
        "storage_identity_id": str(descriptor.get("storage_identity_id") or "").strip(),
        "identity_root": _normalize_identity_root(descriptor.get("identity_root")),
    }
    mismatched = [
        key
        for key, expected in required.items()
        if descriptor_checks.get(key) != expected
    ]
    if mismatched:
        raise TransferError(
            ErrorCode.INVALID_PLAN,
            f"{role} 路径与冻结 binding 不一致：{','.join(mismatched)}",
        )
    frozen = getattr(resolver, "resolve_frozen_remote", None)
    if not callable(frozen):
        raise TransferError(
            ErrorCode.OPENLIST_UNAVAILABLE,
            "locator resolver does not support frozen OpenList revisions",
        )
    try:
        value = frozen(
            namespace_id=required["namespace_id"],
            mount_id=required["mount_id"],
            mount_revision=required["mount_revision"],
            connection_id=required["connection_id"],
            connection_revision=required["connection_revision"],
            storage_identity_id=required["storage_identity_id"],
            identity_root=required["identity_root"],
            path=str(descriptor.get("path") or ""),
            role=role,
        )
    except TransferError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransferError(
            ErrorCode.OPENLIST_UNAVAILABLE,
            f"{role} OpenList 冻结路径解析失败：{exc}",
        ) from exc
    if not str(value or "").strip():
        raise TransferError(ErrorCode.OPENLIST_UNAVAILABLE, f"{role} OpenList remote 为空")
    return str(value)


def _split_resolved_remote(resolver: Any, value: Any) -> tuple[str, str]:
    splitter = getattr(resolver, "split_remote_path", None)
    try:
        if callable(splitter):
            remote, path = splitter(value)
        else:
            text = str(value or "").strip().replace("\\", "/")
            if ":" not in text.split("/", 1)[0]:
                raise ValueError("resolved remote path is missing a remote name")
            remote, path = text.split(":", 1)
            path = "/" + path.strip("/")
    except Exception as exc:  # noqa: BLE001
        raise TransferError(
            ErrorCode.OPENLIST_UNAVAILABLE,
            f"受控 remote 路径格式无效：{exc}",
        ) from exc
    managed_remote = str(getattr(resolver, "remote_name", "") or "").strip()
    if managed_remote and str(remote).strip() != managed_remote:
        raise TransferError(
            ErrorCode.OPENLIST_UNAVAILABLE,
            "resolver 返回了非托管 rclone remote",
        )
    return str(remote).strip(), str(path).replace("\\", "/")


def _resolve_local_plan_root(
    resolver: Any,
    descriptor: dict[str, Any],
    root: str,
    *,
    relay: bool,
) -> str:
    namespace = str(descriptor.get("namespace_id") or "").strip()
    value = str(root or "").strip().replace("\\", "/")
    raw_path = str(descriptor.get("path") or "").replace("\\", "/")
    if not value.startswith("/") or any(
        part in {".", ".."} for part in raw_path.split("/") if part
    ):
        raise TransferError(ErrorCode.INVALID_PLAN, "本地任务路径必须是无越级的绝对路径")
    if relay:
        if namespace != "local:relay":
            raise TransferError(ErrorCode.INVALID_PLAN, "relay 本地路径 namespace 必须是 local:relay")
        relay_resolver = getattr(resolver, "relay", None)
        if not callable(relay_resolver):
            raise TransferError(ErrorCode.INVALID_PLAN, "locator resolver 无法校验 relay 路径")
        try:
            resolved = relay_resolver(namespace_id=namespace, path=value)
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.INVALID_PLAN, f"relay 路径解析失败：{exc}") from exc
        normalized = str(resolved or "").strip().replace("\\", "/").rstrip("/")
        if not normalized.startswith("/"):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay 路径必须是绝对路径")
        return normalized
    local = getattr(resolver, "local", None)
    if not callable(local):
        raise TransferError(ErrorCode.INVALID_PLAN, "locator resolver 无法解析本地目标")
    try:
        resolved = local(namespace_id=namespace, path=value)
    except Exception as exc:  # noqa: BLE001
        raise TransferError(ErrorCode.INVALID_PLAN, f"本地目标解析失败：{exc}") from exc
    normalized = str(resolved or "").strip().replace("\\", "/").rstrip("/")
    if not normalized.startswith("/"):
        raise TransferError(ErrorCode.INVALID_PLAN, "本地目标必须是绝对路径")
    return normalized


class RcloneLocatorResolver:
    """把计划的 namespace/mount/path 解析为 rclone ``remote:path`` 或受控本地路径。"""

    def remote(self, *, namespace_id: str = "", mount_id: str = "", path: str = "") -> str:
        raise NotImplementedError

    def local(self, *, namespace_id: str = "", path: str = "") -> str:
        raise NotImplementedError

    def relay(self, *, namespace_id: str = "", path: str = "") -> str:
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
        # The executor owns a task-scoped RcloneOperationAdapter.  Binding only
        # the outer executor leaves the adapter's plan unset and makes the
        # first start_copy call fail with INVALID_PLAN.
        bind = getattr(self.rclone, "bind", None)
        if callable(bind):
            bind(plan)
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
            receipt = self.rclone.start_copy(
                self.plan.source_landing or {},
                self.plan.destination_staging or {},
                self.plan.idempotency_key,
                copy_run=copy_run,
            )
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"rclone 提交失败：{exc}", retryable=True) from exc
        return receipt

    def build_copy_run(self) -> dict[str, Any]:
        return build_managed_copy_run(self.plan, self.locator_resolver)

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            status = self.rclone.poll(receipt)
        except Exception as exc:  # noqa: BLE001
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, f"rclone 状态查询失败：{exc}", retryable=True) from exc
        return status

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
            result = cancel_op(receipt)
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
