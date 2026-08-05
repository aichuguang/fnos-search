"""通知配置：读取、校验、脱敏与运行时解析。

配置整体存放在 ``app_settings`` 的单条 JSON（key = ``notifications``），
沿用现有设置持久化方式。凭据字段只接受 ``env:环境变量名`` 引用或
AES-GCM 密文（见 ``secrets.py``），接口返回时一律脱敏。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from ..time_utils import utc_now_iso
from . import events as event_defs
from . import secrets as secret_store

NOTIFICATIONS_SETTING_KEY = "notifications"

DEFAULT_NOTIFICATIONS_CONFIG: dict[str, Any] = {
    "enabled": False,
    "public_base_url": "",
    "digest_hour": 9,
    "digest_timezone": "Asia/Shanghai",
    "delivery_retention_days": 90,
    "guest_anonymize_days": 30,
    "guest": {"enabled": True},
    "smtp": {
        "enabled": False,
        "host": "",
        "port": 465,
        "security": "ssl",
        "username": "",
        "password": "",
        "from_name": "",
        "from_email": "",
        "admin_recipients": [],
    },
    "webhook": {
        "enabled": False,
        "url": "",
        "secret": "",
        "allow_private": False,
    },
    "rules": {
        event_key: list(channels)
        for event_key, channels in event_defs.DEFAULT_RULES.items()
    },
}

_SMTP_SECURITY_VALUES = {"ssl", "starttls", "none"}
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_config(db: Any) -> dict[str, Any]:
    settings = db.get_app_settings()
    stored = settings.get(NOTIFICATIONS_SETTING_KEY)
    if not isinstance(stored, dict):
        stored = {}
    return deep_merge(copy.deepcopy(DEFAULT_NOTIFICATIONS_CONFIG), stored)


def write_config(db: Any, config: dict[str, Any]) -> None:
    db.set_app_settings({NOTIFICATIONS_SETTING_KEY: config})


def normalize(payload: dict[str, Any], *, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """把前端提交的字段规范化并合并到当前配置，未知字段丢弃。"""
    merged = deep_merge(copy.deepcopy(DEFAULT_NOTIFICATIONS_CONFIG), copy.deepcopy(current or {}))
    source = payload if isinstance(payload, dict) else {}

    merged["enabled"] = _bool(source.get("enabled"), merged["enabled"])
    if isinstance(source.get("public_base_url"), str):
        public_base_url = source["public_base_url"].strip().rstrip("/")
        if public_base_url:
            parsed_base_url = urlparse(public_base_url)
            if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
                raise ValueError("public_base_url 必须是完整的 http/https 地址")
            if parsed_base_url.username or parsed_base_url.password:
                raise ValueError("public_base_url 不能包含用户名或密码")
        merged["public_base_url"] = public_base_url
    if "digest_hour" in source:
        merged["digest_hour"] = _int(source["digest_hour"], merged.get("digest_hour", 9), 0, 23)
    if isinstance(source.get("digest_timezone"), str):
        merged["digest_timezone"] = source["digest_timezone"].strip() or "Asia/Shanghai"
    if "delivery_retention_days" in source:
        merged["delivery_retention_days"] = _int(
            source["delivery_retention_days"], merged.get("delivery_retention_days", 90), 7, 3650
        )
    if "guest_anonymize_days" in source:
        merged["guest_anonymize_days"] = _int(
            source["guest_anonymize_days"], merged.get("guest_anonymize_days", 30), 7, 3650
        )
    guest_source = source.get("guest") if isinstance(source.get("guest"), dict) else {}
    guest = merged.setdefault("guest", {})
    guest["enabled"] = _bool(guest_source.get("enabled"), guest.get("enabled", True))

    smtp_source = source.get("smtp") if isinstance(source.get("smtp"), dict) else {}
    smtp = merged.setdefault("smtp", {})
    smtp["enabled"] = _bool(smtp_source.get("enabled"), smtp.get("enabled", False))
    if isinstance(smtp_source.get("host"), str):
        smtp["host"] = smtp_source["host"].strip()
    if "port" in smtp_source:
        smtp["port"] = _int(smtp_source["port"], smtp.get("port", 465), 1, 65535)
    if isinstance(smtp_source.get("security"), str) and smtp_source["security"].strip() in _SMTP_SECURITY_VALUES:
        smtp["security"] = smtp_source["security"].strip()
    if isinstance(smtp_source.get("username"), str):
        smtp["username"] = smtp_source["username"].strip()
    if isinstance(smtp_source.get("password"), str) and smtp_source["password"] != "":
        smtp["password"] = _store_secret(
            smtp_source["password"], smtp.get("password", ""), field="smtp.password"
        )
    if isinstance(smtp_source.get("from_name"), str):
        smtp["from_name"] = smtp_source["from_name"].strip()
    if isinstance(smtp_source.get("from_email"), str):
        smtp["from_email"] = smtp_source["from_email"].strip()
    if "admin_recipients" in smtp_source:
        recipients = smtp_source.get("admin_recipients")
        if isinstance(recipients, str):
            recipients = [part.strip() for part in recipients.replace(";", ",").split(",") if part.strip()]
        elif isinstance(recipients, list):
            recipients = [str(item).strip() for item in recipients if str(item).strip()]
        else:
            recipients = None
        if recipients is not None:
            smtp["admin_recipients"] = recipients

    webhook_source = source.get("webhook") if isinstance(source.get("webhook"), dict) else {}
    webhook = merged.setdefault("webhook", {})
    webhook["enabled"] = _bool(webhook_source.get("enabled"), webhook.get("enabled", False))
    if isinstance(webhook_source.get("url"), str) and webhook_source["url"] != "":
        webhook["url"] = _store_secret(
            webhook_source["url"], webhook.get("url", ""), field="webhook.url"
        )
    if isinstance(webhook_source.get("secret"), str) and webhook_source["secret"] != "":
        webhook["secret"] = _store_secret(
            webhook_source["secret"], webhook.get("secret", ""), field="webhook.secret"
        )
    webhook["allow_private"] = _bool(webhook_source.get("allow_private"), webhook.get("allow_private", False))

    clear_secrets = source.get("clear_secrets")
    if isinstance(clear_secrets, list):
        for path in {str(item) for item in clear_secrets}:
            if path == "smtp.password":
                smtp["password"] = ""
            elif path == "webhook.url":
                webhook["url"] = ""
            elif path == "webhook.secret":
                webhook["secret"] = ""

    rules_source = source.get("rules") if isinstance(source.get("rules"), dict) else {}
    if rules_source:
        merged["rules"] = normalize_rules(rules_source, current=merged.get("rules", {}))

    return merged


def normalize_rules(payload: dict[str, Any], *, current: dict[str, Any]) -> dict[str, Any]:
    rules = copy.deepcopy(current if isinstance(current, dict) else {})
    for event_key, channels in payload.items():
        if event_key not in event_defs.ALL_EVENTS:
            continue
        if isinstance(channels, str):
            channels = [part.strip() for part in channels.replace(";", ",").split(",") if part.strip()]
        if not isinstance(channels, list):
            continue
        normalized = [str(ch) for ch in channels if str(ch) in event_defs.ALL_CHANNELS]
        # 去重保持顺序
        seen: set[str] = set()
        unique: list[str] = []
        for ch in normalized:
            if ch not in seen:
                seen.add(ch)
                unique.append(ch)
        rules[event_key] = unique
    return rules


def redact(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    smtp = result.get("smtp") if isinstance(result.get("smtp"), dict) else {}
    if "password" in smtp:
        smtp["password"] = _redact_secret(smtp["password"])
    webhook = result.get("webhook") if isinstance(result.get("webhook"), dict) else {}
    if "url" in webhook:
        webhook["url"] = _redact_secret(webhook["url"])
    if "secret" in webhook:
        webhook["secret"] = _redact_secret(webhook["secret"])
    return result


def resolve_channels(config: dict[str, Any], event_type: str) -> list[str]:
    """返回该事件命中且已启用配置的渠道列表（顺序稳定）。"""
    rules = config.get("rules") if isinstance(config.get("rules"), dict) else {}
    channels = [str(ch) for ch in rules.get(event_type, [])]
    return [ch for ch in channels if channel_enabled(config, ch)]


def channel_enabled(config: dict[str, Any], channel: str) -> bool:
    if channel == event_defs.CHANNEL_EMAIL:
        smtp = config.get("smtp") if isinstance(config.get("smtp"), dict) else {}
        return bool(
            smtp.get("enabled")
            and smtp.get("host")
            and smtp.get("from_email")
            and smtp.get("admin_recipients")
        )
    if channel == event_defs.CHANNEL_WEBHOOK:
        webhook = config.get("webhook") if isinstance(config.get("webhook"), dict) else {}
        return bool(webhook.get("enabled")) and bool(webhook.get("url"))
    if channel == event_defs.CHANNEL_GUEST_EMAIL:
        smtp = config.get("smtp") if isinstance(config.get("smtp"), dict) else {}
        guest = config.get("guest") if isinstance(config.get("guest"), dict) else {}
        return bool(
            smtp.get("enabled")
            and smtp.get("host")
            and smtp.get("from_email")
            and guest.get("enabled", True)
        )
    return False


def smtp_config(config: dict[str, Any]) -> dict[str, Any]:
    smtp = config.get("smtp") if isinstance(config.get("smtp"), dict) else {}
    resolved = copy.deepcopy(smtp)
    stored_password = str(smtp.get("password") or "")
    resolved_password = secret_store.resolve(stored_password)
    resolved["password"] = resolved_password
    resolved["_password_resolution_failed"] = bool(stored_password and not resolved_password)
    return resolved


def webhook_config(config: dict[str, Any]) -> dict[str, Any]:
    webhook = config.get("webhook") if isinstance(config.get("webhook"), dict) else {}
    resolved = copy.deepcopy(webhook)
    resolved["url"] = secret_store.resolve(webhook.get("url", ""))
    resolved["secret"] = secret_store.resolve(webhook.get("secret", ""))
    return resolved


def channel_revision(config: dict[str, Any], channel: str) -> str:
    if channel in {event_defs.CHANNEL_EMAIL, event_defs.CHANNEL_GUEST_EMAIL}:
        value = config.get("smtp") if isinstance(config.get("smtp"), dict) else {}
        value = copy.deepcopy(value)
        if channel == event_defs.CHANNEL_GUEST_EMAIL:
            value.pop("admin_recipients", None)
    elif channel == event_defs.CHANNEL_WEBHOOK:
        value = config.get("webhook") if isinstance(config.get("webhook"), dict) else {}
    else:
        value = {}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def guest_email_available(config: dict[str, Any]) -> bool:
    return (
        bool(config.get("enabled"))
        and bool(str(config.get("public_base_url") or "").strip())
        and channel_enabled(config, event_defs.CHANNEL_GUEST_EMAIL)
    )


def _store_secret(value: str, current_stored: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return current_stored if isinstance(current_stored, str) else ""
    if secret_store.is_env_ref(text):
        env_name = text[len(secret_store.ENV_PREFIX):].strip()
        if not _ENV_NAME_PATTERN.fullmatch(env_name):
            raise ValueError(f"{field} 的环境变量引用不正确")
        return text
    # 提交值若是接口返回的脱敏占位，忽略它（保留原值）
    if text in {"***", "********"}:
        return current_stored if isinstance(current_stored, str) else ""
    return secret_store.store(text)


def _redact_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if secret_store.is_env_ref(text):
        return text
    if secret_store.is_encrypted(text):
        return "********"
    return "********" if text else ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def stamp_now() -> str:
    return utc_now_iso()


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "否", ""}:
            return False
    return default


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
