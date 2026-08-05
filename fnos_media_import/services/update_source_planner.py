from __future__ import annotations

from typing import Any


class UpdateSourcePlanner:
    """Builds the fixed-source/search fallback plan for an update run."""

    @staticmethod
    def plan(
        subscription: dict[str, Any],
        *,
        fallback_threshold: int,
        target_key: str,
    ) -> dict[str, Any]:
        sources = [source for source in (subscription.get("sources") or []) if source.get("enabled", True)]
        if not sources:
            sources = [UpdateSourcePlanner.default_search_source()]
        search_sources = [source for source in sources if str(source.get("type") or "").strip().lower() == "search"]
        fixed_sources = [source for source in sources if str(source.get("type") or "").strip().lower() != "search"]
        strategy = str(subscription.get("source_strategy") or "mixed").strip().lower()

        if strategy == "search_only" or not fixed_sources:
            return {
                "mode": "search_only" if strategy == "search_only" else "search_default",
                "primary_sources": search_sources or [UpdateSourcePlanner.default_search_source()],
                "fallback_sources": [],
                "fixed_sources": [],
                "threshold": max(1, int(fallback_threshold or 1)),
                "target_key": target_key,
            }
        return {
            "mode": "fixed_first",
            "primary_sources": fixed_sources,
            "fallback_sources": search_sources or [UpdateSourcePlanner.default_fallback_source()],
            "fixed_sources": fixed_sources,
            "threshold": max(1, int(fallback_threshold or 1)),
            "target_key": target_key,
        }

    @staticmethod
    def default_search_source() -> dict[str, Any]:
        return {"type": "search", "name": "综合搜索", "enabled": True, "priority": 100, "options": {}}

    @staticmethod
    def default_fallback_source() -> dict[str, Any]:
        return {"type": "search", "name": "自动搜索兜底", "enabled": True, "priority": 999, "options": {}}
