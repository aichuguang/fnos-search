from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from fnos_media_import.transfer import OperationReceipt, TransferPlan, UploadOutcome
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor, _UploadProgressStream
from tests.test_transfer_v1 import make_plan


class _Recorder:
    def __init__(self):
        self.lines = []
        self.condition = threading.Condition()

    def __call__(self, message):
        with self.condition:
            self.lines.append(message)
            self.condition.notify_all()

    def wait_for(self, predicate, timeout=2):
        with self.condition:
            return self.condition.wait_for(lambda: predicate(self.lines), timeout)


class _BlockingUploader:
    provider_kind = "mobile"
    display_name = "移动云"

    def __init__(self, values=(), *, outcome=None, error=None):
        self.values = values
        self.outcome = outcome or UploadOutcome("done", "Provider confirmed completion")
        self.error = error
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.progress = None

    def upload_local_dir(self, **kwargs):
        self.calls += 1
        self.progress = kwargs["progress"]
        self.entered.set()
        for value in self.values:
            self.progress(value)
        if not self.release.wait(3):
            raise RuntimeError("Test upload was not released")
        if self.error is not None:
            raise self.error
        return self.outcome


def _hash(percent):
    return f"CMCC API local hash progress: movie.mkv: {percent} MiB / 100 MiB, {percent}%, 10 MiB/s, ETA 1s"


def _put(percent):
    return f"CMCC API PUT progress: movie.mkv: {percent} MiB / 100 MiB, {percent}%, 10 MiB/s, ETA 1s"


class RelayLiveUploadProgressTests(unittest.TestCase):
    def setUp(self):
        self.streams = []
        def stream_factory(*args, **kwargs):
            stream = _UploadProgressStream(*args, **kwargs)
            self.streams.append(stream)
            return stream
        self.enterContext(patch("fnos_media_import.transfer.provider_relay_executor._UploadProgressStream", side_effect=stream_factory))

    def _executor(self, uploader, sink, *, interval=5, heartbeat=0.04):
        plan = TransferPlan.from_dict(make_plan(
            source_kind="quark", dest_kind="mobile", strategy="relay_upload_provider",
            landing="/_fnos_ingress/job-1", final="/target", local_temp="/data/relay/job-1",
        ))
        executor = ProviderRelayExecutor(
            Mock(), uploader, relay_root="/data/relay", progress_sink=sink,
            progress_interval=interval, progress_heartbeat_seconds=heartbeat,
        ).bind(plan)
        receipt = OperationReceipt(
            "download-1", "running", "rclone",
            metadata={"step": "download", "operation_key": f"relay-dl:{plan.idempotency_key}"},
        )
        return executor, receipt

    def _run(self, executor, receipt, uploader):
        result = {}
        completed = threading.Event()
        def run():
            try:
                result["outcome"] = executor._upload(receipt)
            except BaseException as exc:
                result["error"] = exc
            finally:
                completed.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        def cleanup():
            uploader.release.set()
            thread.join(timeout=2)
            for stream in self.streams:
                stream.finish()
        self.addCleanup(cleanup)
        self.assertTrue(uploader.entered.wait(1), "Native uploader never entered")
        return result, completed

    def _assert_stopped(self):
        for stream in self.streams:
            self.assertTrue(stream.finished.wait(1), "Progress delivery did not stop after terminal message")
            if stream.thread is not None:
                stream.thread.join(timeout=1)
                self.assertFalse(stream.thread.is_alive())

    def test_start_phase_changes_and_100_percent_are_visible_before_upload_finishes(self):
        recorder = _Recorder()
        uploader = _BlockingUploader([_hash(45), _put(10), _put(100)])
        executor, receipt = self._executor(uploader, recorder)
        result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(recorder.wait_for(lambda lines: any(line.startswith("上传中 movie.mkv") and "100%" in line for line in lines)))
        self.assertTrue(recorder.lines[0].startswith("开始上传：移动云（准备校验本地文件并提交网盘）"))
        self.assertTrue(any(line.startswith("秒传校验") for line in recorder.lines))
        self.assertTrue(any(line.startswith("上传中 movie.mkv") and "10%" in line for line in recorder.lines))
        self.assertFalse(completed.is_set(), "Progress was only emitted after upload returned")
        self.assertFalse(any(line.startswith("上传完成") for line in recorder.lines))
        self.assertNotIn("upload_outcome", receipt.metadata)
        uploader.release.set()
        self.assertTrue(completed.wait(1))
        self._assert_stopped()
        self.assertEqual(result["outcome"].status, "done")
        self.assertTrue(recorder.lines[-1].startswith("上传完成"))
        count = len(recorder.lines)
        uploader.progress(_hash(1))
        uploader.progress(_put(1))
        self.assertEqual(len(recorder.lines), count, "Late callbacks regressed a finished upload")
        self.assertEqual(uploader.calls, 1)

    def test_blocking_request_keeps_heartbeat_until_call_returns(self):
        recorder = _Recorder()
        uploader = _BlockingUploader()
        executor, receipt = self._executor(uploader, recorder)
        _result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(recorder.wait_for(lambda lines: sum(line.startswith("上传请求仍在执行") for line in lines) >= 2))
        self.assertFalse(completed.is_set())
        heartbeats = [line for line in recorder.lines if line.startswith("上传请求仍在执行")]
        self.assertTrue(all("未收到新的字节进度，等待网盘响应" in line and "%" not in line for line in heartbeats))
        uploader.release.set()
        self.assertTrue(completed.wait(1))
        self._assert_stopped()
        self.assertTrue(recorder.lines[-1].startswith("上传完成"))

    def test_hash_wait_is_not_reported_as_uploading_bytes(self):
        recorder = _Recorder()
        uploader = _BlockingUploader([_hash(45)])
        executor, receipt = self._executor(uploader, recorder)
        _result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(recorder.wait_for(lambda lines: any(line.startswith("本地校验仍在执行") for line in lines)))
        self.assertFalse(completed.is_set())
        self.assertFalse(any(line.startswith("上传中") or line.startswith("上传请求仍在执行") for line in recorder.lines))
        uploader.release.set()
        self.assertTrue(completed.wait(1))
        self._assert_stopped()

    def test_slow_network_log_bridge_does_not_block_native_upload_or_terminal_result(self):
        recorder = _Recorder()
        bridge_entered = threading.Event()
        release_bridge = threading.Event()
        self.addCleanup(release_bridge.set)
        def bridge(message):
            if message.startswith("开始上传"):
                bridge_entered.set()
                release_bridge.wait(3)
            recorder(message)
        uploader = _BlockingUploader([10, 100])
        uploader.release.set()
        executor, receipt = self._executor(uploader, bridge)
        result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(bridge_entered.wait(1))
        self.assertTrue(completed.wait(0.5), "Upload main thread waited for the blocked log bridge")
        self.assertEqual(result["outcome"].status, "done")
        self.assertEqual(receipt.metadata["upload_outcome"]["status"], "done")
        self.assertEqual(uploader.calls, 1)
        release_bridge.set()
        self._assert_stopped()
        self.assertTrue(recorder.lines[-1].startswith("上传完成"))
        self.assertFalse(any(line.startswith("上传请求仍在执行") for line in recorder.lines))

    def test_callback_throttle_allows_stage_switch_and_100_but_not_every_update(self):
        recorder = _Recorder()
        executor, _receipt = self._executor(_BlockingUploader(), recorder)
        progress = executor._make_upload_progress_handler()
        with patch("fnos_media_import.transfer.provider_relay_executor.time.monotonic", return_value=100) as clock:
            progress(10)
            progress(20)
            progress(100)
            progress(100)
            progress(_hash(10))
            progress(_hash(15))
            progress(_hash(100))
            progress(_put(1))
            progress(_put(2))
            clock.return_value = 106
            progress(_put(2))
            for invalid in (True, float("nan"), float("inf"), "CMCC API create request: secret"):
                progress(invalid)
        self.assertEqual(len(recorder.lines), 6)
        self.assertEqual(recorder.lines[0:2], ["上传中：10%", "上传中：100%"])
        self.assertTrue(recorder.lines[2].startswith("秒传校验"))
        self.assertIn("100%", recorder.lines[3])
        self.assertTrue(recorder.lines[4].startswith("上传中 movie.mkv"))

    def test_failed_upload_stops_heartbeat_and_keeps_failure_semantics(self):
        recorder = _Recorder()
        uploader = _BlockingUploader(error=RuntimeError("Native upload failed"))
        executor, receipt = self._executor(uploader, recorder)
        result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(recorder.wait_for(lambda lines: any(line.startswith("上传请求仍在执行") for line in lines)))
        uploader.release.set()
        self.assertTrue(completed.wait(1))
        self._assert_stopped()
        self.assertEqual(result["outcome"].status, "failed")
        self.assertEqual(receipt.metadata["upload_submission_fence"]["state"], "failed")
        self.assertTrue(recorder.lines[-1].startswith("上传失败"))
        self.assertEqual(uploader.calls, 1)

    def test_100_percent_does_not_turn_pending_provider_outcome_into_done(self):
        recorder = _Recorder()
        uploader = _BlockingUploader([100], outcome=UploadOutcome("running", "Provider is still confirming"))
        executor, receipt = self._executor(uploader, recorder)
        result, completed = self._run(executor, receipt, uploader)
        self.assertTrue(recorder.wait_for(lambda lines: "上传中：100%" in lines))
        self.assertFalse(completed.is_set())
        uploader.release.set()
        self.assertTrue(completed.wait(1))
        self._assert_stopped()
        self.assertEqual(result["outcome"].status, "running")
        self.assertEqual(receipt.metadata["upload_outcome"]["status"], "running")
        self.assertFalse(any(line.startswith("上传完成") for line in recorder.lines))

    def test_replayed_completed_upload_does_not_start_progress_thread_or_native_call(self):
        recorder = _Recorder()
        uploader = _BlockingUploader()
        executor, receipt = self._executor(uploader, recorder)
        receipt.metadata["upload_outcome"] = {"status": "done", "message": "Already complete"}
        self.assertEqual(executor._upload(receipt).status, "done")
        self.assertEqual(uploader.calls, 0)
        self.assertEqual(self.streams, [])
        self.assertEqual(recorder.lines, [])


if __name__ == "__main__":
    unittest.main()
