from __future__ import annotations

from typing import Any


TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "是", "启用"})


def positive_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def non_negative_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_VALUES


def positive_int_list(value: Any) -> list[int]:
    raw = value if isinstance(value, list) else str(value or "").replace("\n", ",").split(",")
    result: list[int] = []
    for item in raw:
        number = positive_int(item)
        if number and number not in result:
            result.append(number)
    return result


def unique_string_list(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").replace("\n", ",").split(",")
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def allowed_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default
