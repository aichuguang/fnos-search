from __future__ import annotations

from typing import Any, Callable


class WorkerTaskDispatcher:
    """Creates durable business tasks while preserving legacy synchronous mode."""

    def __init__(
        self,
        *,
        repository: Any,
        enabled: Callable[[], bool],
        config_revision: Callable[[], int],
    ) -> None:
        self.repository = repository
        self.enabled = enabled
        self.config_revision = config_revision

    def dispatch(
        self,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        max_attempts: int = 3,
        reactivate_terminal: bool = False,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if not force and not self.enabled():
            return None
        task_id, created = self.repository.enqueue(
            task_type,
            payload,
            idempotency_key,
            max_attempts=max_attempts,
            config_revision=self.config_revision(),
            reactivate_terminal=reactivate_terminal,
        )
        return {
            "success": True,
            "queued": True,
            "created": created,
            "worker_task_id": task_id,
            "task_type": task_type,
            "message": "后台任务已加入持久化 Worker 队列" if created else "后台任务已在 Worker 队列中",
        }

    def organizer_process(
        self,
        task_id: int,
        *,
        auto_apply: bool = True,
        respect_schedule: bool = True,
    ) -> dict[str, Any] | None:
        return self.dispatch(
            "organizer_process",
            {
                "task_id": int(task_id),
                "auto_apply": bool(auto_apply),
                "respect_schedule": bool(respect_schedule),
            },
            f"organizer-process:{int(task_id)}",
            reactivate_terminal=True,
            force=True,
        )

    def organizer_apply(self, task_id: int) -> dict[str, Any] | None:
        return self.dispatch(
            "organizer_apply",
            {"task_id": int(task_id)},
            f"organizer-apply:{int(task_id)}",
            reactivate_terminal=True,
            force=True,
        )

    def media_refresh(self, library: str, dir_list: Any = None, *, guid: str = "") -> dict[str, Any] | None:
        normalized_dirs = list(dir_list or []) if isinstance(dir_list, (list, tuple)) else dir_list
        suffix = guid or library
        return self.dispatch(
            "media_refresh",
            {"library": library, "dir_list": normalized_dirs, "guid": guid},
            f"media-refresh:{suffix}",
            reactivate_terminal=True,
        )

    def media_category_refresh(self, category: str) -> dict[str, Any] | None:
        return self.dispatch(
            "media_category_refresh",
            {"category": str(category)},
            f"media-category-refresh:{str(category)}",
            reactivate_terminal=True,
        )

    def rclone_repair(self, *, limit: int = 50, recovery_key: str = "startup") -> dict[str, Any] | None:
        return self.dispatch(
            "rclone_repair",
            {"limit": int(limit)},
            f"rclone-repair:{recovery_key}",
            reactivate_terminal=True,
        )

    def import_retry(self, job_id: int, *, reason: str = "") -> dict[str, Any] | None:
        return self.dispatch(
            "import_retry",
            {"job_id": int(job_id), "reason": str(reason or f"worker_retry:{int(job_id)}")},
            f"import-retry:{int(job_id)}",
            reactivate_terminal=True,
        )

    def public_import_create(
        self,
        *,
        guest_request_id: int,
        request_token: str,
        submit_payload: dict[str, Any],
        request_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.dispatch(
            "public_import_create",
            {
                "guest_request_id": int(guest_request_id),
                "request_token": str(request_token),
                "submit_payload": dict(submit_payload),
                "request_updates": dict(request_updates or {}),
            },
            f"public-import:{request_token}",
            max_attempts=3,
        )
