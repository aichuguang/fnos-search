"""One-file relay coordination; the outer task receipt never changes identity.

The rclone worker freezes the full source list, but each run materializes only
one entry. Native upload receipts survive media eviction. An entry cannot
advance until remote verification and its durable checkpoint precede unlink.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from . import (
    CancelResult, ErrorCode, OperationReceipt, OperationStatus, TransferError,
    normalize_receipt, normalize_verify_result,
)
from .relay_file_state import RelayFileState


class SequentialRelay:
    def __init__(self, executor: Any) -> None:
        self.executor = executor
        self.plan = executor.plan
        self.store = RelayFileState(
            executor._relay_dir(), job_id=self.plan.job_id, plan_key=self.plan.idempotency_key,
        )

    def _download_key(self, index: int) -> str:
        base = self.plan.idempotency_key
        return f"relay-dl:{base}" if index == 0 else f"relay-file:{index}:{base}"

    def _upload_key(self, index: int) -> str:
        return f"relay-file-up:{index}:{self.plan.idempotency_key}"

    def _upload_manifest(self, operation_key: str) -> tuple[bool, dict[str, Any]]:
        uploader = self.executor.provider_uploader
        path_for = getattr(uploader, "_operation_manifest_path", None)
        read = getattr(uploader, "_read_operation_manifest", None)
        if not callable(path_for) or not callable(read):
            return False, {}
        path = path_for(self.executor._relay_dir(), operation_key)
        if not os.path.lexists(path):
            return False, {}
        if os.path.islink(path) or not os.path.isfile(path):
            return True, {}
        return True, read(path)

    @staticmethod
    def _legacy_upload_started(receipt: OperationReceipt) -> bool:
        metadata = receipt.metadata or {}
        return bool(
            metadata.get("step") == "upload" or metadata.get("upload_submission_fence")
            or metadata.get("upload_outcome") or metadata.get("upload_idempotency_key")
        )

    @staticmethod
    def _completed_upload_manifest(manifest: dict[str, Any], key: str) -> bool:
        records = manifest.get("files")
        return bool(
            manifest.get("idempotency_key") == key
            and manifest.get("status") == "completed"
            and isinstance(records, dict) and records
            and all(
                isinstance(record, dict) and record.get("success") is True
                and isinstance(record.get("size"), int) and not isinstance(record["size"], bool)
                and record["size"] >= 0
                for record in records.values()
            )
        )

    def _legacy_upload_status(self, receipt: OperationReceipt) -> OperationStatus | None:
        """Never migrate an existing native upload into fresh per-file keys."""
        key = f"relay-up:{self.plan.idempotency_key}"
        exists, manifest = self._upload_manifest(key)
        if not exists and not self._legacy_upload_started(receipt):
            return None
        if not self._completed_upload_manifest(manifest, key):
            return OperationStatus("unknown", "旧中继原生上传结果未确认，保留原幂等回执，禁止重新上传", error_code=ErrorCode.RESULT_UNKNOWN)
        verified = normalize_verify_result(self.executor.verify())
        records = manifest["files"]
        expected_size = sum(record["size"] for record in records.values())
        if verified.status != "ok" or verified.file_count != len(records) or verified.total_size != expected_size:
            return OperationStatus(
                "unknown", f"旧中继上传完成回执尚未通过远端复核，禁止重复上传：{verified.message}",
                error_code=verified.code or ErrorCode.RESULT_UNKNOWN,
            )
        return OperationStatus(
            "completed", "旧中继原生上传已按原幂等回执核验完成，无需重新上传",
            expected_file_count=verified.file_count, completed_file_count=verified.file_count,
            total_size=verified.total_size,
        )

    def submit(self) -> OperationReceipt:
        # This is the task's stable application fence, not a particular file's
        # rclone run. The first poll submits/reconciles the first file run.
        return OperationReceipt(
            operation_id=f"relay:{self.plan.job_id}:{self.plan.idempotency_key}",
            status="running", provider_kind="rclone", message="逐文件中继已就绪",
            metadata={"operation_key": self._download_key(0), "step": "download", "relay_sequence_version": 1},
        )

    def _args(self, index: int) -> dict[str, Any]:
        return {
            "source": self.plan.source_landing, "local_dir": self.executor._relay_dir(),
            "idempotency_key": self._download_key(index),
        }

    def _nested(self, receipt: OperationReceipt, index: int) -> OperationReceipt | None:
        metadata = receipt.metadata
        value = metadata.get("relay_file_receipt")
        if isinstance(value, dict) and metadata.get("relay_file_index") == index:
            if not isinstance(value.get("metadata"), dict):
                raise TransferError(ErrorCode.RESULT_UNKNOWN, "逐文件下载回执缺少原始元数据")
            # This is our own dataclass snapshot, not an external Provider
            # payload (the generic normalizer wraps the latter as metadata).
            nested = OperationReceipt(**value)
            if nested.metadata.get("operation_key") != self._download_key(index):
                raise TransferError(ErrorCode.RESULT_UNKNOWN, "逐文件下载回执与冻结操作不一致")
            return nested
        # A pre-upgrade receipt can identify the original first download.
        if index == 0 and metadata.get("run_id") and not metadata.get("relay_sequence_version"):
            return dataclasses.replace(receipt, metadata=dict(metadata))
        return None

    def _align_receipt_index(self, receipt: OperationReceipt, index: int) -> None:
        """Make the outer receipt describe the file the ledger is actually on.

        A worker can finish downloading the next file and then lose its reply;
        the app may still hold ``relay_file_index=0``.  Reusing that stale
        nested receipt would wait for/retry the wrong file forever.  The
        operation key remains the submission fence (``relay-dl:...``); only
        the per-file locator and its nested download receipt are replaced.
        """
        metadata = receipt.metadata
        if not isinstance(metadata, dict) or metadata.get("relay_file_index") == index:
            return
        metadata["relay_file_index"] = index
        metadata.pop("relay_file_receipt", None)

    def _download(self, receipt: OperationReceipt, index: int) -> tuple[OperationReceipt, OperationStatus]:
        nested = self._nested(receipt, index)
        if nested is None:
            nested = self.executor.rclone_adapter.download(**self._args(index))
            nested = normalize_receipt(nested, provider_kind="rclone")
            receipt.metadata.update(
                relay_file_index=index, relay_file_receipt=dataclasses.asdict(nested), relay_sequence_version=1,
            )
        observed = self.executor._check_download(nested)
        return nested, OperationStatus(observed.status, observed.message, error_code=observed.error_code)

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            return self._poll(receipt)
        except TransferError as exc:
            return OperationStatus(
                "unknown" if exc.code in {ErrorCode.RESULT_UNKNOWN, ErrorCode.INVALID_PLAN, ErrorCode.PATH_CONFLICT} else "failed",
                exc.message, error_code=exc.code,
            )
        except OSError as exc:
            return OperationStatus("failed", f"逐文件中继缓存或回执操作失败，保留文件：{exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
        except (ValueError, KeyError, TypeError) as exc:
            return OperationStatus("unknown", f"逐文件中继回执无效，停止搬运：{exc}", error_code=ErrorCode.RESULT_UNKNOWN)

    def _poll(self, receipt: OperationReceipt) -> OperationStatus:
        state = self.store.load()
        if state is None:
            legacy = self._legacy_upload_status(receipt)
            if legacy is not None:
                return legacy
        index = self.store.next_index(state) if state else 0
        if state:
            self._align_receipt_index(receipt, index)
        if state and index == len(state["files"]):
            return self._complete(state)
        entry = state["files"][index] if state else None
        # ``downloaded`` is already a durable worker receipt.  Re-issuing the
        # download after a restart can race an old run and leave the app
        # waiting on a stale index; go straight to upload instead.
        if entry is None or entry["state"] == "pending":
            nested, status = self._download(receipt, index)
            if status.status != "completed":
                return status
            state = self.store.load()
            if state is None:
                # Historical successful whole-directory runs have no ledger.
                # Re-run the first scoped download to freeze the list; existing
                # complete local files are reused by the new worker.
                if self.executor.rclone_adapter.invalidate(nested, "旧中继运行缺少逐文件清单，重新核验来源和缓存"):
                    receipt.metadata.pop("relay_file_receipt", None)
                    receipt.metadata["relay_sequence_version"] = 1
                    return OperationStatus("running", "旧中继缓存正在迁移到逐文件流程")
                return OperationStatus("unknown", "下载完成但逐文件清单缺失，拒绝上传或清理", error_code=ErrorCode.RESULT_UNKNOWN)
            if index >= len(state["files"]):
                return OperationStatus("unknown", "逐文件清单与下载序号不一致", error_code=ErrorCode.RESULT_UNKNOWN)
            entry = state["files"][index]
            if entry["state"] == "cleaned":
                return OperationStatus("running", "该文件已完成并清理，继续核对剩余清单")
            if entry["state"] == "pending":
                return OperationStatus("unknown", "下载回执未确认当前文件，拒绝上传", error_code=ErrorCode.RESULT_UNKNOWN)

        relative = entry["path"]
        size = int(entry["size"])
        path = self.store.file_path(relative)
        upload_key = self._upload_key(index)
        if entry["state"] == "downloaded":
            if not os.path.isfile(path) or os.path.getsize(path) != size:
                return OperationStatus("unknown", f"待上传缓存缺失或大小变化，保留回执：{relative}", error_code=ErrorCode.RESULT_UNKNOWN)
            # A cancellation after a crash must distinguish a completed
            # download from a possibly submitted native upload.
            entry["upload_started"] = True
            self.store.save(state)
            per_file = dataclasses.replace(receipt, metadata={})
            outcome = self.executor._upload(per_file, relative_paths=[relative], operation_key=upload_key)
            upload_status = self.executor._upload_status(outcome)
            if upload_status.status != "completed":
                return upload_status
            verification = normalize_verify_result(self.executor.provider_uploader.verify_target(
                target_locator=self.executor._destination_target(), expected_file_count=1,
                expected_total_size=size, idempotency_key=upload_key, local_dir=self.executor._relay_dir(),
            ))
            if verification.status != "ok" or verification.file_count != 1 or verification.total_size != size:
                return OperationStatus(
                    "unknown" if verification.status == "unknown" else "failed",
                    f"单文件远端校验未通过，保留本地缓存：{relative}；{verification.message}",
                    error_code=verification.code or ErrorCode.VERIFY_MISMATCH,
                )
            entry.update(
                state="verified", upload_key=upload_key,
                verification=dataclasses.asdict(verification), upload_outcome=dataclasses.asdict(outcome),
            )
            # If persistence fails, no unlink and no next download are allowed.
            self.store.save(state)
            self.executor._emit_event("info", f"远端已验证，逐文件成功回执已持久化：{relative}")

        if entry["state"] != "verified" or entry.get("upload_key") != upload_key:
            return OperationStatus("unknown", "缺少匹配的逐文件验证回执，禁止删除缓存", error_code=ErrorCode.RESULT_UNKNOWN)
        verification = entry.get("verification") or {}
        if verification.get("status") != "ok" or verification.get("file_count") != 1 or verification.get("total_size") != size:
            return OperationStatus("unknown", "逐文件验证回执不完整，禁止删除缓存", error_code=ErrorCode.RESULT_UNKNOWN)
        # A crash after unlink but before the second save is safely replayed:
        # the durable verified checkpoint proves why the local file is absent.
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) != size:
                return OperationStatus("unknown", f"已验证缓存发生变化，拒绝删除：{relative}", error_code=ErrorCode.PATH_CONFLICT)
            os.unlink(path)
        # Retry this even when unlink already happened: the previous attempt
        # may have failed before the directory entry was durably removed.
        if os.name == "posix":
            descriptor = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        entry["state"] = "cleaned"
        self.store.save(state)
        receipt.metadata.pop("relay_file_receipt", None)
        self.executor._emit_event("info", f"本地缓存已释放：{relative}；完成 {index + 1}/{len(state['files'])}")
        if index + 1 == len(state["files"]):
            return self._complete(state)
        return OperationStatus("running", f"已上传、验证并释放 {index + 1}/{len(state['files'])} 个文件，准备下载下一个")

    def _complete(self, state: dict[str, Any]) -> OperationStatus:
        keys = [entry.get("upload_key") for entry in state["files"]]
        if not keys or keys != [self._upload_key(index) for index in range(len(keys))]:
            return OperationStatus("unknown", "逐文件完成回执不完整，拒绝结束任务", error_code=ErrorCode.RESULT_UNKNOWN)
        outcome = self.executor.provider_uploader.merge_upload_manifests(
            local_dir=self.executor._relay_dir(), idempotency_key=f"relay-up:{self.plan.idempotency_key}",
            file_operation_keys=keys,
        )
        return self.executor._upload_status(outcome)

    def _cancel_receipt(self, receipt: OperationReceipt) -> OperationReceipt | None:
        state = self.store.load()
        index = self.store.next_index(state) if state else 0
        if state and index == len(state["files"]):
            return None
        nested = self._nested(receipt, index)
        if nested is not None:
            return nested
        return self.executor.rclone_adapter.find_download(**self._args(index))

    def _cancel_upload_status(self, receipt: OperationReceipt) -> OperationStatus:
        """Preserve all external evidence once native upload may have started."""
        state = self.store.load()
        completed = False
        checks = [(f"relay-up:{self.plan.idempotency_key}", self._legacy_upload_started(receipt))]
        if state:
            for index, entry in enumerate(state["files"]):
                recorded = entry["state"] in {"verified", "cleaned"}
                completed = completed or recorded
                if not recorded:
                    checks.append((self._upload_key(index), bool(entry.get("upload_started"))))
        for key, started in checks:
            exists, manifest = self._upload_manifest(key)
            if not exists and not started:
                continue
            if not self._completed_upload_manifest(manifest, key):
                return OperationStatus("unknown", "原生上传可能已提交但结果未知，保留来源、缓存及目标等待复核", error_code=ErrorCode.RESULT_UNKNOWN)
            completed = True
        if completed:
            # The orchestrator/dispatcher use completed cancellation to skip
            # destructive cleanup of already uploaded or verified artifacts.
            return OperationStatus("completed", "外部操作已完成，停止后续中继，保留来源、缓存及目标文件")
        return OperationStatus("cancelled", "逐文件下载已停止，尚未启动原生上传")

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        try:
            nested = self._cancel_receipt(receipt)
            if nested is not None:
                observed = self.executor._check_download(nested)
                if observed.status not in {"completed", "failed", "cancelled"}:
                    result = self.executor.rclone_adapter.cancel(nested)
                    if result.status != "cancelled":
                        return result
            upload_status = self._cancel_upload_status(receipt)
            return CancelResult(
                "cancel_pending" if upload_status.status == "unknown" else "cancelled",
                upload_status.message, operation_id=receipt.operation_id,
            )
        except Exception as exc:  # noqa: BLE001 - uncertainty is not confirmed cancellation
            return CancelResult("cancel_pending", f"逐文件取消状态待确认：{exc}", operation_id=receipt.operation_id)

    def poll_for_cancel(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            nested = self._cancel_receipt(receipt)
            if nested is not None:
                status = self.executor._check_download(nested)
                if status.status not in {"completed", "failed", "cancelled"}:
                    return OperationStatus(status.status, status.message, error_code=status.error_code)
            return self._cancel_upload_status(receipt)
        except Exception as exc:  # noqa: BLE001
            return OperationStatus("unknown", f"逐文件取消状态未知：{exc}", error_code=ErrorCode.RESULT_UNKNOWN)
