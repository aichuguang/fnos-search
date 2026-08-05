"""通用 Webhook 渠道：只 POST、默认 HTTPS、HMAC-SHA256 签名、SSRF 防护。

Webhook 目标由单管理员配置，默认禁止解析到私有/保留地址，避免 SSRF 与
内网探测；NAS 场景需要本地回环或内网地址时，可在高级设置中开启
``allow_private``（会同时放行 http）。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlparse, urlunsplit

import urllib3


class WebhookPermanentError(Exception):
    """重试无意义（配置错误、4xx、SSRF 拦截）。"""


class WebhookTransientError(Exception):
    """可重试（网络错误、超时、5xx、429）。"""


_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def send_webhook(
    config: dict[str, Any],
    payload: dict[str, Any],
    *,
    notification_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发送一次 webhook，返回 (status_code, response_summary)。

    成功返回结果 dict；临时失败抛 :class:`WebhookTransientError`；永久失败
    抛 :class:`WebhookPermanentError`。
    """
    url = str(config.get("url") or "").strip()
    secret = str(config.get("secret") or "")
    allow_private = bool(config.get("allow_private"))
    if not url:
        raise WebhookPermanentError("Webhook URL 未配置")
    parsed, target_ip = _resolve_target(url, allow_private=allow_private)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Notification-Id": str(notification_id),
        "X-Webhook-Timestamp": timestamp,
    }
    if secret:
        signed = timestamp.encode("ascii") + b"." + body
        signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    host = str(parsed.hostname or "")
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    header_host = f"[{host}]" if ":" in host else host
    host_header = header_host if parsed.port is None else f"{header_host}:{port}"
    headers["Host"] = host_header
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    timeout_config = urllib3.Timeout(connect=timeout, read=timeout)
    pool_class = urllib3.HTTPSConnectionPool if parsed.scheme == "https" else urllib3.HTTPConnectionPool
    pool_kwargs: dict[str, Any] = {
        "port": port,
        "timeout": timeout_config,
        "retries": False,
        "maxsize": 1,
        "block": True,
    }
    if parsed.scheme == "https":
        pool_kwargs.update({"assert_hostname": host, "server_hostname": host})
    pool = pool_class(target_ip, **pool_kwargs)
    try:
        response = pool.urlopen(
            "POST",
            target,
            body=body,
            headers=headers,
            redirect=False,
            preload_content=False,
        )
        response.read(4096, decode_content=True)
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise WebhookTransientError("webhook 网络请求失败") from exc
    finally:
        try:
            pool.close()
        except Exception:  # noqa: BLE001
            pass

    status_code = int(response.status)
    if 200 <= status_code < 300:
        return {"status_code": status_code, "response_summary": f"HTTP {status_code}"}
    if status_code == 429 or status_code >= 500:
        raise WebhookTransientError(f"webhook 返回 HTTP {status_code}")
    raise WebhookPermanentError(f"webhook 返回 HTTP {status_code}")


def _validate_target(url: str, *, allow_private: bool) -> None:
    _resolve_target(url, allow_private=allow_private)


def _resolve_target(url: str, *, allow_private: bool) -> tuple[Any, str]:
    parsed = urlparse(url)
    scheme = str(parsed.scheme or "").lower()
    host = str(parsed.hostname or "")
    if scheme not in {"http", "https"}:
        raise WebhookPermanentError("webhook 仅支持 http/https")
    if not allow_private and scheme != "https":
        raise WebhookPermanentError("webhook 默认只允许 HTTPS；如需内网 http，请在高级设置开启“允许私有网络地址”")
    if not host:
        raise WebhookPermanentError("webhook URL 缺少主机名")
    if parsed.username or parsed.password:
        raise WebhookPermanentError("webhook URL 不能包含用户名或密码")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebhookPermanentError("webhook 目标无法解析") from exc
    seen: set[str] = set()
    permitted: list[str] = []
    for address in addresses:
        ip_text = str(address[4][0])
        if ip_text in seen:
            continue
        seen.add(ip_text)
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if not allow_private and _is_blocked(ip):
            raise WebhookPermanentError("webhook 目标命中私有/保留地址，已阻止")
        permitted.append(ip_text)
    if not permitted:
        raise WebhookPermanentError("webhook 目标没有可用地址")
    return parsed, permitted[0]


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked(ip.ipv4_mapped)
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    for network in _PRIVATE_NETWORKS:
        if ip.version == network.version and ip in network:
            return True
    return False


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"
