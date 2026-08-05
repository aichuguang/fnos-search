"""后台通知设置服务：读取、保存、渠道测试与发送记录。"""

from __future__ import annotations

from typing import Any, Callable

from ..notifications import config as notify_config
from ..notifications import events as event_defs
from ..notifications import smtp as smtp_channel
from ..notifications import webhook as webhook_channel
from ..notifications.smtp import SmtpPermanentError, SmtpTransientError
from ..notifications.webhook import WebhookPermanentError, WebhookTransientError
from ..repositories.notification_delivery_repository import (
    DELIVERY_FAILED,
    DELIVERY_RETRYABLE,
    DELIVERY_SUCCESS,
)


class NotificationSettingsService:
    def __init__(self, *, db: Any, public_base_url: Callable[[], str] = lambda: "") -> None:
        self._db = db
        self._public_base_url = public_base_url

    def config(self) -> tuple[dict[str, Any], int]:
        return {
            "success": True,
            "config": notify_config.redact(notify_config.read_config(self._db)),
            "events": {key: event_defs.EVENT_LABELS.get(key, key) for key in event_defs.ALL_EVENTS},
            "summary": self._db.notification_delivery_summary(),
        }, 200

    def update(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "设置格式不正确"}, 400
        if "config" in payload and not isinstance(payload.get("config"), dict):
            return {"success": False, "message": "config 必须是对象"}, 400
        source = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        try:
            normalized = notify_config.normalize(
                source,
                current=notify_config.read_config(self._db),
            )
        except ValueError as exc:
            return {"success": False, "message": str(exc)}, 400
        notify_config.write_config(self._db, normalized)
        return {
            "success": True,
            "message": "通知设置已保存",
            "config": notify_config.redact(normalized),
        }, 200

    def test(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "测试参数格式不正确"}, 400
        if "config" in payload and not isinstance(payload.get("config"), dict):
            return {"success": False, "message": "config 必须是对象"}, 400
        channel = str((payload or {}).get("channel") or "").strip()
        source = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        try:
            merged = notify_config.normalize(source, current=notify_config.read_config(self._db))
        except ValueError as exc:
            return {"success": False, "message": str(exc)}, 400
        public_base_url = str(merged.get("public_base_url") or "") or str(self._public_base_url() or "")

        if channel == event_defs.CHANNEL_EMAIL:
            return self._test_email(merged, public_base_url)
        if channel == event_defs.CHANNEL_WEBHOOK:
            return self._test_webhook(merged)
        return {"success": False, "message": f"未知渠道：{channel}"}, 400

    def deliveries(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "查询参数格式不正确"}, 400
        try:
            limit = int(payload.get("limit") or 50)
            offset = int(payload.get("offset") or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "limit 和 offset 必须是整数"}, 400
        if limit < 1 or limit > 500 or offset < 0:
            return {"success": False, "message": "limit 必须在 1-500 之间，offset 不能小于 0"}, 400
        rows = self._db.list_notification_deliveries(
            event_type=str(payload.get("event_type") or "").strip() or None,
            channel=str(payload.get("channel") or "").strip() or None,
            limit=limit,
            offset=offset,
        )
        return {
            "success": True,
            "deliveries": rows,
            "summary": self._db.notification_delivery_summary(),
        }, 200

    def retry(self, task_id: int) -> tuple[dict[str, Any], int]:
        task = self._db.worker_tasks.get(int(task_id))
        if not task or str(task.get("task_type") or "") != "notification_deliver":
            return {"success": False, "message": "通知任务不存在"}, 404
        if str(task.get("status") or "") != "failed":
            return {"success": False, "message": "只有失败的通知任务可以重试"}, 409
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        channels = [str(item) for item in payload.get("channels") or []]
        config = notify_config.read_config(self._db)
        enabled_channels = [
            channel for channel in channels if notify_config.channel_enabled(config, channel)
        ]
        if not enabled_channels:
            return {"success": False, "message": "原任务没有仍启用的投递渠道"}, 400
        refreshed_payload = {
            **payload,
            "channels": enabled_channels,
            "retry_failed_channels": True,
            "channel_revisions": {
                channel: notify_config.channel_revision(config, channel)
                for channel in enabled_channels
            },
        }
        reactivated_id, reactivated = self._db.worker_tasks.enqueue(
            "notification_deliver",
            refreshed_payload,
            str(task.get("idempotency_key") or ""),
            max_attempts=max(1, int(task.get("max_attempts") or 5)),
            config_revision=max(1, int(task.get("config_revision") or 1)),
            reactivate_terminal=True,
        )
        if not reactivated:
            return {"success": False, "message": "任务状态已变化，请刷新后重试"}, 409
        return {
            "success": True,
            "message": "通知任务已重新入队",
            "task_id": reactivated_id,
        }, 200

    def _test_email(self, merged: dict[str, Any], public_base_url: str) -> tuple[dict[str, Any], int]:
        smtp_cfg = notify_config.smtp_config(merged)
        recipients = [str(r) for r in (smtp_cfg.get("admin_recipients") or []) if str(r).strip()]
        subject, body = event_defs.build_email(
            event_defs.EVENT_GUEST_NEW,
            {"title": "测试通知", "category": "movie", "category_label": "电影", "source_type": "unknown"},
            public_base_url,
        )
        html_body = event_defs.build_email_html(
            event_defs.EVENT_GUEST_NEW,
            {"title": "测试通知", "category": "movie", "category_label": "电影", "source_type": "unknown"},
            public_base_url,
        )
        try:
            smtp_channel.send_email(
                smtp_cfg,
                f"【影视搜索】测试通知 - {subject}",
                body,
                recipients=recipients,
                html_body=html_body,
            )
        except SmtpPermanentError as exc:
            self._record_test("email", DELIVERY_FAILED, error=str(exc))
            return {"success": False, "channel": "email", "message": f"测试失败（永久）：{exc}"}, 400
        except SmtpTransientError as exc:
            self._record_test("email", DELIVERY_RETRYABLE, error=str(exc))
            return {"success": False, "channel": "email", "message": f"测试失败（临时）：{exc}"}, 502
        self._record_test("email", DELIVERY_SUCCESS, status_code=250)
        return {
            "success": True,
            "channel": "email",
            "message": f"测试邮件已发送到 {len(recipients)} 个收件人",
        }, 200

    def _test_webhook(self, merged: dict[str, Any]) -> tuple[dict[str, Any], int]:
        webhook_cfg = notify_config.webhook_config(merged)
        test_payload = {
            "event_id": "notify:test",
            "event_type": "test",
            "occurred_at": "",
            "severity": "info",
            "subject": "【影视搜索】Webhook 测试",
            "message": "这是一条来自影视搜索的通知测试。",
            "admin_url": "",
            "request": {},
        }
        try:
            result = webhook_channel.send_webhook(
                webhook_cfg, test_payload, notification_id="notify:test"
            )
        except WebhookPermanentError as exc:
            self._record_test("webhook", DELIVERY_FAILED, error=str(exc))
            return {"success": False, "channel": "webhook", "message": f"测试失败（永久）：{exc}"}, 400
        except WebhookTransientError as exc:
            self._record_test("webhook", DELIVERY_RETRYABLE, error=str(exc))
            return {"success": False, "channel": "webhook", "message": f"测试失败（临时）：{exc}"}, 502
        self._record_test(
            "webhook",
            DELIVERY_SUCCESS,
            status_code=int(result.get("status_code") or 200),
        )
        return {
            "success": True,
            "channel": "webhook",
            "message": f"Webhook 测试成功（HTTP {result.get('status_code')}）",
        }, 200

    def _record_test(
        self,
        channel: str,
        status: str,
        *,
        status_code: int | None = None,
        error: str = "",
    ) -> None:
        self._db.record_notification_delivery(
            task_id=None,
            event_type="test",
            channel=channel,
            recipient="configured",
            status=status,
            attempts=1,
            status_code=status_code,
            response_summary=(f"HTTP {status_code}" if status_code else ""),
            error_message=error,
        )
