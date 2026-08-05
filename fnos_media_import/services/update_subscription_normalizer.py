from __future__ import annotations

from typing import Any, Callable

from .update_values import allowed_choice, non_negative_int, positive_int, positive_int_list, unique_string_list


class UpdateSubscriptionNormalizer:
    """Validates and normalizes create/update payloads for update subscriptions."""

    def __init__(
        self,
        *,
        categories: Callable[[], dict[str, dict[str, Any]]],
        tmdb_schedule_hint: Callable[[int, str, dict[str, Any]], dict[str, Any]],
        tmdb_basic_hint: Callable[[int, str], dict[str, Any]],
        path_health: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
        normalize_source: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.categories = categories
        self.tmdb_schedule_hint = tmdb_schedule_hint
        self.tmdb_basic_hint = tmdb_basic_hint
        self.path_health = path_health
        self.normalize_source = normalize_source

    def normalize(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        category_key = str(payload.get("category") or "anime").strip()
        category = self.categories().get(category_key, {})
        tmdb_id = _to_int(payload.get("tmdb_id"))
        if not tmdb_id:
            raise ValueError("请先从 TMDB 搜索并选择要追更的影视")
        media_type = str(payload.get("media_type") or ("movie" if category_key == "movie" else "tv")).strip().lower()
        if media_type not in {"movie", "tv"}:
            media_type = "movie" if category_key == "movie" else "tv"
        raw_data = dict(payload.get("raw_data")) if isinstance(payload.get("raw_data"), dict) else {}
        tmdb_hint = self.tmdb_schedule_hint(tmdb_id, media_type, payload)
        tmdb_basic = self.tmdb_basic_hint(tmdb_id, media_type) if media_type == "movie" or not tmdb_hint.get("title") or not tmdb_hint.get("year") else {}
        title = str(tmdb_hint.get("title") or tmdb_basic.get("title") or payload.get("title") or "").strip()
        if not title:
            raise ValueError("TMDB 条目读取失败，请重新搜索并选择")
        if tmdb_hint:
            raw_data["tmdb_schedule"] = tmdb_hint
        if tmdb_basic:
            raw_data["tmdb_basic"] = tmdb_basic
        raw_data["path_health"] = self.path_health(category_key, category, raw_data)
        season = _to_season(payload.get("season"))
        if season is None:
            season = _to_season(tmdb_hint.get("season"))

        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        normalized_sources = [self.normalize_source(item) for item in sources if isinstance(item, dict)]
        if not normalized_sources:
            normalized_sources = [
                {"type": "search", "name": "综合搜索", "provider": "all", "priority": 100, "enabled": True, "options": {}}
            ]
        schedule_default = "tmdb" if media_type == "tv" else "weekly"
        data = {
            "title": title,
            "category": category_key,
            "category_label": category.get("label") or payload.get("category_label") or category_key,
            "media_type": media_type,
            "season": season,
            "year": str(tmdb_hint.get("year") or tmdb_basic.get("year") or payload.get("year") or "").strip(),
            "tmdb_id": tmdb_id,
            "query_template": str(payload.get("query_template") or "").strip(),
            "aliases": _string_list(payload.get("aliases")),
            "schedule_kind": _choice(payload.get("schedule_kind"), {"daily", "weekly", "interval", "manual", "tmdb"}, schedule_default),
            "days_of_week": _int_list(payload.get("days_of_week")) or [5],
            "time_of_day": str(payload.get("time_of_day") or "10:00").strip(),
            "interval_minutes": _to_int(payload.get("interval_minutes")),
            "timezone": str(payload.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai",
            "next_run_at": str(payload.get("next_run_at") or ""),
            "last_run_at": str(payload.get("last_run_at") or ""),
            "last_success_at": str(payload.get("last_success_at") or ""),
            "next_episode": _to_int(payload.get("next_episode")) or _to_int(tmdb_hint.get("episode")),
            "last_success_episode": _to_int(payload.get("last_success_episode")),
            "missing_episodes": _int_list(payload.get("missing_episodes")),
            "source_strategy": _choice(payload.get("source_strategy"), {"mixed", "cloud_first", "search_only", "cloud_only"}, "mixed"),
            "auto_import_policy": "auto_high_confidence",
            "min_score": _to_int(payload.get("min_score")) or 75,
            "quality_profile": str(payload.get("quality_profile") or "").strip(),
            "include_keywords": _string_list(payload.get("include_keywords")),
            "exclude_keywords": _string_list(payload.get("exclude_keywords")),
            "status": _choice(payload.get("status"), {"enabled", "paused", "archived"}, "enabled"),
            "raw_data": raw_data,
        }
        if data["schedule_kind"] == "tmdb" and media_type != "tv":
            data["schedule_kind"] = "weekly"
        return data, normalized_sources


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _to_season(value: Any) -> int | None:
    return non_negative_int(value)


def _int_list(value: Any) -> list[int]:
    return positive_int_list(value)


def _string_list(value: Any) -> list[str]:
    return unique_string_list(value)


def _choice(value: Any, allowed: set[str], default: str) -> str:
    return allowed_choice(value, allowed, default)
