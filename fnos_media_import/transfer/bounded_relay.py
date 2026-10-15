"""One upload plus bounded, same-job read-ahead; no extra import jobs.

The coordinator owns upload order. A short-lived producer only submits and
observes one download at a time while the provider's synchronous upload runs.
Every file keeps its frozen operation key and the shared ledger is updated
under short cross-process locks, never under a network-duration lock.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from contextlib import contextmanager
from typing import Any

from . import CancelResult, ErrorCode, OperationReceipt, OperationStatus, TransferError, normalize_receipt, normalize_verify_result
from .sequential_relay import SequentialRelay


class BoundedRelay(SequentialRelay):
    POLL_SECONDS = 0.5

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            return self._poll(receipt)
        except BlockingIOError:
            state = self.store.load()
            if state and state.get("version") == 2:
                self._buffer(state, reason="等待缓存空间")
            return OperationStatus("running", "等待缓存释放或磁盘空间，不新增下载")
        except TransferError as exc:
            return OperationStatus("unknown" if exc.code in {ErrorCode.RESULT_UNKNOWN, ErrorCode.INVALID_PLAN, ErrorCode.PATH_CONFLICT} else "failed",
                exc.message, error_code=exc.code)
        except OSError as exc:
            return OperationStatus("failed", f"中继缓存或回执操作失败，保留文件：{exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
        except (ValueError, KeyError, TypeError) as exc:
            return OperationStatus("unknown", f"中继流水线回执无效，停止搬运：{exc}", error_code=ErrorCode.RESULT_UNKNOWN)

    def _cancel_requested(self) -> bool:
        check = getattr(self.executor, "cancel_requested", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:
            return True  # Do not start further side effects from unknown job state.

    def _emit(self, topic: str, data: dict[str, Any]) -> None:
        emit = getattr(self.executor, "_emit_progress", None)
        if callable(emit):
            emit(topic + ": " + json.dumps({"job_id": self.plan.job_id, **data}, ensure_ascii=False))

    def _buffer(self, state: dict[str, Any], *, reason: str = "") -> None:
        index = self.store.next_index(state)
        entries = state["files"][index + 1:]
        depth = int(state.get("pipeline", {}).get("prefetch_files", 0))
        ready = sum(item["state"] == "downloaded" for item in entries)
        reserved = sum(bool(item.get("download_reserved") or item.get("download_submitting")) and item["state"] == "pending" for item in entries)
        self._emit("relay-buffer", {
            "enabled": True, "depth": depth, "ready": ready, "reserved": reserved,
            "waiting": bool(reason or state.get("prefetch_paused") or (depth and ready + reserved >= depth)),
            "reason": reason or str(state.get("prefetch_paused") or ""),
        })

    def submit(self) -> OperationReceipt:
        state = self.store.load()
        if state and state.get("version") == 2:
            index = self.store.next_index(state)
            if index < len(state["files"]):
                entry = state["files"][index]
                if (entry.get("download_error") or {}).get("status") == "failed":
                    # Only the outer bounded retry calls submit again. Never
                    # loop on failed/unknown download receipts in the producer.
                    self.store.update_entry(index, download_receipt=None, download_error=None, download_submitting=False)
                    if state.get("prefetch_paused") != "cancelled":
                        self.store.resume_prefetch()
        return super().submit()

    def _receipt(self, index: int, value: Any) -> OperationReceipt:
        if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
            raise TransferError(ErrorCode.RESULT_UNKNOWN, "预下载回执不完整，保留缓存")
        receipt = OperationReceipt(**value)
        if not receipt.operation_id or receipt.metadata.get("operation_key") != self._download_key(index):
            raise TransferError(ErrorCode.RESULT_UNKNOWN, "预下载回执与当前任务文件不一致")
        return receipt

    def _download_one(self, index: int, stopped: threading.Event | None = None) -> OperationStatus:
        state = self.store.load()
        entry = state["files"][index]
        if entry["state"] in {"downloaded", "verified", "cleaned"}:
            return OperationStatus("completed")
        error = entry.get("download_error")
        if error:
            return OperationStatus(**error)
        value = entry.get("download_receipt")
        if value:
            nested = self._receipt(index, value)
        else:
            if self._cancel_requested() or state.get("prefetch_paused") or (stopped is not None and stopped.is_set()):
                return OperationStatus("running", "预下载已暂停，未提交新的下载")
            newly_prepared = False
            def preparing(latest: dict[str, Any]) -> None:
                nonlocal newly_prepared
                if latest.get("prefetch_paused") or self._cancel_requested() or (stopped is not None and stopped.is_set()):
                    raise TransferError(ErrorCode.RESULT_UNKNOWN, "取消期间禁止提交新的预下载")
                newly_prepared = not latest["files"][index].get("download_submitting")
                latest["files"][index]["download_submitting"] = True
            self.store.mutate(preparing)
            self._emit("relay-file", {"channel": "download", "filename": entry["path"], "file_id": self._download_key(index)})
            # Log delivery is I/O too. The upload may fail while it blocks.
            if self._cancel_requested() or self.store.load().get("prefetch_paused") or (stopped is not None and stopped.is_set()):
                if newly_prepared:
                    def not_submitted(latest: dict[str, Any]) -> None:
                        item = latest["files"][index]
                        if item["state"] == "pending" and not item.get("download_receipt"):
                            item["download_submitting"] = False
                    self.store.mutate(not_submitted)
                return OperationStatus("running", "预下载已暂停，未提交新的下载")
            nested = normalize_receipt(self.executor.rclone_adapter.download(**self._args(index)), provider_kind="rclone")
            self._receipt(index, dataclasses.asdict(nested))
            self.store.record_download(index, dataclasses.asdict(nested))
        status = self.executor._check_download(nested)
        result = OperationStatus(status.status, status.message, error_code=status.error_code)
        if result.status in {"failed", "unknown", "cancelled"}:
            self.store.update_entry(index, download_error={"status": result.status, "message": result.message, "error_code": result.error_code})
        return result

    @contextmanager
    def _producer(self, index: int, *, enabled: bool):
        stopped = threading.Event()

        def run() -> None:
            try:
                while not stopped.is_set():
                    state = self.store.load()
                    if self._cancel_requested():
                        self.store.pause_prefetch("cancelled")
                        return
                    if state.get("prefetch_paused"):
                        return
                    depth = int(state["pipeline"]["prefetch_files"])
                    candidates = [i for i in range(index + 1, min(len(state["files"]), index + depth + 1))
                                  if state["files"][i]["state"] == "pending"]
                    if not candidates:
                        self._buffer(state)
                        return
                    status = self._download_one(candidates[0], stopped)
                    self._buffer(self.store.load())
                    if status.status in {"unknown", "failed", "cancelled"}:
                        self.store.pause_prefetch(status.message or "预下载结果待处理")
                        return
                    # Completion can precede the worker's durable file mark;
                    # only that mark releases the producer's single run slot.
                    if stopped.wait(self.POLL_SECONDS):
                        return
            except BlockingIOError:
                self._buffer(self.store.load(), reason="等待缓存空间")
            except Exception as exc:
                self.store.pause_prefetch(f"预下载暂停：{exc}")

        thread = None
        if enabled:
            thread = threading.Thread(target=run, name=f"relay-prefetch-{self.plan.job_id}", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stopped.set()
            if thread is not None:
                # Requests have bounded I/O timeouts. Join before releasing the
                # execution claim so an old producer cannot mutate a new run.
                thread.join()

    def _poll(self, receipt: OperationReceipt) -> OperationStatus:
        state = self.store.load()
        if self._cancel_requested():
            if state and state.get("version") == 2:
                self.store.pause_prefetch("cancelled")
            return OperationStatus("unknown", "任务已请求取消，未提交新的下载", error_code=ErrorCode.RESULT_UNKNOWN)
        if state and state.get("version") == 2:
            state = self.store.apply_environment_budget_increase()
        if state is None:
            legacy = self._legacy_upload_status(receipt)
            if legacy is not None:
                return legacy
            _nested, status = self._download(receipt, 0)
            if status.status != "completed":
                return status
            state = self.store.load()
            if state is None:
                return super()._poll(receipt)  # Existing whole-directory migration guards.
        if state.get("version") == 1:
            state = self.store.enable_pipeline()
        index = self.store.next_index(state)
        if index == len(state["files"]):
            return self._complete(state)
        if self._cancel_requested():
            self.store.pause_prefetch("cancelled")
            return OperationStatus("unknown", "任务已请求取消，保留下载和上传回执", error_code=ErrorCode.RESULT_UNKNOWN)
        entry = state["files"][index]
        if entry["state"] == "pending":
            self._buffer(state)
            status = self._download_one(index)
            if status.status != "completed":
                return status
            state = self.store.load()
            entry = state["files"][index]
            if entry["state"] == "pending":
                return OperationStatus("running", "等待下载完成回执持久化")
        relative, size = entry["path"], int(entry["size"])
        path = self.store.file_path(relative)
        upload_key = self._upload_key(index)
        if entry["state"] == "downloaded":
            if not os.path.isfile(path) or os.path.getsize(path) != size:
                return OperationStatus("unknown", f"缓存缺失或大小变化：{relative}", error_code=ErrorCode.RESULT_UNKNOWN)
            # A replay of an uncertain upload must first reconcile its old
            # native manifest; it must not begin further read-ahead meanwhile.
            can_prefetch = not entry.get("upload_started") and not state.get("prefetch_paused")
            newly_started = False
            def starting_upload(latest: dict[str, Any]) -> None:
                nonlocal newly_started
                if self._cancel_requested() or latest.get("prefetch_paused") == "cancelled":
                    raise TransferError(ErrorCode.RESULT_UNKNOWN, "取消期间禁止启动上传")
                newly_started = not latest["files"][index].get("upload_started")
                latest["files"][index]["upload_started"] = True
            self.store.mutate(starting_upload)
            if self._cancel_requested() or self.store.load().get("prefetch_paused") == "cancelled":
                if newly_started:
                    # This invocation has not crossed the provider boundary.
                    # Do not turn a known-not-submitted cancellation into an
                    # ambiguous upload, nor clear a pre-existing upload fence.
                    self.store.update_entry(index, upload_started=False)
                return OperationStatus("unknown", "取消期间不再启动上传，保留文件", error_code=ErrorCode.RESULT_UNKNOWN)
            identity = {"job_id": self.plan.job_id, "filename": relative, "file_id": upload_key}
            self._emit("relay-file", {**identity, "channel": "upload"})
            self._buffer(state)
            with self._producer(index, enabled=can_prefetch):
                if self._cancel_requested() or self.store.load().get("prefetch_paused") == "cancelled":
                    if newly_started:
                        self.store.update_entry(index, upload_started=False)
                    return OperationStatus("unknown", "取消期间不再启动上传，保留文件", error_code=ErrorCode.RESULT_UNKNOWN)
                per_file = dataclasses.replace(receipt, metadata={})
                outcome = self.executor._upload(per_file, relative_paths=[relative], operation_key=upload_key, progress_identity=identity)
                upload_status = self.executor._upload_status(outcome)
                if upload_status.status != "completed":
                    self.store.pause_prefetch(upload_status.message or "上传结果待处理")
                    return upload_status
                verification = normalize_verify_result(self.executor.provider_uploader.verify_target(
                    target_locator=self.executor._destination_target(), expected_file_count=1,
                    expected_total_size=size, idempotency_key=upload_key, local_dir=self.executor._relay_dir(),
                ))
                if verification.status != "ok" or verification.file_count != 1 or verification.total_size != size:
                    self.store.pause_prefetch(verification.message or "远端校验未通过")
                    return OperationStatus("unknown" if verification.status == "unknown" else "failed",
                        f"远端校验未通过，保留本地缓存：{relative}；{verification.message}",
                        error_code=verification.code or ErrorCode.VERIFY_MISMATCH)
                self.store.update_entry(index, state="verified", upload_key=upload_key,
                    verification=dataclasses.asdict(verification), upload_outcome=dataclasses.asdict(outcome))
        # Re-read after the producer stopped: never overwrite its next-file mark.
        state = self.store.load()
        entry = state["files"][index]
        verified = entry.get("verification") or {}
        if entry["state"] != "verified" or entry.get("upload_key") != upload_key or (
            verified.get("status") != "ok" or verified.get("file_count") != 1 or verified.get("total_size") != size
        ):
            return OperationStatus("unknown", "缺少逐文件验证回执，禁止释放缓存", error_code=ErrorCode.RESULT_UNKNOWN)
        if self._cancel_requested():
            self.store.pause_prefetch("cancelled")
            return OperationStatus("unknown", "取消期间保留已上传文件和缓存", error_code=ErrorCode.RESULT_UNKNOWN)
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) != size:
                return OperationStatus("unknown", "缓存发生变化，拒绝删除", error_code=ErrorCode.PATH_CONFLICT)
            os.unlink(path)
        if os.name == "posix":
            descriptor = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.store.update_entry(index, state="cleaned", download_reserved=False, download_submitting=False)
        state = self.store.load()
        if state.get("prefetch_paused") != "cancelled":
            self.store.resume_prefetch()
        state = self.store.load()
        self._buffer(state)
        self.executor._emit_event("info", f"上传验证完成并释放缓存：{relative}；完成 {index + 1}/{len(state['files'])}")
        if index + 1 == len(state["files"]):
            return self._complete(state)
        return OperationStatus("running", f"已完成 {index + 1}/{len(state['files'])} 个文件，继续原任务")

    def _all_downloads(self, receipt: OperationReceipt) -> list[OperationReceipt]:
        state = self.store.load()
        if not state or state.get("version") != 2:
            nested = self._cancel_receipt(receipt)
            return [nested] if nested is not None else []
        result = []
        for index, entry in enumerate(state["files"]):
            if entry["state"] == "cleaned":
                continue
            if entry.get("download_receipt"):
                result.append(self._receipt(index, entry["download_receipt"]))
            elif entry.get("download_submitting") or entry.get("download_reserved"):
                nested = self.executor.rclone_adapter.find_download(**self._args(index))
                if nested is None:
                    raise TransferError(ErrorCode.RESULT_UNKNOWN, "预下载已提交但回执尚不可确认")
                result.append(nested)
        return result

    def cancel(self, receipt: OperationReceipt) -> CancelResult:
        try:
            state = self.store.load()
            if state and state.get("version") == 2:
                self.store.pause_prefetch("cancelled")
            pending = False
            for nested in self._all_downloads(receipt):
                status = self.executor._check_download(nested)
                if status.status not in {"completed", "failed", "cancelled"}:
                    cancelled = self.executor.rclone_adapter.cancel(nested)
                    pending = pending or cancelled.status != "cancelled"
            upload = self._cancel_upload_status(receipt)
            return CancelResult("cancel_pending" if pending or upload.status == "unknown" else "cancelled",
                upload.message, operation_id=receipt.operation_id)
        except Exception as exc:
            return CancelResult("cancel_pending", f"预下载取消状态待核对：{exc}", operation_id=receipt.operation_id)

    def poll_for_cancel(self, receipt: OperationReceipt) -> OperationStatus:
        try:
            state = self.store.load()
            if state and state.get("version") == 2:
                self.store.pause_prefetch("cancelled")
            for nested in self._all_downloads(receipt):
                status = self.executor._check_download(nested)
                if status.status not in {"completed", "failed", "cancelled"}:
                    return OperationStatus(status.status, status.message, error_code=status.error_code)
            return self._cancel_upload_status(receipt)
        except Exception as exc:
            return OperationStatus("unknown", f"预下载取消结果未知：{exc}", error_code=ErrorCode.RESULT_UNKNOWN)
