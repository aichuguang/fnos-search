from __future__ import annotations

import threading
from typing import Any, Callable


class RcloneHistoryRepairWorker:
    """Runs one lease-protected recovery pass for unfinished historical rclone jobs."""

    lease_name = "rclone-history-repair"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str,
        repair: Callable[..., dict[str, Any]],
        log: Callable[[str], None],
        limit: int = 50,
        interval_seconds: float = 300,
    ) -> None:
        self.database = database
        self.owner_id = owner_id
        self.repair = repair
        self.log = log
        self.limit = max(1, int(limit or 50))
        self.interval_seconds = max(0.05, float(interval_seconds or 300))
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._run,
            name=self.lease_name,
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                # Lease acquisition itself can fail before run_once enters its
                # internal try/finally.  Keep the only periodic recovery thread
                # alive and retry on the next interval.
                try:
                    self.log(f"历史 rclone 状态兜底线程异常：{exc}")
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(self.interval_seconds):
                break

    def run_once(self) -> dict[str, Any]:
        if not self.database.acquire_scheduler_lease(self.lease_name, self.owner_id, 300):
            return {"success": True, "skipped": True, "message": "其他 Worker 正在执行历史状态恢复"}
        try:
            result = self.repair(limit=self.limit)
            if result.get("run_ids"):
                self.log(result.get("message") or "已完成历史 rclone 状态兜底修复")
            return result
        except Exception as exc:  # noqa: BLE001
            message = f"历史 rclone 状态兜底修复异常：{exc}"
            self.log(message)
            return {"success": False, "message": message, "run_ids": []}
        finally:
            self.database.release_scheduler_lease(self.lease_name, self.owner_id)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(5.0, self.interval_seconds + 1.0))
        # A timed-out join means repair may still be mutating state.  Releasing
        # its distributed lease here would let another worker overlap it; the
        # run_once finally block is the sole owner of lease release.
