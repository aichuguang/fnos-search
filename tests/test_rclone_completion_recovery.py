from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.repositories.rclone_repository import RcloneRepository
from fnos_media_import.services.rclone_process_runner import ProcessResult
from fnos_media_import.services.rclone_run_completion_service import RcloneRunCompletionService
from fnos_media_import.services.rclone_run_state import RcloneRunState
from tests.test_rclone_lock_busy_and_relay_reconcile import _bare_service, _ImmediateTimer


ROOT = Path(__file__).resolve().parents[1]


class _RcloneRecoveryFixture:
    descriptor = {"job_id": 9, "_scoped_run_id": 8, "_operation_key": "relay-dl:test"}

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE rclone_runs (
                id INTEGER PRIMARY KEY, operation_key TEXT, job_id INTEGER, status TEXT,
                copy_descriptor_json TEXT, started_at TEXT, updated_at TEXT, finished_at TEXT,
                exit_code INTEGER, error_message TEXT
            );
            CREATE TABLE rclone_events (
                id INTEGER PRIMARY KEY, run_id INTEGER, level TEXT, message TEXT, raw_data TEXT, created_at TEXT
            );
            INSERT INTO rclone_runs (id, operation_key, job_id, status)
                VALUES (8, 'relay-dl:test', 9, 'queued');
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
        self.service.process_runner = SimpleNamespace(run=Mock(return_value=ProcessResult(0)))
        self.service._start_next_queued_run = Mock()
        self.service.run_queue = SimpleNamespace(request_stop=Mock())
        self.service.log_sink = SimpleNamespace(close=Mock())
        _ImmediateTimer.instances = []
        timer_patch = patch("fnos_media_import.services.rclone_service.threading.Timer", _ImmediateTimer)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)

    def _status(self, run_id=8):
        return self.connection.execute("SELECT status FROM rclone_runs WHERE id=?", (run_id,)).fetchone()[0]

    def _set_fields(self, **fields):
        self.connection.execute(
            "UPDATE rclone_runs SET " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=8",
            tuple(fields.values()),
        )
        self.connection.commit()

    def _fail_once(self, name):
        original = getattr(self.repo, name)
        failed = False

        def call(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        mock = Mock(side_effect=call)
        setattr(self.repo, name, mock)
        return mock

    def _run_with_pending_result(self, exit_code=0):
        self.service.process_runner.run.return_value = ProcessResult(exit_code, "test result")
        self._fail_once("update_rclone_run")
        self.service._run_script("test", staging_run=self.descriptor)
        self.assertEqual(self._status(), "running")
        self.assertIsNone(self.service.run_state.current_run_id)
        self.assertEqual(len(self.service._completion_retry_timers), 1)
        return _ImmediateTimer.instances[-1]


class RcloneCompletionRecoveryTests(_RcloneRecoveryFixture, unittest.TestCase):
    def test_success_result_is_retried_without_copying_again(self):
        timer = self._run_with_pending_result()
        timer.fire()
        self.assertEqual(self._status(), "success")
        self.assertEqual(self.wakeups, [9])
        self.service.process_runner.run.assert_called_once()
        self.assertEqual(self.service.start_calls, [])
        self.assertEqual(self.service._completion_retry_attempts, {})
        self.assertEqual(self.service._completion_retry_timers, {})

    def test_failed_result_is_retried_without_copying_again(self):
        self._run_with_pending_result(1).fire()
        self.assertEqual(self._status(), "failed")
        self.assertEqual(self.wakeups, [9])
        self.assertEqual(self.service.start_calls, [])

    def test_transient_result_read_failure_is_also_recovered(self):
        self._fail_once("get_rclone_run")
        self.service._run_script("test", staging_run=self.descriptor)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "success")
        self.assertEqual(self.wakeups, [9])

    def test_repeated_write_failure_retains_result_past_download_retry_budget(self):
        update = self.repo.update_rclone_run
        self.repo.update_rclone_run = Mock(side_effect=sqlite3.OperationalError("locked"))
        self.service._run_script("test", staging_run=self.descriptor)
        for _ in range(4):
            _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(self.service._completion_retry_timers), 1)
        self.assertEqual(self._status(), "running")
        self.assertEqual(self.wakeups, [])
        self.assertLessEqual(_ImmediateTimer.instances[-1].interval, 60)
        self.repo.update_rclone_run = update
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "success")
        self.service.process_runner.run.assert_called_once()

    def test_old_result_does_not_change_next_jobs_in_memory_state(self):
        timer = self._run_with_pending_result()
        self.service.run_state.mark_running(99, "next-job-start")
        state = self.service.run_state.snapshot()
        timer.fire()
        self.assertEqual(self._status(), "success")
        self.assertEqual(self.service.run_state.snapshot(), state)

    def test_cancel_pending_wins_over_success_retry(self):
        timer = self._run_with_pending_result()
        self._set_fields(status="cancel_pending")
        timer.fire()
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])

    def test_cancel_between_result_read_and_write_wins(self):
        timer = self._run_with_pending_result()
        update = self.repo.update_rclone_run

        def cancel(*args, **kwargs):
            if kwargs.get("expected_status") == "running":
                self._set_fields(status="cancel_pending")
            return update(*args, **kwargs)

        self.repo.update_rclone_run = cancel
        timer.fire()
        self.assertEqual(self._status(), "cancelled")
        self.assertEqual(self.wakeups, [])

    def test_new_attempt_between_read_and_write_is_not_completed_by_old_result(self):
        timer = self._run_with_pending_result()
        update = self.repo.update_rclone_run

        def new_attempt(*args, **kwargs):
            self._set_fields(started_at="new-attempt")
            return update(*args, **kwargs)

        self.repo.update_rclone_run = new_attempt
        timer.fire()
        self.assertEqual(self._status(), "running")
        self.assertEqual(self.wakeups, [])

    def test_replaced_operation_between_read_and_write_is_not_completed(self):
        timer = self._run_with_pending_result()
        update = self.repo.update_rclone_run

        def other_operation(*args, **kwargs):
            self._set_fields(operation_key="other-operation")
            return update(*args, **kwargs)

        self.repo.update_rclone_run = other_operation
        timer.fire()
        self.assertEqual(self._status(), "running")
        self.assertEqual(self.wakeups, [])

    def test_missing_run_has_no_late_callback_side_effects(self):
        timer = self._run_with_pending_result()
        self.connection.execute("DELETE FROM rclone_runs WHERE id=8")
        self.connection.commit()
        self.repo.update_rclone_run = Mock(wraps=self.repo.update_rclone_run)
        self.repo.add_rclone_event = Mock(wraps=self.repo.add_rclone_event)
        timer.fire()
        self.repo.update_rclone_run.assert_not_called()
        self.repo.add_rclone_event.assert_not_called()
        self.assertEqual(self.wakeups, [])

    def test_successful_commit_with_lost_reply_is_reconciled_without_rewrite(self):
        update = self.repo.update_rclone_run

        def commit_then_lose_reply(*args, **kwargs):
            update(*args, **kwargs)
            raise sqlite3.OperationalError("commit reply lost")

        self.repo.update_rclone_run = Mock(side_effect=commit_then_lose_reply)
        self.service._run_script("test", staging_run=self.descriptor)
        self.assertEqual(self._status(), "success")
        _ImmediateTimer.instances[-1].fire()
        self.repo.update_rclone_run.assert_called_once()
        self.assertEqual(self.wakeups, [9])

    def test_existing_terminal_state_is_not_overwritten(self):
        timer = self._run_with_pending_result(1)
        self._set_fields(status="success")
        timer.fire()
        self.assertEqual(self._status(), "success")
        self.assertEqual(self.wakeups, [])

    def test_shutdown_cancels_timers_and_stale_callback_cannot_write(self):
        timer = self._run_with_pending_result()
        self.service.shutdown()
        self.assertTrue(timer.cancelled)
        self.assertEqual(self.service._completion_retry_timers, {})
        self.assertEqual(self.service._completion_retry_attempts, {})
        timer.fire()
        self.assertEqual(self._status(), "running")
        self.assertEqual(self.wakeups, [])
        self.service.log_sink.close.assert_called_once()

    def test_late_event_buffers_cannot_recreate_deleted_run_history(self):
        self.connection.execute("DELETE FROM rclone_runs WHERE id=8")
        self.connection.commit()
        self.assertEqual(self.repo.add_rclone_event(8, "info", "late"), 0)
        self.repo.add_rclone_events([(8, "info", "buffered late")])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM rclone_events").fetchone()[0], 0)
        self.assertGreater(self.repo.add_rclone_event(None, "info", "global event"), 0)


class RcloneStartupPaginationTests(_RcloneRecoveryFixture, unittest.TestCase):
    def _populate(self, count):
        self.connection.execute("DELETE FROM rclone_runs")
        self.connection.executemany(
            "INSERT INTO rclone_runs(id, operation_key, job_id, status, copy_descriptor_json) "
            "VALUES (?, ?, ?, 'queued', ?)",
            [(i, f"key-{i}", i, json.dumps({"job_id": i})) for i in range(1, count + 1)],
        )
        self.connection.commit()

    def test_more_than_two_pages_recover_exactly_once(self):
        self._populate(205)
        result = self.service.activate_startup_recovery()
        ids = [descriptor["_scoped_run_id"] for _reason, descriptor in self.service.start_calls]
        self.assertEqual(ids, list(range(1, 206)))
        self.assertEqual(result["scoped_runs"]["recovered"], 205)
        self.assertTrue(self.service.activate_startup_recovery()["skipped"])

    def test_recovery_freezes_high_watermark_while_new_jobs_arrive(self):
        self._populate(101)
        start = self.service.start

        def start_and_add(*args, **kwargs):
            if not self.service.start_calls:
                self.connection.execute(
                    "INSERT INTO rclone_runs VALUES (102, 'new-key', 102, 'queued', '{\"job_id\":102}', NULL, NULL, NULL, NULL, NULL)"
                )
                self.connection.commit()
            return start(*args, **kwargs)

        self.service.start = start_and_add
        result = self.service.recover_scoped_runs(limit=25)
        self.assertEqual(result["recovered"], 101)
        self.assertEqual(self._status(102), "queued")

    def test_cancellation_during_page_handoff_is_not_recovered(self):
        self._populate(101)
        mark = self.repo.mark_scoped_rclone_run_status

        def cancel_during_claim(run_id, status, **kwargs):
            if run_id == 101 and status == "recovering":
                self.connection.execute("UPDATE rclone_runs SET status='cancelled' WHERE id=101")
                self.connection.commit()
            return mark(run_id, status, **kwargs)

        self.repo.mark_scoped_rclone_run_status = cancel_during_claim
        result = self.service.recover_scoped_runs()
        self.assertEqual(result["recovered"], 100)
        self.assertEqual(self._status(101), "cancelled")
