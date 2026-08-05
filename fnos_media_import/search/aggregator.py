from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .base import SearchProvider

logger = logging.getLogger(__name__)


class SearchAggregator:
    """多搜索源聚合器。

    聚合后做强去重与受控排序：
    - 强去重：相同分享 ID、magnet hash、规范化 URL、同来源同标题同大小。
    - 不做影视标题强合并，避免把 1080P / 4K / 防和谐简写资源误合并。
    - 标题相关度优先，线路便利性最多抵消 20 分相关度，不做内容类型过滤。
    """

    def __init__(self, providers: list[SearchProvider] | None = None, aliases: dict[str, list[str]] | None = None):
        self.providers = providers or []
        self.aliases = aliases or {}

    def describe_providers(self) -> list[dict[str, Any]]:
        return [provider.describe() for provider in self.providers]

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        total_started = time.perf_counter()
        runtime_options = options if isinstance(options, dict) else {}
        trace_id = str(runtime_options.get("trace_id") or "-")
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("搜索关键词不能为空")

        enabled = [provider for provider in self.providers if provider.is_enabled()]
        if not enabled:
            raise RuntimeError("没有启用的搜索源")

        query_keywords = self._expanded_keywords(keyword)
        all_items: list[dict[str, Any]] = []
        provider_results: list[dict[str, Any]] = []
        provider_errors: list[dict[str, Any]] = []
        logger.info(
            "search_trace=%s stage=aggregator_start keyword=%r providers=%d expanded_keywords=%d",
            trace_id,
            _short_text(keyword),
            len(enabled),
            len(query_keywords),
        )

        for provider in enabled:
            provider_count = 0
            for query_keyword in query_keywords:
                provider_started = time.perf_counter()
                try:
                    result = provider.search(query_keyword, sources=sources, token=token, options=options)
                    items = [self._normalize_item(item, provider, query_keyword, keyword) for item in result.get("items") or []]
                    all_items.extend(items)
                    provider_count += len(items)
                    logger.info(
                        "search_trace=%s stage=provider_done provider=%s keyword=%r elapsed_ms=%.1f items=%d",
                        trace_id,
                        provider.key,
                        _short_text(query_keyword),
                        _elapsed_ms(provider_started),
                        len(items),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "search_trace=%s stage=provider_error provider=%s keyword=%r elapsed_ms=%.1f",
                        trace_id,
                        provider.key,
                        _short_text(query_keyword),
                        _elapsed_ms(provider_started),
                    )
                    provider_errors.append({"provider": provider.key, "keyword": query_keyword, "message": str(exc)})
                    break
            provider_results.append({"provider": provider.key, "name": provider.name, "count": provider_count})

        if not all_items and provider_errors:
            if len(enabled) == 1 and len(provider_errors) == 1:
                raise RuntimeError(provider_errors[0]["message"])
            messages = "；".join(f"{item['provider']}：{item['message']}" for item in provider_errors[:3])
            raise RuntimeError(f"搜索源暂不可用：{messages}")

        dedupe_started = time.perf_counter()
        deduped = self._dedupe(all_items)
        dedupe_elapsed = _elapsed_ms(dedupe_started)
        sort_started = time.perf_counter()
        scored_items: list[tuple[int, dict[str, Any]]] = []
        for item in deduped:
            relevance_score = _relevance_score(item, keyword)
            ranking_score = _ranking_score(item, relevance_score)
            item["relevance_score"] = relevance_score
            item["ranking_score"] = ranking_score
            scored_items.append((ranking_score, item))
        scored_items.sort(key=lambda pair: pair[0], reverse=True)
        sorted_items = [item for _score, item in scored_items]
        for rank, (score, item) in enumerate(scored_items, start=1):
            item["rank"] = rank
            item["score"] = score
        logger.info(
            "search_trace=%s stage=aggregator_done elapsed_ms=%.1f before_dedupe=%d after_dedupe=%d dedupe_ms=%.1f sort_score_ms=%.1f errors=%d",
            trace_id,
            _elapsed_ms(total_started),
            len(all_items),
            len(sorted_items),
            dedupe_elapsed,
            _elapsed_ms(sort_started),
            len(provider_errors),
        )

        return {
            "items": sorted_items,
            "raw": {
                "providers": provider_results,
                "errors": provider_errors,
                "expanded_keywords": query_keywords,
                "total_before_dedupe": len(all_items),
                "total_after_dedupe": len(sorted_items),
            },
        }

    def _expanded_keywords(self, keyword: str) -> list[str]:
        normalized = _normalize_text(keyword)
        values = [keyword]
        for alias in self.aliases.get(normalized, [])[:5]:
            alias_text = str(alias or "").strip()
            if alias_text and alias_text not in values:
                values.append(alias_text)
        return values

    @staticmethod
    def _normalize_item(item: dict[str, Any], provider: SearchProvider, matched_keyword: str, original_keyword: str) -> dict[str, Any]:
        normalized = dict(item)
        normalized.setdefault("provider", provider.key)
        normalized.setdefault("source", provider.key)
        normalized.setdefault("provider_name", provider.name)
        normalized.setdefault("provider_priority", getattr(provider, "priority", 100))
        normalized.setdefault("matched_keyword", matched_keyword)
        normalized["original_keyword"] = original_keyword
        normalized["dedupe_keys"] = _dedupe_keys(normalized)
        normalized["quality_tags"] = _quality_tags(str(normalized.get("title") or ""))
        return normalized

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        key_to_index: dict[str, int] = {}

        for item in items:
            keys = [key for key in item.get("dedupe_keys") or [] if key]
            existing_index = next((key_to_index[key] for key in keys if key in key_to_index), None)
            if existing_index is None:
                item["duplicate_count"] = 1
                item["duplicate_sources"] = [item.get("provider") or item.get("source") or "unknown"]
                deduped.append(item)
                new_index = len(deduped) - 1
                for key in keys:
                    key_to_index[key] = new_index
                continue

            existing = deduped[existing_index]
            existing["duplicate_count"] = int(existing.get("duplicate_count") or 1) + 1
            duplicate_sources = set(existing.get("duplicate_sources") or [])
            duplicate_sources.add(item.get("provider") or item.get("source") or "unknown")
            existing["duplicate_sources"] = sorted(duplicate_sources)
            if _is_better_duplicate(item, existing):
                merged = dict(item)
                merged["duplicate_count"] = existing["duplicate_count"]
                merged["duplicate_sources"] = existing["duplicate_sources"]
                deduped[existing_index] = merged
                for key in keys:
                    key_to_index[key] = existing_index
        return deduped

    @staticmethod
    def _score_item(item: dict[str, Any], keyword: str) -> int:
        relevance_score = _relevance_score(item, keyword)
        return _ranking_score(item, relevance_score)


def _relevance_score(item: dict[str, Any], keyword: str) -> int:
    title = str(item.get("title") or "").strip()
    if not title:
        return 0

    candidates = [str(keyword or "").strip()]
    matched_keyword = str(item.get("matched_keyword") or "").strip()
    if matched_keyword and _normalize_text(matched_keyword) != _normalize_text(keyword):
        candidates.append(matched_keyword)

    scores = []
    for index, candidate in enumerate(candidates):
        if not candidate:
            continue
        score = _query_title_relevance(candidate, title)
        if index > 0:
            score = max(0, score - 3)
        scores.append(score)
    return max(scores, default=5)


def _query_title_relevance(keyword: str, title: str) -> int:
    normalized_keyword = _normalize_text(keyword)
    normalized_title = _normalize_text(title)
    if not normalized_keyword or not normalized_title:
        return 0
    if normalized_title == normalized_keyword:
        return 100

    position = normalized_title.find(normalized_keyword)
    if position >= 0:
        density = len(normalized_keyword) / max(1, len(normalized_title))
        position_ratio = position / max(1, len(normalized_title) - len(normalized_keyword))
        prefix_bonus = 15 if position == 0 else max(0, round(10 * (1 - position_ratio)))
        density_bonus = min(19, round(30 * density))
        score = min(99, 65 + prefix_bonus + density_bonus)

        # A complete, reasonably specific Chinese title remains strong evidence
        # even when a release name adds a site prefix and lengthy media tags.
        cjk_length = len(re.findall(r"[\u4e00-\u9fff]", normalized_keyword))
        if cjk_length >= 4:
            long_title_floor = min(96, 90 + (cjk_length - 4) * 2)
            score = max(score, long_title_floor)
        return score

    # Token matching must use the original keyword. ``_normalize_text`` removes
    # spaces and punctuation, so normalized input would turn an English query
    # into one long token and lose useful partial matches.
    token_score = _keyword_token_hit_score(keyword, title)
    if token_score:
        return min(79, max(10, round(token_score * 0.9)))
    return 5


def _ranking_score(item: dict[str, Any], relevance_score: int) -> int:
    relevance = max(0, min(100, int(relevance_score)))
    route_tier = _route_tier(item)
    route_bonus = {3: 20, 2: 10}.get(route_tier, 0)
    adjusted_relevance = relevance + route_bonus

    quality = item.get("quality_tags") or _quality_tags(str(item.get("title") or ""))
    quality_score = 0
    if "8k" in quality:
        quality_score += 28
    elif "4k" in quality:
        quality_score += 22
    elif "1080p" in quality:
        quality_score += 14
    elif "720p" in quality:
        quality_score += 6
    if "hdr" in quality:
        quality_score += 8

    provider_bonus = max(0, 20 - int(item.get("provider_priority") or 100))
    duplicate_bonus = min(20, (int(item.get("duplicate_count") or 1) - 1) * 5)
    metadata_bonus = int(bool(item.get("size") or item.get("size_text"))) * 4 + int(bool(item.get("datetime"))) * 2
    tie_breaker = min(99, quality_score + provider_bonus + duplicate_bonus + metadata_bonus)

    # The leading component is the only cross-result score. Route convenience
    # can offset at most 20 relevance points; quality and provider metadata only
    # break ties after adjusted relevance, support and route tier.
    return (
        adjusted_relevance * 1_000_000
        + int(bool(item.get("supported"))) * 100_000
        + route_tier * 10_000
        + relevance * 100
        + tie_breaker
    )


def _route_tier(item: dict[str, Any]) -> int:
    if _is_cloud139_item(item):
        return 3
    if _is_sixpan_fast_item(item):
        return 2
    return 1


def _is_better_duplicate(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    if _is_cloud139_item(candidate) != _is_cloud139_item(existing):
        return _is_cloud139_item(candidate)
    if bool(candidate.get("supported")) != bool(existing.get("supported")):
        return bool(candidate.get("supported"))
    candidate_priority = int(candidate.get("provider_priority") or 100)
    existing_priority = int(existing.get("provider_priority") or 100)
    if candidate_priority != existing_priority:
        return candidate_priority < existing_priority
    candidate_has_size = bool(candidate.get("size") or candidate.get("size_text"))
    existing_has_size = bool(existing.get("size") or existing.get("size_text"))
    if candidate_has_size != existing_has_size:
        return candidate_has_size
    return len(str(candidate.get("title") or "")) > len(str(existing.get("title") or ""))


def _is_cloud139_item(item: dict[str, Any]) -> bool:
    source_type = str(item.get("source_type") or "").strip().lower()
    if source_type == "cloud139":
        return True
    url = str(item.get("url") or item.get("source_url") or "").strip().lower()
    source_hint = str(item.get("source_hint") or item.get("source") or "").strip().lower()
    return "yun.139.com" in url or "caiyun.139.com" in url or "mobile" in source_hint or "移动" in source_hint


def _is_sixpan_fast_item(item: dict[str, Any]) -> bool:
    source_type = str(item.get("source_type") or "").strip().lower()
    if source_type in {"magnet", "torrent", "bt"}:
        return True
    url = str(item.get("url") or item.get("source_url") or "").strip().lower()
    source_hint = str(item.get("source_hint") or item.get("source") or item.get("provider") or "").strip().lower()
    route = str(item.get("route") or "").strip().lower()
    return (
        url.startswith("magnet:")
        or ".torrent" in url
        or "magnet" in source_hint
        or "torrent" in source_hint
        or "bt" in source_hint
        or "磁链" in source_hint
        or "种子" in source_hint
        or route == "sixpan_offline"
    )


def _dedupe_keys(item: dict[str, Any]) -> list[str]:
    url = str(item.get("url") or item.get("source_url") or "").strip()
    source_type = str(item.get("source_type") or "").strip().lower()
    source = str(item.get("source") or item.get("provider") or "").strip().lower()
    title = _normalize_text(str(item.get("title") or ""))
    size = _normalize_size(str(item.get("size_text") or item.get("size") or ""))
    keys: list[str] = []

    share_key = _share_key(url, source_type)
    if share_key:
        keys.append(share_key)
    normalized_url = _normalize_url(url)
    if normalized_url:
        keys.append(f"url:{normalized_url}")
    if source and title and size:
        keys.append(f"source-title-size:{source}:{title}:{size}")
    return keys


def _share_key(url: str, source_type: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("magnet:"):
        match = re.search(r"btih:([a-z0-9]{32,40})", lower)
        return f"magnet:{match.group(1)}" if match else f"magnet:{lower}"

    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)

    if "pan.quark.cn" in host and "/s/" in f"/{path}":
        share_id = f"/{path}".split("/s/", 1)[1].split("/", 1)[0]
        return f"quark:{share_id}"
    if "drive.uc.cn" in host and "/s/" in f"/{path}":
        share_id = f"/{path}".split("/s/", 1)[1].split("/", 1)[0]
        return f"uc:{share_id}"
    if "cloud.189.cn" in host:
        code = query.get("code", [""])[0] or path.rsplit("/", 1)[-1]
        return f"cloud189:{code}" if code else ""
    if "yun.139.com" in host or "caiyun.139.com" in host:
        code = query.get("linkID", [""])[0] or query.get("linkId", [""])[0] or path.rsplit("/", 1)[-1]
        return f"cloud139:{code}" if code else ""
    if source_type and path:
        return f"{source_type}:{host}:{path}"
    return ""


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query = parsed.query
    scheme = parsed.scheme.lower() or "https"
    if scheme == "http":
        scheme = "https"
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _short_text(value: Any, limit: int = 40) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _normalize_size(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _quality_tags(title: str) -> list[str]:
    text = str(title or "").lower()
    tags: list[str] = []
    if "8k" in text or "4320" in text:
        tags.append("8k")
    if "4k" in text or "2160" in text or "uhd" in text:
        tags.append("4k")
    if "hdr" in text or "dolby vision" in text or "杜比视界" in text:
        tags.append("hdr")
    if "1080" in text:
        tags.append("1080p")
    if "720" in text:
        tags.append("720p")
    if "remux" in text or "原盘" in text:
        tags.append("remux")
    return tags


def _keyword_token_hit_score(keyword: str, title: str) -> int:
    if not keyword or not title:
        return 0
    normalized_title = _normalize_text(title)
    tokens = [
        _normalize_text(token)
        for token in re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", str(keyword or "").lower())
    ]
    tokens = list(dict.fromkeys(token for token in tokens if len(token) >= 2))
    if not tokens:
        return 0
    matched_weight = sum(len(token) for token in tokens if token in normalized_title)
    total_weight = sum(len(token) for token in tokens)
    return int(80 * matched_weight / total_weight) if total_weight else 0
