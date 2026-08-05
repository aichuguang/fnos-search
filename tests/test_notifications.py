from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import os
import smtplib
import unittest
import uuid
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

from fnos_media_import.database import Database
from fnos_media_import.notifications import config as notify_config
from fnos_media_import.notifications import events as notify_events
from fnos_media_import.notifications import secrets as secret_store
from fnos_media_import.notifications import smtp as smtp_channel
from fnos_media_import.notifications import webhook as webhook_channel
from fnos_media_import.notifications.emitter import emit_notification
from fnos_media_import.notifications.sender import deliver_task
from fnos_media_import.notifications.scheduler import NotificationDigestScheduler
from fnos_media_import.notifications.smtp import SmtpPermanentError, SmtpTransientError
from fnos_media_import.notifications.transitions import emit_organizer_review_required
from fnos_media_import.notifications.webhook import WebhookPermanentError, WebhookTransientError
from fnos_media_import.repositories.guest_notification_subscription_repository import token_hash
from fnos_media_import.services.notification_settings_service import NotificationSettingsService
from fnos_media_import.services.public_submission_service import (
    PublicSubmissionDependencies,
    PublicSubmissionService,
)


def _enabled_config() -> dict:
    config = copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG)
    config["enabled"] = True
    config["webhook"] = {
        "enabled": True,
        "url": "https://example.com/hook",
        "secret": "",
        "allow_private": False,
    }
    config["smtp"] = {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 465,
        "security": "ssl",
        "username": "u",
        "password": "secret",
        "from_name": "",
        "from_email": "notify@example.com",
        "admin_recipients": ["admin@example.com"],
    }
    return config


class _FakeSmtp:
    def __init__(self, code: int) -> None:
        self.code = code

    def login(self, _username: str, _password: str):
        return (235, b"ok")

    def send_message(self, _message):
        raise smtplib.SMTPResponseException(self.code, b"rejected")

    def quit(self):
        return (221, b"bye")

    def close(self):
        pass


class _FakeLoginRejectingSmtp(_FakeSmtp):
    def login(self, _username: str, _password: str, **_kwargs):
        raise smtplib.SMTPResponseException(
            500,
            b"Error: bad syntax.http://mail.qq.com/zh_CN/help/content/rejectedmail.html",
        )


class _FakeSlowQuitSmtp:
    class Socket:
        def __init__(self) -> None:
            self.timeout: float | None = None

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

    def __init__(self) -> None:
        self.sock = self.Socket()
        self.closed = False

    def login(self, _username: str, _password: str):
        return (235, b"ok")

    def send_message(self, _message):
        return {}

    def quit(self):
        if self.sock.timeout != 1.0:
            raise AssertionError(f"unexpected QUIT timeout: {self.sock.timeout}")
        raise TimeoutError("slow SMTP QUIT")

    def close(self):
        self.closed = True


class _FakeHttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, _limit: int, decode_content: bool = True) -> bytes:  # noqa: ARG002
        return b"response body must not enter audit errors"


class _FakePool:
    status = 200
    captured: dict = {}

    def __init__(self, host, **kwargs):
        self.captured.update({"host": host, "pool_kwargs": kwargs})

    def urlopen(self, method, target, *, body, headers, redirect, preload_content):  # noqa: ARG002
        self.captured.update({
            "method": method,
            "target": target,
            "body": body,
            "headers": headers,
            "redirect": redirect,
        })
        return _FakeHttpResponse(self.status)

    def close(self):
        pass


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"notify-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_request(self, token: str = "req1") -> int:
        return self.database.guest_request_commands.create_with_event(
            {
                "request_token": token,
                "title": "测试资源",
                "category": "movie",
                "category_label": "电影",
                "source_type": "quark",
                "source_url": "https://example.com/share",
                "status": "submitted",
                "public_status": "处理中",
            },
            level="info",
            message="访客提交资源",
        )

    def _create_job(self, status: str = "created") -> int:
        job_id, created = self.database.create_job({
            "title": "测试任务",
            "category": "movie",
            "category_label": "电影",
            "source_type": "quark",
            "source_url": f"https://example.com/{uuid.uuid4().hex}",
            "target_route": "quark_to_mobile",
            "target_path": "/电影",
            "status": status,
            "idempotency_key": f"test:{uuid.uuid4().hex}",
        })
        self.assertTrue(created)
        return job_id

    # --- 原子发射：业务状态与通知任务同事务 ---

    def test_atomic_emit_commits_together(self) -> None:
        db = self.database
        request_id = self._create_request()
        notify_config.write_config(db, _enabled_config())

        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = db.guest_request_commands.transition_with_event(
                request_id,
                expected_statuses={"submitted"},
                status="pending_review",
                public_status="等待处理",
                raw_data=None,
                level="info",
                message="进入审核",
                connection=conn,
                emit=lambda c: emit_notification(
                    db,
                    notify_events.EVENT_GUEST_APPROVED,
                    {"title": "测试资源", "category": "movie", "request_id": request_id},
                    idempotency_key=f"notify:guest_approved:{request_id}",
                    connection=c,
                ),
            )
        self.assertTrue(changed)
        self.assertEqual(db.guest_request_queries.get(request_id)["status"], "pending_review")
        tasks = db.worker_tasks.list(task_type="notification_deliver")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["payload"]["event_type"], notify_events.EVENT_GUEST_APPROVED)
        self.assertIn("webhook", tasks[0]["payload"]["channels"])

    def test_atomic_emit_rolls_back_on_emit_failure(self) -> None:
        db = self.database
        request_id = self._create_request()
        notify_config.write_config(db, _enabled_config())

        def _boom(conn) -> None:  # noqa: ARG001
            raise RuntimeError("emit exploded")

        with self.assertRaises(RuntimeError):
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                db.guest_request_commands.transition_with_event(
                    request_id,
                    expected_statuses={"submitted"},
                    status="pending_review",
                    public_status="等待处理",
                    raw_data=None,
                    level="info",
                    message="进入审核",
                    connection=conn,
                    emit=_boom,
                )
        self.assertEqual(db.guest_request_queries.get(request_id)["status"], "submitted")
        self.assertEqual(db.worker_tasks.list(task_type="notification_deliver"), [])

    def test_enqueue_with_connection_shares_caller_transaction(self) -> None:
        db = self.database
        notify_config.write_config(db, _enabled_config())
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_id, created = db.worker_tasks.enqueue_with_connection(
                conn, "notification_deliver", {"event_type": "x"}, "notify:x:1", max_attempts=5
            )
            self.assertTrue(created)
        self.assertIsNotNone(db.worker_tasks.get(task_id))

    # --- 发射器开关 / 幂等 ---

    def test_emit_disabled_returns_none(self) -> None:
        db = self.database
        notify_config.write_config(db, copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG))
        result = emit_notification(
            db, notify_events.EVENT_GUEST_NEW, {"title": "A"}, idempotency_key="notify:guest_new:1"
        )
        self.assertIsNone(result)
        self.assertEqual(db.worker_tasks.list(task_type="notification_deliver"), [])

    def test_emit_no_channel_returns_none(self) -> None:
        db = self.database
        config = copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG)
        config["enabled"] = True  # 所有渠道未启用
        notify_config.write_config(db, config)
        result = emit_notification(
            db, notify_events.EVENT_GUEST_NEW, {"title": "A"}, idempotency_key="notify:guest_new:1"
        )
        self.assertIsNone(result)

    def test_emit_enabled_creates_task_and_dedups(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)

        result = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试", "category_label": "电影", "source_type": "quark"},
            idempotency_key="notify:guest_new:1",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["channels"], ["webhook"])
        tasks = db.worker_tasks.list(task_type="notification_deliver")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["payload"]["event_id"], "notify:guest_new:1")
        self.assertEqual(tasks[0]["payload"]["email"]["subject"], "【影视搜索】新申请待审核：测试")

        repeated = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试", "category_label": "电影", "source_type": "quark"},
            idempotency_key="notify:guest_new:1",
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(len(db.worker_tasks.list(task_type="notification_deliver")), 1)

    # --- Webhook：签名 / SSRF / 分类 ---

    def test_webhook_signs_and_posts(self) -> None:
        _FakePool.status = 200
        _FakePool.captured = {}
        with (
            mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
            mock.patch("fnos_media_import.notifications.webhook.urllib3.HTTPSConnectionPool", _FakePool),
        ):
            result = webhook_channel.send_webhook(
                {"url": "https://example.com/hook", "secret": "s3cret", "allow_private": False},
                {"event_id": "e1"},
                notification_id="notify:test",
            )
        self.assertEqual(result["status_code"], 200)
        captured = _FakePool.captured
        self.assertEqual(captured["host"], "93.184.216.34")
        self.assertEqual(captured["pool_kwargs"]["assert_hostname"], "example.com")
        self.assertEqual(captured["headers"]["Host"], "example.com")
        self.assertEqual(captured["headers"]["X-Notification-Id"], "notify:test")
        timestamp = captured["headers"]["X-Webhook-Timestamp"]
        expected = "sha256=" + hmac.new(
            b"s3cret", timestamp.encode("ascii") + b"." + captured["body"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(captured["headers"]["X-Webhook-Signature"], expected)

    def test_webhook_blocks_private_and_http(self) -> None:
        with self.assertRaises(WebhookPermanentError):
            webhook_channel.send_webhook(
                {"url": "http://127.0.0.1/hook", "secret": "", "allow_private": False},
                {},
                notification_id="n",
            )
        with self.assertRaises(WebhookPermanentError):
            webhook_channel.send_webhook(
                {"url": "https://192.168.1.5/hook", "secret": "", "allow_private": False},
                {},
                notification_id="n",
            )
        with self.assertRaises(WebhookPermanentError):
            webhook_channel.send_webhook(
                {"url": "http://example.com/hook", "secret": "", "allow_private": False},
                {},
                notification_id="n",
            )

    def test_webhook_allow_private_passes_to_post(self) -> None:
        _FakePool.status = 200
        _FakePool.captured = {}
        with mock.patch(
            "fnos_media_import.notifications.webhook.urllib3.HTTPConnectionPool", _FakePool
        ):
            result = webhook_channel.send_webhook(
                {"url": "http://127.0.0.1:8000/hook", "secret": "", "allow_private": True},
                {"event_id": "e1"},
                notification_id="n",
            )
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(_FakePool.captured["host"], "127.0.0.1")

    def test_webhook_classifies_status_codes(self) -> None:
        cases = (
            (404, WebhookPermanentError),
            (418, WebhookPermanentError),
            (429, WebhookTransientError),
            (500, WebhookTransientError),
            (503, WebhookTransientError),
        )
        for code, exc_type in cases:
            with self.subTest(code=code):
                _FakePool.status = code
                with (
                    mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
                    mock.patch("fnos_media_import.notifications.webhook.urllib3.HTTPSConnectionPool", _FakePool),
                ):
                    with self.assertRaises(exc_type):
                        webhook_channel.send_webhook(
                            {"url": "https://example.com/hook", "secret": "", "allow_private": False},
                            {},
                            notification_id="n",
                        )

    # --- SMTP 分类 ---

    def test_smtp_5xx_permanent(self) -> None:
        with mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL", side_effect=lambda *a, **k: _FakeSmtp(550)):
            with self.assertRaises(SmtpPermanentError):
                smtp_channel.send_email(
                    _enabled_config()["smtp"],
                    "subj",
                    "body",
                    recipients=["admin@example.com"],
                )

    def test_smtp_4xx_transient(self) -> None:
        with mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL", side_effect=lambda *a, **k: _FakeSmtp(450)):
            with self.assertRaises(SmtpTransientError):
                smtp_channel.send_email(
                    _enabled_config()["smtp"],
                    "subj",
                    "body",
                    recipients=["admin@example.com"],
                )

    def test_smtp_rejects_unresolved_encrypted_password_before_connecting(self) -> None:
        config = _enabled_config()["smtp"]
        config["username"] = "notify@example.com"
        config["password"] = "enc:unreadable"
        with mock.patch.object(secret_store, "resolve", return_value=""):
            resolved = notify_config.smtp_config({"smtp": config})

        with (
            mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL") as constructor,
            self.assertRaisesRegex(SmtpPermanentError, "NOTIFICATION_ENCRYPTION_KEY"),
        ):
            smtp_channel.send_email(
                resolved,
                "subj",
                "body",
                recipients=["admin@example.com"],
            )

        constructor.assert_not_called()

    def test_smtp_rejects_missing_password_when_username_is_configured(self) -> None:
        config = {**_enabled_config()["smtp"], "password": ""}

        with (
            mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL") as constructor,
            self.assertRaisesRegex(SmtpPermanentError, "密码或授权码未配置"),
        ):
            smtp_channel.send_email(
                config,
                "subj",
                "body",
                recipients=["admin@example.com"],
            )

        constructor.assert_not_called()

    def test_qq_auth_bad_syntax_is_transient(self) -> None:
        smtp = _FakeLoginRejectingSmtp(500)
        with mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL", return_value=smtp):
            with self.assertRaises(SmtpTransientError):
                smtp_channel.send_email(
                    {
                        **_enabled_config()["smtp"],
                        "host": "smtp.exmail.qq.com",
                        "username": "notify@example.com",
                        "password": "secret",
                    },
                    "subj",
                    "body",
                    recipients=["admin@example.com"],
                )

    def test_smtp_success_caps_slow_quit_and_closes_socket(self) -> None:
        smtp = _FakeSlowQuitSmtp()
        with mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL", return_value=smtp):
            result = smtp_channel.send_email(
                _enabled_config()["smtp"],
                "subj",
                "body",
                recipients=["admin@example.com"],
            )

        self.assertEqual(result["status_code"], 250)
        self.assertEqual(smtp.sock.timeout, 1.0)
        self.assertTrue(smtp.closed)

    def test_smtp_builds_plain_and_html_alternatives_with_short_timeout(self) -> None:
        captured: dict = {}

        class CapturingSmtp(_FakeSlowQuitSmtp):
            def send_message(self, message):
                captured["message"] = message
                return {}

            def quit(self):
                return (221, b"bye")

        smtp = CapturingSmtp()
        with mock.patch.object(smtp_channel, "_IPv4FirstSMTPSSL", return_value=smtp) as constructor:
            smtp_channel.send_email(
                _enabled_config()["smtp"],
                "HTML 测试",
                "纯文本正文",
                recipients=["admin@example.com"],
                html_body="<html><body><strong>HTML 正文</strong></body></html>",
            )

        self.assertEqual(constructor.call_args.kwargs["timeout"], 10.0)
        message = captured["message"]
        self.assertTrue(message.is_multipart())
        self.assertIn("纯文本正文", message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("<strong>HTML 正文</strong>", message.get_body(preferencelist=("html",)).get_content())

    def test_smtp_connection_prefers_ipv4_and_falls_back_to_ipv6(self) -> None:
        addresses = [
            (smtp_channel.socket.AF_INET6, smtp_channel.socket.SOCK_STREAM, 6, "", ("2001:db8::1", 465, 0, 0)),
            (smtp_channel.socket.AF_INET, smtp_channel.socket.SOCK_STREAM, 6, "", ("192.0.2.10", 465)),
        ]
        attempts: list[tuple[int, tuple]] = []

        class FakeSocket:
            def __init__(self, family: int) -> None:
                self.family = family
                self.timeouts: list[float | None] = []

            def settimeout(self, timeout: float | None) -> None:
                self.timeouts.append(timeout)

            def connect(self, address: tuple) -> None:
                attempts.append((self.family, address))
                if self.family == smtp_channel.socket.AF_INET:
                    raise OSError("IPv4 endpoint unavailable")

            def close(self) -> None:
                pass

        with (
            mock.patch.object(smtp_channel.socket, "getaddrinfo", return_value=addresses),
            mock.patch.object(
                smtp_channel.socket,
                "socket",
                side_effect=lambda family, _socktype, _proto: FakeSocket(family),
            ),
        ):
            connected = smtp_channel._create_connection_ipv4_first(("smtp.example.com", 465), 10.0)

        self.assertEqual(smtp_channel.socket.AF_INET6, connected.family)
        self.assertEqual(10.0, connected.timeouts[-1])
        self.assertEqual(
            [smtp_channel.socket.AF_INET, smtp_channel.socket.AF_INET6],
            [family for family, _address in attempts],
        )

    def test_smtp_ssl_keeps_original_hostname_for_sni(self) -> None:
        raw_socket = mock.Mock()
        wrapped_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = wrapped_socket
        client = smtp_channel._IPv4FirstSMTPSSL(context=context)
        client._host = "smtp.exmail.qq.com"

        with mock.patch.object(smtp_channel, "_create_connection_ipv4_first", return_value=raw_socket):
            result = client._get_socket("smtp.exmail.qq.com", 465, 10.0)

        self.assertIs(result, wrapped_socket)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="smtp.exmail.qq.com")

    def test_email_html_escapes_dynamic_content_and_wraps_long_titles(self) -> None:
        unsafe_title = '<img src=x onerror="alert(1)">' + ("超长标题" * 40)
        rendered = notify_events.build_email_html(
            notify_events.EVENT_JOB_FAILED,
            {
                "title": unsafe_title,
                "job_id": 7,
                "stage": "organizer",
                "error": '<script>alert("x")</script>',
            },
            "https://media.example.com",
        )

        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("word-break:break-word", rendered)
        self.assertIn("https://media.example.com/admin", rendered)

    def test_organizer_review_event_defaults_to_admin_channels_and_has_review_template(self) -> None:
        self.assertEqual(
            notify_events.DEFAULT_RULES[notify_events.EVENT_ORGANIZER_REVIEW_REQUIRED],
            ["email", "webhook"],
        )
        self.assertNotIn(
            notify_events.CHANNEL_GUEST_EMAIL,
            notify_events.DEFAULT_RULES[notify_events.EVENT_ORGANIZER_REVIEW_REQUIRED],
        )
        context = {
            "task_id": 9,
            "task_revision": 2,
            "job_id": 10,
            "title": "测试影片",
            "issue_count": 1,
            "reason": "1 个文件目标路径冲突",
        }

        subject, body = notify_events.build_email(
            notify_events.EVENT_ORGANIZER_REVIEW_REQUIRED,
            context,
            "https://media.example.com",
        )
        rendered = notify_events.build_email_html(
            notify_events.EVENT_ORGANIZER_REVIEW_REQUIRED,
            context,
            "https://media.example.com",
        )

        self.assertIn("OpenList 整理待审核", subject)
        self.assertIn("Organizer 任务：9", body)
        self.assertIn("异常项目：1", body)
        self.assertIn("进入 Organizer 审核", rendered)
        self.assertIn("1 个文件目标路径冲突", rendered)
        self.assertEqual(
            notify_events.event_severity(notify_events.EVENT_ORGANIZER_REVIEW_REQUIRED),
            "warning",
        )

    def test_organizer_review_transition_is_atomic_and_idempotent_per_revision(self) -> None:
        db = self.database
        config = _enabled_config()
        notify_config.write_config(db, config)
        job_id = self._create_job(status="organizing")
        task_id = db.create_organizer_task(
            category="movie",
            openlist_root_path="/清云/_入库暂存/电影/job-review",
            title="测试影片",
            job_id=job_id,
            status="waiting_review",
        )
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO organizer_mappings
                (task_id, source_path, target_path, status, created_at, updated_at)
                VALUES (?, ?, ?, 'conflict', datetime('now'), datetime('now'))
                """,
                (task_id, "/source/movie.mkv", "/target/movie.mkv"),
            )

        def configured(database, event_type, context, *, idempotency_key, connection):
            channels = notify_config.resolve_channels(notify_config.read_config(database), event_type)
            result = emit_notification(
                database,
                event_type,
                context,
                idempotency_key=idempotency_key,
                connection=connection,
                channels_override=channels,
            )
            return [result] if result else []

        current = {
            "id": job_id,
            "status": "review",
            "title": "测试影片",
            "category": "movie",
            "error_message": "",
            "raw_data": {
                "completion": {
                    "stage": "review",
                    "organizer_task_id": task_id,
                    "message": "1 个文件目标路径冲突",
                }
            },
        }
        with db.connect() as conn:
            self.assertTrue(emit_organizer_review_required(db, conn, current, configured))
            self.assertTrue(emit_organizer_review_required(db, conn, current, configured))

        tasks = db.worker_tasks.list(task_type="notification_deliver")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            tasks[0]["idempotency_key"],
            f"notify:organizer_review_required:{task_id}:1",
        )
        self.assertEqual(tasks[0]["payload"]["context"]["issue_count"], 1)
        self.assertEqual(tasks[0]["payload"]["channels"], ["email", "webhook"])

        with db.connect() as conn:
            conn.execute("UPDATE organizer_tasks SET revision=2 WHERE id=?", (task_id,))
            self.assertTrue(emit_organizer_review_required(db, conn, current, configured))
        tasks = db.worker_tasks.list(task_type="notification_deliver")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            {task["idempotency_key"] for task in tasks},
            {
                f"notify:organizer_review_required:{task_id}:1",
                f"notify:organizer_review_required:{task_id}:2",
            },
        )

    def test_non_organizer_review_does_not_emit_organizer_notification(self) -> None:
        called = False

        def configured(*_args, **_kwargs):
            nonlocal called
            called = True
            return []

        with self.database.connect() as conn:
            emitted = emit_organizer_review_required(
                self.database,
                conn,
                {
                    "id": 1,
                    "status": "review",
                    "raw_data": {"completion": {"stage": "review"}},
                },
                configured,
            )

        self.assertFalse(emitted)
        self.assertFalse(called)

    def test_emitted_email_payload_contains_html_fallback_pair(self) -> None:
        config = _enabled_config()
        config["rules"][notify_events.EVENT_JOB_DONE] = ["email"]
        notify_config.write_config(self.database, config)

        emitted = emit_notification(
            self.database,
            notify_events.EVENT_JOB_DONE,
            {"job_id": 9, "title": "超兽武装"},
            idempotency_key="notify:job_done:9",
        )
        email_payload = self.database.worker_tasks.get(emitted["task_id"])["payload"]["email"]

        self.assertIn("入库任务已完成", email_payload["body"])
        self.assertIn("<!doctype html>", email_payload["html"])
        self.assertIn("超兽武装", email_payload["html"])

    # --- 发送审计与重试 ---

    def test_deliver_task_retries_transient_then_completes(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        emit_result = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试", "category_label": "电影", "source_type": "quark"},
            idempotency_key="notify:guest_new:1",
        )
        task_id = emit_result["task_id"]

        state = {"calls": 0}

        def flaky(cfg, payload, *, notification_id, timeout=30.0):  # noqa: ARG001
            state["calls"] += 1
            if state["calls"] == 1:
                raise WebhookTransientError("boom")
            return {"status_code": 200, "response_summary": "ok"}

        with mock.patch(
            "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
            side_effect=flaky,
        ):
            first = deliver_task(db, db.worker_tasks.get(task_id))
            self.assertEqual(first["worker_outcome"], "retryable")
            second = deliver_task(db, db.worker_tasks.get(task_id))
            self.assertEqual(second["worker_outcome"], "completed")

        deliveries = db.list_notification_deliveries(event_type=notify_events.EVENT_GUEST_NEW)
        statuses = [row["status"] for row in deliveries]
        self.assertIn("retryable", statuses)
        self.assertIn("success", statuses)
        summary = db.notification_delivery_summary()
        self.assertGreaterEqual(summary["success"], 1)

    def test_deliver_task_permanent_failure_is_terminal(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        emit_result = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试", "category_label": "电影", "source_type": "quark"},
            idempotency_key="notify:guest_new:2",
        )
        task_id = emit_result["task_id"]
        with mock.patch(
            "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
            side_effect=WebhookPermanentError("bad url"),
        ):
            result = deliver_task(db, db.worker_tasks.get(task_id))
        self.assertEqual(result["worker_outcome"], "business_failed")
        statuses = [row["status"] for row in db.list_notification_deliveries()]
        self.assertIn("failed", statuses)

    def test_disabled_channel_stops_queued_delivery(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        task_id = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试"},
            idempotency_key="notify:disable:1",
        )["task_id"]
        config["webhook"]["enabled"] = False
        notify_config.write_config(db, config)
        with mock.patch("fnos_media_import.notifications.sender.webhook_channel.send_webhook") as send:
            result = deliver_task(db, db.worker_tasks.get(task_id))
        self.assertEqual(result["worker_outcome"], "business_failed")
        send.assert_not_called()
        self.assertIn("已禁用", db.list_notification_deliveries()[0]["error_message"])

    def test_changed_channel_target_stops_historical_event(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        task_id = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试"},
            idempotency_key="notify:revision:1",
        )["task_id"]
        config["webhook"]["allow_private"] = True
        notify_config.write_config(db, config)
        with mock.patch("fnos_media_import.notifications.sender.webhook_channel.send_webhook") as send:
            result = deliver_task(db, db.worker_tasks.get(task_id))
        self.assertEqual(result["worker_outcome"], "business_failed")
        send.assert_not_called()
        self.assertIn("配置已变更", db.list_notification_deliveries()[0]["error_message"])

    def test_permanent_failure_can_be_reactivated_after_same_target_repair(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        task_id = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试"},
            idempotency_key="notify:reactivate:1",
        )["task_id"]
        with mock.patch(
            "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
            side_effect=WebhookPermanentError("temporary configuration problem"),
        ):
            self.assertEqual(
                deliver_task(db, db.worker_tasks.get(task_id))["worker_outcome"],
                "business_failed",
            )
        claimed = db.worker_tasks.claim("test-worker", task_types=["notification_deliver"])
        self.assertEqual(claimed["id"], task_id)
        self.assertTrue(db.worker_tasks.fail(task_id, "test-worker", "failed", terminal=True))
        body, status = NotificationSettingsService(db=db).retry(task_id)
        self.assertEqual(status, 200, body)
        claimed = db.worker_tasks.claim("test-worker", task_types=["notification_deliver"])
        with mock.patch(
            "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
            return_value={"status_code": 200, "response_summary": "HTTP 200"},
        ):
            self.assertEqual(
                deliver_task(db, claimed)["worker_outcome"],
                "completed",
            )

    def test_automatic_retry_skips_permanently_failed_channel(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["email", "webhook"]
        notify_config.write_config(db, config)
        task_id = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试"},
            idempotency_key="notify:mixed-retry:1",
        )["task_id"]
        with (
            mock.patch(
                "fnos_media_import.notifications.sender.smtp_channel.send_email",
                side_effect=SmtpPermanentError("bad recipient"),
            ),
            mock.patch(
                "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
                side_effect=WebhookTransientError("timeout"),
            ),
        ):
            first = deliver_task(db, db.worker_tasks.get(task_id))
        self.assertEqual(first["worker_outcome"], "retryable")

        with (
            mock.patch(
                "fnos_media_import.notifications.sender.smtp_channel.send_email"
            ) as email_send,
            mock.patch(
                "fnos_media_import.notifications.sender.webhook_channel.send_webhook",
                return_value={"status_code": 200, "response_summary": "HTTP 200"},
            ),
        ):
            second = deliver_task(db, {**db.worker_tasks.get(task_id), "attempts": 2})
        email_send.assert_not_called()
        self.assertEqual(second["worker_outcome"], "business_failed")

    def test_summary_counts_latest_channel_state(self) -> None:
        db = self.database
        db.record_notification_delivery(
            task_id=1, event_type="x", channel="webhook", status="retryable"
        )
        db.record_notification_delivery(
            task_id=1, event_type="x", channel="webhook", status="success"
        )
        db.record_notification_delivery(
            task_id=2, event_type="x", channel="email", status="failed"
        )
        summary = db.notification_delivery_summary()
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["retryable"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["total"], 2)

    def test_job_status_hook_runs_only_on_real_transitions(self) -> None:
        db = self.database
        job_id = self._create_job()
        transitions: list[tuple[str, str]] = []
        db.job_commands.set_status_transition_emitter(
            lambda _conn, previous, current: transitions.append(
                (str(previous["status"]), str(current["status"]))
            )
        )
        db.update_job(job_id, status="failed", error_message="boom")
        db.update_job(job_id, status="failed", error_message="still boom")
        db.update_job(job_id, status="submitted")
        db.update_job(job_id, status="done")
        self.assertEqual(
            transitions,
            [("created", "failed"), ("failed", "submitted"), ("submitted", "done")],
        )

    def test_review_notification_is_emitted_only_on_review_transition(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_REVIEW_REQUIRED] = ["webhook"]
        notify_config.write_config(db, config)
        request_id = self._create_request("review-only")
        service = PublicSubmissionService(
            PublicSubmissionDependencies(
                queries=db.guest_request_queries,
                commands=db.guest_request_commands,
                sync_request=lambda item: item,
                public_status=lambda status: status,
                public_request=lambda item: item,
                db=db,
                emit_notification=emit_notification,
            )
        )

        service.send_to_review(
            request_id,
            reason="需要审核",
            event_message="进入审核",
            event_data={"mode": "review"},
            raw_patch={},
        )
        service.send_to_review(
            request_id,
            reason="重复审核",
            event_message="重复审核",
            event_data={"mode": "review"},
            raw_patch={},
        )
        tasks = db.worker_tasks.list(task_type="notification_deliver")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            tasks[0]["payload"]["event_type"],
            notify_events.EVENT_GUEST_REVIEW_REQUIRED,
        )

    def test_digest_uses_configured_timezone_and_hour(self) -> None:
        db = self.database
        config = _enabled_config()
        config["digest_hour"] = 9
        config["digest_timezone"] = "Asia/Shanghai"
        config["rules"][notify_events.EVENT_DIGEST_DAILY] = ["email"]
        notify_config.write_config(db, config)
        scheduler = NotificationDigestScheduler(database=db, owner_id="digest-test")

        class Morning:
            @staticmethod
            def now(timezone):
                from datetime import datetime

                return datetime(2026, 8, 4, 9, 0, 0, tzinfo=timezone)

        with mock.patch("fnos_media_import.notifications.scheduler.datetime", Morning):
            result = scheduler.run_once()
        self.assertIn("emitted", result, result)
        self.assertTrue(result["emitted"])
        self.assertEqual(
            db.worker_tasks.list(task_type="notification_deliver")[0]["payload"]["event_type"],
            notify_events.EVENT_DIGEST_DAILY,
        )

    def test_digest_default_timezone_fallback_is_not_reported_as_invalid(self) -> None:
        config = _enabled_config()
        config["digest_timezone"] = "Asia/Shanghai"
        notify_config.write_config(self.database, config)
        logs: list[str] = []
        scheduler = NotificationDigestScheduler(
            database=self.database,
            owner_id="digest-timezone-fallback-test",
            log=logs.append,
        )

        with mock.patch(
            "fnos_media_import.notifications.scheduler.ZoneInfo",
            side_effect=ZoneInfoNotFoundError("no local timezone database"),
        ):
            result = scheduler.run_once()

        self.assertTrue(result["success"])
        self.assertFalse(any("invalid notification digest timezone" in item for item in logs))

    # --- 配置脱敏与密钥 ---

    def test_config_secret_env_ref_kept_and_literal_needs_key(self) -> None:
        config = notify_config.normalize(
            {"webhook": {"url": "env:WEBHOOK_URL", "secret": "env:WEBHOOK_SECRET"}},
            current=copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG),
        )
        self.assertEqual(config["webhook"]["secret"], "env:WEBHOOK_SECRET")
        self.assertEqual(config["webhook"]["url"], "env:WEBHOOK_URL")
        redacted = notify_config.redact(config)
        self.assertEqual(redacted["webhook"]["secret"], "env:WEBHOOK_SECRET")

        with self.assertRaises(ValueError):
            notify_config.normalize(
                {"smtp": {"password": "plaintext-secret"}},
                current=copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG),
            )

    def test_secrets_roundtrip(self) -> None:
        key = base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
        encrypted = secret_store.encrypt("smtp-pass", key=key)
        self.assertTrue(encrypted.startswith("enc:"))
        self.assertEqual(secret_store.resolve(encrypted, key=key), "smtp-pass")
        self.assertEqual(secret_store.resolve("env:SMTP_PASSWORD"), os.environ.get("SMTP_PASSWORD", ""))

    def test_arbitrary_encryption_passphrase_roundtrip(self) -> None:
        key = "用户随便输入的口令-2026"
        encrypted = secret_store.encrypt("smtp-pass", key=key)

        self.assertEqual(secret_store.resolve(encrypted, key=key), "smtp-pass")
        self.assertEqual(secret_store.resolve(encrypted, key="另一条口令"), "")
        self.assertEqual(32, len(secret_store.key_bytes(key) or b""))

    def test_legacy_raw_aes_key_derivation_is_unchanged(self) -> None:
        key = "0123456789abcdef"

        self.assertEqual(key.encode("utf-8"), secret_store.key_bytes(key))

    def test_invalid_prefixed_base64_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "解码后必须是"):
            secret_store.key_bytes("base64:c2hvcnQ=")

    def test_config_redact_masks_encrypted_secret(self) -> None:
        key = base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
        # 未配置密钥时，真实密码会被拒绝保存（抛 ValueError）
        with self.assertRaises(ValueError):
            notify_config.normalize(
                {"smtp": {"password": "realsecret"}},
                current=copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG),
            )
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": key}):
            encrypted_config = notify_config.normalize(
                {"smtp": {"password": "realsecret"}},
                current=copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG),
            )
            self.assertTrue(encrypted_config["smtp"]["password"].startswith("enc:"))
            self.assertEqual(notify_config.redact(encrypted_config)["smtp"]["password"], "********")

    def test_webhook_url_is_encrypted_and_redacted(self) -> None:
        key = "base64:" + base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": key}):
            config = notify_config.normalize(
                {"webhook": {"url": "https://example.com/private-hook"}},
                current=copy.deepcopy(notify_config.DEFAULT_NOTIFICATIONS_CONFIG),
            )
            self.assertTrue(config["webhook"]["url"].startswith("enc:"))
            self.assertEqual(notify_config.redact(config)["webhook"]["url"], "********")
            self.assertEqual(
                notify_config.webhook_config(config)["url"],
                "https://example.com/private-hook",
            )

    def test_guest_subscription_verify_and_unsubscribe_tokens_are_one_way(self) -> None:
        key = "base64:" + base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
        request_id = self._create_request("guest-mail")
        verify_token = "verify-token"
        unsubscribe_token = "unsubscribe-token"
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": key}):
            self.database.create_guest_notification_subscription(
                request_id=request_id,
                email_encrypted=secret_store.store("guest@example.com"),
                email_hash=hashlib.sha256(b"guest@example.com").hexdigest(),
                verification_token_encrypted=secret_store.store(verify_token),
                verification_token_hash=token_hash(verify_token),
                verification_expires_at="2999-01-01T00:00:00Z",
                unsubscribe_token_encrypted=secret_store.store(unsubscribe_token),
                unsubscribe_token_hash=token_hash(unsubscribe_token),
            )
            stored = self.database.get_guest_notification_subscription(request_id)
            self.assertNotIn("guest@example.com", str(stored))
            self.assertNotIn(verify_token, str(stored))
            self.assertIsNotNone(self.database.verify_guest_notification_subscription(verify_token))
            self.assertIsNone(self.database.verify_guest_notification_subscription(verify_token))
            self.assertIsNotNone(self.database.opt_out_guest_notification_subscription(unsubscribe_token))
            self.assertIsNotNone(
                self.database.get_guest_notification_subscription(request_id)["opted_out_at"]
            )

    def test_settings_reject_invalid_delivery_pagination_and_test_payload(self) -> None:
        service = NotificationSettingsService(db=self.database)
        self.assertEqual(service.deliveries({"limit": "bad"})[1], 400)
        self.assertEqual(service.deliveries({"offset": -1})[1], 400)
        self.assertEqual(service.test([])[1], 400)
        self.assertEqual(service.update([])[1], 400)
        self.assertEqual(service.update({"config": []})[1], 400)

    def test_test_audit_rows_are_counted_individually(self) -> None:
        service = NotificationSettingsService(db=self.database)
        with mock.patch(
            "fnos_media_import.services.notification_settings_service.smtp_channel.send_email"
        ):
            config = _enabled_config()
            self.assertEqual(service._test_email(config, "https://example.com")[1], 200)
            self.assertEqual(service._test_email(config, "https://example.com")[1], 200)
        summary = self.database.notification_delivery_summary()
        self.assertEqual(summary["success"], 2)
        self.assertEqual(summary["total"], 2)

    def test_admin_retry_reactivates_failed_task_with_current_revision(self) -> None:
        db = self.database
        config = _enabled_config()
        config["rules"][notify_events.EVENT_GUEST_NEW] = ["webhook"]
        notify_config.write_config(db, config)
        emitted = emit_notification(
            db,
            notify_events.EVENT_GUEST_NEW,
            {"title": "测试"},
            idempotency_key="notify:admin-retry:1",
        )
        task_id = emitted["task_id"]
        claimed = db.worker_tasks.claim("test-worker", task_types=["notification_deliver"])
        self.assertEqual(claimed["id"], task_id)
        self.assertTrue(db.worker_tasks.fail(task_id, "test-worker", "failed", terminal=True))
        old_revision = db.worker_tasks.get(task_id)["payload"]["channel_revisions"]["webhook"]
        config["webhook"]["allow_private"] = True
        notify_config.write_config(db, config)

        body, status = NotificationSettingsService(db=db).retry(task_id)

        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        reactivated = db.worker_tasks.get(task_id)
        self.assertEqual(reactivated["status"], "pending")
        self.assertNotEqual(
            reactivated["payload"]["channel_revisions"]["webhook"], old_revision
        )

    def test_terminal_subscription_is_anonymized_after_retention(self) -> None:
        key = "base64:" + base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
        db = self.database
        request_id = self._create_request("anon-sub")
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": key}):
            db.create_guest_notification_subscription(
                request_id=request_id,
                email_encrypted=secret_store.store("guest@example.com"),
                email_hash=hashlib.sha256(b"guest@example.com").hexdigest(),
                verification_token_encrypted=secret_store.store("v-token"),
                verification_token_hash=token_hash("v-token"),
                verification_expires_at="2999-01-01T00:00:00Z",
                unsubscribe_token_encrypted=secret_store.store("u-token"),
                unsubscribe_token_hash=token_hash("u-token"),
            )
        # 置为终态并回拨结束时间，让它超过匿名化保留期
        with db.connect() as connection:
            connection.execute(
                "UPDATE guest_requests SET status='rejected', updated_at='2020-01-01T00:00:00Z' WHERE id=?",
                (request_id,),
            )
        # 仍在处理中的申请不应被匿名化
        fresh_id = self._create_request("anon-fresh")
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": key}):
            db.create_guest_notification_subscription(
                request_id=fresh_id,
                email_encrypted=secret_store.store("fresh@example.com"),
                email_hash=hashlib.sha256(b"fresh@example.com").hexdigest(),
                verification_token_encrypted=secret_store.store("v2"),
                verification_token_hash=token_hash("v2"),
                verification_expires_at="2999-01-01T00:00:00Z",
                unsubscribe_token_encrypted=secret_store.store("u2"),
                unsubscribe_token_hash=token_hash("u2"),
            )

        count = db.anonymize_guest_notification_subscriptions(older_than="2024-01-01T00:00:00Z")
        self.assertEqual(count, 1)
        old = db.get_guest_notification_subscription(request_id)
        self.assertEqual(old["email_encrypted"], "")
        self.assertEqual(old["email_hash"], "")
        self.assertEqual(old["verification_token_hash"], "")
        self.assertEqual(old["unsubscribe_token_encrypted"], "")
        self.assertTrue(old["unsubscribe_token_hash"].startswith("anon:"))
        fresh = db.get_guest_notification_subscription(fresh_id)
        self.assertNotEqual(fresh["email_encrypted"], "")

    def test_migration_includes_encrypted_verification_token(self) -> None:
        with self.database.connect() as connection:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(guest_notification_subscriptions)"
                ).fetchall()
            }
        self.assertIn("verification_token_encrypted", columns)


if __name__ == "__main__":
    unittest.main()
