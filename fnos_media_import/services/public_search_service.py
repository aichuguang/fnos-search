from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class SearchPort(Protocol):
    def search(self, keyword: str, *, sources: list[str], token: str, options: dict[str, Any]) -> dict[str, Any]: ...


class SearchCachePort(Protocol):
    def find_active_search_cache_by_urls(self, keyword: str, urls: list[str]) -> dict[str, dict[str, Any]]: ...
    def save_search_cache_many(self, records: list[tuple[str, dict[str, Any]]], *, keyword: str, expires_minutes: int) -> Any: ...


@dataclass(frozen=True)
class PublicSearchDependencies:
    search: SearchPort
    cache: SearchCachePort
    new_public_id: Callable[[], str]
    present_item: Callable[..., dict[str, Any]]
    log_info: Callable[..., Any]
    clock: Callable[[], float] = time.perf_counter
    expires_minutes: int = 60


class PublicSearchService:
    """Coordinates provider search, stable public IDs and response projection."""

    def __init__(self, dependencies: PublicSearchDependencies) -> None:
        self._deps = dependencies

    def search(
        self,
        keyword: str,
        *,
        sources: list[str],
        token: str,
        options: dict[str, Any],
        hide_full_links: bool,
        trace_id: str,
        cache_keyword: str | None = None,
    ) -> dict[str, Any]:
        started = self._deps.clock()
        result = self._deps.search.search(keyword, sources=sources, token=token, options=options)
        service_ms = (self._deps.clock() - started) * 1000

        cache_started = self._deps.clock()
        items = list(result.get("items") or [])
        urls = [str(item.get("url") or "") for item in items]
        cache_key = str(cache_keyword or keyword)
        existing = self._deps.cache.find_active_search_cache_by_urls(cache_key, urls)
        cache_records: list[tuple[str, dict[str, Any]]] = []
        public_items: list[dict[str, Any]] = []
        for item in items:
            cached = existing.get(str(item.get("url") or ""))
            public_id = str((cached or {}).get("public_id") or "")
            if not public_id:
                public_id = self._deps.new_public_id()
                cache_records.append((public_id, item))
            public_items.append(
                self._deps.present_item(item, public_id=public_id, hide_full_links=hide_full_links)
            )
        self._deps.cache.save_search_cache_many(
            cache_records, keyword=cache_key, expires_minutes=self._deps.expires_minutes
        )
        cache_ms = (self._deps.clock() - cache_started) * 1000
        self._deps.log_info(
            "search_trace=%s stage=public_service_done service_ms=%.1f cache_ms=%.1f items=%d cache_new=%d cache_reused=%d",
            trace_id,
            service_ms,
            cache_ms,
            len(public_items),
            len(cache_records),
            len(public_items) - len(cache_records),
        )
        return {
            "success": True,
            "items": public_items,
            "expires_in_minutes": self._deps.expires_minutes,
        }
