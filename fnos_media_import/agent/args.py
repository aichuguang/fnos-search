"""Argument coercion shared by tool handlers.

Every helper raises ``AgentError`` with a stable code instead of letting raw
``KeyError``/``TypeError`` reach the model.
"""

from __future__ import annotations

from typing import Any

from .errors import invalid_params, missing_argument


def require_text(arguments: dict[str, Any], name: str, *, max_length: int = 300) -> str:
    raw = arguments.get(name)
    text = str(raw or "").strip()
    if not text:
        raise missing_argument(name)
    if len(text) > max_length:
        raise invalid_params(f"{name} 超过 {max_length} 字符")
    return text


def optional_text(arguments: dict[str, Any], name: str, *, max_length: int = 300) -> str:
    text = str(arguments.get(name) or "").strip()
    if len(text) > max_length:
        raise invalid_params(f"{name} 超过 {max_length} 字符")
    return text


def require_int(arguments: dict[str, Any], name: str, *, minimum: int = 1, maximum: int = 2_147_483_647) -> int:
    raw = arguments.get(name)
    if raw is None or raw == "":
        raise missing_argument(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise invalid_params(f"{name} 必须是整数") from None
    if value < minimum or value > maximum:
        raise invalid_params(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def optional_int(arguments: dict[str, Any], name: str, *, default: int, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    raw = arguments.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise invalid_params(f"{name} 必须是整数") from None
    return max(minimum, min(maximum, value))


def optional_bool(arguments: dict[str, Any], name: str, *, default: bool = False) -> bool:
    raw = arguments.get(name)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise invalid_params(f"{name} 必须是布尔值")


def optional_text_list(arguments: dict[str, Any], name: str, *, max_items: int = 20, max_length: int = 64) -> list[str]:
    raw = arguments.get(name)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise invalid_params(f"{name} 必须是字符串数组")
    items: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()
        if not text:
            continue
        if len(text) > max_length:
            raise invalid_params(f"{name} 中存在超过 {max_length} 字符的条目")
        items.append(text)
        if len(items) >= max_items:
            break
    return items
