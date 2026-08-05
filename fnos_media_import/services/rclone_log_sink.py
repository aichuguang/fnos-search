from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any, Callable

from ..constants import EVENT_ERROR, EVENT_INFO


class RcloneLogSink:
    """Thread-safe in-memory log buffer with optional run-event persistence."""

    def __init__(
        self,
        *,
        database: Any = None,
        current_run_id: Callable[[], int | None] | None = None,
        max_lines: int = 500,
    ) -> None:
        self.database = database
        self.current_run_id = current_run_id or (lambda: None)
        self._lines: deque[str] = deque(maxlen=self._normalize_max_lines(max_lines))
        self._lock = threading.Lock()

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
            self.database.add_rclone_event(run_id, level, text)

    def list(self, limit: int = 200) -> list[str]:
        with self._lock:
            items = list(self._lines)
        return items if limit <= 0 else items[-limit:]

    @staticmethod
    def _normalize_max_lines(value: Any) -> int:
        try:
            return max(1, int(value or 500))
        except (TypeError, ValueError):
            return 500
