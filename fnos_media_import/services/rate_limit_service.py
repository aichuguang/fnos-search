from __future__ import annotations

import threading
import time
from typing import Any, Callable


class RateLimitService:
    """Coordinates durable fixed-window limits without depending on Flask."""

    def __init__(
        self,
        *,
        repository: Any,
        enabled: Callable[[], bool],
        window_seconds: Callable[[], int],
        clock: Callable[[], float] = time.time,
        cleanup_interval_seconds: int = 60,
    ) -> None:
        self.repository = repository
        self.enabled = enabled
        self.window_seconds = window_seconds
        self.clock = clock
        self.cleanup_interval_seconds = max(1, int(cleanup_interval_seconds))
        self._cleanup_lock = threading.Lock()
        self._next_cleanup_at = 0.0

    def check(self, bucket_key: str, *, limit: int):
        if not self.enabled() or int(limit) <= 0:
            return None
        self._cleanup_if_due()
        return self.repository.check(
            str(bucket_key),
            limit=int(limit),
            window_seconds=max(1, int(self.window_seconds())),
        )

    def _cleanup_if_due(self) -> None:
        now = self.clock()
        if now < self._next_cleanup_at or not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            if now < self._next_cleanup_at:
                return
            self.repository.prune_expired(limit=500)
            self._next_cleanup_at = now + self.cleanup_interval_seconds
        finally:
            self._cleanup_lock.release()
