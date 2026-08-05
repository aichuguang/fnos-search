from __future__ import annotations

from typing import Any

from ..providers.btbtla import BtbtlaClient
from .base import SearchProviderConfig


class BtbtlaProvider:
    key = "btbtla"
    name = "BTBTLA 磁链搜索"

    def __init__(self, client: BtbtlaClient, config: SearchProviderConfig | None = None):
        self.client = client
        self.config = config or SearchProviderConfig(key=self.key, name=self.name)
        self.priority = int(self.config.priority)

    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.client.configured)

    def apply_runtime_config(self, *, enabled: bool | None = None, priority: int | None = None) -> None:
        if enabled is not None:
            self.config.enabled = bool(enabled)
        if priority is not None:
            self.config.priority = int(priority)
            self.priority = int(priority)

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "enabled": self.is_enabled(),
            "priority": self.priority,
            "configured": bool(self.client.configured),
            "capabilities": {
                "search": True,
                "lazy_magnet_resolve": True,
                "resource_preview": True,
            },
            "message": "搜索影视详情，选中后列出下载资源并解析磁链",
        }

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        source_values = {str(item or "").strip().lower() for item in (sources or []) if str(item or "").strip()}
        if source_values and not ({"btbtla", "bt", "magnet", "all"} & source_values):
            return {"items": [], "raw": {"skipped": True, "reason": "source_filter"}}
        result = self.client.search(keyword)
        items = []
        for item in result.get("items") or []:
            normalized = dict(item)
            normalized.setdefault("source", self.key)
            normalized.setdefault("provider", self.key)
            normalized.setdefault("provider_name", self.name)
            normalized["provider_priority"] = self.priority
            normalized["matched_keyword"] = keyword
            items.append(normalized)
        return {"items": items, "raw": result.get("raw"), "provider": self.key}
