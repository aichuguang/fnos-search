"""Small, path-free media identity contract shared by import and subscriptions."""

from __future__ import annotations

import re
from typing import Any


ORGANIZER_INTENTS = frozenset({"create_or_match", "update_existing"})


def _identity_key(media_type: Any, ns: Any, value: Any) -> tuple[str, str, str] | None:
    if isinstance(value, bool) or value is None:
        return None
    kind = str(media_type or "").strip().lower()
    namespace = str(ns or "").strip().lower()
    identifier = str(value or "").strip()
    if len(identifier) > 64:
        return None
    if kind not in {"tv", "movie"}:
        return None
    if namespace in {"tmdb", "tvdb", "douban"}:
        if not re.fullmatch(r"[0-9]+", identifier) or int(identifier) <= 0:
            return None
        identifier = str(int(identifier))
    elif namespace == "imdb":
        identifier = identifier.lower()
        if not re.fullmatch(r"tt[0-9]+", identifier) or int(identifier[2:]) <= 0:
            return None
    elif namespace == "local":
        identifier = identifier.lower()
        if not re.fullmatch(r"[0-9a-f]{10,64}", identifier):
            return None
    else:
        return None
    return kind, namespace, identifier


def normalize_media_identity(value: Any, *, default_media_type: str = "tv") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    kind = str(value.get("media_type") or default_media_type).strip().lower()
    ns = value.get("ns") or ("tmdb" if value.get("tmdb_id") else "")
    identifier = value.get("value") or (value.get("tmdb_id") if ns == "tmdb" else "")
    key = _identity_key(kind, ns, identifier)
    if not key:
        return {}
    result: dict[str, Any] = {"media_type": key[0], "ns": key[1], "value": key[2]}
    if key[1] == "tmdb":
        result["tmdb_id"] = int(key[2])
    for field, limit in (("title", 500), ("original_title", 500), ("original_query", 500), ("poster_url", 2000), ("overview", 4000)):
        if value.get(field) not in (None, ""):
            result[field] = str(value[field]).strip()[:limit]
    year = value.get("year")
    if year not in (None, ""):
        if isinstance(year, bool) or not re.fullmatch(r"\d{4}", str(year)) or not 1800 <= int(year) <= 2199:
            return {}
        result["year"] = int(year)
    for field in ("season_number",):
        try:
            number = int(value.get(field))
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 999:
            result[field] = number
    aliases = []
    for alias in (value.get("aliases") or [])[:50] if isinstance(value.get("aliases"), list) else []:
        if isinstance(alias, dict):
            normalized = _identity_key(alias.get("media_type") or kind, alias.get("ns"), alias.get("value"))
        elif isinstance(alias, (list, tuple)) and len(alias) == 3:
            normalized = _identity_key(*alias)
        else:
            normalized = None
        if normalized and normalized[0] == kind and normalized != key and list(normalized) not in aliases:
            aliases.append(list(normalized))
    if aliases:
        result["aliases"] = aliases
    return result


def subscription_import_identity(subscription: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
    saved = raw.get("media_identity") if isinstance(raw.get("media_identity"), dict) else {}
    kind = str(subscription.get("media_type") or ("movie" if subscription.get("category") == "movie" else "tv"))
    identity = normalize_media_identity({
        **saved,
        "media_type": kind,
        "ns": subscription.get("identity_ns") or saved.get("ns"),
        "value": subscription.get("identity_value") or saved.get("value"),
        "tmdb_id": subscription.get("tmdb_id") or saved.get("tmdb_id"),
        "title": subscription.get("title") or saved.get("title"),
        "year": subscription.get("year") or saved.get("year"),
        "season_number": subscription.get("season"),
    }, default_media_type=kind)
    binding = raw.get("organized_binding") if isinstance(raw.get("organized_binding"), dict) else {}
    existing = bool(str(subscription.get("canonical_dir") or "").strip() or binding.get("canonical_dir"))
    return identity, "update_existing" if existing else "create_or_match"
