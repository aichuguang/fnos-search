"""SMTP 渠道：SSL/STARTTLS、连接超时、5xx 永久 / 4xx 与超时临时分类。

``send_email`` 抛出的异常类型决定通知 Worker 是否重试：永久错误直接记
失败，临时错误按退避计划重试。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
import hashlib
import logging
import socket
import time
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 10.0
QUIT_TIMEOUT_SECONDS = 1.0

logger = logging.getLogger(__name__)


class SmtpPermanentError(Exception):
    """SMTP 5xx 或配置缺失，重试无意义。"""


class SmtpTransientError(Exception):
    """SMTP 4xx、连接失败或超时，可重试。"""


class _IPv4FirstSMTP(smtplib.SMTP):
    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _create_connection_ipv4_first((host, port), timeout, self.source_address)


class _IPv4FirstSMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw_socket = _create_connection_ipv4_first((host, port), timeout, self.source_address)
        try:
            return self.context.wrap_socket(raw_socket, server_hostname=self._host)
        except Exception:
            raw_socket.close()
            raise


def send_email(
    config: dict[str, Any],
    subject: str,
    body: str,
    *,
    recipients: list[str],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    html_body: str = "",
    message_id: str = "",
) -> dict[str, Any]:
    host = str(config.get("host") or "").strip()
    if not host:
        raise SmtpPermanentError("SMTP 主机未配置")
    port = int(config.get("port") or 465)
    security = str(config.get("security") or "ssl").strip().lower()
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    from_name = str(config.get("from_name") or "")
    from_email = str(config.get("from_email") or "")
    if not from_email:
        raise SmtpPermanentError("发件人地址未配置")
    if username and bool(config.get("_password_resolution_failed")):
        raise SmtpPermanentError(
            "SMTP 密码无法解密；当前 NOTIFICATION_ENCRYPTION_KEY 与保存密码时使用的密钥不一致，请重新输入 SMTP 授权码并保存"
        )
    if username and not password:
        raise SmtpPermanentError("SMTP 密码或授权码未配置")
    recipients = [str(r).strip() for r in recipients if str(r).strip()]
    if not recipients:
        raise SmtpPermanentError("收件人列表为空")
    invalid = [item for item in recipients if parseaddr(item)[1] != item or "@" not in item]
    if invalid:
        raise SmtpPermanentError("收件人邮箱格式不正确")

    message = EmailMessage()
    message["Subject"] = str(subject or "")
    message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    message["To"] = ", ".join(recipients)
    if message_id:
        digest = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:32]
        message["Message-ID"] = f"<{digest}@fnos-media-import.local>"
    message.set_content(str(body or ""))
    if html_body:
        message.add_alternative(str(html_body), subtype="html")

    timeout = max(1.0, float(timeout))
    started = time.monotonic()
    phase = "连接"
    timings: dict[str, int] = {}
    try:
        phase_started = time.monotonic()
        if security == "ssl":
            smtp = _IPv4FirstSMTPSSL(host, port, timeout=timeout)
        else:
            smtp = _IPv4FirstSMTP(host, port, timeout=timeout)
            if security == "starttls":
                smtp.starttls()
        timings["connect_ms"] = _elapsed_ms(phase_started)
        try:
            if username:
                phase = "认证"
                phase_started = time.monotonic()
                smtp.login(username, password)
                timings["auth_ms"] = _elapsed_ms(phase_started)
            phase = "发送"
            phase_started = time.monotonic()
            smtp.send_message(message)
            timings["send_ms"] = _elapsed_ms(phase_started)
        finally:
            phase_started = time.monotonic()
            _close_smtp(smtp, timeout=min(QUIT_TIMEOUT_SECONDS, timeout))
            timings["close_ms"] = _elapsed_ms(phase_started)
    except smtplib.SMTPResponseException as exc:
        code = int(getattr(exc, "smtp_code", 0) or 0)
        detail = _clip(str(getattr(exc, "smtp_error", "") or ""), 200)
        _log_failure(host, phase, started, code=code)
        if _is_transient_auth_rejection(host, phase, code, detail):
            raise SmtpTransientError(f"SMTP 临时错误({code})：{detail}") from exc
        if code >= 500:
            raise SmtpPermanentError(f"SMTP 拒绝({code})：{detail}") from exc
        raise SmtpTransientError(f"SMTP 临时错误({code})：{detail}") from exc
    except (smtplib.SMTPException, OSError) as exc:  # noqa: BLE001
        _log_failure(host, phase, started)
        raise SmtpTransientError(f"SMTP 连接失败：{_clip(str(exc), 200)}") from exc

    total_ms = _elapsed_ms(started)
    logger.info(
        "SMTP send completed: host=%s recipients=%s total_ms=%s connect_ms=%s auth_ms=%s send_ms=%s close_ms=%s",
        host,
        len(recipients),
        total_ms,
        timings.get("connect_ms", 0),
        timings.get("auth_ms", 0),
        timings.get("send_ms", 0),
        timings.get("close_ms", 0),
    )
    return {
        "status_code": 250,
        "response_summary": f"已发送到 {len(recipients)} 个收件人（{total_ms}ms）",
        "timings": {**timings, "total_ms": total_ms},
    }


def _create_connection_ipv4_first(
    address: tuple[str, int],
    timeout: float | None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect within one timeout budget, preferring IPv4 over broken IPv6 routes."""
    host, port = address
    timeout_value = None if timeout is None else float(timeout)
    if timeout_value is not None and timeout_value <= 0:
        raise ValueError("Non-blocking socket (timeout=0) is not supported")

    candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    candidates.sort(
        key=lambda item: 0
        if item[0] == socket.AF_INET
        else (1 if item[0] == socket.AF_INET6 else 2)
    )
    deadline = time.monotonic() + timeout_value if timeout_value is not None else None
    last_error: OSError | None = None

    for family, socktype, proto, _canonname, sockaddr in candidates:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt_timeout = min(3.0, remaining)
        else:
            attempt_timeout = None

        candidate = socket.socket(family, socktype, proto)
        try:
            candidate.settimeout(attempt_timeout)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(sockaddr)
            candidate.settimeout(timeout_value)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()

    if last_error is not None:
        raise last_error
    raise TimeoutError(f"SMTP connection timed out: {host}:{port}")


def _is_transient_auth_rejection(host: str, phase: str, code: int, detail: str) -> bool:
    if phase != "认证" or code != 500 or not str(host).lower().endswith(".qq.com"):
        return False
    normalized = str(detail or "").lower()
    return "bad syntax" in normalized or "rejectedmail" in normalized


def _close_smtp(smtp: Any, *, timeout: float) -> None:
    """Gracefully close without letting a slow QUIT delay a successful send."""
    sock = getattr(smtp, "sock", None)
    if sock is not None:
        try:
            sock.settimeout(timeout)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        smtp.quit()
    except Exception:  # noqa: BLE001
        try:
            smtp.close()
        except Exception:  # noqa: BLE001
            pass


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _log_failure(host: str, phase: str, started: float, *, code: int = 0) -> None:
    logger.warning(
        "SMTP send failed: host=%s phase=%s total_ms=%s code=%s",
        host,
        phase,
        _elapsed_ms(started),
        code or "-",
    )
