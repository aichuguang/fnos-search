"""Bounded relay download scheduling, scoped cancellation and worker backpressure."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.services.rclone_run_queue import RcloneRunQueue
from fnos_media_import.services.rclone_process_runner import ProcessResult
from fnos_media_import.services.rclone_run_state import RcloneRunState
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer import OperationReceipt
from fnos_media_import.transfer.rclone_adapter import RcloneOperationAdapter
from tests.test_rclone_lock_busy_and_relay_reconcile import (
    _ImmediateTimer, _ServiceRepo, _bare_service,
)
from tests.test_rclone_completion_recovery import _RcloneRecoveryFixture


ROOT = Path(__file__).resolve().parents[1]


class _Runs:
    def __init__(self):
        self.rows = {}

    def get_rclone_run(self, run_id):
        return self.rows.get(run_id)

    def mark_scoped_rclone_run_status(self, run_id, status, **fields):
        row = self.rows[run_id]
        expected = fields.get("expected_status")
        if expected is not None and row["status"] != expected:
            return False
        row["status"] = status
        return True


def _descriptor(run_id, *, job_id=9):
    return {
        "job_id": job_id, "_scoped_run_id": run_id,
        "_operation_key": f"relay-file:{run_id}:plan-{job_id}",
        "relay_mode": True,
    }


class ScopedDownloadQueueTests(unittest.TestCase):
    def setUp(self):
        self.repo = _Runs()
        self.service = service = object.__new__(RcloneService)
        service.enabled = True
        service.lock = threading.RLock()
        service.run_queue = RcloneRunQueue()
        service.run_state = RcloneRunState()
        service.run_state.mark_running(1, "now")
        service._database_bound_staging_run = lambda run: (dict(run), "")
        service._append_log = lambda _message: None
        service._cancel_staging_retry_locked = lambda _job_id: None
        service._active_staging_job_id = 9
        service._active_full_staging_job_id = 9
        service._active_scoped_run_identity = (1, _descriptor(1)["_operation_key"])
        service._reserved_full_staging_job_id = 0
        service.is_running_locked = lambda: True
        service.status_locked = lambda: service.run_queue.snapshot(limit=0)
        service.process = object()
        self.stopped = []
        service.process_controller = SimpleNamespace(
            is_active=lambda process: process is not None,
            terminate=lambda process: self.stopped.append(process) or True,
        )
        service.db = SimpleNamespace(
            rclone=self.repo,
            job_queries=SimpleNamespace(get=lambda job_id: {"id": job_id, "status": "waiting_transfer"}),
        )
        for run_id in (1, 2, 3, 4):
            run = _descriptor(run_id)
            self.repo.rows[run_id] = {
                "id": run_id, "job_id": 9, "operation_key": run["_operation_key"],
                "status": "running" if run_id == 1 else "queued",
            }

    def test_same_job_files_queue_independently_but_same_run_deduplicates(self):
        service = self.service
        self.assertTrue(service.start(staging_run=_descriptor(1))["already_running"])
        self.assertTrue(service.start(staging_run=_descriptor(2))["queued"])
        self.assertTrue(service.start(staging_run=_descriptor(3))["queued"])
        self.assertTrue(service.start(staging_run=_descriptor(2))["deduplicated"])
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 2)
        self.assertEqual(service.run_queue.pop_next().staging_run["_scoped_run_id"], 2)
        self.assertEqual(service.run_queue.pop_next().staging_run["_scoped_run_id"], 3)

    def test_reserved_file_only_deduplicates_its_own_receipt(self):
        service = self.service
        service._reserved_full_staging_job_id = 9
        service._reserved_scoped_run_identity = (2, _descriptor(2)["_operation_key"])
        self.assertTrue(service.start(staging_run=_descriptor(2))["starting"])
        self.assertFalse(service.start(staging_run=_descriptor(3)).get("deduplicated", False))
        self.assertEqual(service.run_queue.snapshot()["queue_count"], 1)

    def test_cancelling_queued_receipt_does_not_stop_active_sibling(self):
        service = self.service
        service.start(staging_run=_descriptor(2))
        service.start(staging_run=_descriptor(3))
        result = service.cancel_run(2, operation_key=_descriptor(2)["_operation_key"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.repo.rows[1]["status"], "running")
        started = []
        service._start_worker_locked = lambda reason, **kwargs: started.append(kwargs["staging_run"])
        service._start_next_queued_run()
        self.assertEqual([row["_scoped_run_id"] for row in started], [3])

    def test_cancelling_exact_active_receipt_sets_process_start_fence(self):
        service = self.service
        process = service.process
        service.process = None
        result = service.cancel_run(1, operation_key=_descriptor(1)["_operation_key"])
        self.assertEqual(result["status"], "cancel_pending")
        service._set_process(process)
        self.assertEqual(self.stopped, [process])

    def test_other_job_and_mismatched_operation_cannot_stop_active_copy(self):
        service = self.service
        result = service.cancel_run(1, operation_key="other-operation")
        self.assertFalse(result["success"])
        self.repo.rows[2]["job_id"] = 10
        self.assertEqual(service.cancel_run(2)["status"], "cancelled")
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.repo.rows[1]["status"], "running")

    def test_cancelling_after_defer_before_worker_handoff_converges_immediately(self):
        service = self.service
        self.repo.rows[1]["status"] = "queued"
        service.run_state.mark_deferred("cache budget", "now")
        result = service.cancel_run(1, operation_key=_descriptor(1)["_operation_key"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.repo.rows[1]["status"], "cancelled")
        self.assertEqual(self.stopped, [])


class CacheWaitRetryTests(unittest.TestCase):
    def test_normal_cache_wait_does_not_exhaust_download_retry_budget(self):
        repo = _ServiceRepo({"id": 8, "job_id": 9, "status": "queued", "operation_key": "relay-file:1:k9"})
        service, _logs, wakeups = _bare_service(repo, max_attempts=2)
        _ImmediateTimer.instances = []
        with patch("fnos_media_import.services.rclone_service.threading.Timer", _ImmediateTimer):
            for _ in range(8):
                result = service._schedule_deferred_run(8, _descriptor(8), "prefetch", cache_wait=True)
            self.assertTrue(result["deferred"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(repo.run["status"], "queued")
            self.assertEqual(wakeups, [])
            _ImmediateTimer.instances[-1].fire()
        self.assertEqual(len(service.start_calls), 1)

    def test_only_real_adapter_advertises_bounded_prefetch(self):
        self.assertIs(RcloneOperationAdapter.supports_bounded_prefetch, True)

    def test_adapter_rejects_a_foreign_job_receipt_before_service_cancel(self):
        adapter = RcloneOperationAdapter(SimpleNamespace(), None).bind(SimpleNamespace(job_id=9))
        receipt = OperationReceipt(
            operation_id="rclone:10:2", status="running", provider_kind="rclone",
            metadata={"job_id": 10, "run_id": "2", "operation_key": "relay-file:2:plan-10"},
        )
        self.assertEqual(adapter.cancel(receipt).status, "not_cancellable")


class CacheWaitRunIntegrationTests(_RcloneRecoveryFixture, unittest.TestCase):
    def test_worker_cache_marker_requeues_without_waking_completion_or_exhausting(self):
        def run(*_args, **kwargs):
            kwargs["on_line"]("[2026-10-15 00:00:00] RELAY_CACHE_DEFERRED: budget")
            return ProcessResult(75, "deferred")

        self.service.process_runner.run.side_effect = run
        for _ in range(4):
            self.service._run_script("bounded-prefetch", staging_run={**self.descriptor, "relay_mode": True})
            self.assertEqual(self._status(), "queued")
        self.assertEqual(self.wakeups, [])
        self.assertEqual(self.service._deferred_run_attempts[8], 1)
        self.assertEqual(self.service._start_next_queued_run.call_count, 4)


class BoundedWorkerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("bash") or shutil.which("sh")
        git_shell = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if os.name == "nt" and git_shell.is_file():
            cls.shell = str(git_shell)
        if not cls.shell:
            raise unittest.SkipTest("POSIX shell unavailable")
        cls.functions = (ROOT / "scripts/fnos_rclone_worker.sh").read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]

    def _run(self, body):
        with tempfile.TemporaryDirectory(prefix="relay-prefetch-worker-") as folder:
            root = Path(folder)
            (root / "scratch").mkdir()
            script = root / "harness.sh"
            script.write_text(self.functions + "\n" + body, encoding="utf-8", newline="\n")
            return subprocess.run(
                [self.shell, str(script)], cwd=root, timeout=8,
                env={**os.environ, "TMPDIR": "scratch", "MSYS2_ARG_CONV_EXCL": "*", "LC_ALL": "C.UTF-8"},
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    def test_v2_prepare_returns_75_without_holding_lock_in_wait_loop(self):
        result = self._run(r'''
copy_relay_version=2
relay_file_state() { return 75; }
sleep_or_stop() { echo UNEXPECTED_SLEEP; return 143; }
if prepare_relay_file 100; then rc=0; else rc=$?; fi
exit "$rc"
''')
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("RELAY_CACHE_DEFERRED", result.stderr)
        self.assertNotIn("UNEXPECTED_SLEEP", result.stdout)

    def test_first_file_enables_quota_before_prepare_and_never_starts_over_budget_copy(self):
        result = self._run(r'''
RCLONE_ONLY_JOB_DIR=job-9
APP_CALLBACK_URL=http://local.invalid
RCLONE_COPY_SOURCE_REMOTE=source
RCLONE_COPY_SOURCE_ROOT=landing
RCLONE_COPY_RELAY_MODE=true
RCLONE_COPY_RELAY_SEQUENTIAL=true
RCLONE_COPY_RELAY_ROOT=/controlled
RCLONE_COPY_LOCAL_TARGET=/controlled
LOCAL_TEMP=/unrelated
ensure_rclone_available() { :; }
ensure_remote_dir() { :; }
container_exec() { case "$1" in mkdir) return 0 ;; readlink) printf '%s\n' "$3" ;; *) return 90 ;; esac; }
stable_file_list() { printf 'job-9/first.mkv\n'; }
persist_staging_manifest() { :; }
remote_file_size() { printf '21474836481\n'; }
relay_file_state() {
  case "$1" in
    select) cat ;;
    enable) printf 'quota-enabled' > scratch/enabled ;;
    version) [ -f scratch/enabled ] || return 91; printf '2\n' ;;
    prepare) [ -f scratch/enabled ] || return 92; return 75 ;;
    *) return 93 ;;
  esac
}
copy_file() { printf 'UNEXPECTED_DOWNLOAD\n'; return 94; }
callback_app() { printf 'UNEXPECTED_CALLBACK\n'; return 95; }
if copy_scoped_job; then rc=0; else rc=$?; fi
[ -f scratch/enabled ] || exit 96
exit "$rc"
''')
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("RELAY_CACHE_DEFERRED", result.stderr)
        self.assertNotIn("UNEXPECTED_", result.stdout)

    def test_v2_space_guard_stops_only_copy_and_preserves_upload_cache(self):
        result = self._run(r'''
copy_relay_version=2
copy_rc_file=scratch/result
printf 'uploading-media' > uploading.mkv
sleep 6 &
unrelated=$!
trap 'kill "$unrelated" 2>/dev/null || true; wait "$unrelated" 2>/dev/null || true' EXIT
rclone() { printf 'started' > scratch/copy-started; while :; do :; done; }
checks=0
relay_file_state() { checks=$((checks + 1)); [ "$checks" = 1 ] && return 0; return 75; }
if relay_copy_cmd copyto source target; then rc=0; else rc=$?; fi
[ "$rc" = 75 ] || exit 91
[ -f scratch/copy-started ] || exit 94
kill -0 "$unrelated" || exit 92
[ "$(cat uploading.mkv)" = uploading-media ] || exit 93
printf 'guard-stopped-copy-only\n'
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard-stopped-copy-only", result.stdout)

    def test_backpressure_bypasses_generic_copy_retry(self):
        result = self._run(r'''
attempts=0
copy_wait() { attempts=$((attempts + 1)); return 75; }
if retry_count prefetch 3 copy_wait; then rc=0; else rc=$?; fi
[ "$attempts" = 1 ] || exit 90
exit "$rc"
''')
        self.assertEqual(result.returncode, 75, result.stderr)

    def test_inplace_is_scoped_to_bounded_relay_not_legacy_or_remote_copies(self):
        result = self._run(r'''
copy_rc_file=scratch/result
relay_file_state() { return 0; }
rclone() { printf '%s\n' "$*"; }
copy_relay_version=1
relay_copy_cmd copyto source remote-target > scratch/legacy-args
copy_relay_version=2
relay_copy_cmd copyto source local-target > scratch/bounded-args
grep -q -- '--inplace' scratch/legacy-args && exit 90
grep -q -- '--inplace' scratch/bounded-args || exit 91
''')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
