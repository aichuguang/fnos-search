from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"
_FAKE_RCLONE = r'''
import os
import sys
import time
from pathlib import Path

def emit(value):
    print(value, file=sys.stderr, flush=True)

def wait_for(name):
    deadline = time.monotonic() + 12
    while not Path(name).exists():
        if time.monotonic() > deadline:
            emit("FAKE_TIMEOUT")
            sys.exit(91)
        time.sleep(0.02)

mode = os.environ["FAKE_MODE"]
Path("producer-started").touch()
if mode == "stream":
    emit("NOTICE: movie.mkv: 10%, 1MiB/10MiB, 1MiB/s, ETA 9s")
    wait_for("release-second")
    emit("NOTICE: movie.mkv: 20%, 2MiB/10MiB, 1MiB/s, ETA 8s")
    wait_for("release-finish")
elif mode == "quiet":
    emit("FAKE_RUNNING")
    wait_for("release-finish")
elif mode == "stalled":
    emit("NOTICE: movie.mkv: 0%, 0B/10MiB, 0B/s, ETA -")
    wait_for("release-finish")
else:
    emit("ERROR: fake rclone failure stays on stderr")
Path("producer-finished").write_text("finished", encoding="utf-8")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''

# Model an awk implementation that waits for its non-terminal input buffer
# before evaluating records. The real awk still formats the real pipe bytes;
# this deliberately exposes a bug that fflush()/post-EOF tests cannot detect.
_BUFFERING_AWK = r'''
import os
import subprocess
import sys
payload = sys.stdin.buffer.read()
completed = subprocess.run(
    [os.environ["REAL_AWK"], *sys.argv[1:]], input=payload,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
sys.stdout.buffer.write(completed.stdout)
sys.stdout.buffer.flush()
sys.stderr.buffer.write(completed.stderr)
sys.stderr.buffer.flush()
sys.exit(completed.returncode)
'''


class RcloneLiveStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("bash") or shutil.which("sh")
        cls.awk = shutil.which("awk")
        if os.name == "nt":
            git = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
            if (git / "bin/bash.exe").is_file():
                cls.shell = str(git / "bin/bash.exe")
            if (git / "usr/bin/awk.exe").is_file():
                cls.awk = str(git / "usr/bin/awk.exe")
        if not cls.shell or not cls.awk:
            raise unittest.SkipTest("A POSIX shell and awk are required for live pipe tests")
        content = SCRIPT.read_text(encoding="utf-8")
        cls.functions = content.rsplit('\nmain "$@"', 1)[0]

    @contextmanager
    def _running_copy(self, *, mode="stream", buffered_awk=False, exit_code=0, notice_seconds=30):
        with tempfile.TemporaryDirectory(prefix="rclone-live-") as folder:
            root = Path(folder)
            (root / "scratch").mkdir()
            (root / "fake_rclone.py").write_text(_FAKE_RCLONE, encoding="utf-8")
            (root / "buffering_awk.py").write_text(_BUFFERING_AWK, encoding="utf-8")
            harness = self.functions + '\nrclone() { "$PYTHON_BIN" -u fake_rclone.py "$@"; }\n'
            if buffered_awk:
                harness += 'awk() { "$PYTHON_BIN" -u buffering_awk.py "$@"; }\n'
            harness += '\nif copy_file "OL:/source/job-7/movie.mkv" "/target/job-7/movie.mkv"; then result=0; else result=$?; fi\nexit "$result"\n'
            path = root / "harness.sh"
            path.write_text(harness, encoding="utf-8", newline="\n")
            env = {
                **os.environ,
                "PYTHON_BIN": Path(sys.executable).as_posix(), "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8", "REAL_AWK": Path(self.awk).as_posix(),
                "TMPDIR": "scratch", "FAKE_MODE": mode, "FAKE_EXIT": str(exit_code),
                "RCLONE_PROGRESS_DIRECTION": "下载", "RCLONE_PROGRESS_POLL_SECONDS": "1",
                "RCLONE_PROGRESS_NOTICE_SECONDS": str(notice_seconds),
                "MSYS2_ARG_CONV_EXCL": "*", "LC_ALL": "C.UTF-8",
            }
            command = [self.shell]
            if "bash" in Path(self.shell).name.lower():
                command.extend(["--noprofile", "--norc"])
            command.append(str(path))
            process = subprocess.Popen(
                command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            lines = queue.Queue()
            output = []
            def read_output():
                for line in process.stderr:
                    observed = (time.monotonic(), line.rstrip("\n"))
                    output.append(observed)
                    lines.put(observed)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                yield root, process, lines, output
            finally:
                (root / "release-second").touch()
                (root / "release-finish").touch()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                reader.join(timeout=3)
                process.stdout.close()
                if not reader.is_alive():
                    process.stderr.close()

    def _await_line(self, lines, text, *, output, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                observed = lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if text in observed[1]:
                return observed
        self.fail(f"No live {text!r} before deadline; observed={output!r}")

    def _assert_producer_alive(self, root, process):
        self.assertIsNone(process.poll(), "copy wrapper already exited")
        self.assertFalse((root / "producer-finished").exists(), "progress was delivered only after rclone finished")

    def test_two_progress_updates_arrive_while_fake_rclone_is_still_running(self):
        for buffered in (False, True):
            with self.subTest(buffered_awk=buffered):
                with self._running_copy(buffered_awk=buffered) as (root, process, lines, output):
                    first = self._await_line(lines, "10%", output=output)
                    self.assertIn("下载中 movie.mkv", first[1])
                    self._assert_producer_alive(root, process)
                    # The producer cannot emit the second update or exit until
                    # its parent actually observed the first through the pipe.
                    (root / "release-second").touch()
                    second = self._await_line(lines, "20%", output=output)
                    self.assertGreater(second[0], first[0])
                    self._assert_producer_alive(root, process)
                    (root / "release-finish").touch()
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_waiting_and_stalled_transfers_continue_reporting_without_fake_success(self):
        for mode, phrase in (("quiet", "等待进度"), ("stalled", "进度暂未变化")):
            with self.subTest(mode=mode):
                with self._running_copy(mode=mode, notice_seconds=1) as (root, process, lines, output):
                    first = self._await_line(lines, phrase, output=output)
                    self._assert_producer_alive(root, process)
                    second = self._await_line(lines, phrase, output=output)
                    self.assertGreater(second[0], first[0])
                    self._assert_producer_alive(root, process)
                    self.assertIn("搬运调用尚未结束", second[1])
                    self.assertNotIn("通常是源网盘", second[1])

    def test_failure_and_interruption_exit_codes_and_temp_cleanup_are_preserved(self):
        for exit_code in (17, 143):
            with self.subTest(exit_code=exit_code):
                with self._running_copy(mode="exit", exit_code=exit_code) as (root, process, lines, output):
                    self._await_line(lines, "ERROR: fake rclone failure", output=output)
                    self.assertEqual(process.wait(timeout=3), exit_code)
                    self.assertEqual(process.stdout.read(), "", "progress/errors must not contaminate filename stdout")
                    self.assertEqual(list((root / "scratch").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
