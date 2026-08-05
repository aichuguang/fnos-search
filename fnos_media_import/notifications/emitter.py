"""通知发射器：把一次通知作为 durable worker 任务入队。

关键设计：``connection`` 参数让调用方把通知任务与业务状态变更放在**同一个
SQLite 事务**内提交，避免"状态提交成功但通知丢失"或"通知先落但业务回滚"。
不带 connection 时使用独立连接（用于摘要、批量等非关键场景）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..time_utils import utc_now_iso
from . import config as notify_config
from . import events as event_defs

NOTIFICATION_TASK_TYPE = "notification_deliver"
MAX_ATTEMPTS = 5

logger = logging.getLogger(__name__)


def emit_notification_safe(
    db: Any,
    event_type: str,
    context: dict[str, Any],
    *,
    idempotency_key: str,
    connection: Any = None,
    occurred_at: str | None = None,
    channels_override: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """安全发射通知：异常记录日志但不抛出，避免通知问题打断业务事务。

    ``connection`` 传入时与业务状态变更在同一事务内原子入队。
    """
    try:
        return emit_notification(
            db,
            event_type,
            context,
            idempotency_key=idempotency_key,
            connection=connection,
            occurred_at=occurred_at,
            channels_override=channels_override,
        )
    except Exception:  # noqa: BLE001
        logger.exception("notification emit failed: event=%s key=%s", event_type, idempotency_key)
        return None



def emit_notification(
    db: Any,
    event_type: str,
    context: dict[str, Any],
    *,
    idempotency_key: str,
    connection: Any = None,
    occurred_at: str | None = None,
    channels_override: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """发射一次通知，返回任务信息；未启用或没有渠道命中时返回 None。"""
    config = notify_config.read_config(db)
    if not bool(config.get("enabled")):
        return None
    if channels_override is None:
        channels = notify_config.resolve_channels(config, event_type)
    else:
        channels = [
            str(channel)
            for channel in channels_override
            if str(channel) in event_defs.ALL_CHANNELS
            and notify_config.channel_enabled(config, str(channel))
        ]
    if not channels:
        return None

    public_base_url = str(config.get("public_base_url") or "")
    safe_context = event_defs.sanitize_context(event_type, context)
    subject, body = event_defs.build_email(event_type, safe_context, public_base_url)
    html_body = event_defs.build_email_html(event_type, safe_context, public_base_url)
    webhook_ctx = event_defs.build_webhook_context(event_type, safe_context, public_base_url)

    payload = {
        "event_type": event_type,
        "event_id": idempotency_key,
        "occurred_at": occurred_at or utc_now_iso(),
        "severity": event_defs.event_severity(event_type),
        "channels": channels,
        "channel_revisions": {
            channel: notify_config.channel_revision(config, channel) for channel in channels
        },
        "context": safe_context,
        "email": {"subject": subject, "body": body, "html": html_body},
        "webhook": webhook_ctx,
    }

    if connection is not None:
        task_id, created = db.worker_tasks.enqueue_with_connection(
            connection,
            NOTIFICATION_TASK_TYPE,
            payload,
            idempotency_key,
            max_attempts=MAX_ATTEMPTS,
            config_revision=1,
        )
    else:
        task_id, created = db.worker_tasks.enqueue(
            NOTIFICATION_TASK_TYPE,
            payload,
            idempotency_key,
            max_attempts=MAX_ATTEMPTS,
            config_revision=1,
        )
    return {"task_id": task_id, "created": created, "channels": channels, "event_id": idempotency_key}
