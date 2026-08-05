"""通知任务派发：把一条 notification_deliver worker 任务发到各渠道并写审计。

幂等：重复调用同一任务时，通过 ``notification_deliveries`` 中该任务已成功
或已永久失败的渠道，只重试尚未成功的渠道。已成功渠道不会重复发送。
"""

from __future__ import annotations

from typing import Any

from ..repositories.notification_delivery_repository import (
    DELIVERY_FAILED,
    DELIVERY_RETRYABLE,
    DELIVERY_SUCCESS,
)
from . import config as notify_config
from . import events as event_defs
from . import smtp as smtp_channel
from . import webhook as webhook_channel
from . import secrets as secret_store
from .smtp import SmtpPermanentError, SmtpTransientError
from .webhook import WebhookPermanentError, WebhookTransientError

_BACKOFF_SECONDS = (30, 120, 600, 3600, 21600)


def deliver_task(db: Any, task: dict[str, Any]) -> dict[str, Any]:
    """执行一个通知任务，返回带 ``worker_outcome`` 的结果 dict。"""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    task_id = int(task["id"])
    event_type = str(payload.get("event_type") or "")
    channels = [str(ch) for ch in payload.get("channels") or []]
    if not channels:
        return {"worker_outcome": "completed", "message": "无可用渠道"}
    attempts = max(1, int(task.get("attempts") or 1))

    config = notify_config.read_config(db)
    latest = db.latest_notification_delivery_status_by_task(task_id)
    retry_failed_channels = bool(payload.get("retry_failed_channels")) and attempts == 1
    pending = [
        channel
        for channel in channels
        if latest.get(channel) != DELIVERY_SUCCESS
        and (latest.get(channel) != DELIVERY_FAILED or retry_failed_channels)
    ]
    if not pending:
        failed = [channel for channel in channels if latest.get(channel) == DELIVERY_FAILED]
        if failed:
            return {
                "worker_outcome": "business_failed",
                "failed_channels": failed,
                "message": f"以下渠道永久发送失败：{', '.join(failed)}",
            }
        return {"worker_outcome": "completed", "message": "全部渠道已发送"}

    transient_failed: list[str] = []
    for channel in pending:
        if not bool(config.get("enabled")) or not notify_config.channel_enabled(config, channel):
            db.record_notification_delivery(
                task_id=task_id, event_type=event_type, channel=channel,
                status=DELIVERY_FAILED, attempts=attempts,
                error_message="通知渠道已禁用，已停止投递",
            )
            latest[channel] = DELIVERY_FAILED
            continue
        expected_revisions = payload.get("channel_revisions") if isinstance(payload.get("channel_revisions"), dict) else {}
        expected_revision = str(expected_revisions.get(channel) or "")
        current_revision = notify_config.channel_revision(config, channel)
        if expected_revision and expected_revision != current_revision:
            db.record_notification_delivery(
                task_id=task_id, event_type=event_type, channel=channel,
                status=DELIVERY_FAILED, attempts=attempts,
                error_message="渠道配置已变更，为避免把历史事件发送到新目标，已停止投递",
            )
            latest[channel] = DELIVERY_FAILED
            continue
        try:
            _send_one(db, config, channel, payload, task_id, attempts)
            latest[channel] = DELIVERY_SUCCESS
        except (SmtpPermanentError, WebhookPermanentError) as exc:
            db.record_notification_delivery(
                task_id=task_id, event_type=event_type, channel=channel,
                status=DELIVERY_FAILED, attempts=attempts, error_message=str(exc),
            )
            latest[channel] = DELIVERY_FAILED
        except (SmtpTransientError, WebhookTransientError) as exc:
            db.record_notification_delivery(
                task_id=task_id, event_type=event_type, channel=channel,
                status=DELIVERY_RETRYABLE, attempts=attempts, error_message=str(exc),
            )
            transient_failed.append(channel)

    if transient_failed:
        return {
            "worker_outcome": "retryable",
            "retry_after_seconds": _backoff(attempts),
            "failed_channels": transient_failed,
            "message": f"以下渠道发送失败，等待重试：{', '.join(transient_failed)}",
        }
    permanent_failed = [ch for ch in channels if latest.get(ch) == DELIVERY_FAILED]
    if permanent_failed:
        return {
            "worker_outcome": "business_failed",
            "failed_channels": permanent_failed,
            "message": f"以下渠道永久发送失败：{', '.join(permanent_failed)}",
        }
    return {"worker_outcome": "completed", "message": "通知发送完成"}


def _send_one(
    db: Any,
    config: dict[str, Any],
    channel: str,
    payload: dict[str, Any],
    task_id: int,
    attempts: int,
) -> None:
    event_type = str(payload.get("event_type") or "")
    event_id = str(payload.get("event_id") or "")
    if channel == event_defs.CHANNEL_EMAIL:
        smtp_cfg = notify_config.smtp_config(config)
        recipients = [str(r) for r in (smtp_cfg.get("admin_recipients") or []) if str(r).strip()]
        email_payload = payload.get("email") if isinstance(payload.get("email"), dict) else {}
        result = smtp_channel.send_email(
            smtp_cfg,
            str(email_payload.get("subject") or ""),
            str(email_payload.get("body") or ""),
            recipients=recipients,
            html_body=str(email_payload.get("html") or ""),
            message_id=event_id,
        )
        db.record_notification_delivery(
            task_id=task_id, event_type=event_type, channel=channel,
            status=DELIVERY_SUCCESS, attempts=attempts,
            recipient=", ".join(_mask_email(item) for item in recipients),
            status_code=result.get("status_code"),
            response_summary=result.get("response_summary"),
        )
    elif channel == event_defs.CHANNEL_WEBHOOK:
        webhook_cfg = notify_config.webhook_config(config)
        webhook_payload = _build_webhook_payload(payload)
        result = webhook_channel.send_webhook(
            webhook_cfg, webhook_payload, notification_id=event_id
        )
        db.record_notification_delivery(
            task_id=task_id, event_type=event_type, channel=channel,
            status=DELIVERY_SUCCESS, attempts=attempts,
            recipient=_mask_webhook_url(str(webhook_cfg.get("url") or "")),
            status_code=result.get("status_code"),
            response_summary=result.get("response_summary"),
        )
    elif channel == event_defs.CHANNEL_GUEST_EMAIL:
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        request_id = int(context.get("request_id") or 0)
        subscription = db.get_guest_notification_subscription(request_id) if request_id else None
        if not subscription or subscription.get("opted_out_at"):
            raise SmtpPermanentError("访客未订阅或已停止接收通知")
        if event_type != event_defs.EVENT_GUEST_EMAIL_VERIFY and not subscription.get("verified_at"):
            raise SmtpPermanentError("访客邮箱尚未验证")
        recipient = secret_store.resolve(subscription.get("email_encrypted") or "")
        if not recipient:
            raise SmtpPermanentError("访客邮箱无法解密，请检查通知加密密钥")
        unsubscribe_token = secret_store.resolve(
            subscription.get("unsubscribe_token_encrypted") or ""
        )
        if event_type == event_defs.EVENT_GUEST_EMAIL_VERIFY:
            verification_token = secret_store.resolve(
                subscription.get("verification_token_encrypted") or ""
            )
            if not verification_token:
                raise SmtpPermanentError("邮箱验证令牌已失效")
            context = {**context, "verification_token": verification_token}
        public_base_url = str(config.get("public_base_url") or "")
        subject, body = event_defs.build_guest_email(
            event_type,
            context,
            public_base_url,
            unsubscribe_token=unsubscribe_token,
        )
        html_body = event_defs.build_guest_email_html(
            event_type,
            context,
            public_base_url,
            unsubscribe_token=unsubscribe_token,
        )
        smtp_cfg = notify_config.smtp_config(config)
        result = smtp_channel.send_email(
            smtp_cfg,
            subject,
            body,
            recipients=[recipient],
            html_body=html_body,
            message_id=event_id,
        )
        db.record_notification_delivery(
            task_id=task_id, event_type=event_type, channel=channel,
            status=DELIVERY_SUCCESS, attempts=attempts,
            recipient=_mask_email(recipient), status_code=result.get("status_code"),
            response_summary=result.get("response_summary"),
        )
    else:
        raise SmtpPermanentError(f"未知通知渠道：{channel}")


def _build_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    webhook = payload.get("webhook") if isinstance(payload.get("webhook"), dict) else {}
    context = webhook.get("context")
    return {
        "event_id": payload.get("event_id"),
        "event_type": payload.get("event_type"),
        "occurred_at": payload.get("occurred_at"),
        "severity": payload.get("severity"),
        "subject": webhook.get("subject"),
        "message": webhook.get("message"),
        "admin_url": webhook.get("admin_url"),
        "request": context if isinstance(context, dict) else {},
    }


def _backoff(attempts: int) -> int:
    index = max(0, min(len(_BACKOFF_SECONDS) - 1, int(attempts) - 1))
    return _BACKOFF_SECONDS[index]


def _mask_email(value: str) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return "***"
    local, domain = text.rsplit("@", 1)
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _mask_webhook_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(value or ""))
    if not parsed.scheme or not parsed.hostname:
        return "configured"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}/***"
