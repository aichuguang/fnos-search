from __future__ import annotations

from typing import Any


def normalize_library_name(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def match_library(items: list[Any], library: str) -> dict[str, Any] | None:
    wanted = normalize_library_name(library)
    for item in items:
        if not isinstance(item, dict):
            continue
        names = (item.get("name"), item.get("title"), item.get("label"))
        if any(normalize_library_name(value) == wanted for value in names if value):
            return item
    for item in items:
        if not isinstance(item, dict):
            continue
        for value in (item.get("name"), item.get("title"), item.get("label")):
            normalized = normalize_library_name(value)
            if wanted and normalized and (wanted in normalized or normalized in wanted):
                return item
    return None


def normalize_remote_hint(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if ":" in text.split("/", 1)[0]:
        text = text.split(":", 1)[1]
    while "//" in text:
        text = text.replace("//", "/")
    if not text:
        return ""
    if text.startswith("/"):
        return "/" + text.strip("/")
    return text.strip("/")


def split_values(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = value.replace("\n", ",").replace("|", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    return [str(item or "").strip() for item in values if str(item or "").strip()]


def path_tails(path: str) -> list[str]:
    parts = [part for part in str(path or "").split("/") if part]
    return ["/".join(parts[index:]) for index in range(len(parts))]


def match_actual_dirs(actual_dirs: list[str], hint: str) -> list[str]:
    hint_norm = normalize_remote_hint(hint)
    if not hint_norm:
        return []
    hint_rel = hint_norm.strip("/")
    result: list[str] = []
    for actual in actual_dirs:
        actual_norm = normalize_remote_hint(actual)
        actual_rel = actual_norm.strip("/")
        if hint_norm.startswith("/"):
            if actual_norm == hint_norm:
                result.append(actual_norm)
            elif hint_norm.startswith(f"{actual_norm}/"):
                result.append(hint_norm)
            elif actual_norm.startswith(f"{hint_norm}/"):
                result.append(actual_norm)
            continue
        if actual_rel == hint_rel or actual_rel.endswith(f"/{hint_rel}"):
            result.append(actual_norm)
            continue
        for tail in path_tails(actual_rel):
            if hint_rel == tail:
                result.append(actual_norm)
                break
            if hint_rel.startswith(f"{tail}/"):
                suffix = hint_rel[len(tail) + 1 :].strip("/")
                result.append(f"{actual_norm.rstrip('/')}/{suffix}" if suffix else actual_norm)
                break
    return list(dict.fromkeys(result))
