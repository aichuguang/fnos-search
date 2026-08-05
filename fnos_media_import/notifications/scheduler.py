"""每日摘要调度器：租约保护的定时任务，生成 digest_daily 通知。

在通知配置指定的时区和小时触发一次；重复触发由发射器的幂等键
``notify:digest_daily:YYYY-MM-DD`` 兜底，多进程下不会重复发送。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..time_utils import utc_now_iso, utc_now_iso_offset
from . import config as notify_config
from . import events as event_defs
from .emitter import emit_notification


class NotificationDigestScheduler:
    lease_name = "notification-daily-digest"

    def __init__(
        self,
        *,
        database: Any,
        owner_id: str = "",
        hour: int = 9,
        interval_seconds: int = 60,
        shutdown_timeout_seconds: float = 5,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.owner_id = str(owner_id or f"notification-digest-{id(self)}")
        self.hour = max(0, min(23, int(hour)))
        self.interval_seconds = max(10, int(interval_seconds))
        self.shutdown_timeout_seconds = max(0, float(shutdown_timeout_seconds))
        self.log = log or (lambda _message: None)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_run_at = ""
        self.last_error = ""
        self._run_lock = threading.Lock()
        self._last_emitted_date = ""
        self._last_pruned_date = ""
        self._last_anonymized_date = ""

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name=self.lease_name, daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.shutdown_timeout_seconds)

    def run_once(self) -> dict[str, Any]:
        if self.stop_event.is_set():
            return {"success": True, "skipped": True, "stopped": True}
        if not self._run_lock.acquire(blocking=False):
            return {"success": True, "skipped": True, "busy": True}
        acquired = False
        try:
            acquired = bool(
                self.database.acquire_scheduler_lease(
                    self.lease_name, self.owner_id, ttl_seconds=60
                )
            )
            if not acquired:
                return {"success": True, "skipped": True, "lease": False}
            config = notify_config.read_config(self.database)
            timezone_name = str(config.get("digest_timezone") or "Asia/Shanghai")
            try:
                timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                if timezone_name in {"UTC", "Etc/UTC", "GMT"}:
                    timezone = datetime_timezone.utc
                else:
                    timezone = datetime_timezone(timedelta(hours=8), name="Asia/Shanghai")
                    # Windows may not ship the IANA database. The documented default
                    # remains exact under the fixed UTC+8 fallback, so it is not invalid.
                    if timezone_name != "Asia/Shanghai":
                        self._safe_log(
                            f"invalid notification digest timezone: {timezone_name}; fallback=Asia/Shanghai"
                        )
            now = datetime.now(timezone)
            date_key = now.strftime("%Y-%m-%d")
            if date_key != self._last_pruned_date:
                retention_days = max(7, int(config.get("delivery_retention_days") or 90))
                self.database.prune_notification_deliveries(
                    before=utc_now_iso_offset(days=-retention_days)
                )
                self._last_pruned_date = date_key
            if date_key != self._last_anonymized_date:
                anonymize_days = max(7, int(config.get("guest_anonymize_days") or 30))
                anonymized = self.database.anonymize_guest_notification_subscriptions(
                    older_than=utc_now_iso_offset(days=-anonymize_days)
                )
                if anonymized:
                    self._safe_log(
                        f"anonymized {anonymized} terminal guest notification subscriptions"
                    )
                self._last_anonymized_date = date_key
            configured_hour = max(0, min(23, int(config.get("digest_hour", self.hour))))
            if now.hour != configured_hour or date_key == self._last_emitted_date:
                return {"success": True, "skipped": True}
            result = self._emit(date_key)
            self.last_run_at = utc_now_iso()
            self.last_error = ""
            return {"success": True, **result}
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._safe_log(f"notification digest failed: {exc}")
            return {"success": False, "message": str(exc)}
        finally:
            if acquired:
                try:
                    self.database.release_scheduler_lease(self.lease_name, self.owner_id)
                except Exception as exc:  # noqa: BLE001
                    self._safe_log(f"notification digest lease release failed: {exc}")
            self._run_lock.release()

    def _emit(self, date_key: str) -> dict[str, Any]:
        stats = self._collect_stats()
        result = emit_notification(
            self.database,
            event_defs.EVENT_DIGEST_DAILY,
            {**stats, "date": date_key},
            idempotency_key=event_defs.idempotency_key(event_defs.EVENT_DIGEST_DAILY, date_key),
        )
        if result is None:
            return {"emitted": False, "reason": "notifications disabled or no channel"}
        self._last_emitted_date = date_key
        return {
            "emitted": True,
            "task_id": result["task_id"],
            "created": result["created"],
            "channels": result["channels"],
            "stats": stats,
        }

    def _collect_stats(self) -> dict[str, int]:
        cutoff = utc_now_iso_offset(hours=-24)
        with self.database.connect() as conn:
            new_count = conn.execute(
                "SELECT COUNT(*) FROM guest_requests WHERE created_at >= ?", (cutoff,)
            ).fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM guest_requests WHERE status = 'pending_review'"
            ).fetchone()[0]
            done_count = conn.execute(
                "SELECT COUNT(*) FROM import_jobs WHERE status = 'done' AND updated_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            failed_count = conn.execute(
                "SELECT COUNT(*) FROM import_jobs WHERE status = 'failed' AND updated_at >= ?",
                (cutoff,),
            ).fetchone()[0]
        return {
            "new_count": int(new_count),
            "pending_review_count": int(pending_count),
            "done_count": int(done_count),
            "failed_count": int(failed_count),
        }

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                self._safe_log(f"notification digest loop error: {exc}")

    def _safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001
            pass
