from __future__ import annotations

from typing import Any, Callable


class UpdateSubscriptionQueryService:
    """Reads update subscriptions and overlays current path-health data."""

    def __init__(
        self,
        *,
        database: Any,
        categories: Callable[[], dict[str, dict[str, Any]]],
        path_health: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.database = database
        self.categories = categories
        self.path_health = path_health

    def list(self, *, page: int = 1, per_page: int = 50, status: str | None = None) -> dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, int(per_page or 50))
        offset = (safe_page - 1) * safe_per_page
        items = self.database.list_update_subscriptions(
            limit=safe_per_page,
            offset=offset,
            status=status,
            include_sources=True,
        )
        total = self.database.count_update_subscriptions(status=status)
        return {"items": [self.with_path_health(item) for item in items], "total": total}

    def get(self, subscription_id: int) -> dict[str, Any] | None:
        item = self.database.get_update_subscription(subscription_id, include_sources=True)
        return self.with_path_health(item)

    def with_path_health(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return item
        data = dict(item)
        category_key = str(data.get("category") or "movie")
        category = self.categories().get(category_key, {})
        raw_data = dict(data.get("raw_data")) if isinstance(data.get("raw_data"), dict) else {}
        raw_data["path_health"] = self.path_health(category_key, category, raw_data)
        data["raw_data"] = raw_data
        return data
