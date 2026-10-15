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

import dataclasses
import json
import math
import re
import threading
import time
from collections import deque
from typing import Any, Callable

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

# 移动云 CmccUploader 的 _ProgressMeter 输出行（英文、格式固定）：
# "{label}: {done} / {total}, {percent}%, {speed}/s, ETA {eta}"，
# label 形如 "CMCC API PUT progress: 文件名" / "CMCC API local hash progress: 文件名"。
_CMCC_PUT_PROGRESS_LINE = re.compile(
    r"^CMCC API PUT progress: (?P<name>.*): (?P<done>.+) / (?P<total>[^,]+), "
    r"(?P<percent>\d+)%, (?P<speed>.+)/s, ETA (?P<eta>.+)$"
)
_CMCC_HASH_PROGRESS_LINE = re.compile(
    r"^CMCC API local hash progress: (?P<name>.*): (?P<done>.+) / (?P<total>[^,]+), "
    r"(?P<percent>\d+)%, .+/s, ETA .+$"
)

# Legacy callers share a plain set[str], so the short check/add/migration
# critical sections need a common lock even across separate executor instances.
_AUTH_HALT_LOCK = threading.Lock()
_AUTH_HALT_PREFIX = "relay-auth-v1:"


class _UploadProgressStream:
    """Serialize best-effort log delivery without blocking Provider upload calls."""

    def __init__(self, sink: Callable[[str], None] | None, heartbeat_seconds: float) -> None:
        self.sink = sink
        self.interval = heartbeat_seconds
        self.condition = threading.Condition()
        self.pending: deque[tuple[str, bool]] = deque(maxlen=64)
        self.active = True
        self.closed = False
        self.phase = "upload"
        self.last_value: Any = None
        self.last_change = time.monotonic()
        self.next_notice = self.last_change + self.interval
        self.finished = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, message: str) -> None:
        if self.sink is None:
            return
        self.emit(message, True)
        self.thread = threading.Thread(target=self._run, name="relay-upload-progress", daemon=True)
        try:
            self.thread.start()
        except Exception:  # noqa: BLE001 - unavailable logging must not prevent the actual upload
            self.thread = None
            self.sink = None
            self.pending.clear()
            self.finished.set()

    def emit(self, message: str, important: bool = False) -> None:
        if self.sink is None or not message:
            return
        with self.condition:
            if not self.active or self.closed:
                return
            if self.pending and not important and not self.pending[-1][1]:
                self.pending[-1] = (message, False)
            else:
                self.pending.append((message, important))
            self.condition.notify()

    def observe(self, phase: str, value: Any) -> None:
        with self.condition:
            if not self.active or self.closed:
                return
            current = (phase, value)
            if current != self.last_value:
                self.phase = phase
                self.last_value = current
                self.last_change = time.monotonic()
                self.next_notice = self.last_change + self.interval
                self.condition.notify()

    def stop_heartbeat(self) -> None:
        with self.condition:
            self.active = False
            self.condition.notify()

    def finish(self, message: str = "") -> None:
        with self.condition:
            if self.closed:
                return
            self.active = False
            self.closed = True
            if self.sink is not None and message:
                self.pending.append((message, True))
            self.condition.notify()
        if self.thread is None:
            self.finished.set()

    def _run(self) -> None:
        try:
            while True:
                with self.condition:
                    heartbeat = False
                    if self.pending:
                        message, _important = self.pending.popleft()
                    elif self.closed:
                        return
                    else:
                        now = time.monotonic()
                        if not self.active or now < self.next_notice:
                            self.condition.wait(None if not self.active else self.next_notice - now)
                            continue
                        waited = max(0, int(now - self.last_change))
                        if self.phase == "hash":
                            message = f"本地校验仍在执行：已 {waited} 秒未收到新的校验进度，正在等待本地文件校验"
                        else:
                            message = f"上传请求仍在执行：已 {waited} 秒未收到新的字节进度，等待网盘响应"
                        heartbeat = True
                        self.next_notice = now + self.interval
                if heartbeat and not self.active:
                    continue
                try:
                    self.sink(message)
                except Exception:  # noqa: BLE001 - never alter upload outcome for a failed log bridge
                    pass
        finally:
            self.finished.set()


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
    # Historical receipts may describe only a completed download. Even after
    # restore, the orchestrator must poll the relay before treating it as done.
    completion_requires_poll = True

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
        progress_sink: Callable[[str], None] | None = None,
        progress_interval: float = 5.0,
        progress_heartbeat_seconds: float = 30.0,
        event_sink: Callable[[str, str], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.rclone_adapter = rclone_adapter
        self.provider_uploader = provider_uploader
        self.source_materializer = source_materializer
        self.visibility_poller = visibility_poller
        self.relay_root = self._normalize_root(relay_root)
        self.alert_service = alert_service
        self.halted_account_ids = halted_account_ids if isinstance(halted_account_ids, set) else set()
        self.progress_sink = progress_sink
        self.progress_interval = max(float(progress_interval or 0.0), 0.0)
        heartbeat = float(progress_heartbeat_seconds or 30.0)
        self.progress_heartbeat_seconds = heartbeat if math.isfinite(heartbeat) and heartbeat > 0 else 30.0
        self.event_sink = event_sink
        self.cancel_requested = cancel_requested
        self._sequential = None

    def bind(self, plan: TransferPlan) -> "ProviderRelayExecutor":
        self.plan = plan
        bind = getattr(self.rclone_adapter, "bind", None)
        if callable(bind):
            bind(plan)
        self._sequential = None
        if getattr(self.rclone_adapter, "supports_sequential_relay", False) is True:
            from .sequential_relay import SequentialRelay

            self._sequential = SequentialRelay(self)
            if getattr(self.rclone_adapter, "supports_bounded_prefetch", False) is True:
                from .bounded_relay import BoundedRelay

                self._sequential = BoundedRelay(self)
        self._migrate_legacy_auth_halt()
        return self

    def _auth_halt_key(self) -> str:
        account_id = self.plan.destination_account_id
        identity = account_id or self.plan.destination_id
        if not identity:
            raise TransferError(ErrorCode.INVALID_PLAN, "中继认证状态缺少冻结账号或目标身份")
        return _AUTH_HALT_PREFIX + json.dumps(
            [
                self._provider_kind(),
                "account" if account_id else "destination",
                identity,
                self.plan.destination_account_revision,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def _migrate_legacy_auth_halt(self) -> None:
        # Bare ids do not prove which credentials failed. Never assign one to
        # a real frozen revision: the first bound plan could already contain
        # newly authenticated credentials. Revision-less legacy callers retain
        # their old stop semantics, scoped away from every real revision.
        legacy_id = self.plan.destination_account_id or self.plan.destination_id
        key = self._auth_halt_key()
        with _AUTH_HALT_LOCK:
            if legacy_id in self.halted_account_ids:
                self.halted_account_ids.discard(legacy_id)
                if self.plan.destination_account_revision <= 0:
                    self.halted_account_ids.add(key)

    def _auth_halted(self) -> bool:
        key = self._auth_halt_key()
        with _AUTH_HALT_LOCK:
            return key in self.halted_account_ids

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

    def prepare_submission(self) -> dict[str, Any]:
        """Check the relay download before the dispatcher persists a submit fence."""
        landing = self.plan.source_landing
        if landing is None:
            raise TransferError(ErrorCode.INVALID_PLAN, "Provider relay 需要来源落地区")
        prepare = getattr(self.rclone_adapter, "prepare_download", None)
        if not callable(prepare):
            raise TransferError(ErrorCode.INVALID_PLAN, "rclone 适配器未提供只读搬运前置校验")
        return prepare(
            source=landing,
            local_dir=self._relay_dir(),
            idempotency_key=f"relay-dl:{self.plan.idempotency_key}",
        )

    def submit(self, existing_receipt: OperationReceipt | None = None) -> OperationReceipt:
        if existing_receipt is not None:
            return existing_receipt
        if self._sequential is not None:
            return self._sequential.submit()
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
        if receipt.status == "completed":
            # 下载只是中继链路的第一段。适配器按 operation_key 对账到一个已
            # 完成的历史 run 时会直接给出 completed 回执；若原样上抛，编排器
            # 会把它当成整个传输完成而跳过上传。这里降级为 running 并保留
            # step=download，让 poll 沿正常路径走完“下载确认 -> 上传”。
            receipt = dataclasses.replace(
                receipt,
                status="running",
                message=receipt.message or "中继下载已完成，等待上传",
            )
        return receipt

    def poll(self, receipt: OperationReceipt) -> OperationStatus:
        if not isinstance(receipt, OperationReceipt):
            return OperationStatus("unknown", "relay 回执无效", error_code=ErrorCode.RESULT_UNKNOWN)
        if self._sequential is not None:
            return self._sequential.poll(receipt)
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
            if status.status == "cancelled":
                return OperationStatus("cancelled", status.message or "中继下载已取消")
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

    def poll_for_cancel(self, receipt: OperationReceipt) -> OperationStatus:
        """Observe cancellation without advancing download -> upload."""
        if not isinstance(receipt, OperationReceipt):
            return OperationStatus("unknown", "relay 回执无效", error_code=ErrorCode.RESULT_UNKNOWN)
        if self._sequential is not None:
            return self._sequential.poll_for_cancel(receipt)
        step = str((receipt.metadata or {}).get("step") or "download").strip().lower()
        if step == "download":
            status = self._check_download(receipt)
            if status.status in {"completed", "failed", "cancelled"}:
                return OperationStatus("cancelled", "中继下载已结束，取消任务，不再启动上传")
            return OperationStatus(status.status, status.message, error_code=status.error_code)
        if step == "upload":
            return self._poll_upload(receipt)
        return OperationStatus("unknown", "无法确认中继取消状态", error_code=ErrorCode.RESULT_UNKNOWN)

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
        if text in {"cancelled", "canceled", "stopped"}:
            return "cancelled"
        if text == "unknown":
            return "unknown"
        return "running"

    def _upload(
        self, receipt: OperationReceipt, *, relative_paths: list[str] | None = None,
        operation_key: str | None = None,
        progress_identity: dict[str, Any] | None = None,
    ) -> UploadOutcome:
        local_dir = self._relay_dir()
        target = self._destination_target()
        metadata = receipt.metadata
        operation_key = operation_key or f"relay-up:{self.plan.idempotency_key}"
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
        if self._auth_halted():
            return UploadOutcome(
                "auth_expired",
                f"{self._provider_label()}冻结账号修订已停止上传，请重新登录后重新规划任务",
                error_code=ErrorCode.AUTH_EXPIRED,
            )
        metadata["step"] = "upload"
        metadata.setdefault("upload_idempotency_key", operation_key)
        fence = metadata.setdefault("upload_submission_fence", {})
        if isinstance(fence, dict):
            fence.setdefault("operation_key", operation_key)
            fence.setdefault("state", "prepared")
        self._emit_event("info", "开始上传到目标网盘")
        def emit_upload(message: str) -> None:
            # Capture the file identity now: queued log delivery can outlive
            # this upload, and a parallel download has a different run id.
            if progress_identity is not None:
                message = "relay-progress: " + json.dumps({**progress_identity, "message": message}, ensure_ascii=False)
            self._emit_progress(message)

        progress_stream = _UploadProgressStream(
            emit_upload if self.progress_sink is not None else None,
            self.progress_heartbeat_seconds,
        )
        progress_stream.start(f"开始上传：{self._provider_label()}（准备校验本地文件并提交网盘）")
        try:
            try:
                outcome = self.provider_uploader.upload_local_dir(
                    local_dir=local_dir,
                    target_locator=target,
                    idempotency_key=operation_key,
                    progress=self._make_upload_progress_handler(
                        emit=progress_stream.emit, observe=progress_stream.observe,
                    ),
                    **({"relative_paths": relative_paths} if relative_paths is not None else {}),
                )
            except TransferError as exc:
                outcome = UploadOutcome("failed", exc.message, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                outcome = UploadOutcome(
                    "failed",
                    f"{self._provider_label()}上传异常：{exc}",
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
            finally:
                progress_stream.stop_heartbeat()
            normalized = normalize_upload_outcome(outcome)
            if normalized.status in {"done", "completed"}:
                progress_stream.finish(f"上传完成：{normalized.message or '成功'}")
                self._emit_event("info", "单文件上传完成，等待远端校验" if relative_paths is not None else "上传完成，准备整理")
            elif normalized.status in {"failed", "auth_expired"}:
                progress_stream.finish(f"上传失败：{normalized.message}")
                self._emit_event("error", f"上传失败：{normalized.message}")
                if normalized.error_code == ErrorCode.SOURCE_LANDING_UNAVAILABLE:
                    self._invalidate_download(receipt, normalized.message)
            metadata["upload_outcome"] = _upload_outcome_dict(normalized)
            if isinstance(fence, dict):
                fence["state"] = normalized.status
            return normalized
        finally:
            # Stop heartbeat generation and reject late callbacks. Delivery is
            # serialized and ends after the final message; do not join a thread
            # that may be waiting on the network log bridge.
            progress_stream.finish()

    def _invalidate_download(self, receipt: OperationReceipt, message: str) -> None:
        """中继目录为空时作废对应下载 run，否则重试会一直对账到假成功。"""
        invalidate = getattr(self.rclone_adapter, "invalidate", None)
        if not callable(invalidate):
            return
        try:
            done = invalidate(receipt, f"中继目录为空，下载结果不可信：{message}")
        except Exception:  # noqa: BLE001
            done = False
        if done:
            self._emit_event("warn", "中继目录为空，已作废本次下载记录，重试时将重新下载")

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
        """上传与校验共同的目标定位器。

        启用 Organizer 时中继上传写入冻结的任务级原生暂存目录
        （分类父目录/_入库暂存/分类名/job-N），整理完成后由 Organizer 搬进
        正式分类目录；未启用 Organizer 时直写 destination_final。

        暂存值直接读冻结计划里的原始 paths 字典：``path()`` 会把定位器规范
        化成以 "/" 开头的 POSIX 路径，而天翼云的 folder_path 是
        ``{root_folder_id}::{相对路径}`` 复合定位器，加上前导 "/" 就解析不出
        root_folder_id 了（与 ``native_submit_locator`` 同一约束）。
        """

        staging = self.plan.paths.get("destination_staging")
        if isinstance(staging, dict) and str(staging.get("kind") or "") == "native":
            value = str(staging.get("path") or "").strip()
            if value:
                target = dict(staging)
                target["path"] = value
                return target
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

    def _emit_event(self, level: str, message: str) -> None:
        """任务事件时间线只收里程碑（开始/完成/失败），高频进度走日志流。"""
        if self.event_sink is None:
            return
        try:
            self.event_sink(level, message)
        except Exception:  # noqa: BLE001
            pass

    def _emit_progress(self, message: str) -> None:
        if self.progress_sink is None or not message:
            return
        try:
            self.progress_sink(message)
        except Exception:  # noqa: BLE001
            pass

    def _make_upload_progress_handler(
        self,
        *,
        emit: Callable[[str, bool], None] | None = None,
        observe: Callable[[str, Any], None] | None = None,
    ) -> Callable[[Any], None]:
        """把上传器进度回调归一化成中文事件并节流后送入 progress_sink。

        不同 Provider 的回调形态不同：天翼云给整体百分比浮点，移动云给
        _ProgressMeter 的英文进度行。这里统一翻译成用户可见中文；其余
        诊断性输出（create request、raw response 等）不进事件流。
        """

        state: dict[str, Any] = {"last_emit": None, "phase": "", "name": "", "terminal": False}
        lock = threading.Lock()

        def handler(value: Any) -> None:
            if self.progress_sink is None:
                return
            message = self._format_upload_progress(value)
            if not message:
                return
            phase, name, position, terminal = self._upload_progress_identity(value)
            if observe is not None:
                observe(phase, (name, position))
            now = time.monotonic()
            with lock:
                name = name or (state["name"] if phase == state["phase"] else "")
                transition = phase != state["phase"] or name != state["name"]
                important = transition or (terminal and not state["terminal"])
                if not important and state["last_emit"] is not None and now - state["last_emit"] < self.progress_interval:
                    return
                state.update(last_emit=now, phase=phase, name=name, terminal=terminal)
                if emit is None:
                    self._emit_progress(message)
                else:
                    emit(message, important)

        return handler

    @staticmethod
    def _upload_progress_identity(value: Any) -> tuple[str, str, Any, bool]:
        if isinstance(value, (int, float)):
            percent = max(0.0, min(100.0, float(value)))
            return "upload", "", percent, percent >= 100
        text = str(value or "")
        for pattern, phase in ((_CMCC_HASH_PROGRESS_LINE, "hash"), (_CMCC_PUT_PROGRESS_LINE, "upload")):
            match = pattern.match(text)
            if match is not None:
                return phase, match.group("name"), (match.group("done"), match.group("percent")), int(match.group("percent")) >= 100
        return "upload", "", None, False

    def _format_upload_progress(self, value: Any) -> str:
        if isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                return ""
            percent = max(0.0, min(100.0, float(value)))
            return f"上传中：{percent:.0f}%"
        text = str(value or "")
        put = _CMCC_PUT_PROGRESS_LINE.match(text)
        if put:
            return (
                f"上传中 {put.group('name')}：{put.group('done')} / {put.group('total')}，"
                f"{put.group('percent')}%，速度 {put.group('speed')}/s，"
                f"预计剩余 {put.group('eta')}"
            )
        hashed = _CMCC_HASH_PROGRESS_LINE.match(text)
        if hashed:
            return (
                f"秒传校验 {hashed.group('name')}：{hashed.group('done')} / "
                f"{hashed.group('total')}，{hashed.group('percent')}%"
            )
        return ""

    def _on_auth_expired(self) -> None:
        account_id = self.plan.destination_account_id or self.plan.destination_id
        key = self._auth_halt_key()
        with _AUTH_HALT_LOCK:
            already_halted = key in self.halted_account_ids
            self.halted_account_ids.add(key)
        if self.alert_service is not None and not already_halted:
            try:
                self.alert_service(
                    account_id=account_id,
                    code=ErrorCode.AUTH_EXPIRED,
                    message=(
                        f"{self._provider_label()}认证已过期，已暂停该账号修订 "
                        f"{self.plan.destination_account_revision} 的后续上传任务"
                    ),
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
        if self._sequential is not None:
            return self._sequential.cancel(receipt)
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
