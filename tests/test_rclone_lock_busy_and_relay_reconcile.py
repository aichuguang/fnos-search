"""互斥锁被占用（exit 75）与中继空目录作废的回归测试。

背景（任务 #9 事故）：worker 脚本拿不到 mkdir 锁时以 0 退出，主程序把这次
"一个字节都没搬"的 run 记成 success 并唤醒上传；中继目录为空上传失败后，
重试又按 operation_key 对账到那个假成功的 run，拿到 completed 回执直接跳过
上传进目标校验，最终"移动云目标目录不存在"耗尽重试。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.constants import RCLONE_EXIT_LOCK_BUSY, RCLONE_RUN_FAILED
from fnos_media_import.services.rclone_process_runner import ProcessResult
from fnos_media_import.services.rclone_run_completion_service import (
    RcloneRunCompletionService,
)
from fnos_media_import.services.rclone_run_state import RcloneRunState
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer import (
    ErrorCode,
    OperationReceipt,
    PHASE_COMPLETED,
    TransferOrchestrator,
    UploadOutcome,
    VisibilityStatus,
)
from fnos_media_import.transfer.provider_relay_executor import (
    DownloadStatus,
    ProviderRelayExecutor,
)
from fnos_media_import.transfer.rclone_adapter import RcloneOperationAdapter
from fnos_media_import.transfer.verifier import TransferVerifier
from tests.test_transfer_v1 import (
    _FakeAck,
    _FakeCleanup,
    _FakeFinalizer,
    _FakeMaterializer,
    _FakeTargetLister,
    _FakeUploader,
    _drive,
    _EventLog,
    _visibility_poller,
    make_plan,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 脚本层：flock + 独立退出码
# ---------------------------------------------------------------------------
class WorkerScriptLockContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "scripts" / "fnos_rclone_worker.sh").read_text(encoding="utf-8")

    def test_lock_uses_flock_and_busy_exit_code(self) -> None:
        self.assertIn("flock -n 9", self.script)
        self.assertIn("EXIT_LOCK_BUSY=75", self.script)
        self.assertIn('exit "$EXIT_LOCK_BUSY"', self.script)
        self.assertEqual(RCLONE_EXIT_LOCK_BUSY, 75)

    def test_mkdir_lock_and_silent_success_exit_are_gone(self) -> None:
        self.assertNotIn("LOCK_DIR", self.script)
        self.assertNotIn('mkdir "$LOCK_DIR"', self.script)
        lock_body = self.script.split("acquire_lock() {", 1)[1].split("}", 1)[0]
        self.assertNotIn("exit 0", lock_body)


# ---------------------------------------------------------------------------
# 进程运行结果
# ---------------------------------------------------------------------------
class ProcessResultTests(unittest.TestCase):
    def test_lock_busy_is_not_started(self) -> None:
        self.assertTrue(ProcessResult(RCLONE_EXIT_LOCK_BUSY, "busy").not_started)
        self.assertFalse(ProcessResult(0).not_started)
        self.assertFalse(ProcessResult(1, "boom").not_started)
        self.assertFalse(ProcessResult(143, "stopped").not_started)


# ---------------------------------------------------------------------------
# 完成服务：exit 75 不落 success、不唤醒、回滚到 queued
# ---------------------------------------------------------------------------
class _RcloneRepository:
    def __init__(self, run: dict | None):
        self.run = run
        self.updates: list[tuple[int, dict]] = []
        self.scoped_status: list[tuple[int, str, dict]] = []
        self.events: list[tuple] = []

    def get_rclone_run(self, run_id: int) -> dict | None:
        return self.run

    def update_rclone_run(self, run_id: int, **fields) -> None:
        self.updates.append((run_id, fields))

    def mark_scoped_rclone_run_status(self, run_id: int, status: str, **fields) -> bool:
        self.scoped_status.append((run_id, status, fields))
        return True

    def add_rclone_event(self, *args, **kwargs) -> None:
        self.events.append(args)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CompletionServiceLockBusyTests(unittest.TestCase):
    def _service(self, run: dict | None):
        repo = _RcloneRepository(run)
        state = RcloneRunState()
        state.mark_running(int((run or {}).get("id") or 1), "2026-09-18T02:51:17Z")
        wakeups: list[int] = []
        logs: list[str] = []
        service = RcloneRunCompletionService(
            database=SimpleNamespace(rclone=repo),
            state=state,
            state_lock=_NullLock(),
            log=logs.append,
            now=lambda: "2026-09-18T02:51:18Z",
            transfer_wakeup=lambda job_id: wakeups.append(job_id) or {"woken": True},
        )
        return service, repo, state, wakeups, logs

    def test_scoped_run_is_requeued_and_transfer_is_not_woken(self) -> None:
        service, repo, state, wakeups, _logs = self._service(
            {"id": 8, "job_id": 9, "status": "running", "operation_key": "relay-dl:k9"}
        )

        service.finish(RCLONE_EXIT_LOCK_BUSY, "rclone 互斥锁被占用")

        self.assertEqual(wakeups, [])
        self.assertEqual(repo.updates, [], "延期不能走终态 update_rclone_run")
        self.assertEqual(len(repo.scoped_status), 1)
        run_id, status, _fields = repo.scoped_status[0]
        self.assertEqual((run_id, status), (8, "queued"))
        self.assertEqual(state.status, "idle")
        self.assertIsNone(state.current_run_id)
        self.assertEqual(state.last_exit_code, RCLONE_EXIT_LOCK_BUSY)
        self.assertTrue(any(event[1] == "warn" for event in repo.events))

    def test_unscoped_run_is_recorded_failed_without_wakeup(self) -> None:
        service, repo, _state, wakeups, _logs = self._service(
            {"id": 3, "status": "running", "operation_key": ""}
        )

        service.finish(RCLONE_EXIT_LOCK_BUSY, "rclone 互斥锁被占用")

        self.assertEqual(wakeups, [])
        self.assertEqual(repo.scoped_status, [])
        self.assertEqual(len(repo.updates), 1)
        self.assertEqual(repo.updates[0][1]["status"], RCLONE_RUN_FAILED)
        self.assertEqual(repo.updates[0][1]["exit_code"], RCLONE_EXIT_LOCK_BUSY)

    def test_cancelled_run_still_converges_to_cancelled(self) -> None:
        service, repo, _state, wakeups, _logs = self._service(
            {"id": 5, "job_id": 2, "status": "cancel_pending", "operation_key": "k"}
        )

        service.finish(RCLONE_EXIT_LOCK_BUSY, "busy")

        self.assertEqual(wakeups, [])
        self.assertEqual(repo.scoped_status, [])
        self.assertEqual(repo.updates[0][1]["status"], "cancelled")

    def test_success_still_wakes_transfer(self) -> None:
        service, _repo, _state, wakeups, _logs = self._service(
            {"id": 6, "job_id": 4, "status": "running", "operation_key": "k"}
        )

        service.finish(0, "")

        self.assertEqual(wakeups, [4])


# ---------------------------------------------------------------------------
# 服务层：延后重排、耗尽、作废
# ---------------------------------------------------------------------------
class _ServiceRepo:
    def __init__(self, run: dict | None):
        self.run = dict(run) if run else None
        self.scoped_status: list[tuple[int, str, dict]] = []
        self.events: list[tuple] = []

    def get_rclone_run(self, run_id: int):
        if self.run and int(self.run.get("id") or 0) == int(run_id):
            return dict(self.run)
        return None

    def mark_scoped_rclone_run_status(self, run_id: int, status: str, **fields) -> bool:
        self.scoped_status.append((run_id, status, fields))
        if self.run and int(self.run.get("id") or 0) == int(run_id):
            self.run["status"] = status
        return True

    def add_rclone_event(self, *args, **kwargs) -> None:
        self.events.append(args)


class _ImmediateTimer:
    """替代 threading.Timer：记录参数，不真的睡。"""

    instances: list["_ImmediateTimer"] = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False
        _ImmediateTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function(*self.args)


def _bare_service(repo: _ServiceRepo, *, max_attempts: int = 3) -> tuple[RcloneService, list[str], list[int]]:
    import threading

    service = object.__new__(RcloneService)
    service.db = SimpleNamespace(rclone=repo)
    service.lock = threading.RLock()
    service.config = {
        "staging_retry_delay_seconds": 5,
        "staging_retry_max_delay_seconds": 60,
        "staging_retry_max_attempts": max_attempts,
    }
    logs: list[str] = []
    service._append_log = logs.append
    service._deferred_run_timers = {}
    service._deferred_run_attempts = {}
    service._shutdown_requested = False
    wakeups: list[int] = []
    service.run_completion = SimpleNamespace(
        _wake_transfer_execution=lambda run_id, run: wakeups.append(int(run.get("job_id") or 0))
    )
    service.start_calls = []
    service.start_result = {"success": True}

    def _start(reason, staging_run=None, **kwargs):
        service.start_calls.append((reason, staging_run))
        return dict(service.start_result)

    service.start = _start
    return service, logs, wakeups


class DeferredRunSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        _ImmediateTimer.instances = []
        import fnos_media_import.services.rclone_service as module

        self._module = module
        self._original_timer = module.threading.Timer
        module.threading.Timer = _ImmediateTimer

    def tearDown(self) -> None:
        self._module.threading.Timer = self._original_timer

    def _run(self, status="queued") -> dict:
        return {"id": 8, "job_id": 9, "status": status, "operation_key": "relay-dl:k9"}

    def test_deferred_run_is_resubmitted_after_backoff(self) -> None:
        repo = _ServiceRepo(self._run())
        service, logs, wakeups = _bare_service(repo)
        descriptor = {"job_id": 9, "_scoped_run_id": 8, "_operation_key": "relay-dl:k9"}

        result = service._schedule_deferred_run(8, descriptor, "transfer:9:rclone_copy")

        self.assertTrue(result["deferred"])
        self.assertEqual(result["attempts"], 1)
        timer = _ImmediateTimer.instances[-1]
        self.assertTrue(timer.started)
        self.assertEqual(timer.interval, 5)
        self.assertEqual(wakeups, [])

        timer.fire()

        self.assertEqual(len(service.start_calls), 1)
        reason, staging_run = service.start_calls[0]
        self.assertTrue(reason.startswith("lock_deferred:8:"))
        self.assertEqual(staging_run, descriptor)
        self.assertEqual(repo.scoped_status, [], "重排成功不应改 run 状态")
        self.assertTrue(any("重新排队" in line for line in logs))

    def test_backoff_grows_and_exhaustion_fails_run_and_wakes(self) -> None:
        repo = _ServiceRepo(self._run())
        service, _logs, wakeups = _bare_service(repo, max_attempts=2)
        descriptor = {"job_id": 9, "_scoped_run_id": 8}

        first = service._schedule_deferred_run(8, descriptor, "r")
        second = service._schedule_deferred_run(8, descriptor, "r")
        third = service._schedule_deferred_run(8, descriptor, "r")

        self.assertEqual((first["delay_seconds"], second["delay_seconds"]), (5, 10))
        self.assertTrue(third.get("exhausted"))
        self.assertEqual(repo.run["status"], RCLONE_RUN_FAILED)
        self.assertEqual(wakeups, [9])
        # 前一个定时器被替换时应取消，避免重复提交。
        self.assertTrue(_ImmediateTimer.instances[0].cancelled)

    def test_resume_skips_when_run_no_longer_queued(self) -> None:
        repo = _ServiceRepo(self._run(status="cancelled"))
        service, logs, _wakeups = _bare_service(repo)

        service._resume_deferred_run(8, {"job_id": 9}, "r")

        self.assertEqual(service.start_calls, [])
        self.assertTrue(any("跳过延后重排" in line for line in logs))

    def test_resume_failure_fails_run_and_wakes_transfer(self) -> None:
        repo = _ServiceRepo(self._run())
        service, _logs, wakeups = _bare_service(repo)
        service.start_result = {"success": False, "message": "rclone 集成未启用"}

        service._resume_deferred_run(8, {"job_id": 9}, "r")

        self.assertEqual(repo.run["status"], RCLONE_RUN_FAILED)
        self.assertEqual(wakeups, [9])

    def test_shutdown_cancels_pending_deferred_timers(self) -> None:
        repo = _ServiceRepo(self._run())
        service, _logs, _wakeups = _bare_service(repo)
        service._schedule_deferred_run(8, {"job_id": 9}, "r")

        service._cancel_all_deferred_runs()

        self.assertTrue(_ImmediateTimer.instances[-1].cancelled)
        self.assertEqual(service._deferred_run_attempts, {})


class InvalidateRunTests(unittest.TestCase):
    def _service(self, status: str):
        repo = _ServiceRepo({"id": 8, "job_id": 9, "status": status, "operation_key": "relay-dl:k9"})
        service, logs, _wakeups = _bare_service(repo)
        return service, repo, logs

    def test_success_run_becomes_failed(self) -> None:
        service, repo, logs = self._service("success")

        result = service.invalidate_run(8, operation_key="relay-dl:k9", reason="中继目录为空")

        self.assertTrue(result["success"])
        self.assertEqual(repo.run["status"], RCLONE_RUN_FAILED)
        self.assertEqual(repo.scoped_status[0][2]["error_message"], "中继目录为空")
        self.assertTrue(repo.scoped_status[0][2]["terminal"])
        self.assertTrue(any("已作废" in line for line in logs))

    def test_running_run_is_never_invalidated(self) -> None:
        service, repo, _logs = self._service("running")

        result = service.invalidate_run(8, operation_key="relay-dl:k9")

        self.assertFalse(result["success"])
        self.assertEqual(repo.run["status"], "running")

    def test_operation_key_mismatch_is_rejected(self) -> None:
        service, repo, _logs = self._service("success")

        result = service.invalidate_run(8, operation_key="other")

        self.assertFalse(result["success"])
        self.assertEqual(repo.run["status"], "success")

    def test_already_failed_is_idempotent(self) -> None:
        service, repo, _logs = self._service("failed")

        result = service.invalidate_run(8)

        self.assertTrue(result["success"])
        self.assertTrue(result["already"])
        self.assertEqual(repo.scoped_status, [])


# ---------------------------------------------------------------------------
# 中继执行器：completed 下载回执降级 + 空目录作废
# ---------------------------------------------------------------------------
class _ReconcilingDownloadAdapter:
    """模拟适配器对账到历史 success run：download 直接返回 completed 回执。"""

    def __init__(self, *, events: _EventLog, download_status: str = "completed") -> None:
        self.events = events
        self.download_status = download_status
        self.invalidated: list[tuple[OperationReceipt, str]] = []

    def download(self, *, source, local_dir, idempotency_key):
        self.events.add("relay_download")
        return OperationReceipt(
            operation_id="rclone:1:8",
            status="completed",
            provider_kind="rclone",
            message="rclone copy 已完成",
            metadata={"run_id": "8", "operation_key": idempotency_key},
        )

    def check(self, receipt):
        self.events.add("relay_download_check")
        return DownloadStatus(self.download_status)

    def invalidate(self, receipt, reason=""):
        self.invalidated.append((receipt, reason))
        return True


class _EmptyDirUploader(_FakeUploader):
    """第一次上传报中继目录为空，之后成功。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.attempts = 0

    def upload_local_dir(self, *, local_dir, target_locator, idempotency_key, progress=None):
        self.attempts += 1
        if self.events is not None:
            self.events.add("cmcc_upload")
        if self.attempts == 1:
            return UploadOutcome(
                "failed",
                f"中继目录为空或不存在：{local_dir}",
                error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE,
            )
        return UploadOutcome("done")


class ProviderRelayReconciledDownloadTests(unittest.TestCase):
    def _orchestrator(self, adapter, uploader, events):
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_入库暂存/job-1",
            final="/移动云/电视剧",
            local_temp="/data/relay/job-1",
        )
        executor = ProviderRelayExecutor(
            adapter,
            uploader,
            source_materializer=_FakeMaterializer(events=events),
            visibility_poller=_visibility_poller(VisibilityStatus.visible, events),
            relay_root="/data/relay",
        )
        return TransferOrchestrator(
            plan,
            {"relay_upload_provider": executor},
            verifier=TransferVerifier(target_lister=_FakeTargetLister(events=events)),
            cleanup=_FakeCleanup(events=events),
            ack_service=_FakeAck(events=events),
            finalizer_service=_FakeFinalizer(),
        ), executor

    def test_submit_downgrades_completed_download_receipt_to_running(self) -> None:
        events = _EventLog()
        adapter = _ReconcilingDownloadAdapter(events=events)
        _orch, executor = self._orchestrator(adapter, _FakeUploader(events=events), events)
        executor.bind(_orch.plan)

        receipt = executor.submit()

        self.assertEqual(receipt.status, "running")
        self.assertEqual(receipt.metadata["step"], "download")
        self.assertEqual(receipt.metadata["run_id"], "8")
        self.assertEqual(receipt.message, "rclone copy 已完成")

    def test_reconciled_completed_download_still_uploads(self) -> None:
        events = _EventLog()
        adapter = _ReconcilingDownloadAdapter(events=events)
        uploader = _FakeUploader(events=events)
        orch, _executor = self._orchestrator(adapter, uploader, events)

        _drive(orch)

        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        order = events.names()
        self.assertIn("cmcc_upload", order, "对账到已完成下载后仍必须上传")
        self.assertLess(order.index("relay_download"), order.index("cmcc_upload"))
        self.assertLess(order.index("cmcc_upload"), order.index("cmcc_verify"))
        self.assertEqual(len(uploader.calls), 1)

    def test_empty_relay_dir_invalidates_download_run_then_retries_to_completion(self) -> None:
        events = _EventLog()
        adapter = _ReconcilingDownloadAdapter(events=events)
        uploader = _EmptyDirUploader(events=events)
        orch, _executor = self._orchestrator(adapter, uploader, events)

        _drive(orch)

        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertEqual(len(adapter.invalidated), 1)
        receipt, reason = adapter.invalidated[0]
        self.assertEqual(receipt.metadata["run_id"], "8")
        self.assertIn("中继目录为空", reason)
        self.assertEqual(uploader.attempts, 2)
        # 作废后重试重新走下载 -> 上传，而不是直接进目标校验。
        self.assertEqual(events.names().count("relay_download"), 2)

    def test_other_upload_failures_do_not_invalidate_download(self) -> None:
        events = _EventLog()
        adapter = _ReconcilingDownloadAdapter(events=events)
        uploader = _FakeUploader(status="failed", events=events)
        orch, _executor = self._orchestrator(adapter, uploader, events)

        _drive(orch)

        self.assertEqual(adapter.invalidated, [])


class RcloneAdapterInvalidateTests(unittest.TestCase):
    class _Service:
        def __init__(self, result=None, error: Exception | None = None) -> None:
            self.result = result if result is not None else {"success": True}
            self.error = error
            self.calls: list[tuple] = []

        def invalidate_run(self, run_id, *, operation_key="", reason=""):
            self.calls.append((run_id, operation_key, reason))
            if self.error is not None:
                raise self.error
            return self.result

    def _receipt(self, **metadata) -> OperationReceipt:
        return OperationReceipt(
            operation_id="rclone:1:8",
            status="completed",
            provider_kind="rclone",
            metadata={"run_id": "8", "operation_key": "relay-dl:k", **metadata},
        )

    def test_forwards_run_id_operation_key_and_reason(self) -> None:
        service = self._Service()
        adapter = RcloneOperationAdapter(service, None).bind(SimpleNamespace(job_id=1))

        ok = adapter.invalidate(self._receipt(), "中继目录为空")

        self.assertTrue(ok)
        self.assertEqual(service.calls, [(8, "relay-dl:k", "中继目录为空")])

    def test_missing_run_id_or_service_method_returns_false(self) -> None:
        adapter = RcloneOperationAdapter(self._Service(), None).bind(SimpleNamespace(job_id=1))
        bare = OperationReceipt(operation_id="x", status="completed", metadata={})
        self.assertFalse(adapter.invalidate(bare))

        legacy = RcloneOperationAdapter(SimpleNamespace(), None).bind(SimpleNamespace(job_id=1))
        self.assertFalse(legacy.invalidate(self._receipt()))

    def test_service_error_is_swallowed(self) -> None:
        service = self._Service(error=RuntimeError("db down"))
        adapter = RcloneOperationAdapter(service, None).bind(SimpleNamespace(job_id=1))

        self.assertFalse(adapter.invalidate(self._receipt()))


if __name__ == "__main__":
    unittest.main()
