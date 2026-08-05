from __future__ import annotations

import logging
from typing import Any, Callable


logger = logging.getLogger(__name__)


class UpdateSubscriptionCommandService:
    """Owns create, update, status and delete commands for update subscriptions."""

    def __init__(
        self,
        *,
        database: Any,
        normalize: Callable[[dict[str, Any]], tuple[dict[str, Any], list[dict[str, Any]]]],
        compute_next_run: Callable[..., str],
        refresh_context: Callable[[int], dict[str, Any]],
        get_subscription: Callable[[int], dict[str, Any] | None],
    ) -> None:
        self.database = database
        self.normalize = normalize
        self.compute_next_run = compute_next_run
        self.refresh_context = refresh_context
        self.get_subscription = get_subscription

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        data, sources = self.normalize(payload)
        data["next_run_at"] = self.compute_next_run(data)
        create_with_outcome = getattr(self.database, "create_update_subscription_with_outcome", None)
        if callable(create_with_outcome):
            subscription_id, created = create_with_outcome(data, sources)
        else:  # pragma: no cover - compatibility for external repository adapters
            subscription_id = self.database.create_update_subscription(data, sources)
            created = True
        item = self.get_subscription(subscription_id) or {"id": subscription_id}
        if not created:
            return {
                **item,
                "_created": False,
                "message": "已存在相同 TMDB、分类和季号的追更订阅，本次提交未覆盖原配置",
            }
        self.database.add_update_event(
            subscription_id,
            None,
            "info",
            "创建定时追更订阅",
            {"title": data.get("title")},
        )
        self._refresh_and_warn(subscription_id)
        return {**(self.get_subscription(subscription_id) or item), "_created": True}

    def create_from_trending_candidate(self, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        data, sources = self.normalize(payload)
        data["next_run_at"] = self.compute_next_run(data)
        repository = getattr(self.database, "update", None)
        get_or_create = getattr(repository, "get_or_create_trending_subscription", None)
        if not callable(get_or_create):
            raise RuntimeError("当前数据库不支持热榜追更幂等创建")
        subscription_id, created = get_or_create(int(candidate_id), data, sources)
        if created:
            try:
                self.database.add_update_event(
                    subscription_id,
                    None,
                    "info",
                    "从热榜候选创建定时追更订阅",
                    {"candidate_id": int(candidate_id), "title": data.get("title")},
                )
                self._refresh_and_warn(subscription_id)
            except Exception:  # noqa: BLE001
                # 订阅与候选已经在同一事务内可靠绑定；非关键的事件或路径体检
                # 失败不能让客户端误以为创建失败并再次提交。
                logger.exception(
                    "post-create trending subscription refresh failed: candidate_id=%s subscription_id=%s",
                    int(candidate_id),
                    subscription_id,
                )
        item = self.get_subscription(subscription_id) or {"id": subscription_id}
        return {**item, "_created": created}

    def update(self, subscription_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.database.get_update_subscription(subscription_id, include_sources=True)
        if not current:
            raise ValueError("追更订阅不存在")
        merged_payload = {**current, **payload}
        if isinstance(payload.get("raw_data"), dict):
            current_raw = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
            merged_payload["raw_data"] = {**current_raw, **payload["raw_data"]}
        data, sources = self.normalize(merged_payload)
        if payload.get("recompute_next_run", True):
            data["next_run_at"] = self.compute_next_run(data)
        self.database.update_update_subscription(subscription_id, data, sources)
        self.database.add_update_event(subscription_id, None, "info", "更新定时追更订阅")
        self._refresh_and_warn(subscription_id)
        return self.get_subscription(subscription_id) or {"id": subscription_id}

    def set_status(self, subscription_id: int, status: str) -> dict[str, Any]:
        if status not in {"enabled", "paused", "archived"}:
            raise ValueError("不支持的订阅状态")
        current = self.database.get_update_subscription(subscription_id, include_sources=True)
        if not current:
            raise ValueError("追更订阅不存在")
        updates: dict[str, Any] = {"status": status}
        if status == "enabled":
            updates["next_run_at"] = self.compute_next_run(current)
        self.database.update_update_subscription(subscription_id, updates)
        self.database.add_update_event(subscription_id, None, "info", f"订阅状态已更新为 {status}")
        return self.database.get_update_subscription(subscription_id, include_sources=True) or {"id": subscription_id}

    def delete(self, subscription_id: int) -> dict[str, Any]:
        current = self.database.get_update_subscription(subscription_id, include_sources=False)
        if not current or not self.database.delete_update_subscription(subscription_id):
            raise ValueError("追更订阅不存在")
        return {"success": True, "message": "追更订阅已删除", "id": subscription_id}

    def _refresh_and_warn(self, subscription_id: int) -> None:
        refreshed = self.refresh_context(subscription_id)
        raw_data = refreshed.get("raw_data") if isinstance(refreshed.get("raw_data"), dict) else {}
        path_health = raw_data.get("path_health") if isinstance(raw_data.get("path_health"), dict) else None
        if isinstance(path_health, dict) and not path_health.get("success"):
            self.database.add_update_event(
                subscription_id,
                None,
                "warn",
                path_health.get("message") or "追更路径体检存在风险",
                path_health,
            )
