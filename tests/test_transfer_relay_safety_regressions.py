from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.constants import RCLONE_EXIT_LOCK_BUSY
from fnos_media_import.importers.tianyi import TianyiApiError
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.repositories.rclone_repository import RcloneRepository
from fnos_media_import.services.rclone_process_runner import (
    LEGACY_LOCK_BUSY_MESSAGE,
    ProcessResult,
    RcloneProcessRunner,
)
from fnos_media_import.services.rclone_run_completion_service import RcloneRunCompletionService
from fnos_media_import.services.rclone_run_state import RcloneRunState
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer import (
    CancelResult,
    ErrorCode,
    PHASE_CANCELLED,
    PHASE_CANCEL_PENDING,
    PHASE_ORGANIZING,
    PHASE_TARGET_VERIFYING,
    PHASE_TRANSFERRING,
    TransferOrchestrator,
    TransferPlan,
)
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor
from fnos_media_import.transfer.dispatch import TransferDispatchService, _OrganizerAdapter
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.rclone_adapter import RcloneOperationAdapter
from fnos_media_import.transfer.tianyi_uploader import TianyiCloudUploader
from tests.test_rclone_lock_busy_and_relay_reconcile import (
    _bare_service,
    _EmptyDirUploader,
    _ImmediateTimer,
    _ServiceRepo,
)
from tests.test_rclone_managed_descriptor_v1 import _MemoryDatabase, _plan as managed_plan, _relay_plan, _resolver
from tests.test_tianyi_upload_v1 import _FakeDriveImporter
from tests.test_transfer_v1 import _FakeDownloadAdapter, _FakeUploader, make_plan


ROOT = Path(__file__).resolve().parents[1]


class RelayCancellationTests(unittest.TestCase):
    def _orchestrator(self, download_status: str = "completed"):
        adapter = _FakeDownloadAdapter(status=download_status)
        adapter.cancel = Mock(return_value=CancelResult("cancel_pending"))
        uploader = _FakeUploader()
        executor = ProviderRelayExecutor(adapter, uploader, relay_root="/data/relay")
        plan = make_plan(
            source_kind="quark", dest_kind="mobile", strategy="relay_upload_provider",
            landing="/landing/job-1", final="/target", local_temp="/data/relay/job-1",
        )
        orch = TransferOrchestrator(plan, {"relay_upload_provider": executor})
        executor.bind(orch.plan)
        orch.state.executor_strategy = "relay_upload_provider"
        orch.state.phase = PHASE_TRANSFERRING
        orch.state.receipt = executor.submit()
        return orch, adapter, uploader

    def test_direct_cancel_after_download_does_not_start_upload(self):
        orch, adapter, uploader = self._orchestrator()
        result = orch.cancel(direct=True)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(orch.state.phase, PHASE_CANCELLED)
        self.assertEqual(uploader.calls, [])
        adapter.cancel.assert_not_called()

    def test_pending_cancel_after_download_does_not_start_upload(self):
        orch, _adapter, uploader = self._orchestrator()
        orch.state.phase = PHASE_CANCEL_PENDING
        orch.step()
        self.assertEqual(orch.state.phase, PHASE_CANCELLED)
        self.assertEqual(uploader.calls, [])

    def test_active_or_unknown_download_is_not_confirmed_cancelled(self):
        for status in ("running", "unknown"):
            with self.subTest(status=status):
                orch, adapter, uploader = self._orchestrator(status)
                result = orch.cancel(direct=True)
                self.assertEqual(result.status, "cancel_pending")
                self.assertEqual(orch.state.phase, PHASE_CANCEL_PENDING)
                self.assertEqual(uploader.calls, [])
                adapter.cancel.assert_called_once()

    def test_unknown_upload_cancellation_stays_pending(self):
        orch, _adapter, uploader = self._orchestrator()
        orch.state.receipt.metadata.update(step="upload", upload_outcome={"status": "unknown"})
        result = orch.cancel(direct=True)
        self.assertEqual(result.status, "cancel_pending")
        self.assertEqual(uploader.calls, [])

    def test_completed_upload_cancellation_preserves_external_artifacts(self):
        orch, _adapter, uploader = self._orchestrator()
        orch.state.receipt.metadata.update(step="upload", upload_outcome={"status": "done"})
        result = orch.cancel(direct=True)
        self.assertEqual(result.status, "cancelled")
        # Dispatch uses this marker to avoid deleting completed external work.
        self.assertIn("外部操作已完成", result.message)
        self.assertEqual(uploader.calls, [])

    def test_restored_completed_download_still_uploads_before_verification(self):
        orch, _adapter, uploader = self._orchestrator()
        snapshot = orch.snapshot()
        snapshot["receipt"]["status"] = "completed"
        orch.restore(snapshot)
        orch.step()
        self.assertEqual(len(uploader.calls), 1)
        self.assertEqual(orch.state.phase, PHASE_TARGET_VERIFYING)


class TianyiPreUploadRetryTests(unittest.TestCase):
    target = {"kind": "folder_id", "path": "root"}

    def test_folder_lookup_failure_remains_safely_retryable(self):
        importer = _FakeDriveImporter()
        importer.resolve_folder_locator = Mock(side_effect=[
            TianyiApiError("temporary folder lookup outage", code="HTTP_503"), "root",
        ])
        uploader = TianyiCloudUploader(importer)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "movie.mkv").write_bytes(b"x")
            kwargs = dict(local_dir=root, target_locator=self.target, idempotency_key="relay-up:test")
            first = uploader.upload_local_dir(**kwargs)
            manifest_path = uploader._operation_manifest_path(root, "relay-up:test")
            self.assertEqual(uploader._read_operation_manifest(manifest_path)["status"], "prepared")
            self.assertEqual(importer.upload_calls, [])
            second = uploader.upload_local_dir(**kwargs)
        self.assertEqual((first.status, first.error_code), ("failed", ErrorCode.NETWORK_TRANSIENT))
        self.assertEqual(second.status, "done")
        self.assertEqual(len(importer.upload_calls), 1)

    def test_subfolder_failure_retries_without_reuploading_completed_file(self):
        importer = _FakeDriveImporter()
        resolve = importer._ensure_folder_path
        importer._ensure_folder_path = Mock(side_effect=[
            TianyiApiError("temporary subfolder outage", code="HTTP_503"),
            "nested",
        ])
        importer.items["nested"] = []
        uploader = TianyiCloudUploader(importer)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.mkv").write_bytes(b"a")
            Path(root, "nested").mkdir()
            Path(root, "nested", "b.mkv").write_bytes(b"b")
            kwargs = dict(local_dir=root, target_locator=self.target, idempotency_key="relay-up:partial")
            first = uploader.upload_local_dir(**kwargs)
            self.assertEqual(len(importer.upload_calls), 1)
            second = uploader.upload_local_dir(**kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "done")
        self.assertEqual([item["target_name"] for item in importer.upload_calls], ["a.mkv", "b.mkv"])
        importer._ensure_folder_path = resolve

    def test_uncertain_upload_keeps_crash_fence_and_is_not_resubmitted(self):
        importer = _FakeDriveImporter()
        importer.upload_file = Mock(side_effect=RuntimeError("connection lost after submission"))
        uploader = TianyiCloudUploader(importer)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "movie.mkv").write_bytes(b"x")
            kwargs = dict(local_dir=root, target_locator=self.target, idempotency_key="relay-up:unknown")
            first = uploader.upload_local_dir(**kwargs)
            second = uploader.upload_local_dir(**kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual((second.status, second.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
        importer.upload_file.assert_called_once()


class DeferredRunRecoveryTests(unittest.TestCase):
    descriptor = {"job_id": 9, "_scoped_run_id": 8, "_operation_key": "relay-dl:test"}

    def setUp(self):
        _ImmediateTimer.instances = []
        self.timer_patch = patch("fnos_media_import.services.rclone_service.threading.Timer", _ImmediateTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.repo = _ServiceRepo({"id": 8, "job_id": 9, "status": "queued", "operation_key": "relay-dl:test"})
        self.service, self.logs, self.wakeups = _bare_service(self.repo, max_attempts=2)

    def _schedule(self):
        self.service._schedule_deferred_run(8, self.descriptor, "transfer:9:rclone_copy")

    def test_timer_exception_is_rescheduled_and_recovers(self):
        self.service.start = Mock(side_effect=[sqlite3.OperationalError("database is locked"), {"success": True}])
        self._schedule()
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self.repo.run["status"], "queued")
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self.service.start.call_count, 2)
        self.assertEqual(self.service._deferred_run_timers, {})
        self.assertEqual(self.wakeups, [])

    def test_transient_status_read_error_does_not_drop_timer(self):
        read = self.repo.get_rclone_run
        self.repo.get_rclone_run = Mock(side_effect=[sqlite3.OperationalError("database is locked"), read(8)])
        self._schedule()
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(self.service.start_calls), 1)

    def test_repeated_errors_have_bounded_starts_and_wake_transfer(self):
        self.service.start = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        self._schedule()
        _ImmediateTimer.instances[-1].fire()
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self.service.start.call_count, 2)
        self.assertEqual(self.repo.run["status"], "failed")
        self.assertEqual(self.wakeups, [9])
        self.assertEqual(self.service._deferred_run_timers, {})
        self.assertEqual(self.service._deferred_run_attempts, {})

    def test_failed_terminal_write_is_retried_without_another_start(self):
        self.service.config["staging_retry_max_attempts"] = 1
        self.service.start = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        mark = self.repo.mark_scoped_rclone_run_status
        calls = 0

        def mark_after_recovery(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return mark(*args, **kwargs)

        self.repo.mark_scoped_rclone_run_status = mark_after_recovery
        self._schedule()
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        self.assertEqual(self.wakeups, [])
        _ImmediateTimer.instances[-1].fire()
        self.service.start.assert_called_once()
        self.assertEqual(self.repo.run["status"], "failed")
        self.assertEqual(self.wakeups, [9])
        self.assertEqual(self.service._deferred_run_timers, {})

    def test_terminal_handoff_never_overwrites_cancellation(self):
        self.repo.run["status"] = "cancelled"
        self.service.config["staging_retry_max_attempts"] = 0
        self._schedule()
        self.assertEqual(self.repo.run["status"], "cancelled")
        self.assertEqual(self.wakeups, [])
        self.assertEqual(self.repo.scoped_status, [])

    def test_real_completion_clears_lock_attempt_budget(self):
        self._schedule()
        timer = _ImmediateTimer.instances[-1]
        self.service.run_state = RcloneRunState()
        self.service.run_state.mark_running(8, "now")
        self.service.run_completion = Mock()
        self.service._finish(0, "")
        self.assertEqual(self.service._deferred_run_attempts, {})
        self.assertEqual(self.service._deferred_run_timers, {})
        self.assertTrue(timer.cancelled)


class LegacyWorkerCompatibilityTests(unittest.TestCase):
    def _run(self, lines: str, code: int):
        process = SimpleNamespace(stdout=io.StringIO(lines), wait=lambda: code)
        with patch("fnos_media_import.services.rclone_process_runner.subprocess.Popen", return_value=process):
            return RcloneProcessRunner().run(["sh", "worker"], cwd=ROOT, env={}, on_line=lambda _: None, on_started=lambda _: None)

    def test_old_lock_skip_exit_zero_is_deferred(self):
        result = self._run(f"[2026-10-09 10:00:00] {LEGACY_LOCK_BUSY_MESSAGE}\n", 0)
        self.assertEqual(result.exit_code, RCLONE_EXIT_LOCK_BUSY)
        self.assertTrue(result.not_started)

    def test_normal_success_and_real_errors_are_not_reclassified(self):
        self.assertEqual(self._run("copy complete\n", 0).exit_code, 0)
        self.assertEqual(self._run(LEGACY_LOCK_BUSY_MESSAGE, 1).exit_code, 1)

    def test_new_script_has_explicit_empty_relay_failure(self):
        script = (ROOT / "scripts" / "fnos_rclone_worker.sh").read_text(encoding="utf-8")
        empty_branch = script.split('if [ -z "$copy_file_list" ]; then', 1)[1].split('log "rclone_copy 源文件数', 1)[0]
        self.assertIn('"$RCLONE_COPY_RELAY_MODE" = "true"', empty_branch)
        self.assertIn("return 1", empty_branch)
        self.assertIn('SCRIPT_VERSION="v1.3-sequential-relay"', script)

    @unittest.skipUnless(os.name == "posix" and shutil.which("flock"), "requires Linux flock runtime")
    def test_busy_lock_is_exit_75_and_released_lock_is_reusable(self):
        import fcntl

        with tempfile.TemporaryDirectory() as root:
            lock = Path(root, "worker.lock")
            env = {**os.environ, "LOCK_FILE": str(lock), "RCLONE_COPY_MODE": "false"}
            command = ["sh", str(ROOT / "scripts" / "fnos_rclone_worker.sh")]
            with lock.open("w") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = subprocess.run(command, env=env, capture_output=True, timeout=10)
            released = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertEqual(busy.returncode, RCLONE_EXIT_LOCK_BUSY)
            # Reaches plan validation after acquiring the released lock; no provider call.
            self.assertEqual(released.returncode, 2)


class DurableRcloneSchedulingRaceTests(unittest.TestCase):
    descriptor = {"job_id": 9, "_scoped_run_id": 8, "_operation_key": "relay-dl:test"}

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE rclone_runs (
                id INTEGER PRIMARY KEY, operation_key TEXT, job_id INTEGER, status TEXT,
                started_at TEXT, updated_at TEXT, finished_at TEXT, exit_code INTEGER, error_message TEXT
            );
            CREATE TABLE rclone_events (
                id INTEGER PRIMARY KEY, run_id INTEGER, level TEXT, message TEXT, raw_data TEXT, created_at TEXT
            );
            INSERT INTO rclone_runs (id, operation_key, job_id, status) VALUES (8, 'relay-dl:test', 9, 'queued');
        """)
        self.repo = RcloneRepository(lambda: self.connection, lambda row: dict(row) if row else None)
        self.service, self.logs, self.wakeups = _bare_service(self.repo, max_attempts=2)
        self.service.run_state = RcloneRunState()
        self.service.run_completion = RcloneRunCompletionService(
            database=self.service.db, state=self.service.run_state, state_lock=self.service.lock,
            log=self.logs.append, now=lambda: "now", transfer_wakeup=self.wakeups.append,
        )
        self.service._now = lambda: "now"
        self.service.base_dir = ROOT
        self.service._script_path = lambda: ROOT / "scripts" / "fnos_rclone_worker.sh"
        self.service._command = lambda _path: ["unused-mocked-worker"]
        self.service._env = lambda **_kwargs: {}
        self.service._remote_dir = lambda _value: ""
        self.service._ensure_managed_remote_config = Mock()
        self.service.process_runner = SimpleNamespace(run=Mock(return_value=ProcessResult(RCLONE_EXIT_LOCK_BUSY, "busy")))
        self.service._start_next_queued_run = Mock()
        _ImmediateTimer.instances = []
        timer_patch = patch("fnos_media_import.services.rclone_service.threading.Timer", _ImmediateTimer)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)

    def _set_status(self, status):
        self.connection.execute("UPDATE rclone_runs SET status=? WHERE id=8", (status,))
        self.connection.commit()

    def _status(self):
        return self.connection.execute("SELECT status FROM rclone_runs WHERE id=8").fetchone()[0]

    def test_initial_busy_completion_read_failure_retains_scheduler(self):
        get_run = self.repo.get_rclone_run
        reads = 0

        def transient_read(run_id):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise sqlite3.OperationalError("database is locked")
            return get_run(run_id)

        self.repo.get_rclone_run = transient_read
        self.service._run_script("test", staging_run=self.descriptor)
        self.assertEqual(self._status(), "running")
        self.assertIsNone(self.service.run_state.current_run_id)
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "queued")
        self.assertEqual(len(self.service.start_calls), 1)
        self.assertEqual(self.wakeups, [])

    def test_initial_busy_completion_write_failure_retains_scheduler(self):
        mark = self.repo.mark_scoped_rclone_run_status
        writes = 0

        def transient_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise sqlite3.OperationalError("database is locked")
            return mark(*args, **kwargs)

        self.repo.mark_scoped_rclone_run_status = transient_write
        self.service._run_script("test", staging_run=self.descriptor)
        self.assertEqual(self._status(), "running")
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "queued")
        self.assertEqual(len(self.service.start_calls), 1)
        self.assertEqual(self.wakeups, [])

    def test_busy_audit_failure_does_not_prevent_requeue(self):
        self.repo.add_rclone_event = Mock(side_effect=sqlite3.OperationalError("event write failed"))
        self.service._run_script("test", staging_run=self.descriptor)
        self.assertEqual(self._status(), "queued")
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(self.service.start_calls), 1)
        self.assertEqual(self.wakeups, [])

    def test_async_claim_exception_recovers_without_starting_process(self):
        mark_started = self.repo.mark_scoped_rclone_run_started

        def ambiguous_claim(run_id, **identity):
            mark_started(run_id, **identity)
            raise sqlite3.OperationalError("claim result lost")

        self.repo.mark_scoped_rclone_run_started = ambiguous_claim
        self.service._run_script("test", staging_run=self.descriptor)
        self.service.process_runner.run.assert_not_called()
        self.service._start_next_queued_run.assert_called_once()
        self.assertEqual(len(self.service._deferred_run_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "queued")
        self.assertEqual(len(self.service.start_calls), 1)
        self.assertEqual(self.wakeups, [])

    def test_cancellation_between_busy_read_and_write_cannot_be_requeued(self):
        self._set_status("running")
        self.service.run_state.mark_running(8, "now")
        mark = self.repo.mark_scoped_rclone_run_status

        def cancel_before_requeue(*args, **kwargs):
            self._set_status("cancel_pending")
            return mark(*args, **kwargs)

        self.repo.mark_scoped_rclone_run_status = cancel_before_requeue
        self.service.run_completion.finish(RCLONE_EXIT_LOCK_BUSY, "busy")
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])

    def test_cancellation_between_exhaustion_read_and_write_is_preserved(self):
        mark = self.repo.mark_scoped_rclone_run_status

        def cancel_before_failure(*args, **kwargs):
            self._set_status("cancelled")
            return mark(*args, **kwargs)

        self.repo.mark_scoped_rclone_run_status = cancel_before_failure
        self.service._fail_deferred_run(8, "exhausted")
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])

    def test_cancellation_between_completion_read_and_write_wins(self):
        self._set_status("running")
        self.service.run_state.mark_running(8, "now")
        update = self.repo.update_rclone_run

        def cancel_before_completion(*args, **kwargs):
            if kwargs.get("expected_status") == "running":
                self._set_status("cancel_pending")
            return update(*args, **kwargs)

        self.repo.update_rclone_run = cancel_before_completion
        self.service.run_completion.finish(0, "")
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])

    def test_cancellation_intent_is_durable_before_process_stop(self):
        self._set_status("running")
        self.service.run_state.mark_running(8, "now")

        def stop(job_id, *, stop_running):
            self.assertEqual((job_id, stop_running), (9, True))
            self.assertEqual(self._status(), "cancel_pending")
            self.service.run_completion.finish(143, "stopped immediately")
            return {"active_match": True}

        self.service.cancel_job = stop
        self.service.cancel_run(8, operation_key="relay-dl:test")
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])


class DurableRelayReconcileTests(unittest.TestCase):
    def test_empty_historical_success_retries_same_run_and_frozen_operation_key(self):
        plan = _relay_plan()
        plan["paths"].pop("destination_staging", None)
        plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
        attach_plan_integrity(plan)
        database = _MemoryDatabase(plan)
        self.addCleanup(database.connection.close)
        database.connection.executescript("""
            CREATE TABLE rclone_runs (
                id INTEGER PRIMARY KEY, trigger_reason TEXT, operation_key TEXT UNIQUE,
                job_id INTEGER, copy_descriptor_json TEXT, status TEXT, started_at TEXT,
                updated_at TEXT, finished_at TEXT, exit_code INTEGER, error_message TEXT DEFAULT ''
            );
            CREATE TABLE rclone_events (
                id INTEGER PRIMARY KEY, run_id INTEGER, level TEXT, message TEXT, raw_data TEXT, created_at TEXT
            );
        """)
        database.rclone = RcloneRepository(database.connect, lambda row: dict(row) if row else None)
        service = object.__new__(RcloneService)
        service.db = database
        service._v1_locator_resolver = _resolver()
        service._append_log = Mock()
        service.start = Mock(return_value={"success": True, "queued": True})
        frozen = TransferPlan.from_dict(plan)
        adapter = RcloneOperationAdapter(service, service._v1_locator_resolver).bind(frozen)
        key = f"relay-dl:{frozen.idempotency_key}"
        original = adapter.download(source=frozen.source_landing, local_dir=frozen.local_temp["path"], idempotency_key=key)
        run_id = int(original.metadata["run_id"])
        database.rclone.update_rclone_run(run_id, status="success", exit_code=0)
        uploader = _EmptyDirUploader()
        executor = ProviderRelayExecutor(adapter, uploader, relay_root="/data/relay").bind(frozen)
        stale = executor.submit()
        self.assertEqual(stale.status, "running")
        migrating = executor.poll(stale)
        self.assertEqual(migrating.status, "running")
        self.assertEqual(database.rclone.get_rclone_run(run_id)["status"], "failed")
        # The task fence stays fixed; only the nested per-file receipt changes.
        executor.poll(stale)
        nested = stale.metadata["relay_file_receipt"]
        self.assertEqual(int(nested["metadata"]["run_id"]), run_id)
        self.assertEqual(stale.metadata["operation_key"], key)
        self.assertEqual(database.rclone.get_rclone_run(run_id)["status"], "queued")
        self.assertEqual(service.start.call_count, 2)
        self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM rclone_runs").fetchone()[0], 1)
        self.assertEqual(uploader.attempts, 0, "No upload is allowed before the worker confirms a file ledger")


class CleanupOnlyOrganizerContractTests(unittest.TestCase):
    def _database(self, evidence, *, status="done"):
        plan = managed_plan()
        plan["postprocess"]["organizer_enabled"] = True
        plan["paths"]["organizer"] = dict(plan["paths"]["destination_final"])
        plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
        attach_plan_integrity(plan)
        database = _MemoryDatabase(plan)
        self.addCleanup(database.connection.close)
        database.job["status"] = "organizing"
        database.connection.executescript("""
            CREATE TABLE organizer_tasks (id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT, error_message TEXT, evidence TEXT);
            CREATE TABLE organizer_mappings (task_id INTEGER, target_path TEXT, status TEXT);
            UPDATE import_jobs SET status='organizing';
        """)
        database.connection.execute(
            "INSERT INTO organizer_tasks VALUES (1, 7, ?, '', ?)", (status, json.dumps(evidence))
        )
        database.connection.commit()
        return database, plan

    def test_only_completed_task_can_confirm_cleanup_only(self):
        for evidence, status, expected in (
            ({"cleanup_only": True}, "done", True),
            ({"completion_confirmation": {"cleanup_only": True, "success": True}}, "done", True),
            ({"cleanup_only": True}, "waiting_review", False),
            ({"cleanup_only": "false"}, "done", False),
            ({"completion_confirmation": {"cleanup_only": True, "success": False}}, "done", False),
        ):
            with self.subTest(evidence=evidence, status=status):
                database, raw = self._database(evidence, status=status)
                result = _OrganizerAdapter(database, Mock()).organize(TransferPlan.from_dict(raw))
                self.assertEqual(result.cleanup_only, expected)
                if expected:
                    self.assertEqual(result.status, "done")
                    self.assertEqual(result.final_path, "")

    def test_cleanup_only_is_persisted_terminal_without_ack_or_extra_cleanup(self):
        database, plan = self._database({"cleanup_only": True})

        def project_job(connection, job_id, excluded, **fields):
            row = connection.execute("SELECT status FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            if row["status"] in excluded:
                return False
            connection.execute(
                "UPDATE import_jobs SET status=?, error_message=? WHERE id=?",
                (fields["status"], fields["error_message"], job_id),
            )
            database.job["status"] = fields["status"]
            return True

        database.job_commands = SimpleNamespace(update_unless_status_with_connection=project_job)
        snapshot = {"phase": PHASE_ORGANIZING, "executor_strategy": "rclone_copy", "staging_verified": True, "organizer_task_id": 1}
        database.connection.execute(
            "UPDATE transfer_executions SET phase=?, snapshot_json=?", (PHASE_ORGANIZING, json.dumps(snapshot))
        )
        database.connection.commit()
        verifier, cleanup, ack, finalizer = Mock(), Mock(), Mock(), Mock()
        organizer_service = Mock()
        orch = TransferOrchestrator(
            plan, {}, verifier=verifier, cleanup=cleanup, ack_service=ack,
            finalizer_service=finalizer, organizer_service=_OrganizerAdapter(database, organizer_service),
        )
        dispatch = object.__new__(TransferDispatchService)
        dispatch.database = database
        dispatch.executions = TransferExecutionRepository(database)
        dispatch.execution_lease_seconds = 60
        dispatch._orchestrator = lambda _plan: orch
        dispatch._validation_deps = lambda: {}
        dispatch._claim_heartbeat = lambda *_args: nullcontext()
        with patch("fnos_media_import.transfer.dispatch.validate_plan"):
            outcome = dispatch.handle_worker({"job_id": 7}, {"id": 1, "owner_id": "cleanup-only-test"})
        self.assertEqual(outcome["worker_outcome"], "completed")
        self.assertEqual(outcome["phase"], PHASE_CANCELLED)
        persisted = dispatch.executions.get(7)
        self.assertEqual(persisted.phase, PHASE_CANCELLED)
        self.assertFalse(persisted.app_acked)
        self.assertFalse(persisted.final_verified)
        self.assertTrue(persisted.snapshot["cleanup"]["cleanup_only"])
        self.assertEqual(database.connection.execute("SELECT status FROM import_jobs WHERE id=7").fetchone()[0], "cancelled")
        self.assertEqual(dispatch.executions.list_due(), [])
        for side_effect in (verifier, cleanup, ack, finalizer, organizer_service):
            self.assertEqual(side_effect.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
