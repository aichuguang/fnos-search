from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Callable


def title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _identity_year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _identity_category(item: dict[str, Any]) -> str:
    raw = str(item.get("category") or item.get("media_type") or "").strip().lower()
    return {
        "movie": "movie",
        "film": "movie",
        "tv": "tv",
        "series": "tv",
        "anime": "anime",
        "animation": "anime",
        "variety": "variety",
        "show": "variety",
    }.get(raw, "")


def _record_year(item: dict[str, Any]) -> str:
    for key in ("year", "tmdb_year", "release_year", "canonical_year"):
        year = _identity_year(item.get(key))
        if year:
            return year
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (raw_data, raw_data.get("metadata"), raw_data.get("tmdb")):
        if not isinstance(container, dict):
            continue
        for key in ("year", "tmdb_year", "release_year", "canonical_year"):
            year = _identity_year(container.get(key))
            if year:
                return year
    return _identity_year(item.get("title"))


def _record_tmdb_id(item: dict[str, Any]) -> int:
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (
        item,
        raw_data,
        raw_data.get("subscription_identity"),
        raw_data.get("tmdb"),
    ):
        if not isinstance(container, dict):
            continue
        try:
            tmdb_id = int(container.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            continue
        if tmdb_id > 0:
            return tmdb_id
    return 0


def _record_season(item: dict[str, Any]) -> int | None:
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (
        item,
        raw_data,
        raw_data.get("subscription_identity"),
        raw_data.get("tmdb"),
    ):
        if not isinstance(container, dict):
            continue
        value = container.get("season")
        if value in (None, ""):
            continue
        try:
            season = int(value)
        except (TypeError, ValueError):
            continue
        if season >= 0:
            return season
    return None


def identity_matches(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_tmdb_id = _record_tmdb_id(candidate)
    existing_tmdb_id = _record_tmdb_id(existing)
    same_tmdb_identity = bool(candidate_tmdb_id and candidate_tmdb_id == existing_tmdb_id)
    if candidate_tmdb_id and existing_tmdb_id:
        if candidate_tmdb_id != existing_tmdb_id:
            return False
    elif title_key(candidate.get("title")) != title_key(existing.get("title")):
        return False
    candidate_category = _identity_category(candidate)
    existing_category = _identity_category(existing)
    if not candidate_category or not existing_category or candidate_category != existing_category:
        return False
    if _record_season(candidate) != _record_season(existing):
        return False
    candidate_year = _record_year(candidate)
    existing_year = _record_year(existing)
    if not same_tmdb_identity:
        if bool(candidate_year) != bool(existing_year):
            return False
        if candidate_year != existing_year:
            return False
    return True


def find_existing_subscription(
    item: dict[str, Any],
    subscriptions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return one unambiguous existing subscription for a hot candidate."""

    matches = [
        row
        for row in subscriptions
        if str(row.get("status") or "enabled").strip().lower() != "archived"
        and identity_matches(item, row)
    ]
    return matches[0] if len(matches) == 1 else None


def create_from_candidate(
    item: dict[str, Any],
    tmdb: Any,
    create_subscription: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Create or bind a subscription from an unambiguous TMDB match."""

    title = str(item.get("title") or "").strip()
    if not title:
        return {
            "success": False,
            "message": "候选缺少标题，无法创建追更订阅",
        }, 400
    raw_media_type = str(item.get("media_type") or "").strip().lower()
    category = (
        raw_media_type
        if raw_media_type in {"movie", "tv", "anime", "variety"}
        else "tv"
    )
    media_type = "movie" if category == "movie" else "tv"
    if not getattr(tmdb, "configured", False):
        return {
            "success": False,
            "message": "TMDB 未配置，无法自动匹配，请到追更页手动创建订阅",
        }, 400
    try:
        rows = tmdb.search(title, media_type)
    except Exception:  # noqa: BLE001
        return {
            "success": False,
            "message": "TMDB 查询失败，请稍后重试或到追更页手动创建订阅",
        }, 502
    if not rows:
        return {
            "success": False,
            "message": "TMDB 未匹配到该影视，请到追更页手动创建订阅",
        }, 400
    candidate_year = str(item.get("year") or "").strip()
    expected_titles = [
        str(item.get("title") or "").strip(),
        str(item.get("original_title") or "").strip(),
    ]
    scored = sorted(
        (
            (_tmdb_match_score(expected_titles, candidate_year, media_type, row), row)
            for row in rows
            if isinstance(row, dict) and _tmdb_result_id(row) > 0
        ),
        key=lambda entry: entry[0],
        reverse=True,
    )
    match_preview = [
        {
            "tmdb_id": _tmdb_result_id(row),
            "title": str(row.get("title") or ""),
            "original_title": str(row.get("original_title") or ""),
            "year": str(row.get("year") or ""),
            "media_type": str(row.get("media_type") or media_type),
            "score": score,
        }
        for score, row in scored[:3]
    ]
    if not scored or scored[0][0] < 85:
        return {
            "success": False,
            "message": "TMDB 匹配置信度不足，已停止自动创建，请到追更页手动选择正确影视",
            "matches": match_preview,
        }, 409
    if len(scored) > 1 and scored[1][0] >= 75 and scored[0][0] - scored[1][0] < 8:
        return {
            "success": False,
            "message": "TMDB 匹配结果存在歧义，已停止自动创建，请到追更页手动选择正确影视",
            "matches": match_preview,
        }, 409
    best = scored[0][1]
    tmdb_id = _tmdb_result_id(best)
    if not tmdb_id:
        return {
            "success": False,
            "message": "TMDB 未匹配到该影视，请到追更页手动创建订阅",
        }, 400
    aliases = []
    for value in expected_titles + [str(best.get("original_title") or "").strip()]:
        if value and value != str(best.get("title") or "").strip() and value not in aliases:
            aliases.append(value)
    payload = {
        "title": str(best.get("title") or title),
        "category": category,
        "media_type": media_type,
        "year": str(best.get("year") or item.get("year") or ""),
        "tmdb_id": tmdb_id,
        "season": _record_season(item),
        "aliases": aliases,
    }
    try:
        subscription = create_subscription(payload)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}, 400
    except Exception:  # noqa: BLE001
        return {
            "success": False,
            "message": "创建追更订阅失败",
        }, 500
    subscription_id = int(subscription.get("id") or 0)
    if not subscription_id:
        return {
            "success": False,
            "message": "创建追更订阅失败",
        }, 500
    created = bool(subscription.get("_created", True))
    return {
        "success": True,
        "created": created,
        "message": (
            "追更订阅已创建"
            if created
            else f"已绑定现有追更订阅 #{subscription_id}"
        ),
        "subscription_id": subscription_id,
    }, 200


def _tmdb_match_score(
    expected_titles: list[str],
    expected_year: str,
    expected_media_type: str,
    result: dict[str, Any],
) -> int:
    expected = [_normalize_tmdb_title(value, expected_year) for value in expected_titles]
    actual_year = str(result.get("year") or "").strip()
    actual = [
        _normalize_tmdb_title(result.get("title"), actual_year),
        _normalize_tmdb_title(result.get("original_title"), actual_year),
    ]
    expected = [value for value in expected if value]
    actual = [value for value in actual if value]
    title_score = 0
    for left in expected:
        for right in actual:
            if left == right:
                title_score = max(title_score, 70)
                continue
            shorter = min(len(left), len(right))
            if shorter >= 4 and (left in right or right in left):
                title_score = max(title_score, 52)
            ratio = SequenceMatcher(None, left, right).ratio()
            if ratio >= 0.92:
                title_score = max(title_score, 62)
            elif ratio >= 0.84:
                title_score = max(title_score, 52)
    score = title_score
    if expected_year and actual_year:
        score += 20 if expected_year == actual_year else -25
    elif expected_year and not actual_year:
        score -= 5
    result_media_type = str(result.get("media_type") or expected_media_type).strip().lower()
    score += 10 if result_media_type == expected_media_type else -30
    return max(0, min(100, int(score)))


def _tmdb_result_id(result: dict[str, Any]) -> int:
    try:
        value = int(result.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _normalize_tmdb_title(value: Any, year: str = "") -> str:
    text = str(value or "").strip().casefold()
    expected_year = str(year or "").strip()
    if expected_year:
        text = re.sub(
            rf"[\s._\-()（）\[\]【】]*{re.escape(expected_year)}[\s._\-()（）\[\]【】]*$",
            "",
            text,
        )
    return "".join(character for character in text if character.isalnum())


__all__ = [
    "create_from_candidate",
    "find_existing_subscription",
    "identity_matches",
    "title_key",
]
