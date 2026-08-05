from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..process_role import role_runs


logger = logging.getLogger(__name__)


class TrendingDiscoveryScheduler:
    """每日固定时间运行热榜发现，且使用 SQLite Scheduler 租约防止多进程重复。"""

    lease_name = "trending-discovery-scheduler"

    def __init__(
        self,
        *,
        service: Any,
        database: Any,
        owner_id: str,
        enabled: bool = False,
        run_at: str = "08:30",
        timezone_name: str = "Asia/Shanghai",
        process_role: str = "all",
        clock: Callable[[], datetime] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.database = database
        self.owner_id = str(owner_id or f"trending-scheduler-{id(self)}")
        self.enabled = bool(enabled)
        self.run_at = self._validate_run_at(run_at)
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        try:
            self.timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            # Windows Python installations often omit the optional tzdata
            # package. China has used UTC+8 without daylight saving since
            # 1991, so the configured default can safely use a fixed offset.
            if self.timezone_name == "Asia/Shanghai":
                self.timezone = timezone(timedelta(hours=8), self.timezone_name)
            else:
                raise ValueError(f"未知时区：{self.timezone_name}") from exc
        self.process_role = str(process_role or "all")
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self.log = log or logger
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_run_at = ""
        self.last_error = ""
        self.next_run_at = ""

    def start(self) -> None:
        if not self.enabled or not role_runs(self.process_role, "scheduler"):
            return
        if self.thread and self.thread.is_alive():
            return
        stop_event = threading.Event()
        self.stop_event = stop_event
        self.thread = threading.Thread(target=self._loop, args=(stop_event,), name=self.lease_name, daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.thread = None
        try:
            self.database.release_scheduler_lease(self.lease_name, self.owner_id)
        except Exception:  # noqa: BLE001
            self.log.exception("release trending scheduler lease failed")

    def apply_config(self, *, enabled: bool | None = None, run_at: str | None = None) -> None:
        previous_run_at = self.run_at
        if run_at is not None:
            self.run_at = self._validate_run_at(run_at)
        if enabled is not None:
            self.enabled = bool(enabled)
        if not self.enabled:
            self.shutdown()
            return
        if self.thread and self.thread.is_alive() and self.run_at != previous_run_at:
            self.stop_event.set()
            self.thread = None
        self.start()

    def run_now(self) -> dict[str, Any]:
        return self.service.run(trigger_type="manual")

    def status(self) -> dict[str, Any]:
        service_status = self.service.status()
        return {
            "enabled": self.enabled,
            "running": bool(self.thread and self.thread.is_alive()),
            "process_role": self.process_role,
            "run_at": self.run_at,
            "timezone": self.timezone_name,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "task_running": bool(service_status.get("running")),
            "latest_run": service_status.get("latest_run"),
            "last_result": service_status.get("last_result"),
        }

    def _loop(self, stop_event: threading.Event | None = None) -> None:
        active_stop_event = stop_event or self.stop_event
        while not active_stop_event.is_set():
            now = self.clock()
            next_run = self._next_run(now)
            self.next_run_at = next_run.isoformat(timespec="seconds")
            if active_stop_event.wait(max(0.1, (next_run - now).total_seconds())):
                break
            acquired_lease = False
            try:
                acquired_lease = bool(self.database.acquire_scheduler_lease(self.lease_name, self.owner_id, 3600))
                if not acquired_lease:
                    continue
                result = self.service.run(trigger_type="schedule", lease_held=True)
                self.last_run_at = self._utc_text(self.clock())
                self.last_error = "" if result.get("success") else "; ".join(result.get("errors") or [])
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.log.exception("trending discovery scheduler failed")
            finally:
                if acquired_lease:
                    try:
                        self.database.release_scheduler_lease(self.lease_name, self.owner_id)
                    except Exception:  # noqa: BLE001
                        self.log.exception("release trending scheduler lease failed")

    def _next_run(self, now: datetime) -> datetime:
        hour, minute = (int(part) for part in self.run_at.split(":", 1))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _validate_run_at(value: str) -> str:
        text = str(value or "08:30").strip()
        try:
            hour, minute = (int(part) for part in text.split(":", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("run_at 必须使用 HH:MM 格式") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("run_at 必须是有效的每日时间")
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _utc_text(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
