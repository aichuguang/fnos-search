"""通知子系统：统一业务事件 → durable worker 队列 → 多渠道发送与审计。

对外主要入口：
- ``emit_notification``：在业务事务内（或独立）发射通知任务；
- ``make_notification_deliver_handler``：注册到 DurableWorkerRuntime；
- ``NotificationDigestScheduler``：每日摘要定时任务；
- ``config``：读取/校验/脱敏通知配置。
"""

from __future__ import annotations

from . import config, events, secrets
from .emitter import emit_notification
from .scheduler import NotificationDigestScheduler
from .sender import deliver_task
from .worker import make_notification_deliver_handler

__all__ = [
    "config",
    "events",
    "secrets",
    "emit_notification",
    "NotificationDigestScheduler",
    "deliver_task",
    "make_notification_deliver_handler",
]
