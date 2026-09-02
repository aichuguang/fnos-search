"""Task-scoped rclone adapter used by the V1 transfer executors.

The old rclone service exposes a process-wide ``status()``/``cancel_job()``
pair.  Those methods are deliberately *not* used here: a transfer receipt is
the authority for polling and cancellation.  A service implementation used by
V1 must return a durable run id from submission and expose one of the scoped
poll/cancel methods documented below.  If it cannot do that, this adapter
fails closed instead of accidentally observing or stopping another job.
"""

from __future__ import annotations

import os
from typing import Any

from . import (
    CancelResult,
    ErrorCode,
    OperationReceipt,
    OperationStatus,
    TransferError,
    normalize_operation_status,
    normalize_receipt,
)


class RcloneOperationAdapter:
    """Single rclone data-plane adapter for remote and local copies.

    Required service contract:

    * ``start_copy(source, destination, operation_key, copy_run=...)``;
    * ``find_run_by_operation_key(operation_key)``;
    * ``poll_run(run_id, operation_key=..., receipt=...)``;
    * ``cancel_run(run_id, operation_key=..., receipt=...)``.

    A process-wide ``status()`` or ``cancel_job()`` is never a valid V1
    fallback.
    """

    def __init__(self, service: Any, locator_resolver: Any) -> None:
        self.service = service
        self.locator_resolver = locator_resolver
        self.plan: Any = None

    def bind(self, plan: Any) -> "RcloneOperationAdapter":
        self.plan = plan
        return self

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def start_copy(
        self,
        source: dict[str, str],
        destination: dict[str, str],
        operation_key: str,
        *,
        copy_run: dict[str, Any],
    ) -> OperationReceipt:
        self._require_plan()
        key = self._require_operation_key(operation_key)
        run = dict(copy_run or {})
        run.setdefault("operation_key", key)
        run.setdefault("idempotency_key", key)
        run.setdefault("job_id", int(self.plan.job_id))

        # A retry may reach the adapter after the worker lost its in-memory
        # receipt.  Let a durable service reconcile by operation key when it
        # supports that explicitly.  Never infer a run from global status.
        existing = self._find_existing_run(key)
        if existing is not None:
            return self._receipt_from_run(
                existing,
                operation_key=key,
                source=source,
                destination=destination,
                copy_run=run,
            )

        try:
            result = self.service.start_copy(
                source,
                destination,
                key,
                copy_run=run,
            )
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"rclone 提交失败：{exc}",
                retryable=True,
            ) from exc

        receipt = self._coerce_submission_receipt(
            result,
            operation_key=key,
            source=source,
            destination=destination,
            copy_run=run,
        )
        return receipt

    def download(self, *, source: dict[str, str], local_dir: str, idempotency_key: str) -> OperationReceipt:
        """Submit a source-landing -> task-local relay copy.

        ``local_dir`` is intentionally explicit and must be a job-scoped
        absolute path.  The adapter never substitutes ``/data/relay`` (or a
        provider root) when it is absent.
        """

        self._require_plan()
        key = self._require_operation_key(idempotency_key)
        local_path = self._validate_relay_path(local_dir)
        if self.plan.local_temp is not None:
            frozen_path = str(self.plan.local_temp.get("path") or "")
            if self._norm_path(frozen_path) != self._norm_path(local_path):
                raise TransferError(
                    ErrorCode.INVALID_PLAN,
                    "relay 本地路径与冻结计划不一致",
                )
        from .rclone_executor import build_managed_copy_run

        copy_run = build_managed_copy_run(self.plan, self.locator_resolver)
        expected_local_dir = self._norm_path(
            f"{str(copy_run.get('local_target') or '').rstrip('/')}/"
            f"{str(copy_run.get('job_dir_name') or '').strip('/')}"
        )
        if not copy_run.get("relay_mode") or expected_local_dir != local_path:
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "relay 复制描述符与冻结 local_temp 路径不一致",
            )
        return self.start_copy(
            source,
            dict(self.plan.local_temp or {}),
            key,
            copy_run=copy_run,
        )

    # ------------------------------------------------------------------
    # Receipt-scoped status/cancel
    # ------------------------------------------------------------------
    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        self._require_receipt(receipt)
        metadata = receipt.metadata or {}
        run_id = self._run_id(metadata.get("run_id"))
        if not run_id:
            return OperationStatus("unknown", "rclone 回执缺少持久 run_id", error_code=ErrorCode.RESULT_UNKNOWN)
        operation_key = str(metadata.get("operation_key") or "")
        try:
            value = self._poll_scoped(run_id, receipt, operation_key)
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"rclone 限定状态查询失败：{exc}",
                retryable=True,
            ) from exc
        if value is None:
            return OperationStatus(
                "unknown",
                "rclone 服务无法查询回执限定的 run",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        status = normalize_operation_status(value)
        # A scoped service may return a run record with an id.  Reject a
        # mismatched record rather than exposing another task's status.
        if isinstance(value, dict):
            returned = self._run_id(value.get("run_id") or value.get("id"))
            if returned and str(returned) != str(run_id):
                return OperationStatus("unknown", "rclone 返回的 run_id 与回执不一致", error_code=ErrorCode.RESULT_UNKNOWN)
        return status

    def check(self, receipt: OperationReceipt) -> dict[str, Any]:
        status = self.poll(receipt)
        return {
            "status": status.status,
            "message": status.message,
            "error_code": status.error_code,
            "metadata": dict(status.metadata or {}),
        }

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        if self.plan is None:
            return CancelResult("not_cancellable", "rclone 适配器未绑定计划", operation_id=getattr(receipt, "operation_id", ""))
        if not isinstance(receipt, OperationReceipt):
            return CancelResult("not_cancellable", "rclone 回执无效", operation_id="")
        run_id = self._run_id((receipt.metadata or {}).get("run_id"))
        if not run_id:
            return CancelResult("not_cancellable", "rclone 回执缺少持久 run_id", operation_id=receipt.operation_id)
        operation_key = str((receipt.metadata or {}).get("operation_key") or "")
        try:
            result = self._cancel_scoped(run_id, receipt, operation_key)
        except Exception as exc:  # noqa: BLE001
            return CancelResult("cancel_pending", f"rclone 取消查询失败：{exc}", operation_id=receipt.operation_id)
        if result is None:
            return CancelResult(
                "not_cancellable",
                "rclone 服务未提供回执限定的取消方法",
                operation_id=receipt.operation_id,
            )
        if isinstance(result, CancelResult):
            return result
        if isinstance(result, dict):
            if result.get("success") is False:
                return CancelResult("not_cancellable", str(result.get("message") or "rclone 拒绝取消"), operation_id=receipt.operation_id)
            status = str(result.get("status") or "cancel_pending").strip().lower()
            if status in {"cancelled", "canceled", "stopped", "done"}:
                status = "cancelled"
            elif status not in {"cancel_pending", "resume_ack", "not_cancellable", "already_final"}:
                status = "cancel_pending"
            return CancelResult(status, str(result.get("message") or "已请求 rclone 取消"), operation_id=receipt.operation_id)
        if result is True:
            return CancelResult("cancel_pending", "已请求 rclone 取消", operation_id=receipt.operation_id)
        return CancelResult("cancel_pending", str(result or "已请求 rclone 取消"), operation_id=receipt.operation_id)

    # ------------------------------------------------------------------
    # Internal contract helpers
    # ------------------------------------------------------------------
    def _require_plan(self) -> None:
        if self.plan is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "rclone 适配器未绑定计划")

    @staticmethod
    def _require_operation_key(value: Any) -> str:
        key = str(value or "").strip()
        if not key:
            raise TransferError(ErrorCode.INVALID_PLAN, "rclone 操作需要非空幂等键")
        return key

    @staticmethod
    def _run_id(value: Any) -> int | str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return int(text) if text.isdigit() else text

    @staticmethod
    def _norm_path(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        while "//" in text:
            text = text.replace("//", "/")
        return text.rstrip("/") or "/"

    def _validate_relay_path(self, value: Any) -> str:
        text = self._norm_path(value)
        if not text.startswith("/"):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay 本地路径必须是绝对路径")
        parts = [part for part in text.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay 本地路径包含路径穿越段")
        expected = f"job-{int(self.plan.job_id)}"
        if not parts or parts[-1] != expected:
            raise TransferError(ErrorCode.INVALID_PLAN, f"relay 本地路径必须以 {expected} 结尾")
        frozen = self.plan.local_temp
        if not isinstance(frozen, dict):
            raise TransferError(ErrorCode.INVALID_PLAN, "relay plan 缺少 frozen local_temp")
        frozen_kind = str(frozen.get("kind") or "").strip().lower()
        frozen_namespace = str(frozen.get("namespace_id") or "").strip().lower()
        if frozen_kind != "relay" or frozen_namespace != "local:relay":
            raise TransferError(
                ErrorCode.INVALID_PLAN,
                "relay local_temp 必须使用 kind=relay 且命名空间为 local:relay",
            )
        return text

    def _find_existing_run(self, operation_key: str) -> dict[str, Any] | OperationReceipt | None:
        try:
            value = self.service.find_run_by_operation_key(operation_key)
        except Exception as exc:  # noqa: BLE001
            raise TransferError(
                ErrorCode.NETWORK_TRANSIENT,
                f"rclone 对账查询失败：{exc}",
                retryable=True,
            ) from exc
        if isinstance(value, dict):
            run_job = value.get("job_id") or value.get("jobId")
            if run_job not in (None, "") and int(run_job) != int(self.plan.job_id):
                raise TransferError(
                    ErrorCode.RESULT_UNKNOWN,
                    "rclone 操作查询返回了其他任务",
                )
        return value or None

    def _coerce_submission_receipt(
        self,
        value: Any,
        *,
        operation_key: str,
        source: dict[str, str],
        destination: dict[str, str],
        copy_run: dict[str, Any],
    ) -> OperationReceipt:
        if isinstance(value, OperationReceipt):
            receipt = value
        elif isinstance(value, dict) and value.get("success") is False:
            raise TransferError(ErrorCode.NETWORK_TRANSIENT, str(value.get("message") or "rclone 提交失败"), retryable=True)
        else:
            receipt = normalize_receipt(value, provider_kind="rclone")
        metadata = dict(receipt.metadata or {})
        returned_key = str(metadata.get("operation_key") or metadata.get("idempotency_key") or "").strip()
        if returned_key and returned_key != operation_key:
            raise TransferError(
                ErrorCode.RESULT_UNKNOWN,
                "rclone 返回的 operation_key 与当前任务不一致",
            )
        run_id = self._run_id(metadata.get("run_id") or (value.get("run_id") if isinstance(value, dict) else None))
        # ``current_run_id`` is a process-global status field and is not a
        # valid submission receipt.  Requiring run_id prevents cross-job mixups.
        if not run_id:
            raise TransferError(
                ErrorCode.RESULT_UNKNOWN,
                "rclone 提交未返回持久的任务级 run_id",
            )
        metadata.update(
            {
                "run_id": str(run_id),
                "operation_key": operation_key,
                "job_id": int(self.plan.job_id),
                "source": dict(source or {}),
                "destination": dict(destination or {}),
                "copy_run": dict(copy_run or {}),
            }
        )
        operation_id = str(receipt.operation_id or f"rclone:{self.plan.job_id}:{run_id}")
        return OperationReceipt(
            operation_id=operation_id,
            status=receipt.status if receipt.status in {"submitted", "running", "completed", "failed", "unknown", "cancelled"} else "running",
            provider_kind="rclone",
            message=receipt.message,
            external_ref=receipt.external_ref or str(run_id),
            error_code=receipt.error_code,
            metadata=metadata,
        )

    def _receipt_from_run(
        self,
        value: dict[str, Any] | OperationReceipt,
        *,
        operation_key: str,
        source: dict[str, str],
        destination: dict[str, str],
        copy_run: dict[str, Any],
    ) -> OperationReceipt:
        if isinstance(value, OperationReceipt):
            return self._coerce_submission_receipt(value, operation_key=operation_key, source=source, destination=destination, copy_run=copy_run)
        return self._coerce_submission_receipt(value, operation_key=operation_key, source=source, destination=destination, copy_run=copy_run)

    def _require_receipt(self, receipt: OperationReceipt) -> None:
        if not isinstance(receipt, OperationReceipt):
            raise TransferError(ErrorCode.RESULT_UNKNOWN, "rclone 操作回执无效")
        if not str(receipt.operation_id or "").strip():
            raise TransferError(ErrorCode.RESULT_UNKNOWN, "rclone 回执缺少 operation_id")

    def _poll_scoped(self, run_id: int | str, receipt: OperationReceipt, operation_key: str) -> Any:
        return self.service.poll_run(
            run_id,
            operation_key=operation_key,
            receipt=receipt,
        )

    def _cancel_scoped(self, run_id: int | str, receipt: OperationReceipt, operation_key: str) -> Any:
        return self.service.cancel_run(
            run_id,
            operation_key=operation_key,
            receipt=receipt,
        )
