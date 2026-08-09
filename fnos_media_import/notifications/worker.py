"""通知 Worker：把 notification_deliver 任务交给发送器执行。"""

from __future__ import annotations

from typing import Any, Callable

from ..repositories.repository_set import NotificationRepositories
from .sender import deliver_task


def make_notification_deliver_handler(
    db: NotificationRepositories,
) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]:
    """构造供 DurableWorkerRuntime 注册的 ``notification_deliver`` handler。"""

    def handler(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
        return deliver_task(db, task)

    return handler
