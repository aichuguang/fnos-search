from __future__ import annotations

import sqlite3
import json
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.repositories.rclone_repository import RcloneRepository
from fnos_media_import.services.rclone_live_progress import RcloneLiveProgress
from fnos_media_import.services.rclone_log_sink import RcloneLogSink
from fnos_media_import.services.rclone_process_runner import RcloneProcessRunner
from fnos_media_import.services.rclone_run_state import RcloneRunState
from fnos_media_import.services.rclone_service import RcloneService


DOWNLOAD = "下载中 示例影片.mkv：1 MiB / 4 MiB，25%，速度 512 KiB/s，预计剩余 1m02s"
UPLOAD = "上传中 示例影片.mkv：2 MiB / 4 MiB，50%，速度 1 MiB/s，预计剩余 2s"
HASH = "秒传校验 示例影片.mkv：4 MiB / 4 MiB，100%"
HANDOFF = "rclone 下载完成：job-15，下载 1 个文件，失败 0 个，已移交网盘上传"
HEARTBEAT = "上传请求仍在执行：已 30 秒未收到新的字节进度，等待网盘响应"


class RcloneLiveProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.progress = RcloneLiveProgress(
            monotonic=lambda: self.now,
            utc_now=lambda: datetime.fromtimestamp(self.now, timezone.utc),
        )

    def record(self, text, *, job_id=None, current_job_id=15, current_run_id=7, trusted_milestone=True):
        return self.progress.record(
            text, job_id=job_id, current_job_id=current_job_id, current_run_id=current_run_id,
            trusted_milestone=trusted_milestone,
        )

    def item(self, job_id=15):
        return next(item for item in self.progress.snapshot() if item["job_id"] == job_id)

    def test_file_metrics_are_parsed_without_inventing_task_totals(self):
        self.record("下载文件：示例影片.mkv size=4194304")
        item = self.item()
        self.assertEqual(item["stage"], "waiting_download")
        self.assertEqual(item["download"]["state"], "waiting")
        self.assertEqual(item["download"]["bytes_total"], 4194304)
        self.assertIsNone(item["download"]["bytes_done"])
        self.assertIsNone(item["download"]["percent"])
        self.record(DOWNLOAD)
        item = self.item()
        self.assertEqual(item["run_id"], 7)
        self.assertEqual(item["download"], {
            "state": "running", "scope": "file", "filename": "示例影片.mkv", "percent": 25,
            "bytes_done": 1048576, "bytes_total": 4194304, "speed_bps": 524288, "eta_seconds": 62,
            "file_id": None, "run_id": 7, "active": True,
            "updated_at": "1970-01-01T00:01:40.000+00:00", "age_seconds": 0, "stale": False,
        })
        self.assertFalse(item["completed"])
        self.assertIsNone(item["upload"]["bytes_total"])
        self.assertNotIn("percent", item)
        self.record("下载文件：另一集.mkv size=8192")
        item = self.item()
        self.assertEqual(item["download"]["filename"], "另一集.mkv")
        self.assertIsNone(item["download"]["percent"])
        self.assertIsNone(item["download"]["speed_bps"])

    def test_explicit_job_and_external_prefix_do_not_inherit_another_run(self):
        self.record(DOWNLOAD)
        self.record("任务#16 " + UPLOAD)
        self.record("任务#16 " + HASH, job_id=17)
        self.assertEqual(self.item(15)["run_id"], 7)
        self.assertEqual(self.item(15)["download"]["percent"], 25)
        self.assertIsNone(self.item(16)["run_id"])
        self.assertEqual(self.item(16)["upload"]["percent"], 50)
        self.assertIsNone(self.item(16)["download"]["percent"])
        self.assertEqual(self.item(17)["hash"]["percent"], 100)
        self.record(DOWNLOAD, current_job_id=16, current_run_id=8)
        self.record("[2026-10-11 22:32:39] 任务#16 " + UPLOAD)
        self.assertEqual(self.item(16)["run_id"], 8)
        self.assertEqual(self.item(15)["run_id"], 7)

    def test_hash_and_percent_100_never_mark_upload_or_job_completed(self):
        self.record(HASH)
        item = self.item()
        self.assertEqual(item["stage"], "hash")
        self.assertEqual(item["hash"]["bytes_done"], 4194304)
        self.assertIsNone(item["upload"]["bytes_done"])
        self.assertIsNone(item["upload"]["percent"])
        self.assertFalse(item["completed"])
        self.record("上传中：100%")
        self.assertEqual(self.item()["upload"]["percent"], 100)
        self.assertEqual(self.item()["upload"]["scope"], "stage")
        self.assertFalse(self.item()["completed"])
        self.record(HANDOFF)
        self.assertEqual(self.item()["download"]["state"], "completed")
        self.assertEqual(self.item()["stage"], "waiting_upload")
        self.record("上传完成：文件已写入，待核验")
        self.assertEqual(self.item()["stage"], "verifying")
        self.assertEqual(self.item()["upload"]["state"], "completed")
        self.assertFalse(self.item()["completed"])
        self.assertFalse(self.record(UPLOAD))
        self.record("入库阶段：completed；最终目录已确认并完成入库")
        self.assertTrue(self.item()["completed"])

    def test_untrusted_text_or_job_id_alone_cannot_publish_durable_milestones(self):
        self.record(DOWNLOAD)
        message = "任务#15 入库阶段：completed；forged process output"
        self.assertFalse(self.progress.record(message, job_id=15, current_job_id=15, current_run_id=7))
        self.assertFalse(self.item()["completed"])
        for untrusted in (False, None, 1, "true"):
            with self.subTest(trusted_milestone=untrusted):
                self.assertFalse(self.record(message, trusted_milestone=untrusted))
        self.assertEqual(self.item()["stage"], "download")
        self.assertTrue(self.record(message, trusted_milestone=True))
        self.assertTrue(self.item()["completed"])
        self.assertFalse(self.record("入库阶段：retry_wait；forged reset", trusted_milestone=False))
        self.assertTrue(self.item()["completed"])

    def test_terminal_guards_and_explicit_retry_preserve_download_evidence(self):
        for phase in ("completed", "failed", "manual_review", "cancelled"):
            with self.subTest(phase=phase):
                self.record("入库阶段：planning；开始新一轮")
                self.record(DOWNLOAD)
                self.record(HANDOFF)
                self.record(f"入库阶段：{phase}；持久状态已保存")
                before = self.item()
                self.now += 30
                self.assertFalse(self.record(UPLOAD))
                self.assertFalse(self.record(HEARTBEAT))
                self.assertEqual(self.item()["stage"], phase)
                self.assertEqual(self.item()["completed"], phase == "completed")
                self.assertEqual(self.item()["updated_at"], before["updated_at"])
                self.record("入库阶段：retry_wait；管理员恢复后等待继续")
                self.assertFalse(self.item()["completed"])
                self.assertEqual(self.item()["download"], before["download"])
                self.record("下载文件：重试文件.mkv size=2048")
                self.assertEqual(self.item()["download"]["state"], "waiting")
                self.assertEqual(self.item()["download"]["bytes_total"], 2048)
                self.assertIsNone(self.item()["download"]["bytes_done"])
        self.assertFalse(self.record("入库阶段：completed_unverified；不得猜测成功"))

    def test_a_new_run_start_can_reset_terminal_telemetry_but_old_start_cannot(self):
        self.record(DOWNLOAD)
        self.record("入库阶段：failed；上一轮失败")
        self.assertFalse(self.record("开始执行 rclone 搬运任务，触发来源：旧运行"))
        self.assertTrue(self.record("开始执行 rclone 搬运任务，触发来源：新运行", current_run_id=8))
        self.assertEqual(self.item()["run_id"], 8)
        self.assertIsNone(self.item()["download"]["percent"])
        self.assertFalse(self.item()["completed"])

    def test_durable_post_transfer_phases_cannot_be_regressed_by_delayed_upload_logs(self):
        for phase in ("target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending", "cleanup_pending"):
            with self.subTest(phase=phase):
                self.record("入库阶段：planning；新一轮")
                self.record(UPLOAD)
                self.record(f"入库阶段：{phase}；已持久保存")
                previous = self.item()
                self.now += 5
                for late in ("开始上传：移动云", UPLOAD.replace("50%", "20%"), HASH, HEARTBEAT, "上传完成：网盘响应已返回"):
                    self.assertFalse(self.record(late))
                self.assertEqual(self.item()["stage"], phase)
                self.assertEqual(self.item()["updated_at"], previous["updated_at"])
                self.assertEqual(self.item()["upload"]["percent"], 50)
                self.assertIsNone(self.item()["upload"]["speed_bps"])
                self.record("入库阶段：retry_wait；明确恢复")
                self.assertTrue(self.record("开始上传：移动云"))
                self.assertEqual(self.item()["stage"], "upload")
                self.assertTrue(self.record(UPLOAD.replace("50%", "20%")))
                self.assertEqual(self.item()["upload"]["percent"], 20)

    def test_heartbeat_preserves_bytes_and_byte_age_but_clears_stale_speed(self):
        self.record("开始上传：移动云（准备校验本地文件并提交网盘）")
        self.assertEqual(self.item()["upload"]["state"], "waiting")
        self.record(UPLOAD)
        first_timestamp = self.item()["updated_at"]
        self.now += 30
        self.record(HEARTBEAT)
        item = self.item()
        self.assertEqual(item["upload"]["percent"], 50)
        self.assertEqual(item["upload"]["bytes_done"], 2097152)
        self.assertIsNone(item["upload"]["speed_bps"])
        self.assertIsNone(item["upload"]["eta_seconds"])
        self.assertEqual(item["upload"]["state"], "waiting")
        self.assertEqual(item["age_seconds"], 30)
        self.assertTrue(item["stale"])
        self.assertNotEqual(item["updated_at"], first_timestamp)
        self.now += 2
        self.record(UPLOAD)
        self.assertEqual(self.item()["age_seconds"], 32)
        self.assertIsNone(self.item()["upload"]["speed_bps"])
        self.record(UPLOAD.replace("2 MiB", "3 MiB").replace("50%", "75%"))
        self.assertEqual(self.item()["age_seconds"], 0)
        self.assertFalse(self.item()["stale"])
        self.assertEqual(self.item()["upload"]["state"], "running")

    def test_download_and_hash_heartbeats_preserve_byte_age_and_channel_scope(self):
        for heartbeat, progress, channel in (
            ("等待进度：暂未收到下载统计", DOWNLOAD, "download"),
            ("进度暂未变化：等待下载连接恢复", DOWNLOAD, "download"),
            ("本地校验仍在执行：已 30 秒没有新的校验进度", HASH, "hash"),
        ):
            with self.subTest(channel=channel, heartbeat=heartbeat):
                self.record("入库阶段：planning；新一轮")
                self.record(progress)
                original = self.item()[channel]
                self.now += 30
                self.assertTrue(self.record(heartbeat))
                item = self.item()
                self.assertEqual(item["stage"], channel)
                self.assertEqual(item["age_seconds"], 30)
                self.assertEqual(item[channel]["bytes_done"], original["bytes_done"])
                self.assertEqual(item[channel]["percent"], original["percent"])
                self.assertEqual(item[channel]["state"], "waiting")
                self.assertIsNone(item[channel]["speed_bps"])
                self.assertIsNone(item[channel]["eta_seconds"])
                self.assertIsNone(item["upload"]["bytes_done"])
                self.assertFalse(item["completed"])
                self.record("入库阶段：target_verifying；持久状态已推进")
                self.assertFalse(self.record(heartbeat))
                self.assertEqual(self.item()["stage"], "target_verifying")

        self.record("入库阶段：retry_wait；明确恢复")
        self.record(UPLOAD)
        self.assertTrue(self.record("本地校验仍在执行：迟到的校验心跳"))
        self.assertEqual(self.item()["stage"], "upload")
        self.assertEqual(self.item()["upload"]["bytes_done"], 2097152)

    def test_zero_unknown_values_snapshot_isolation_and_capacity(self):
        self.record("下载中 待下载.mkv：0 B / 4 MiB，0%，速度 0 B/s，预计剩余 -")
        self.assertEqual(self.item()["download"]["bytes_done"], 0)
        self.assertEqual(self.item()["download"]["percent"], 0)
        self.assertIsNone(self.item()["download"]["eta_seconds"])
        self.assertFalse(self.item()["completed"])
        self.now += 16
        self.assertTrue(self.item()["stale"])
        self.assertEqual(self.item()["download"]["state"], "running")
        self.assertIsNone(self.item()["download"]["speed_bps"])
        detached = self.item()
        detached["download"]["percent"] = 99
        self.assertEqual(self.item()["download"]["percent"], 0)
        for job_id in range(100, 156):
            self.record("上传中：0%", job_id=job_id)
        items = self.progress.snapshot()
        self.assertEqual(len(items), 50)
        self.assertEqual(items[0]["job_id"], 155)
        self.assertEqual(items[-1]["job_id"], 106)
        self.assertTrue(all(not key.startswith("_") for item in items for key in item))
        self.assertFalse(self.record("ordinary diagnostics", job_id=999))
        self.assertEqual(len(self.progress.snapshot()), 50)

    def relay(self, kind, **values):
        return self.record(f"relay-{kind}: " + json.dumps({"job_id": 15, **values}))

    def pipeline(self):
        self.relay("buffer", enabled=True, depth=2, ready=1, reserved=1, waiting=False, reason="prefetch")

    def test_pipeline_keeps_upload_file_and_clock_while_next_download_run_starts(self):
        self.pipeline()
        self.relay("file", channel="upload", filename="Episode 1.mkv", file_id="up:1", run_id=7)
        self.relay("progress", filename="Episode 1.mkv", file_id="up:1", message=UPLOAD.replace("示例影片", "Episode 1"))
        self.now += 20
        self.relay("file", channel="download", filename="Episode 2.mkv", file_id="dl:2", run_id=8)
        self.record("开始执行 rclone 搬运任务，新文件", current_run_id=8)
        self.record(DOWNLOAD.replace("示例影片", "Episode 2"), current_run_id=8)
        item = self.item()
        self.assertEqual(item["run_id"], 8)
        self.assertEqual(item["upload"]["filename"], "Episode 1.mkv")
        self.assertEqual(item["upload"]["run_id"], 7)
        self.assertEqual(item["upload"]["file_id"], "up:1")
        self.assertEqual(item["upload"]["percent"], 50)
        self.assertTrue(item["upload"]["active"])
        self.assertTrue(item["upload"]["stale"])
        self.assertEqual(item["upload"]["age_seconds"], 20)
        self.assertIsNone(item["upload"]["speed_bps"])
        self.assertEqual(item["download"]["filename"], "Episode 2.mkv")
        self.assertEqual(item["download"]["file_id"], "dl:2")
        self.assertEqual(item["download"]["run_id"], 8)
        self.assertFalse(item["download"]["stale"])
        self.assertEqual(item["download"]["speed_bps"], 524288)
        self.assertFalse(item["stale"])
        self.assertFalse(item["completed"])

    def test_pipeline_file_completion_does_not_block_the_next_file_or_other_lane(self):
        self.pipeline()
        self.record(DOWNLOAD)
        self.record(UPLOAD)
        self.record(HANDOFF)
        self.assertTrue(self.item()["upload"]["active"])
        self.assertFalse(self.item()["download"]["active"])
        self.assertEqual(self.item()["stage"], "transferring")
        self.record("上传完成：本文件完成")
        self.assertEqual(self.item()["stage"], "transferring")
        self.assertFalse(self.item()["completed"])
        self.assertTrue(self.record(DOWNLOAD.replace("示例影片", "另一文件")))
        self.assertTrue(self.record(UPLOAD.replace("示例影片", "另一文件").replace("50%", "10%")))
        self.assertEqual(self.item()["upload"]["percent"], 10)
        self.assertTrue(self.item()["download"]["active"])
        self.record("入库阶段：target_verifying；任务已进入正式校验")
        self.assertFalse(self.record(DOWNLOAD))
        self.assertFalse(self.relay("buffer", enabled=True, depth=2, ready=2, reserved=0, waiting=True))

    def test_zero_prefetch_keeps_sequential_file_identity_without_closing_the_job(self):
        self.assertTrue(self.relay("buffer", enabled=True, depth=0, ready=0, reserved=0, waiting=False))
        self.relay("file", channel="upload", filename="first.mkv", file_id="up:1")
        self.relay("progress", filename="first.mkv", file_id="up:1", message="上传完成：本文件完成")
        self.assertEqual(self.item()["stage"], "transferring")
        self.assertEqual(self.item()["relay_buffer"]["depth"], 0)
        self.relay("file", channel="upload", filename="second.mkv", file_id="up:2")
        self.assertFalse(self.relay("progress", filename="first.mkv", file_id="up:1", message="上传完成：迟到重复完成"))
        self.assertTrue(self.relay("progress", filename="second.mkv", file_id="up:2", message="上传中：10%"))
        self.assertEqual(self.item()["upload"]["file_id"], "up:2")
        self.assertEqual(self.item()["upload"]["percent"], 10)
        self.assertFalse(self.item()["completed"])
        self.assertFalse(self.relay("buffer", enabled=True, depth=0, ready=1, reserved=0, waiting=False))

    def test_context_reset_has_no_old_metrics_and_repeated_context_is_idempotent(self):
        self.pipeline()
        self.relay("file", channel="upload", filename="folder/first.mkv", file_id="up:1", run_id=7)
        self.record(UPLOAD.replace("示例影片", "first"))
        self.assertEqual(self.item()["upload"]["filename"], "folder/first.mkv")
        self.now += 10
        self.relay("file", channel="upload", filename="folder/first.mkv", file_id="up:1", run_id=7)
        self.assertEqual(self.item()["upload"]["percent"], 50)
        self.assertEqual(self.item()["upload"]["age_seconds"], 10)
        self.record(UPLOAD.replace("示例影片", "first"))
        self.assertEqual(self.item()["upload"]["age_seconds"], 10)
        self.relay("file", channel="upload", filename="folder/second.mkv", file_id="up:2", run_id=8)
        self.assertIsNone(self.item()["upload"]["percent"])
        self.assertIsNone(self.item()["upload"]["bytes_done"])
        self.assertIsNone(self.item()["upload"]["speed_bps"])
        self.assertFalse(self.record(UPLOAD.replace("示例影片", "first")))
        self.assertTrue(self.record(UPLOAD.replace("示例影片", "second")))
        self.assertEqual(self.item()["upload"]["file_id"], "up:2")

    def test_delayed_wrapped_upload_events_cannot_close_or_overwrite_the_next_file(self):
        self.pipeline()
        self.relay("file", channel="upload", filename="same.mkv", file_id="up:2", run_id=8)
        self.relay("progress", filename="same.mkv", file_id="up:2", message="上传中：20%")
        before = self.item()
        for message in ("开始上传：云盘", "上传中：100%", HEARTBEAT, "上传完成：旧文件完成"):
            with self.subTest(message=message):
                self.assertFalse(self.relay("progress", filename="same.mkv", file_id="up:1", message=message))
        self.assertEqual(self.item(), before)
        self.assertEqual(self.item()["upload"]["percent"], 20)
        self.assertEqual(self.item()["upload"]["filename"], "same.mkv")
        self.assertFalse(self.relay("progress", filename="same.mkv", file_id="up:2", message="入库阶段：completed"))

    def test_pipeline_hash_and_heartbeat_are_independent_of_download(self):
        self.pipeline()
        self.relay("file", channel="upload", filename="示例影片.mkv", file_id="up:1", run_id=7)
        self.relay("progress", filename="示例影片.mkv", file_id="up:1", message=HASH)
        self.now += 30
        self.record(DOWNLOAD.replace("示例影片", "next"), current_run_id=8)
        self.record("本地校验仍在执行：等待本地文件")
        item = self.item()
        self.assertEqual(item["hash"]["run_id"], 7)
        self.assertTrue(item["hash"]["stale"])
        self.assertEqual(item["hash"]["state"], "waiting")
        self.assertTrue(item["hash"]["active"])
        self.assertFalse(item["upload"]["active"])
        self.assertIsNone(item["upload"]["bytes_done"])
        self.assertFalse(item["download"]["stale"])
        self.assertEqual(item["download"]["speed_bps"], 524288)
        self.relay("progress", filename="示例影片.mkv", file_id="up:1", message=UPLOAD)
        self.assertFalse(self.item()["hash"]["active"])
        self.assertTrue(self.item()["download"]["active"])
        self.assertTrue(self.item()["upload"]["active"])

    def test_buffer_is_read_only_detached_and_scoped_without_refreshing_byte_age(self):
        self.record(DOWNLOAD)
        before = self.item()
        self.now += 20
        self.relay("buffer", enabled=True, depth=2, ready=1, reserved=1, waiting=True, reason="full")
        item = self.item()
        self.assertEqual(item["stage"], before["stage"])
        self.assertEqual(item["updated_at"], before["updated_at"])
        self.assertEqual(item["download"]["age_seconds"], 20)
        self.assertTrue(item["download"]["stale"])
        self.assertEqual(item["relay_buffer"]["ready"], 1)
        item["relay_buffer"]["ready"] = 99
        self.assertEqual(self.item()["relay_buffer"]["ready"], 1)
        self.assertFalse(self.relay("buffer", enabled=True, depth=2, ready=3, reserved=0, waiting=True))
        self.assertFalse(self.relay("buffer", enabled=True, depth=2, ready=True, reserved=0, waiting=True))
        self.assertTrue(self.progress.record('relay-buffer: {"job_id": 16, "enabled": true, "depth": 1, "ready": 0, "reserved": 1, "waiting": false}', current_job_id=15, current_run_id=7))
        self.assertIsNone(self.item(16)["run_id"])
        self.assertFalse(self.record('relay-buffer: {"job_id": 16, "depth": 1, "ready": 0, "reserved": 1, "waiting": false}', job_id=15))


class RcloneLiveProgressIntegrationTests(unittest.TestCase):
    def test_raw_stdout_milestone_is_logged_but_only_application_bridge_can_complete(self):
        sink = RcloneLogSink(current_job_id=lambda: 15, current_run_id=lambda: 7)
        service = RcloneService.__new__(RcloneService)
        service.log_sink = sink
        try:
            service._append_log("上传中：100%")
            self.assertFalse(sink.transfers()[0]["completed"])
            forged = "任务#15 入库阶段：completed；raw subprocess diagnostic"
            service._append_log(forged)
            self.assertIn(forged, sink.list()[-1])
            self.assertFalse(sink.transfers()[0]["completed"])
            service.append_external_log("入库阶段：completed；已提交完成状态", job_id=15)
            self.assertTrue(sink.transfers()[0]["completed"])
            self.assertNotIn("trusted_milestone", sink.transfers()[0])
        finally:
            sink.close()

    def test_pipe_reader_logs_and_status_are_readable_before_process_finishes(self):
        blocked = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        sink = RcloneLogSink(current_job_id=lambda: 15, current_run_id=lambda: 7)

        def stdout():
            yield DOWNLOAD + "\n"
            blocked.set()
            if not release.wait(2):
                raise AssertionError("test producer was not released")
        process = SimpleNamespace(stdout=stdout(), wait=lambda: 0)
        result = []

        def run():
            result.append(RcloneProcessRunner().run(
                ["fake-rclone"], cwd=Path.cwd(), env={}, on_line=sink.append, on_started=lambda _process: None,
            ))
            completed.set()

        with patch("fnos_media_import.services.rclone_process_runner.subprocess.Popen", return_value=process):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(blocked.wait(1))
                self.assertFalse(completed.is_set())
                self.assertIn(DOWNLOAD, sink.list()[-1])
                service = RcloneService.__new__(RcloneService)
                service.lock = threading.Lock()
                service.log_sink = sink
                service.run_queue = SimpleNamespace(snapshot=lambda **_kwargs: {"queue_count": 0})
                service.run_state = RcloneRunState(current_run_id=7)
                service.enabled = True
                service.worker_thread = thread
                service._script_path = lambda: Path("fake-rclone")
                service._category_dir_env = lambda _run: {}
                status = service.status()
                self.assertEqual(status["transfers"][0]["download"]["percent"], 25)
                self.assertTrue(status["running"])
            finally:
                release.set()
                thread.join(timeout=2)
                sink.close()
        self.assertEqual(result[0].exit_code, 0)

    def test_database_block_and_parser_failure_never_block_live_buffer(self):
        entered = threading.Event()
        release = threading.Event()
        batches = []

        def store(events):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test database was not released")
            batches.append(events)

        sink = RcloneLogSink(
            database=SimpleNamespace(rclone=SimpleNamespace(add_rclone_events=store)),
            current_job_id=lambda: 15, current_run_id=lambda: 7,
        )
        times = ["2026-10-11T14:32:30+00:00", "2026-10-11T14:32:31+00:00", "2026-10-11T14:32:32+00:00"]
        try:
            with patch("fnos_media_import.services.rclone_log_sink.utc_now_iso", side_effect=times):
                sink.append(DOWNLOAD)
                self.assertTrue(entered.wait(1))
                sink.append("任务#16 " + UPLOAD)
                self.assertEqual({item["job_id"] for item in sink.transfers()}, {15, 16})
                with patch.object(sink._progress, "record", side_effect=RuntimeError("telemetry failed")):
                    sink.append("ordinary log must survive")
                self.assertIn("ordinary log must survive", sink.list()[-1])
        finally:
            release.set()
            sink.close(timeout=2)
        events = [event for batch in batches for event in batch]
        self.assertEqual([event[3] for event in events], [times[0], times[2]])

    def test_external_job_logs_never_persist_under_another_jobs_current_run(self):
        batches = []
        sink = RcloneLogSink(
            database=SimpleNamespace(rclone=SimpleNamespace(add_rclone_events=lambda events: batches.append(events))),
            current_job_id=lambda: 17, current_run_id=lambda: 42,
        )
        try:
            sink.append("任务#16 " + UPLOAD)
            sink.append(UPLOAD, job_id=18)
            sink.append("任务#16 ordinary diagnostic")
            sink.append(DOWNLOAD)
            self.assertEqual(len(sink.list()), 4)
            transfers = {item["job_id"]: item for item in sink.transfers()}
            self.assertEqual(set(transfers), {16, 17, 18})
            self.assertIsNone(transfers[16]["run_id"])
            self.assertIsNone(transfers[18]["run_id"])
            self.assertEqual(transfers[17]["run_id"], 42)
        finally:
            sink.close(timeout=2)
        events = [event for batch in batches for event in batch]
        self.assertEqual([event[:3] for event in events], [(42, "info", DOWNLOAD)])

    def test_event_repository_preserves_received_time_and_supports_legacy_tuple(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE rclone_runs (id INTEGER PRIMARY KEY);
            INSERT INTO rclone_runs VALUES (7);
            CREATE TABLE rclone_events (id INTEGER PRIMARY KEY, run_id INTEGER, level TEXT, message TEXT, raw_data TEXT, created_at TEXT);
        """)

        @contextmanager
        def connect():
            with connection:
                yield connection

        repository = RcloneRepository(connect, lambda row: dict(row) if row else None)
        received = "2026-10-11T14:32:30+00:00"
        flushed = "2026-10-11T14:35:00+00:00"
        with patch("fnos_media_import.repositories.rclone_repository.utc_now", return_value=flushed):
            repository.add_rclone_events([(7, "info", "live", received), (7, "info", "legacy")])
        rows = connection.execute("SELECT message, created_at FROM rclone_events ORDER BY id").fetchall()
        self.assertEqual(rows, [("live", received), ("legacy", flushed)])


if __name__ == "__main__":
    unittest.main()
