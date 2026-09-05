from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any, Callable

from ..constants import EVENT_ERROR, EVENT_INFO


class RcloneLogSink:
    """Thread-safe in-memory log buffer with asynchronous run-event persistence.

    append() 只做内存写入（读 rclone 管道的线程调用，绝不能被数据库阻塞——
    Worker 与 Web 容器共用同一个 SQLite 文件，跨进程写锁竞争曾把读线程卡住
    一分钟以上，进度行全部攒到传输结束才涌出）。事件持久化交给后台
    flusher 线程批量落库，失败即丢弃：实时日志以内存缓冲为准，事件表是
    尽力而为的持久化副本。
    """

    _MAX_PENDING_EVENTS = 2000

    def __init__(
        self,
        *,
        database: Any = None,
        current_run_id: Callable[[], int | None] | None = None,
        max_lines: int = 500,
        flush_interval: float = 1.0,
    ) -> None:
        self.database = database
        self.current_run_id = current_run_id or (lambda: None)
        self._lines: deque[str] = deque(maxlen=self._normalize_max_lines(max_lines))
        self._lock = threading.Lock()
        self._flush_interval = max(0.05, float(flush_interval or 1.0))
        self._pending: deque[tuple[int, str, str]] = deque()
        self._pending_lock = threading.Lock()
        self._flush_event = threading.Event()
        self._closed = False
        self._flusher: threading.Thread | None = None
        if self.database is not None:
            self._flusher = threading.Thread(
                target=self._flush_loop,
                name="rclone-log-event-flusher",
                daemon=True,
            )
            self._flusher.start()

    def resize(self, max_lines: Any) -> None:
        with self._lock:
            self._lines = deque(self._lines, maxlen=self._normalize_max_lines(max_lines))

    def append(self, message: str) -> None:
        text = str(message or "")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = text if text.startswith("[20") else f"[{timestamp}] {text}"
        with self._lock:
            self._lines.append(line)
        run_id = self.current_run_id()
        if self.database and run_id:
            lowered = text.lower()
            level = EVENT_ERROR if any(token in lowered for token in ("失败", "异常", "error", "failed")) else EVENT_INFO
            with self._pending_lock:
                if len(self._pending) >= self._MAX_PENDING_EVENTS:
                    self._pending.popleft()
                self._pending.append((int(run_id), level, text))
            self._flush_event.set()

    def list(self, limit: int = 200) -> list[str]:
        with self._lock:
            items = list(self._lines)
        return items if limit <= 0 else items[-limit:]

    def flush_pending(self) -> None:
        """同步把待持久化事件写入数据库（测试与关停兜底用）。"""
        while True:
            with self._pending_lock:
                batch = list(self._pending)
                self._pending.clear()
            if not batch:
                return
            try:
                self.database.rclone.add_rclone_events(batch)
            except Exception:  # noqa: BLE001
                # 事件持久化是尽力而为：失败丢批，不影响内存实时日志。
                return

    def close(self, timeout: float = 3.0) -> None:
        self._closed = True
        self._flush_event.set()
        flusher = self._flusher
        if flusher is not None and flusher.is_alive():
            flusher.join(timeout=timeout)

    def _flush_loop(self) -> None:
        while True:
            self._flush_event.wait()
            self._flush_event.clear()
            self.flush_pending()
            if self._closed:
                return
            # close 竞态兜底：等待间隙里新入队的事件在超时后再刷一轮。
            self._flush_event.wait(self._flush_interval)

    @staticmethod
    def _normalize_max_lines(value: Any) -> int:
        try:
            return max(1, int(value or 500))
        except (TypeError, ValueError):
            return 500
