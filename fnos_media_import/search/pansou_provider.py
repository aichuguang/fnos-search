from __future__ import annotations

from typing import Any

from ..providers.pansou import PanSouClient
from .base import SearchProviderConfig


class PanSouProvider:
    key = "pansou"
    name = "PanSou"

    def __init__(self, client: PanSouClient, config: SearchProviderConfig | None = None):
        self.client = client
        self.config = config or SearchProviderConfig(key=self.key, name=self.name)
        self.priority = int(self.config.priority)

    def is_enabled(self) -> bool:
        return bool(self.config.enabled)

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
            "configured": bool(self.client.base_url),
            "capabilities": {
                "search": True,
                "requires_token": True,
                "alias_expansion": True,
                "cloud_types": list(getattr(self.client, "cloud_types", [])),
                "async_poll": bool(getattr(self.client, "async_poll_enabled", False)),
                "result_type": getattr(self.client, "result_type", "merge"),
                "source_scope": getattr(self.client, "source_scope", "all"),
            },
            "message": "PanSou 搜索源已接入聚合器" if self.client.base_url else "PanSou 地址未配置",
        }

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        source_values = {str(item or "").strip().lower() for item in (sources or []) if str(item or "").strip()}
        if source_values and not ({"pansou", "all", "quark", "tianyi", "mobile", "magnet", "tg", "plugin"} & source_values):
            return {"items": [], "raw": {"skipped": True, "reason": "source_filter"}, "provider": self.key}
        result = self.client.search(keyword, sources=sources, token=token, options=options)
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
