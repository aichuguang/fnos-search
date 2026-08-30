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
        self.result_provider_priority = list(
            getattr(client, "result_provider_priority", ["quark", "mobile", "tianyi", "magnet"])
        )
        self._result_provider_positions = {
            provider_kind: index
            for index, provider_kind in enumerate(self.result_provider_priority, start=1)
        }

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
                "result_provider_priority": list(self.result_provider_priority),
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
            provider_kind = _result_provider_kind(normalized)
            result_priority = self._result_provider_positions.get(provider_kind, 999)
            normalized["result_provider_priority"] = result_priority
            normalized["result_provider_priority_bonus"] = {1: 10, 2: 5, 3: 2}.get(
                result_priority,
                0,
            )
            normalized["matched_keyword"] = keyword
            items.append(normalized)
        return {"items": items, "raw": result.get("raw"), "provider": self.key}


def _result_provider_kind(item: dict[str, Any]) -> str:
    provider_kind = str(item.get("provider_kind") or "").strip().lower()
    source_form = str(item.get("source_form") or "").strip().lower()
    source_type = str(item.get("source_type") or "").strip().lower()
    if provider_kind in {"quark", "mobile", "tianyi"}:
        return provider_kind
    if source_form == "magnet" or source_type in {"magnet", "torrent"}:
        return "magnet"
    return ""
