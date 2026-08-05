from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..database import Database
from ..search.aggregator import SearchAggregator

logger = logging.getLogger(__name__)


class SearchService:
    def __init__(self, db: Database, aggregator: SearchAggregator):
        self.db = db
        self.aggregator = aggregator

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        total_started = time.perf_counter()
        runtime_options = options if isinstance(options, dict) else {}
        trace_id = str(runtime_options.get("trace_id") or "-")
        settings_started = time.perf_counter()
        self._apply_runtime_settings()
        logger.info("search_trace=%s stage=runtime_settings elapsed_ms=%.1f", trace_id, _elapsed_ms(settings_started))
        aggregator_started = time.perf_counter()
        result = self.aggregator.search(keyword, sources=sources, token=token, options=options)
        logger.info(
            "search_trace=%s stage=aggregator_service_return elapsed_ms=%.1f items=%d",
            trace_id,
            _elapsed_ms(aggregator_started),
            len(result.get("items") or []),
        )
        if runtime_options.get("save_resources") is False:
            logger.info(
                "search_trace=%s stage=save_resources skipped=true count=%d total_ms=%.1f",
                trace_id,
                len(result.get("items") or []),
                _elapsed_ms(total_started),
            )
            return result
        save_started = time.perf_counter()
        items = result.get("items") or []
        resource_ids = self.db.save_resources(items)
        for item, resource_id in zip(items, resource_ids):
            item["resource_id"] = resource_id
        saved = len(resource_ids)
        save_elapsed = _elapsed_ms(save_started)
        logger.info(
            "search_trace=%s stage=save_resources elapsed_ms=%.1f count=%d avg_ms=%.1f total_ms=%.1f",
            trace_id,
            save_elapsed,
            saved,
            (save_elapsed / saved) if saved else 0,
            _elapsed_ms(total_started),
        )
        return result

    def describe_providers(self) -> list[dict[str, Any]]:
        self._apply_runtime_settings()
        return self.aggregator.describe_providers()

    def _apply_runtime_settings(self) -> None:
        settings = self.db.get_app_settings()
        provider_settings = settings.get("search.providers")
        if not isinstance(provider_settings, dict):
            provider_settings = {}

        for provider in self.aggregator.providers:
            item_settings = provider_settings.get(provider.key)
            if not isinstance(item_settings, dict):
                continue
            enabled = item_settings.get("enabled")
            priority = item_settings.get("priority")
            try:
                priority_value = int(priority) if priority is not None else None
            except (TypeError, ValueError):
                priority_value = None
            if hasattr(provider, "apply_runtime_config"):
                provider.apply_runtime_config(
                    enabled=_to_bool(enabled, provider.is_enabled()) if enabled is not None else None,
                    priority=priority_value,
                )

        aliases = settings.get("search.aliases")
        if isinstance(aliases, dict):
            self.aggregator.aliases = {
                _normalize_text(key): [str(item).strip() for item in values if str(item).strip()]
                for key, values in aliases.items()
                if isinstance(values, list)
            }


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000
