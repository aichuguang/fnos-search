"""V1.0 传输执行器与编排（Transfer Orchestrator + Executors）契约测试。

运行：C:\\WorkSpace\\fnos-search\\.venv\\Scripts\\python.exe -m unittest tests.test_transfer_v1

全部使用 FAKE 执行器 / Adapter / 校验器 / 清理器 / ACK / 上传器，无真实网络与文件系统副作用
（除 LocalFilesystemOps 的临时目录 containment 测试外）。
"""

from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from fnos_media_import.domain.models import ProviderCapabilities
from fnos_media_import.transfer import (
    AckResult,
    CancelResult,
    CleanupItem,
    CleanupResult,
    ErrorCode,
    OperationReceipt,
    OperationStatus,
    OrganizeResult,
    PHASE_ACK_PENDING,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    PHASE_CLEANUP_PENDING,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW,
    PHASE_NATIVE_SUBMITTING,
    PHASE_NATIVE_WAITING,
    PHASE_PLANNING,
    PHASE_RETRY_WAIT,
    PHASE_TARGET_VERIFYING,
    PHASE_TRANSFERRING,
    TERMINAL_PHASES,
    TransferError,
    TransferOrchestrator,
    TransferPlan,
    TransferVerifyResult,
    UploadOutcome,
    VisibilityStatus,
)
from fnos_media_import.transfer.cleanup import (
    CompositeFilesystemOps,
    LocalFilesystemOps,
    TransferCleanupService,
)
from fnos_media_import.transfer.provider_relay_executor import (
    DownloadStatus,
    ProviderRelayExecutor,
)
from fnos_media_import.transfer.native_executor import NativeTransferExecutor
from fnos_media_import.transfer.rclone_adapter import RcloneOperationAdapter
from fnos_media_import.transfer.rclone_executor import RcloneTransferExecutor
from fnos_media_import.transfer.verifier import ListedFiles, TransferVerifier


# ---------------------------------------------------------------------------
# 计划构造
# ---------------------------------------------------------------------------
def make_plan(
    *,
    job_id: int = 1,
    source_kind: str = "quark",
    source_form: str = "share",
    dest_kind: str = "quark",
    strategy: str = "native_save",
    category: str = "movie",
    landing: str | None = None,
    staging: str | None = None,
    final: str | None = None,
    local_temp: str | None = None,
    organizer_enabled: bool = False,
    expected_file_count: int = 2,
    expected_total_size: int = 100,
    delete_source_after_ack: bool = True,
    idempotency_key: str = "import:job-1",
) -> dict:
    connection_id = "ol_main"
    source_mount_id = f"mount_{source_kind}"
    destination_mount_id = f"mount_{dest_kind}"
    source_ns = f"openlist:{connection_id}:{source_mount_id}"
    dest_ns = f"openlist:{connection_id}:{destination_mount_id}"
    source_mount = {
        "kind": "openlist",
        "namespace_id": source_ns,
        "mount_id": source_mount_id,
        "mount_revision": 1,
        "connection_id": connection_id,
        "connection_revision": 1,
        "storage_identity_id": f"storage:{source_kind}",
        "identity_root": "/",
    }
    destination_mount = {
        "kind": "openlist",
        "namespace_id": dest_ns,
        "mount_id": destination_mount_id,
        "mount_revision": 1,
        "connection_id": connection_id,
        "connection_revision": 1,
        "storage_identity_id": f"storage:{dest_kind}",
        "identity_root": "/",
    }
    paths: dict = {"organizer": None, "local_temp": None}
    if landing is not None:
        paths["source_landing"] = {**source_mount, "path": landing}
    else:
        paths["source_landing"] = None
    if staging is not None:
        paths["destination_staging"] = {**destination_mount, "path": staging}
    else:
        paths["destination_staging"] = None
    if final is not None:
        paths["destination_final"] = {
            "kind": "native" if strategy in {"native_save", "native_offline", "relay_upload_provider"} else "openlist",
            "namespace_id": f"native:{dest_kind}:{dest_kind}_lib" if strategy in {"native_save", "native_offline", "relay_upload_provider"} else dest_ns,
            "path": final,
            **({} if strategy in {"native_save", "native_offline", "relay_upload_provider"} else destination_mount),
        }
    else:
        paths["destination_final"] = (
            {"namespace_id": f"native:{dest_kind}:{dest_kind}_lib", "path": "/电影"}
            if strategy in {"native_save", "native_offline"}
            else None
        )
    if organizer_enabled:
        paths["organizer"] = {**destination_mount, "path": "/影视/整理完成"}
    if local_temp is not None:
        paths["local_temp"] = {
            "kind": "relay",
            "namespace_id": "local:relay",
            "path": local_temp,
        }
    return {
        "version": 1,
        "job_id": job_id,
        "created_at": "2026-08-07T00:00:00Z",
        "source": {
            "provider_kind": source_kind,
            "source_form": source_form,
            "canonical_source_id": f"{source_kind}:share:abc",
            "selection_hash": "sha256:abc",
            "account_id": f"acc_{source_kind}",
            "account_revision": 1,
            "account_selection_revision": 1,
        },
        "destination": {
            "id": f"{dest_kind}_lib",
            "provider_kind": dest_kind,
            "target_revision": 1,
            "account_id": f"acc_{dest_kind}",
            "account_revision": 1,
            "account_selection_revision": 1,
        },
        "transfer": {
            "strategy": strategy,
            "executor": strategy,
            "idempotency_key": idempotency_key,
            "delete_source_after_ack": delete_source_after_ack,
        },
        "bindings": {
            "source_mount_id": source_mount_id,
            "source_mount_revision": 1,
            "source_mount_namespace_id": source_ns,
            "source_connection_id": connection_id,
            "source_connection_revision": 1,
            "source_mount_storage_identity_id": f"storage:{source_kind}",
            "source_mount_identity_root": "/",
            "destination_mount_id": destination_mount_id,
            "destination_mount_revision": 1,
            "destination_mount_namespace_id": dest_ns,
            "destination_connection_id": connection_id,
            "destination_connection_revision": 1,
            "destination_mount_storage_identity_id": f"storage:{dest_kind}",
            "destination_mount_identity_root": "/",
        },
        "paths": paths,
        "verification": {
            "minimum": ["file_count", "total_size"],
            "checksum": "when_supported",
            "expected_file_count": expected_file_count,
            "expected_total_size": expected_total_size,
        },
        "postprocess": {"organizer_enabled": organizer_enabled, "media_refresh_enabled": False},
        "category": category,
    }


# ---------------------------------------------------------------------------
# FAKE 依赖
# ---------------------------------------------------------------------------
class _EventLog(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def add(self, name: str, **kwargs) -> None:
        self.items.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.items]


class _FakeNativeAdapter(RepositoryFakeMixin):
    def __init__(
        self,
        kind: str,
        *,
        caps_save: bool = True,
        caps_offline: bool = False,
        submit_status: str = "submitted",
        poll_status: str = "completed",
        verify_status: str = "ok",
        fail_code: str | None = None,
        poll_failures: int = 0,
        unknown_poll: bool = False,
        not_implemented: bool = False,
        supports_cancel: bool = True,
        events: _EventLog | None = None,
    ) -> None:
        self.kind = kind
        self.caps_save = caps_save
        self.caps_offline = caps_offline
        self.submit_status = submit_status
        self.poll_status = poll_status
        self.verify_status = verify_status
        self.fail_code = fail_code
        self.poll_failures = poll_failures
        self.unknown_poll = unknown_poll
        self.not_implemented = not_implemented
        self.supports_cancel = supports_cancel
        self.events = events
        self.submit_count = 0
        self.poll_count = 0
        self.cancel_count = 0

    def capabilities(self, account=None) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_native_save=self.caps_save,
            supports_native_offline=self.caps_offline,
            supports_operation_polling=True,
            supports_native_verify=True,
        )

    def native_save(self, request):
        self.submit_count += 1
        if self.events is not None:
            self.events.add("native_save", provider=self.kind)
        if self.not_implemented:
            raise NotImplementedError("not implemented")
        if self.fail_code:
            raise TransferError(self.fail_code, "boom")
        return OperationReceipt(operation_id=f"op-save-{self.kind}", status=self.submit_status, provider_kind=self.kind)

    def native_offline(self, request):
        self.submit_count += 1
        if self.events is not None:
            self.events.add("native_offline", provider=self.kind)
        if self.not_implemented:
            raise NotImplementedError("not implemented")
        if self.fail_code:
            raise TransferError(self.fail_code, "boom")
        return OperationReceipt(operation_id=f"op-offline-{self.kind}", status=self.submit_status, provider_kind=self.kind)

    def poll_operation(self, receipt):
        self.poll_count += 1
        if self.events is not None:
            self.events.add("poll", provider=self.kind)
        if self.poll_failures > 0:
            self.poll_failures -= 1
            return OperationStatus("failed", "transient network", error_code=ErrorCode.NETWORK_TRANSIENT)
        if self.unknown_poll:
            return OperationStatus("unknown", "provider result unclear")
        return OperationStatus(self.poll_status)

    def verify_native_result(self, request):
        if self.events is not None:
            self.events.add("verify_native", provider=self.kind)
        return TransferVerifyResult(self.verify_status)

    def cancel_operation(self, receipt):
        self.cancel_count += 1
        if self.events is not None:
            self.events.add("cancel_operation", provider=self.kind)
        if self.supports_cancel:
            return CancelResult("cancelled", "confirmed")
        return CancelResult("not_cancellable", "unsupported")


class _FakeRcloneService(RepositoryFakeMixin):
    def __init__(self, *, start_ok: bool = True, run_status: str = "success", running: bool = False, events: _EventLog | None = None) -> None:
        self.start_ok = start_ok
        self.run_status = run_status
        self.running = running
        self.events = events
        self.starts: list[tuple[str, dict]] = []
        self.status_calls = 0

    def start(self, reason, staging_run):
        self.starts.append((reason, staging_run))
        if self.events is not None:
            self.events.add("rclone_start")
        if not self.start_ok:
            return {"success": False, "message": "rclone 集成不可用"}
        return {"success": True, "message": "started", "run_id": "run-1"}

    def start_copy(self, source, destination, operation_key, *, copy_run):
        result = self.start(reason=operation_key, staging_run=copy_run)
        if not result.get("success"):
            raise RuntimeError(result.get("message") or "start failed")
        return OperationReceipt(
            operation_id=f"rclone:{operation_key}",
            status="running",
            provider_kind="rclone",
            metadata={"run_id": result.get("run_id")},
        )

    def status(self):
        self.status_calls += 1
        if self.events is not None:
            self.events.add("rclone_status")
        return {"running": self.running, "run_status": self.run_status}

    def poll(self, receipt):
        return OperationStatus(
            "running" if self.running else ("completed" if self.run_status == "success" else "unknown")
        )

    def cancel(self, *_args, **_kwargs):
        if self.events is not None:
            self.events.add("rclone_cancel")
        return {"success": True, "message": "stopped"}


class _ScriptedRcloneService(RepositoryFakeMixin):
    """可按脚本返回 rclone status 的服务，用于取消/未知结果测试。"""

    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.starts: list[tuple[str, dict]] = []
        self.status_calls = 0

    def start(self, reason, staging_run):
        self.starts.append((reason, staging_run))
        return {"success": True, "run_id": "run-1"}

    def start_copy(self, source, destination, operation_key, *, copy_run):
        result = self.start(reason=operation_key, staging_run=copy_run)
        return OperationReceipt(operation_id="rclone:run-1", status="running", provider_kind="rclone", metadata={"run_id": "run-1"})

    def status(self):
        self.status_calls += 1
        value = self.statuses.pop(0) if self.statuses else "success"
        return {"running": False, "run_status": value}

    def poll(self, receipt):
        value = self.status()["run_status"]
        return OperationStatus("completed" if value == "success" else ("running" if value == "running" else "unknown"))


class _FakeResolver:
    remote_name = "OL"

    def resolve_frozen_remote(self, **values):
        return f"{self.remote_name}:{values.get('path') or ''}"

    @staticmethod
    def split_remote_path(value):
        remote, path = str(value).split(":", 1)
        return remote, path

    def local(self, *, namespace_id="", path=""):
        return path

    def relay(self, *, namespace_id="", path=""):
        return path


class _FakeMaterializer(RepositoryFakeMixin):
    def __init__(self, receipt: OperationReceipt | None = None, events: _EventLog | None = None) -> None:
        self.receipt = receipt or OperationReceipt(operation_id="landing-op", status="completed", provider_kind="quark")
        self.events = events
        self.calls = []

    def __call__(self, plan):
        self.calls.append(plan)
        if self.events is not None:
            self.events.add("materialize")
        return self.receipt


def _visibility_poller(result, events: _EventLog | None = None):
    calls = []

    def poller(receipt):
        calls.append(receipt)
        if events is not None:
            events.add("visibility")
        return result

    poller.calls = calls
    return poller


class _FakeTargetLister(RepositoryFakeMixin):
    def __init__(self, *, exists: bool = True, file_count: int = 2, total_size: int = 100, unknown: bool = False, events: _EventLog | None = None) -> None:
        self.exists = exists
        self.file_count = file_count
        self.total_size = total_size
        self.unknown = unknown
        self.events = events
        self.calls = []

    def __call__(self, descriptor):
        self.calls.append(descriptor)
        if self.events is not None:
            self.events.add("target_list")
        if self.unknown:
            return ListedFiles(exists=False, unknown=True, message="list query failed")
        return ListedFiles(exists=self.exists, file_count=self.file_count, total_size=self.total_size)


class _FakeAck(RepositoryFakeMixin):
    def __init__(self, *, acked: bool = True, fail_first: int = 0, events: _EventLog | None = None) -> None:
        self.acked = acked
        self.fail_first = fail_first
        self.events = events
        self.calls = 0

    def ack(self, plan):
        self.calls += 1
        if self.events is not None:
            self.events.add("ack")
        if self.fail_first >= self.calls:
            return AckResult(acked=False, message="app ack incomplete")
        return AckResult(acked=self.acked, message="acked")


class _FakeOrganizer(RepositoryFakeMixin):
    def __init__(self, *, status: str = "done", events: _EventLog | None = None) -> None:
        self.status = status
        self.events = events
        self.calls = 0

    def organize(self, plan):
        self.calls += 1
        if self.events is not None:
            self.events.add("organize")
        return OrganizeResult(self.status, final_path="/影视/整理完成" if self.status == "done" else "")


class _FakeFinalizer:
    def finalize(self, _plan):
        return {"status": "done"}


class _FakeCleanup(RepositoryFakeMixin):
    def __init__(self, *, ok: bool = True, events: _EventLog | None = None) -> None:
        self.ok = ok
        self.events = events
        self.calls: list[dict] = []

    def cleanup(self, plan, *, target_verified, app_acked):
        self.calls.append({"target_verified": target_verified, "app_acked": app_acked})
        if self.events is not None:
            self.events.add("cleanup", target_verified=target_verified, app_acked=app_acked)
        return CleanupResult(ok=self.ok, items=[])


class _FakeDownloadAdapter(RepositoryFakeMixin):
    def __init__(self, *, status: str = "completed", events: _EventLog | None = None) -> None:
        self.status = status
        self.events = events
        self.calls = []

    def download(self, *, source, local_dir, idempotency_key):
        self.calls.append((source, local_dir, idempotency_key))
        if self.events is not None:
            self.events.add("relay_download")
        return OperationReceipt(operation_id="dl-1", status="running", provider_kind="rclone", metadata={"step": "download"})

    def check(self, receipt):
        if self.events is not None:
            self.events.add("relay_download_check")
        return DownloadStatus(self.status)


class _FakeUploader(RepositoryFakeMixin):
    def __init__(
        self,
        *,
        status: str = "done",
        auth_expired: bool = False,
        events: _EventLog | None = None,
        provider_kind: str = "",
        event_prefix: str = "cmcc",
    ) -> None:
        self.status = status
        self.auth_expired = auth_expired
        self.events = events
        self.calls = []
        self.provider_kind = provider_kind
        self.event_prefix = event_prefix

    def upload_local_dir(self, *, local_dir, target_locator, idempotency_key, progress=None):
        self.calls.append((local_dir, target_locator, idempotency_key))
        if self.events is not None:
            self.events.add(f"{self.event_prefix}_upload")
        if self.auth_expired:
            return UploadOutcome("auth_expired", "移动云认证已过期")
        return UploadOutcome(self.status)

    def verify_target(self, *, target_locator, expected_file_count, expected_total_size):
        if self.events is not None:
            self.events.add(f"{self.event_prefix}_verify")
        return TransferVerifyResult("ok")


class _FakeAlert(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *, account_id, code, message):
        self.calls.append((account_id, code, message))


class _FakeFilesystem(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.deleted: list[dict] = []

    def delete_tree(self, descriptor):
        self.deleted.append(dict(descriptor))
        return {"ok": True, "message": "deleted"}

    def resolve_real(self, descriptor):
        return os.path.abspath(str(descriptor.get("path") or ""))


def _drive(orch: TransferOrchestrator, rounds: int = 40):
    """驱动编排器直到终态/人工复核/取消等待，并自动推进重试退避。"""
    last = None
    for _ in range(rounds):
        last = orch.run()
        phase = orch.state.phase
        if phase in TERMINAL_PHASES or phase in {PHASE_MANUAL_REVIEW, PHASE_CANCEL_PENDING, PHASE_ACK_PENDING}:
            return last
        if phase == PHASE_RETRY_WAIT:
            orch.mark_retry_ready()
    return last


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
class NativeSaveFlowTests(unittest.TestCase):
    def test_native_save_full_flow_completes(self) -> None:
        events = _EventLog()
        adapter = _FakeNativeAdapter("quark", events=events)
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        ack = _FakeAck(events=events)
        cleanup = _FakeCleanup(events=events)
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(
            plan,
            {"native_save": executor},
            verifier=None,
            cleanup=cleanup,
            ack_service=ack,
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertTrue(orch.state.target_verified)
        self.assertTrue(orch.state.app_acked)
        self.assertEqual(adapter.submit_count, 1)
        self.assertEqual(adapter.poll_count, 1)
        order = events.names()
        self.assertLess(order.index("native_save"), order.index("poll"))
        self.assertLess(order.index("poll"), order.index("verify_native"))
        self.assertLess(order.index("verify_native"), order.index("ack"))
        self.assertLess(order.index("ack"), order.index("cleanup"))
        self.assertEqual(len(cleanup.calls), 1)
        self.assertTrue(cleanup.calls[0]["target_verified"])
        self.assertTrue(cleanup.calls[0]["app_acked"])

    def test_native_save_requires_same_provider(self) -> None:
        adapter = _FakeNativeAdapter("quark")
        plan = make_plan(source_kind="quark", dest_kind="tianyi", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.NATIVE_TRANSFER_UNAVAILABLE)
        self.assertEqual(adapter.submit_count, 0)

    def test_tianyi_not_implemented_fails_without_degrading(self) -> None:
        adapter = _FakeNativeAdapter("tianyi", not_implemented=True)
        rclone = _FakeRcloneService()
        plan = make_plan(source_kind="tianyi", dest_kind="tianyi", strategy="native_save")
        native = NativeTransferExecutor({"tianyi": adapter}, strategy="native_save")
        rclone_exec = RcloneTransferExecutor(rclone, _FakeResolver(), source_materializer=_FakeMaterializer())
        orch = TransferOrchestrator(plan, {"native_save": native, "rclone_copy": rclone_exec})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.NATIVE_TRANSFER_UNAVAILABLE)
        self.assertEqual(rclone.starts, [], "原生转存未实现时绝不降级为 rclone")

    def test_unknown_poll_result_goes_manual_review_and_never_resubmits(self) -> None:
        adapter = _FakeNativeAdapter("quark", unknown_poll=True)
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_MANUAL_REVIEW)
        self.assertEqual(orch.state.error_code, ErrorCode.RESULT_UNKNOWN)
        self.assertEqual(adapter.submit_count, 1, "未知结果不得触发第二次原生提交")

    def test_resume_with_unknown_receipt_never_recreates(self) -> None:
        adapter = _FakeNativeAdapter("quark", unknown_poll=True)
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor})
        orch.restore(
            {
                "phase": PHASE_NATIVE_WAITING,
                "executor_strategy": "native_save",
                "receipt": {
                    "operation_id": "op-save-quark",
                    "status": "unknown",
                    "provider_kind": "quark",
                    "message": "previous submit result unknown",
                },
            }
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_MANUAL_REVIEW)
        self.assertEqual(adapter.submit_count, 0, "恢复时必须轮询既有 receipt，绝不重复创建原生转存")
        self.assertGreaterEqual(adapter.poll_count, 1)

    def test_resume_native_submitting_with_receipt_skips_submit(self) -> None:
        adapter = _FakeNativeAdapter("quark", poll_status="completed")
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor}, cleanup=_FakeCleanup(), ack_service=_FakeAck())
        orch.restore(
            {
                "phase": PHASE_NATIVE_SUBMITTING,
                "executor_strategy": "native_save",
                "receipt": {"operation_id": "op-save-quark", "status": "submitted", "provider_kind": "quark"},
            }
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertEqual(adapter.submit_count, 0, "已存在 receipt 时跳过提交，直接轮询")

    def test_bounded_retry_exhausts_to_failed(self) -> None:
        adapter = _FakeNativeAdapter("quark", poll_failures=10)
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        from fnos_media_import.transfer.orchestrator import RetryPolicy

        orch = TransferOrchestrator(
            plan,
            {"native_save": executor},
            retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.001),
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertIn("重试次数耗尽", orch.state.message)
        self.assertEqual(adapter.poll_count, 3, "最多 3 次失败后失败关闭")

    def test_planning_cancel_direct(self) -> None:
        adapter = _FakeNativeAdapter("quark")
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor})
        result = orch.cancel()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(orch.state.phase, PHASE_CANCELLED)
        self.assertEqual(adapter.submit_count, 0)

    def test_target_verified_no_ack_keeps_source_then_resumes(self) -> None:
        events = _EventLog()
        adapter = _FakeNativeAdapter("quark", events=events)
        ack = _FakeAck(fail_first=1, events=events)
        cleanup = _FakeCleanup(events=events)
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": executor}, cleanup=cleanup, ack_service=ack)
        _drive(orch)
        # 目标已确认但 ACK 未完成：停在 ack_pending，来源不清理
        self.assertEqual(orch.state.phase, PHASE_ACK_PENDING)
        self.assertTrue(orch.state.target_verified)
        self.assertFalse(orch.state.app_acked)
        self.assertEqual(len(cleanup.calls), 0, "ACK 未完成不得清理来源")
        # 取消请求在此时应恢复 ACK/清理而非重复传输
        cancel = orch.cancel()
        self.assertEqual(cancel.status, "resume_ack")
        self.assertEqual(orch.state.phase, PHASE_ACK_PENDING)
        self.assertEqual(adapter.submit_count, 1, "不得重复传输")
        # 应用 ACK 完成后恢复清理并完成
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertTrue(orch.state.app_acked)
        self.assertEqual(len(cleanup.calls), 1)
        self.assertTrue(cleanup.calls[0]["app_acked"])


class MagnetRestrictionTests(unittest.TestCase):
    def test_magnet_non_sixpan_rejected(self) -> None:
        adapter = _FakeNativeAdapter("quark", caps_offline=True)
        plan = make_plan(source_kind="quark", source_form="magnet", dest_kind="tianyi", strategy="native_save")
        native = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(plan, {"native_save": native})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.MAGNET_TARGET_RESTRICTED)

    def test_magnet_to_sixpan_native_offline_completes(self) -> None:
        events = _EventLog()
        adapter = _FakeNativeAdapter("sixpan", caps_offline=True, events=events)
        plan = make_plan(
            source_kind="sixpan",
            source_form="magnet",
            dest_kind="sixpan",
            strategy="native_offline",
        )
        ack = _FakeAck(events=events)
        cleanup = _FakeCleanup(events=events)
        executor = NativeTransferExecutor({"sixpan": adapter}, strategy="native_offline")
        orch = TransferOrchestrator(plan, {"native_offline": executor}, cleanup=cleanup, ack_service=ack)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertIn("native_offline", events.names())
        self.assertEqual(adapter.submit_count, 1)

    def test_native_offline_to_non_sixpan_rejected_by_executor(self) -> None:
        adapter = _FakeNativeAdapter("quark", caps_offline=True)
        plan = make_plan(source_kind="quark", source_form="magnet", dest_kind="tianyi", strategy="native_offline")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_offline")
        orch = TransferOrchestrator(plan, {"native_offline": executor})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.MAGNET_TARGET_RESTRICTED)


class RcloneCopyTests(unittest.TestCase):
    def _orchestrator(self, events: _EventLog, *, target_ok: bool = True, target_unknown: bool = False):
        rclone = _FakeRcloneService(events=events)
        resolver = _FakeResolver()
        materializer = _FakeMaterializer(events=events)
        visibility = _visibility_poller(VisibilityStatus.visible, events)
        lister = _FakeTargetLister(exists=target_ok, unknown=target_unknown, events=events)
        verifier = TransferVerifier(target_lister=lister)
        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        executor = RcloneTransferExecutor(
            rclone,
            resolver,
            source_materializer=materializer,
            visibility_poller=visibility,
            verifier=verifier,
        )
        ack = _FakeAck(events=events)
        cleanup = _FakeCleanup(events=events)
        orch = TransferOrchestrator(plan, {"rclone_copy": executor}, verifier=verifier, cleanup=cleanup, ack_service=ack, finalizer_service=_FakeFinalizer())
        return orch, rclone, lister, cleanup

    def test_rclone_copy_verify_ack_cleanup_ordering(self) -> None:
        events = _EventLog()
        orch, rclone, lister, cleanup = self._orchestrator(events)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        order = events.names()
        self.assertLess(order.index("materialize"), order.index("visibility"))
        self.assertLess(order.index("visibility"), order.index("rclone_start"))
        self.assertLess(order.index("rclone_start"), order.index("target_list"))
        self.assertEqual(order.count("target_list"), 2)
        self.assertLess(order.index("target_list"), order.index("ack"))
        self.assertLess(order.index("ack"), order.index("cleanup"))
        self.assertEqual(len(lister.calls), 2)
        self.assertEqual(len(cleanup.calls), 1)
        self.assertTrue(cleanup.calls[0]["target_verified"])
        self.assertTrue(cleanup.calls[0]["app_acked"])
        self.assertTrue(rclone.starts, "rclone_copy 必须通过 rclone_service 启动")

    def test_rclone_copy_builds_job_scoped_copy_run(self) -> None:
        events = _EventLog()
        orch, rclone, _lister, _cleanup = self._orchestrator(events)
        _drive(orch)
        copy_run = rclone.starts[0][1]
        self.assertEqual(copy_run["copy_mode"], True)
        self.assertEqual(copy_run["job_id"], 1)
        self.assertEqual(copy_run["job_dir_name"], "job-1")
        self.assertEqual(copy_run["delete_source_after_ack"], True)
        self.assertEqual(copy_run["destination_id"], "sixpan_lib")

    def test_rclone_target_missing_retries_verify_without_cleanup(self) -> None:
        events = _EventLog()
        orch, _rclone, _lister, cleanup = self._orchestrator(events, target_ok=False)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.VERIFY_MISMATCH)
        self.assertEqual(len(cleanup.calls), 0, "目标未确认不得清理")

    def test_rclone_target_unknown_manual_review(self) -> None:
        events = _EventLog()
        orch, _rclone, _lister, cleanup = self._orchestrator(events, target_unknown=True)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_MANUAL_REVIEW)
        self.assertEqual(len(cleanup.calls), 0, "校验未知不得清理来源")

    def test_cancel_transfer_unknown_goes_cancel_pending(self) -> None:
        rclone = _ScriptedRcloneService(["cancelled"])
        resolver = _FakeResolver()
        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        executor = RcloneTransferExecutor(
            rclone,
            resolver,
            source_materializer=_FakeMaterializer(),
            visibility_poller=_visibility_poller(VisibilityStatus.visible),
        )
        orch = TransferOrchestrator(plan, {"rclone_copy": executor})
        # 启动传输（submit -> running receipt -> 需要等待轮询）
        orch.run()
        self.assertEqual(orch.state.phase, PHASE_TRANSFERRING)
        # 模拟「已提交但结果未知」的 receipt
        orch.state.receipt = OperationReceipt(
            operation_id="rclone-copy:1",
            status="unknown",
            provider_kind="rclone",
            message="previous run result unknown",
        )
        cancel = orch.cancel()
        self.assertEqual(cancel.status, "cancel_pending")
        self.assertEqual(orch.state.phase, PHASE_CANCEL_PENDING)
        # 轮询确认取消
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_CANCEL_PENDING)

    def test_cancel_transfer_direct_after_completed(self) -> None:
        rclone = _ScriptedRcloneService(["success"])
        resolver = _FakeResolver()
        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        executor = RcloneTransferExecutor(
            rclone,
            resolver,
            source_materializer=_FakeMaterializer(),
            visibility_poller=_visibility_poller(VisibilityStatus.visible),
        )
        orch = TransferOrchestrator(plan, {"rclone_copy": executor})
        # 恢复：传输已提交但目标未校验，rclone 运行实际已成功
        orch.restore(
            {
                "phase": PHASE_TRANSFERRING,
                "executor_strategy": "rclone_copy",
                "receipt": {"operation_id": "rclone-copy:1", "status": "running", "provider_kind": "rclone"},
            }
        )
        # direct 取消：操作已完成但未通过目标校验，取消任务（目标可能残留，不清理）
        cancel = orch.cancel(direct=True)
        self.assertEqual(cancel.status, "cancelled")
        self.assertEqual(orch.state.phase, PHASE_CANCELLED)

    def test_cancel_pending_normalizes_dict_poll_without_resubmitting(self) -> None:
        class _Executor:
            strategy = "rclone_copy"

            def __init__(self) -> None:
                self.plan = None
                self.poll_calls = 0
                self.cancel_calls = 0
                self.submit_calls = 0

            def bind(self, plan) -> None:
                self.plan = plan

            def poll(self, _receipt):
                self.poll_calls += 1
                return {"status": "cancelled", "message": "confirmed"}

            def cancel(self, _receipt):
                self.cancel_calls += 1
                return CancelResult("cancelled", "confirmed")

            def submit(self):
                self.submit_calls += 1
                raise AssertionError("cancel recovery must never resubmit")

        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        executor = _Executor()
        orch = TransferOrchestrator(plan, {"rclone_copy": executor})
        orch.restore(
            {
                "phase": PHASE_CANCEL_PENDING,
                "executor_strategy": "rclone_copy",
                "receipt": {
                    "operation_id": "rclone-copy:1",
                    "status": "running",
                    "provider_kind": "rclone",
                },
            }
        )

        result = orch.step()

        self.assertEqual(result.phase, PHASE_CANCELLED)
        self.assertEqual(executor.poll_calls, 1)
        self.assertEqual(executor.cancel_calls, 0)
        self.assertEqual(executor.submit_calls, 0)

    def test_direct_cancel_normalizes_terminal_dict_poll_before_cancel(self) -> None:
        class _Executor:
            strategy = "rclone_copy"

            def __init__(self) -> None:
                self.plan = None
                self.poll_calls = 0
                self.cancel_calls = 0

            def bind(self, plan) -> None:
                self.plan = plan

            def poll(self, _receipt):
                self.poll_calls += 1
                return {"status": "failed", "message": "provider task failed"}

            def cancel(self, _receipt):
                self.cancel_calls += 1
                raise AssertionError("terminal provider task must not be cancelled again")

        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        executor = _Executor()
        orch = TransferOrchestrator(plan, {"rclone_copy": executor})
        orch.restore(
            {
                "phase": PHASE_TRANSFERRING,
                "executor_strategy": "rclone_copy",
                "receipt": {
                    "operation_id": "rclone-copy:1",
                    "status": "running",
                    "provider_kind": "rclone",
                },
            }
        )

        result = orch.cancel(direct=True)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(orch.state.phase, PHASE_CANCELLED)
        self.assertEqual(executor.poll_calls, 1)
        self.assertEqual(executor.cancel_calls, 0)

    def test_rclone_rejects_provider_api_upload_destinations(self) -> None:
        for destination in ("mobile", "tianyi"):
            with self.subTest(destination=destination):
                plan = make_plan(
                    source_kind="quark",
                    dest_kind=destination,
                    strategy="rclone_copy",
                    landing="/_fnos_ingress/job-1",
                    staging=f"/{destination}/_fnos_staging/job-1",
                    final=f"/{destination}/电影",
                )
                executor = RcloneTransferExecutor(
                    _FakeRcloneService(),
                    _FakeResolver(),
                    source_materializer=_FakeMaterializer(),
                )
                orch = TransferOrchestrator(plan, {"rclone_copy": executor})
                _drive(orch)
                self.assertEqual(orch.state.phase, PHASE_FAILED)
                self.assertEqual(orch.state.error_code, ErrorCode.INVALID_PLAN)


class RcloneReconcileFenceTests(unittest.TestCase):
    class _Service:
        def __init__(self, lookup_result=None, lookup_error: Exception | None = None) -> None:
            self.lookup_result = lookup_result
            self.lookup_error = lookup_error
            self.lookup_calls = 0
            self.start_calls = 0

        def find_run_by_operation_key(self, operation_key: str):
            self.lookup_calls += 1
            if self.lookup_error is not None:
                raise self.lookup_error
            return self.lookup_result

        def start_copy(self, source, destination, operation_key, *, copy_run):
            self.start_calls += 1
            return OperationReceipt(
                operation_id="rclone:new-run",
                status="running",
                provider_kind="rclone",
                metadata={"run_id": "new-run", "operation_key": operation_key},
            )

    @staticmethod
    def _adapter(service):
        return RcloneOperationAdapter(service, None).bind(SimpleNamespace(job_id=1))

    def test_lookup_error_never_falls_through_to_submission(self) -> None:
        service = self._Service(lookup_error=RuntimeError("database unavailable"))
        adapter = self._adapter(service)
        for _attempt in range(2):
            with self.assertRaises(TransferError) as raised:
                adapter.start_copy(
                    {"kind": "openlist", "path": "/source"},
                    {"kind": "local", "path": "/target"},
                    "operation-key-1",
                    copy_run={"job_id": 1},
                )
            self.assertEqual(raised.exception.code, ErrorCode.NETWORK_TRANSIENT)
        self.assertEqual(service.lookup_calls, 2)
        self.assertEqual(service.start_calls, 0)

    def test_explicit_empty_lookup_submits_once(self) -> None:
        service = self._Service(lookup_result=None)
        receipt = self._adapter(service).start_copy(
            {"kind": "openlist", "path": "/source"},
            {"kind": "local", "path": "/target"},
            "operation-key-1",
            copy_run={"job_id": 1},
        )
        self.assertEqual(receipt.status, "running")
        self.assertEqual(service.start_calls, 1)

    def test_existing_lookup_result_is_reused(self) -> None:
        service = self._Service(
            lookup_result={
                "operation_id": "rclone:existing",
                "run_id": "existing-run",
                "operation_key": "operation-key-1",
                "job_id": 1,
                "status": "running",
            }
        )
        receipt = self._adapter(service).start_copy(
            {"kind": "openlist", "path": "/source"},
            {"kind": "local", "path": "/target"},
            "operation-key-1",
            copy_run={"job_id": 1},
        )
        self.assertEqual(receipt.operation_id, "rclone:existing")
        self.assertEqual(service.start_calls, 0)


class ProviderRelayTests(unittest.TestCase):
    def _orchestrator(self, events: _EventLog, *, uploader_status: str = "done", auth_expired: bool = False):
        download = _FakeDownloadAdapter(events=events)
        uploader = _FakeUploader(status=uploader_status, auth_expired=auth_expired, events=events)
        alert = _FakeAlert()
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="/移动云/电影",
            local_temp="/data/relay/job-1",
        )
        executor = ProviderRelayExecutor(
            download,
            uploader,
            source_materializer=_FakeMaterializer(events=events),
            visibility_poller=_visibility_poller(VisibilityStatus.visible, events),
            relay_root="/data/relay",
            alert_service=alert,
        )
        ack = _FakeAck(events=events)
        cleanup = _FakeCleanup(events=events)
        verifier = TransferVerifier(target_lister=_FakeTargetLister(events=events))
        orch = TransferOrchestrator(plan, {"relay_upload_provider": executor}, verifier=verifier, cleanup=cleanup, ack_service=ack, finalizer_service=_FakeFinalizer())
        return orch, download, uploader, alert, cleanup

    def test_provider_relay_rejects_same_provider(self) -> None:
        plan = make_plan(
            source_kind="quark",
            dest_kind="quark",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="/影视/电影",
            local_temp="/data/relay/job-1",
        )
        executor = ProviderRelayExecutor(_FakeDownloadAdapter(), _FakeUploader(), source_materializer=_FakeMaterializer())
        orch = TransferOrchestrator(plan, {"relay_upload_provider": executor})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.INVALID_PLAN)

    def test_relay_full_flow_ordering(self) -> None:
        events = _EventLog()
        orch, download, uploader, _alert, cleanup = self._orchestrator(events)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        order = events.names()
        self.assertLess(order.index("materialize"), order.index("visibility"))
        self.assertLess(order.index("visibility"), order.index("relay_download"))
        self.assertLess(order.index("relay_download"), order.index("relay_download_check"))
        self.assertLess(order.index("relay_download_check"), order.index("cmcc_upload"))
        self.assertLess(order.index("cmcc_upload"), order.index("cmcc_verify"))
        self.assertLess(order.index("cmcc_verify"), order.index("ack"))
        self.assertLess(order.index("ack"), order.index("cleanup"))
        local_dir = download.calls[0][1]
        self.assertEqual(local_dir, "/data/relay/job-1")

    def test_relay_auth_expired_halts_and_alerts_no_retry(self) -> None:
        events = _EventLog()
        orch, _download, _uploader, alert, cleanup = self._orchestrator(events, auth_expired=True)
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(len(alert.calls), 1, "认证失效必须生成账号告警")
        account_id, code, _msg = alert.calls[0]
        self.assertEqual(code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(account_id, "acc_mobile")
        self.assertEqual(len(cleanup.calls), 0, "上传未确认不得清理缓存/来源")

    def test_relay_delete_source_after_ack_false_skips_cleanup(self) -> None:
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="/移动云/电影",
            local_temp="/data/relay/job-1",
            delete_source_after_ack=False,
        )
        download = _FakeDownloadAdapter()
        uploader = _FakeUploader()
        executor = ProviderRelayExecutor(
            download,
            uploader,
            source_materializer=_FakeMaterializer(),
            visibility_poller=_visibility_poller(VisibilityStatus.visible),
            relay_root="/data/relay",
        )
        cleanup = _FakeCleanup()
        orch = TransferOrchestrator(
            plan,
            {"relay_upload_provider": executor},
            verifier=TransferVerifier(target_lister=_FakeTargetLister()),
            cleanup=cleanup,
            ack_service=_FakeAck(),
            finalizer_service=_FakeFinalizer(),
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertEqual(len(cleanup.calls), 0, "delete_source_after_ack=False 时不做清理")


class RelayProviderTests(unittest.TestCase):
    def test_tianyi_provider_relay_runs_shared_pipeline(self) -> None:
        events = _EventLog()
        plan = make_plan(
            source_kind="quark",
            dest_kind="tianyi",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="root-folder-id::\u7535\u5f71",
            local_temp="/data/relay/job-1",
        )
        uploader = _FakeUploader(
            events=events,
            provider_kind="tianyi",
            event_prefix="tianyi",
        )
        download = _FakeDownloadAdapter(events=events)
        executor = ProviderRelayExecutor(
            download,
            uploader,
            source_materializer=_FakeMaterializer(events=events),
            visibility_poller=_visibility_poller(VisibilityStatus.visible, events),
            relay_root="/data/relay",
        )
        cleanup = _FakeCleanup(events=events)
        orch = TransferOrchestrator(
            plan,
            {"relay_upload_provider": executor},
            verifier=TransferVerifier(target_lister=_FakeTargetLister(events=events)),
            cleanup=cleanup,
            ack_service=_FakeAck(events=events),
            finalizer_service=_FakeFinalizer(),
        )

        _drive(orch)

        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        order = events.names()
        self.assertLess(order.index("relay_download_check"), order.index("tianyi_upload"))
        self.assertLess(order.index("tianyi_upload"), order.index("tianyi_verify"))
        self.assertLess(order.index("tianyi_verify"), order.index("ack"))
        self.assertEqual(download.calls[0][1], "/data/relay/job-1")
        self.assertEqual(uploader.calls[0][1]["namespace_id"], "native:tianyi:tianyi_lib")

    def test_provider_relay_rejects_same_provider_source(self) -> None:
        plan = make_plan(
            source_kind="tianyi",
            dest_kind="tianyi",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="root-folder-id::\u7535\u5f71",
            local_temp="/data/relay/job-1",
        )
        uploader = _FakeUploader(provider_kind="tianyi", event_prefix="tianyi")
        orch = TransferOrchestrator(
            plan,
            {
                "relay_upload_provider": ProviderRelayExecutor(
                    _FakeDownloadAdapter(),
                    uploader,
                    source_materializer=_FakeMaterializer(),
                    relay_root="/data/relay",
                )
            },
        )

        _drive(orch)

        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.INVALID_PLAN)
        self.assertEqual(uploader.calls, [])


class CleanupSafetyTests(unittest.TestCase):
    def _plan(self, landing=None, local_temp=None, job_id=1):
        return make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing=landing,
            staging="/影视/_fnos_staging/job-1" if landing else None,
            final="/影视/电影",
            local_temp=local_temp,
            job_id=job_id,
        )

    def test_cleanup_requires_verified_and_acked(self) -> None:
        fs = _FakeFilesystem()
        service = TransferCleanupService(fs, local_roots=("/data/relay",))
        plan = self._plan(landing="/_fnos_ingress/job-1", local_temp="/data/relay/job-1")
        r1 = service.cleanup(plan, target_verified=False, app_acked=True)
        self.assertTrue(r1.skipped)
        self.assertEqual(fs.deleted, [])
        r2 = service.cleanup(plan, target_verified=True, app_acked=False)
        self.assertTrue(r2.skipped)
        self.assertEqual(fs.deleted, [])

    def test_cleanup_deletes_job_scoped_dirs(self) -> None:
        fs = _FakeFilesystem()
        service = TransferCleanupService(fs, local_roots=("/data/relay",))
        plan = self._plan(landing="/_fnos_ingress/job-1", local_temp="/data/relay/job-1")
        result = service.cleanup(plan, target_verified=True, app_acked=True)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(item.ok for item in result.items))
        self.assertEqual(len(fs.deleted), 2)

    def test_cleanup_refuses_non_job_dir(self) -> None:
        fs = _FakeFilesystem()
        service = TransferCleanupService(fs, local_roots=("/data/relay",))
        plan = self._plan(landing="/_fnos_ingress/shared", local_temp="/data/relay/job-1")
        result = service.cleanup(plan, target_verified=True, app_acked=True)
        refused = [item for item in result.items if item.refused]
        self.assertTrue(refused, "非 job-<id> 目录必须被拒绝")
        # 被拒绝的来源落地区绝不能删除；job 作用域的本地缓存正常删除
        landing_deleted = any("_fnos_ingress" in str(item["path"]) for item in fs.deleted)
        self.assertFalse(landing_deleted, "被拒绝的非任务级目录绝不能删除")
        self.assertIn("/data/relay/job-1", [str(item["path"]) for item in fs.deleted])

    def test_cleanup_refuses_mount_root(self) -> None:
        fs = _FakeFilesystem()
        service = TransferCleanupService(fs, local_roots=("/data/relay",))
        plan = self._plan(landing="/", local_temp="/data/relay/job-1")
        result = service.cleanup(plan, target_verified=True, app_acked=True)
        refused = [item for item in result.items if item.refused]
        self.assertTrue(refused)

    def test_cleanup_refuses_local_path_outside_roots(self) -> None:
        fs = _FakeFilesystem()
        service = TransferCleanupService(fs, local_roots=("/data/relay",))
        plan = self._plan(landing="/_fnos_ingress/job-1", local_temp="/tmp/unmanaged/job-1")
        result = service.cleanup(plan, target_verified=True, app_acked=True)
        refused = [item for item in result.items if item.refused]
        self.assertTrue(refused, "未托管根目录外的本地 job 目录必须被拒绝")

    def test_cleanup_failure_is_warning_and_retryable(self) -> None:
        class _FailFilesystem:
            def delete_tree(self, descriptor):
                return {"ok": False, "message": "remote delete failed"}

            def resolve_real(self, descriptor):
                return str(descriptor.get("path") or "")

        service = TransferCleanupService(_FailFilesystem(), local_roots=("/data/relay",))
        plan = self._plan(landing="/_fnos_ingress/job-1", local_temp="/data/relay/job-1")
        result = service.cleanup(plan, target_verified=True, app_acked=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.warning)
        self.assertTrue(result.retryable)

    def test_local_filesystem_ops_containment(self) -> None:
        root = tempfile.mkdtemp(prefix="fnos-transfer-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        job_dir = os.path.join(root, "job-1")
        nested = os.path.join(job_dir, "sub")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "f.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        ops = LocalFilesystemOps(allowed_roots=(root,))
        ok = ops.delete_tree({"kind": "local", "path": job_dir})
        self.assertTrue(ok["ok"])
        self.assertFalse(os.path.exists(job_dir))
        # 拒绝删除配置根自身
        refused = ops.delete_tree({"kind": "local", "path": root})
        self.assertTrue(refused["refused"])
        self.assertTrue(os.path.isdir(root))
        # 拒绝删除根外的路径
        outside = tempfile.mkdtemp(prefix="fnos-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        refused2 = ops.delete_tree({"kind": "local", "path": outside})
        self.assertTrue(refused2["refused"])

    def test_cleanup_exception_stays_cleanup_pending_and_retryable(self) -> None:
        class RaisingCleanup:
            @staticmethod
            def cleanup(*_args, **_kwargs):
                raise RuntimeError("filesystem unavailable")

        orchestrator = TransferOrchestrator(make_plan(), {}, cleanup=RaisingCleanup())
        orchestrator.state.phase = PHASE_CLEANUP_PENDING
        orchestrator.state.target_verified = True
        orchestrator.state.app_acked = True

        result = orchestrator.step()

        self.assertEqual(result.phase, PHASE_CLEANUP_PENDING)
        self.assertTrue(result.finished)
        self.assertTrue(result.needs_wait)
        self.assertFalse(orchestrator.state.cleanup["ok"])
        self.assertEqual(orchestrator.state.error_code, ErrorCode.CLEANUP_FAILED)
        self.assertIn("filesystem unavailable", orchestrator.state.cleanup["reason"])


class UnsupportedPlanTests(unittest.TestCase):
    def test_non_v1_plan_rejected(self) -> None:
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        plan["version"] = 3
        with self.assertRaises(TransferError) as ctx:
            TransferOrchestrator(plan, {})
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_PLAN_VERSION)

    def test_unknown_strategy_fails_closed(self) -> None:
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="teleport")
        orch = TransferOrchestrator(plan, {})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.INVALID_PLAN)

    def test_known_strategy_without_executor_fails_closed(self) -> None:
        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            staging="/影视/_fnos_staging/job-1",
            final="/影视/电影",
        )
        orch = TransferOrchestrator(plan, {})
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertEqual(orch.state.error_code, ErrorCode.NO_EXECUTOR)

    def test_relay_path_contract_does_not_require_destination_staging(self) -> None:
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="/移动云/电影",
            local_temp="/data/relay/job-1",
        )
        self.assertIsNone(plan["paths"]["destination_staging"])
        parsed = TransferPlan.from_dict(plan)
        self.assertIsNone(parsed.destination_staging)
        self.assertEqual(parsed.local_temp["namespace_id"], "local:relay")

    def test_relay_path_contract_requires_local_temp_and_final(self) -> None:
        missing_temp = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            final="/移动云/电影",
        )
        with self.assertRaisesRegex(TransferError, "local_temp"):
            TransferPlan.from_dict(missing_temp)

        missing_final = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1",
            local_temp="/data/relay/job-1",
        )
        with self.assertRaisesRegex(TransferError, "destination_final"):
            TransferPlan.from_dict(missing_final)

    def test_rclone_path_contract_requires_destination_staging(self) -> None:
        plan = make_plan(
            source_kind="quark",
            dest_kind="sixpan",
            strategy="rclone_copy",
            landing="/_fnos_ingress/job-1",
            final="/影视/电影",
        )
        with self.assertRaisesRegex(TransferError, "destination_staging"):
            TransferPlan.from_dict(plan)


class RcloneReceiptRecoveryTests(unittest.TestCase):
    class _Service:
        def __init__(self, run):
            self.run = run
            self.requested_run_id = None

        @staticmethod
        def status():
            return {"running": False, "current_run_id": None}

        def get_run(self, run_id):
            self.requested_run_id = run_id
            return self.run

        def poll_run(self, run_id, *, operation_key="", receipt=None):
            del operation_key, receipt
            return self.get_run(run_id)

    @staticmethod
    def _receipt():
        return OperationReceipt(
            operation_id="rclone-copy:1:41",
            status="running",
            provider_kind="rclone",
            metadata={"run_id": "41"},
        )

    def test_completed_run_is_recovered_from_durable_receipt(self) -> None:
        service = self._Service({"status": "success"})
        status = RcloneOperationAdapter(service, None).poll(self._receipt())
        self.assertEqual(status.status, "completed")
        self.assertEqual(service.requested_run_id, 41)

    def test_failed_run_is_recovered_from_durable_receipt(self) -> None:
        service = self._Service({"status": "failed", "error_message": "copy failed"})
        status = RcloneOperationAdapter(service, None).poll(self._receipt())
        self.assertEqual(status.status, "failed")
        self.assertEqual(status.message, "copy failed")

    def test_missing_receipt_run_fails_closed(self) -> None:
        status = RcloneOperationAdapter(self._Service(None), None).poll(self._receipt())
        self.assertEqual(status.status, "unknown")

class OrganizerTests(unittest.TestCase):
    def test_organizer_gate_before_ack(self) -> None:
        events = _EventLog()
        adapter = _FakeNativeAdapter("quark", events=events)
        organizer = _FakeOrganizer(status="done", events=events)
        plan = make_plan(
            source_kind="quark",
            dest_kind="quark",
            strategy="native_save",
            organizer_enabled=True,
        )
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orch = TransferOrchestrator(
            plan,
            {"native_save": executor},
            organizer_service=organizer,
            verifier=TransferVerifier(target_lister=_FakeTargetLister()),
            ack_service=_FakeAck(events=events),
            cleanup=_FakeCleanup(events=events),
        )
        _drive(orch)
        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        order = events.names()
        self.assertLess(order.index("organize"), order.index("ack"))
        self.assertEqual(organizer.calls, 1)


if __name__ == "__main__":
    unittest.main()
