from __future__ import annotations

import threading
import time
import unittest

from fnos_media_import.services.rclone_log_sink import RcloneLogSink
from fnos_media_import.services.rclone_service import RcloneService


class _GateRepository:
    """可闸门控制的 rclone 仓储替身：落库前先等待放行，模拟跨进程锁竞争卡死。"""

    def __init__(self) -> None:
        self.batches: list[list[tuple[int, str, str]]] = []
        self.gate = threading.Event()
        self.fail = False

    def add_rclone_events(self, events: list[tuple[int, str, str]]) -> None:
        if self.fail:
            raise RuntimeError("数据库写入失败")
        self.gate.wait(timeout=10)
        self.batches.append(list(events))


class _GateDatabase:
    def __init__(self) -> None:
        self.rclone = _GateRepository()


def _sink(database: _GateDatabase, run_id: int = 7) -> RcloneLogSink:
    return RcloneLogSink(database=database, current_run_id=lambda: run_id, max_lines=10)


class RcloneLogSinkAsyncPersistenceTests(unittest.TestCase):
    """append() 只写内存；DB 事件由后台线程批量落库。

    回归场景：Worker 与 Web 容器共用同一个 SQLite 文件，事件同步落库时
    读 rclone 管道的线程被跨进程写锁卡住一分钟以上，进度行攒到传输结束
    才一股脑涌出。append() 绝不能等待数据库。
    """

    def test_append_is_not_blocked_by_slow_database(self) -> None:
        database = _GateDatabase()
        sink = _sink(database)
        try:
            started = time.monotonic()
            for index in range(5):
                sink.append(f"进度 {index}")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
        finally:
            database.rclone.gate.set()
            sink.close()

    def test_pending_events_flush_in_order(self) -> None:
        database = _GateDatabase()
        sink = _sink(database, run_id=4)
        for index in range(4):
            sink.append(f"进度 {index}")

        database.rclone.gate.set()
        # 只由后台 flusher 单线程落库（close 收尾），避免并发落库导致批次乱序。
        sink.close(timeout=5)

        flushed = [event for batch in database.rclone.batches for event in batch]
        self.assertEqual([event[:3] for event in flushed], [(4, "info", f"进度 {index}") for index in range(4)])
        self.assertTrue(all(len(event) == 4 and event[3] for event in flushed))

    def test_close_flushes_remaining_events(self) -> None:
        database = _GateDatabase()
        sink = _sink(database, run_id=9)
        sink.append("下载中：50%")
        sink.append("失败：目标端校验未通过")

        database.rclone.gate.set()
        sink.close(timeout=5)

        flushed = [event for batch in database.rclone.batches for event in batch]
        self.assertEqual(flushed[0][:3], (9, "info", "下载中：50%"))
        self.assertEqual(flushed[1][:3], (9, "error", "失败：目标端校验未通过"))

    def test_flush_failure_is_swallowed(self) -> None:
        database = _GateDatabase()
        database.rclone.fail = True
        sink = _sink(database)
        sink.append("进度行")

        sink.flush_pending()  # 不得抛异常
        sink.close(timeout=5)

    def test_pending_queue_is_bounded(self) -> None:
        database = _GateDatabase()
        sink = _sink(database)
        try:
            for index in range(RcloneLogSink._MAX_PENDING_EVENTS + 120):
                sink.append(f"进度 {index}")
            with sink._pending_lock:
                self.assertLessEqual(len(sink._pending), RcloneLogSink._MAX_PENDING_EVENTS)
        finally:
            database.rclone.gate.set()
            sink.close()

    def test_lines_are_timestamped_and_capped(self) -> None:
        sink = RcloneLogSink(max_lines=3)
        sink.append("第一批")
        sink.append("第二批")
        sink.append("第三批")
        sink.append("第四批")

        lines = sink.list()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[20") and "第二批" in lines[0])
        self.assertTrue("第四批" in lines[-1])


class RcloneServiceExternalLogTests(unittest.TestCase):
    """主应用上传进度经 append_external_log 汇入 rclone 实时日志流。"""

    def _service(self) -> RcloneService:
        service = RcloneService.__new__(RcloneService)
        service.log_sink = RcloneLogSink()
        return service

    def test_external_log_gets_job_prefix_and_timestamp(self) -> None:
        service = self._service()

        result = service.append_external_log("上传中：45%", job_id=4)

        self.assertTrue(result["success"])
        line = service.log_sink.list()[-1]
        self.assertTrue(line.startswith("[20"))
        self.assertIn("任务#4 上传中：45%", line)

    def test_external_log_rejects_blank_message(self) -> None:
        service = self._service()

        result = service.append_external_log("   ")

        self.assertFalse(result["success"])
        self.assertEqual(service.log_sink.list(), [])


if __name__ == "__main__":
    unittest.main()
