from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fnos_media_import.transfer.relay_file_state import RelayFileState


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fnos_rclone_worker.sh"


class RelaySingleFileWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("bash")
        git_shell = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if os.name == "nt" and git_shell.is_file():
            cls.shell = str(git_shell)
        if not cls.shell:
            raise unittest.SkipTest("POSIX shell unavailable")
        cls.functions = SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]

    def _run(self, folder, *, index=0, size=5, copy_rc=0):
        root = Path(folder).resolve()
        cache = root / "relay"
        environment = {
            **os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "PYTHON_BIN": Path(sys.executable).as_posix(),
            "RCLONE_COPY_MODE": "true", "RCLONE_ONLY_JOB_DIR": "job-1", "RCLONE_ONLY_CATEGORY": "tv",
            "APP_CALLBACK_URL": "http://unused.invalid", "RCLONE_COPY_SOURCE_REMOTE": "managed",
            "RCLONE_COPY_SOURCE_ROOT": "source", "RCLONE_COPY_LOCAL_TARGET": cache.as_posix(),
            "RCLONE_COPY_RELAY_ROOT": cache.as_posix(), "RCLONE_COPY_RELAY_MODE": "true",
            "RCLONE_COPY_RELAY_SEQUENTIAL": "true", "RCLONE_COPY_RELAY_PLAN_KEY": "plan:one",
            "RCLONE_COPY_RELAY_FILE_INDEX": str(index), "TEST_SIZE": str(size), "TEST_COPY_RC": str(copy_rc),
            "MAX_RETRIES": "3", "RETRY_DELAY": "0",
        }
        harness = self.functions + r'''
ensure_rclone_available() { return 0; }
ensure_remote_dir() { return 0; }
stable_file_list() { printf 'job-1/01.mkv\njob-1/02.mkv\n'; }
persist_staging_manifest() { printf '%s\n' "$1" > manifest-seen; }
remote_file_size() { printf '%s\n' "$TEST_SIZE"; }
copy_file() {
  printf '%s\n' "$1" >> copy-calls
  if [ "$TEST_COPY_RC" -ne 0 ]; then return "$TEST_COPY_RC"; fi
  printf '12345' > "$2"
}
copy_wait_size_match() { return 0; }
callback_app() { printf '%s\n' "$1" >> callbacks; }
rclone_cmd() { printf '%s\n' "$*" >> remote-mutations; }
copy_scoped_job
'''
        script = root / "harness.sh"
        script.write_text(harness, encoding="utf-8", newline="\n")
        return subprocess.run(
            [self.shell, script.as_posix()], cwd=root, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    def test_real_worker_downloads_only_one_and_keeps_full_manifest_and_source(self):
        with tempfile.TemporaryDirectory(prefix="relay-shell-") as folder:
            root = Path(folder).resolve()
            result = self._run(folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "copy-calls").read_text().splitlines(), ["managed:source/job-1/01.mkv"])
            self.assertEqual((root / "manifest-seen").read_text().splitlines(), ["job-1/01.mkv", "job-1/02.mkv"])
            self.assertFalse((root / "relay/job-1/02.mkv").exists())
            self.assertFalse((root / "remote-mutations").exists(), "Relay must not delete its source after download")
            state = RelayFileState(str(root / "relay/job-1"), job_id=1, plan_key="plan:one").load()
            self.assertEqual([item["state"] for item in state["files"]], ["downloaded", "pending"])
            again = self._run(folder)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(len((root / "copy-calls").read_text().splitlines()), 1)
            prefetched = self._run(folder, index=1)
            self.assertEqual(prefetched.returncode, 0, prefetched.stderr)
            self.assertTrue((root / "relay/job-1/02.mkv").exists())
            self.assertEqual(len((root / "copy-calls").read_text().splitlines()), 2)
            updated = RelayFileState(str(root / "relay/job-1"), job_id=1, plan_key="plan:one").load()
            self.assertEqual([item["state"] for item in updated["files"]], ["downloaded", "downloaded"])

    def test_preflight_disk_shortage_does_not_start_copy(self):
        with tempfile.TemporaryDirectory(prefix="relay-full-") as folder:
            result = self._run(folder, size=10**18)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(folder) / "copy-calls").exists())
            self.assertEqual(result.returncode, 75)
            self.assertIn("缓存预算不足", result.stderr)

    def test_storage_full_exit_is_not_retried(self):
        with tempfile.TemporaryDirectory(prefix="relay-enospc-") as folder:
            result = self._run(folder, copy_rc=74)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len((Path(folder) / "copy-calls").read_text().splitlines()), 1)
            self.assertFalse((Path(folder) / "relay/job-1/02.mkv").exists())

    def test_cache_wait_is_cancellable_and_does_not_consume_download_retries(self):
        with tempfile.TemporaryDirectory(prefix="relay-wait-") as folder:
            script = Path(folder).resolve() / "wait.sh"
            script.write_text(self.functions + r'''
attempt=0
relay_file_state() {
  attempt=$((attempt + 1))
  if [ "$attempt" -eq 1 ]; then return 75; fi
  return 0
}
sleep_or_stop() { return 0; }
prepare_relay_file 10
[ "$attempt" -eq 2 ] || exit 91
relay_file_state() { return 75; }
sleep_or_stop() { return 143; }
if prepare_relay_file 10; then exit 92; else [ "$?" -eq 143 ]; fi
''', encoding="utf-8", newline="\n")
            result = subprocess.run(
                [self.shell, script.as_posix()], capture_output=True, text=True, encoding="utf-8", timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
