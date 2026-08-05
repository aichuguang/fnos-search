from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


class JobQueries(Protocol):
    def list(self, *, limit: int, offset: int = 0, **filters: Any) -> list[dict[str, Any]]: ...


class RequestQueries(Protocol):
    def list(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class AdminDashboardDependencies:
    jobs: JobQueries
    requests: RequestQueries
    reconcile_job: Callable[[dict[str, Any], str], dict[str, Any]]
    decorate_job: Callable[[dict[str, Any]], dict[str, Any]]
    sync_requests: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    system_status: Callable[[], dict[str, Any]]


class AdminDashboardService:
    RECENT_JOB_FIELDS = (
        "id",
        "request_token",
        "title",
        "category",
        "category_label",
        "source_type",
        "target_route",
        "status",
        "updated_at",
    )

    def __init__(self, dependencies: AdminDashboardDependencies) -> None:
        self._deps = dependencies

    def summary(self, *, limit: int = 200) -> dict[str, Any]:
        jobs = [
            self._deps.decorate_job(self._deps.reconcile_job(item, "admin_dashboard"))
            for item in self._deps.jobs.list(limit=limit, offset=0)
        ]
        requests = self._deps.sync_requests(self._deps.requests.list(limit=limit, offset=0))
        system = self._safe_status(self._deps.system_status)
        return {
            "total_recent_jobs": len(jobs),
            "total_recent_guest_requests": len(requests),
            "status_counts": self._count(jobs),
            "guest_request_status_counts": self._count(requests),
            "recent_jobs": [self._dashboard_job(item) for item in jobs[:8]],
            "rclone": system.get("rclone") if isinstance(system.get("rclone"), dict) else {},
            "health": self._health_summary(system),
        }

    @classmethod
    def _health_summary(cls, system: dict[str, Any]) -> dict[str, Any]:
        items = [
            cls._worker_health(system.get("worker_queue")),
            cls._queue_health(system.get("worker_queue")),
            cls._rclone_health(system.get("rclone")),
            cls._organizer_health(system.get("organizer")),
            cls._scheduler_health(system.get("update_scheduler"), system.get("trending_discovery")),
            cls._data_health(system.get("data")),
        ]
        issue_count = sum(item["state"] in {"warn", "error"} for item in items)
        error_count = sum(item["state"] == "error" for item in items)
        return {
            "state": "error" if error_count else "warn" if issue_count else "ok",
            "issue_count": issue_count,
            "items": items,
        }

    @staticmethod
    def _safe_status(loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            value = loader()
            return dict(value) if isinstance(value, dict) else {}
        except Exception as exc:  # noqa: BLE001
            return {"status_error": str(exc)}

    @staticmethod
    def _item(item_id: str, label: str, state: str, summary: str, detail: str = "") -> dict[str, str]:
        return {"id": item_id, "label": label, "state": state, "summary": summary, "detail": detail}

    @classmethod
    def _worker_health(cls, value: Any) -> dict[str, str]:
        status = value if isinstance(value, dict) else {}
        if not status:
            return cls._item("worker", "后台任务", "error", "状态读取失败")
        if status.get("dispatch_error") or status.get("runtime_error"):
            return cls._item("worker", "后台任务", "error", "运行异常", "请查看日志中心")
        if not status.get("dispatch_enabled", status.get("enabled", False)):
            return cls._item("worker", "后台任务", "idle", "未启用")
        if status.get("runtime_required") and (
            not status.get("runtime_running") or status.get("heartbeat_stale")
        ):
            return cls._item("worker", "后台任务", "error", "Worker 心跳异常", "任务可能无法继续执行")
        errors = int(status.get("consecutive_runtime_errors") or 0)
        if errors:
            return cls._item("worker", "后台任务", "error", f"连续 {errors} 次运行错误", "请查看日志中心")
        heartbeat_age = status.get("heartbeat_age_seconds")
        detail = f"心跳 {int(float(heartbeat_age))} 秒前" if heartbeat_age is not None and status.get("runtime_running") else "队列调度正常"
        return cls._item("worker", "后台任务", "ok", "运行正常", detail)

    @classmethod
    def _queue_health(cls, value: Any) -> dict[str, str]:
        status = value if isinstance(value, dict) else {}
        if not status or status.get("repository_error"):
            return cls._item("queue", "任务队列", "error", "队列读取失败")
        pending = int(status.get("pending") or 0)
        running = int(status.get("running") or 0)
        expired = int(status.get("expired_leases") or 0)
        if expired:
            return cls._item("queue", "任务队列", "error", f"{expired} 项执行租约过期", "任务可能已经卡住")
        oldest_age = cls._age_seconds((status.get("oldest_pending") or {}).get("created_at"))
        if pending and oldest_age is not None and oldest_age >= 900:
            minutes = max(1, int(oldest_age // 60))
            return cls._item("queue", "任务队列", "warn", f"最老任务已等待 {minutes} 分钟", f"等待 {pending} 项，执行 {running} 项")
        if pending or running:
            return cls._item("queue", "任务队列", "active", f"等待 {pending} 项，执行 {running} 项")
        return cls._item("queue", "任务队列", "ok", "队列已清空")

    @classmethod
    def _rclone_health(cls, value: Any) -> dict[str, str]:
        status = value if isinstance(value, dict) else {}
        if not status:
            return cls._item("rclone", "文件搬运", "error", "状态读取失败")
        if not status.get("enabled", True):
            return cls._item("rclone", "文件搬运", "idle", "未启用")
        queue_count = int(status.get("queue_count") or 0)
        if status.get("running"):
            return cls._item("rclone", "文件搬运", "active", "正在搬运", f"队列 {queue_count} 项")
        if status.get("last_exit_code") not in (None, 0) or status.get("last_error"):
            return cls._item("rclone", "文件搬运", "error", "最近一次搬运失败", "点击查看日志")
        if queue_count:
            return cls._item("rclone", "文件搬运", "warn", f"{queue_count} 项等待搬运")
        return cls._item("rclone", "文件搬运", "ok", "当前空闲", "最近执行正常")

    @classmethod
    def _organizer_health(cls, value: Any) -> dict[str, str]:
        status = value if isinstance(value, dict) else {}
        if not status:
            return cls._item("organizer", "整理入库", "error", "状态读取失败")
        if not status.get("enabled"):
            return cls._item("organizer", "整理入库", "idle", "未启用")
        if not status.get("openlist_configured"):
            return cls._item("organizer", "整理入库", "warn", "OpenList 未配置", "请检查系统设置")
        counts = status.get("counts") if isinstance(status.get("counts"), dict) else {}
        failed = int(counts.get("failed") or 0)
        executing = int(counts.get("executing") or 0)
        waiting = sum(int(counts.get(key) or 0) for key in ("waiting_openlist", "stabilizing", "pending"))
        if failed:
            return cls._item("organizer", "整理入库", "error", f"{failed} 项整理失败", "点击查看 OpenList 标准化")
        if executing or waiting:
            return cls._item("organizer", "整理入库", "active", f"执行 {executing} 项，等待 {waiting} 项")
        return cls._item("organizer", "整理入库", "ok", "服务就绪")

    @classmethod
    def _scheduler_health(cls, updates_value: Any, trending_value: Any) -> dict[str, str]:
        updates = updates_value if isinstance(updates_value, dict) else {}
        trending = trending_value if isinstance(trending_value, dict) else {}
        errors = [str(item.get("last_error") or "").strip() for item in (updates, trending)]
        if any(errors):
            return cls._item("scheduler", "自动任务", "error", "最近一次调度失败", "点击查看定时追更")
        enabled_count = int(bool(updates.get("enabled"))) + int(bool(trending.get("enabled")))
        if not enabled_count:
            return cls._item("scheduler", "自动任务", "idle", "未启用")
        if updates.get("task_running") or trending.get("task_running"):
            return cls._item("scheduler", "自动任务", "active", "正在执行")
        next_times = [str(item.get("next_run_at") or "").strip() for item in (updates, trending) if item.get("next_run_at")]
        detail = f"下次 {min(next_times)}" if next_times else f"已启用 {enabled_count} 项"
        return cls._item("scheduler", "自动任务", "ok", "调度正常", detail)

    @classmethod
    def _data_health(cls, value: Any) -> dict[str, str]:
        status = value if isinstance(value, dict) else {}
        database = status.get("database") if isinstance(status.get("database"), dict) else {}
        storage = status.get("storage") if isinstance(status.get("storage"), dict) else {}
        if not database.get("healthy"):
            return cls._item("data", "数据与存储", "error", "数据库不可用", "请查看服务日志")
        if not storage:
            return cls._item("data", "数据与存储", "warn", "磁盘状态未知")
        free_bytes = int(storage.get("free_bytes") or 0)
        free_text = cls._format_bytes(free_bytes)
        if storage.get("critical"):
            return cls._item("data", "数据与存储", "error", f"磁盘仅剩 {free_text}", "请立即释放空间")
        if storage.get("warning"):
            return cls._item("data", "数据与存储", "warn", f"磁盘剩余 {free_text}")
        return cls._item("data", "数据与存储", "ok", "读写正常", f"磁盘可用 {free_text}")

    @staticmethod
    def _age_seconds(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            return None

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.0f} {unit}" if unit in {"B", "KB", "MB"} else f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B"

    @classmethod
    def _dashboard_job(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {field: item.get(field) for field in cls.RECENT_JOB_FIELDS}

    @staticmethod
    def _count(items: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            status = str(item.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts
