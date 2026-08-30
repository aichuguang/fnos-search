from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .base import SearchProvider

logger = logging.getLogger(__name__)


class SearchProvidersUnavailableError(RuntimeError):
    """All enabled search providers failed before returning any result."""


class SearchAggregator:
    """多搜索源聚合器。

    聚合后做强去重与受控排序：
    - 强去重：相同分享 ID、magnet hash、规范化 URL、同来源同标题同大小。
    - 不做影视标题强合并，避免把 1080P / 4K / 防和谐简写资源误合并。
    - 标题相关度优先；来源可用性只用于过滤和同相关度候选排序。
    - 不推测传输策略或目标，二者只在创建任务后由冻结执行计划确定。
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
            raise SearchProvidersUnavailableError("没有启用的搜索源")

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
                raise SearchProvidersUnavailableError(provider_errors[0]["message"])
            messages = "；".join(f"{item['provider']}：{item['message']}" for item in provider_errors[:3])
            raise SearchProvidersUnavailableError(f"搜索源暂不可用：{messages}")

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
        raw_title = str(normalized.get("title") or "")
        normalized["quality_tags"] = _quality_tags(_clean_title(raw_title) or raw_title)
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
    if not normalized_keyword or not str(title or "").strip():
        return 0
    variants = _title_variants(title)
    # A fully bracketed release name keeps its real title inside one of the
    # blocks ("资源名称：[流浪地球2][意大利版原盘…]"); promote the block that
    # actually contains the keyword.
    for match in _NOISY_BRACKET_RE.finditer(str(title)):
        inner = (match.group(1) or "").strip()
        if inner and inner not in variants and normalized_keyword in _normalize_text(inner):
            variants.append(inner)
    best = 0
    for variant in variants:
        normalized_title = _normalize_text(variant)
        if not normalized_title:
            continue
        score = _pair_relevance(normalized_keyword, keyword, normalized_title, variant)
        # 合集垃圾（微信引流、几十部剧打包）靠关键词子串混进高档位；
        # 封顶到 59 让它掉出"相关"档。广告壳资源不受影响：信号在壳上，
        # 剥壳后的干净变体照常拿高分。
        if _is_junk_collection_title(variant):
            score = min(score, 59)
        best = max(best, score)
    return best if best > 0 else 5


def _pair_relevance(normalized_keyword: str, raw_keyword: str, normalized_title: str, raw_title: str) -> int:
    if normalized_title == normalized_keyword:
        return 100

    position = normalized_title.find(normalized_keyword)
    if position >= 0:
        density = len(normalized_keyword) / max(1, len(normalized_title))
        position_ratio = position / max(1, len(normalized_title) - len(normalized_keyword))
        prefix_bonus = 15 if position == 0 else max(0, round(10 * (1 - position_ratio)))
        density_bonus = min(19, round(30 * density))
        score = min(99, 70 + prefix_bonus + density_bonus)

        # A complete, reasonably specific Chinese title remains strong evidence
        # even when a release name adds a site prefix and lengthy media tags.
        cjk_length = len(re.findall(r"[\u4e00-\u9fff]", normalized_keyword))
        if cjk_length >= 4:
            long_title_floor = min(96, 90 + (cjk_length - 4) * 2)
            score = max(score, long_title_floor)
        return score

    # The title being the clean core of a longer query (e.g. title "\u4e09\u4f53" for
    # keyword "\u4e09\u4f53 \u5218\u6148\u6b23") is strong evidence, but sequel numbers must agree \u2014
    # "\u6c99\u4e18" must not match a "\u6c99\u4e182" query just because it is a prefix.
    if (
        len(normalized_title) >= 2
        and normalized_keyword.find(normalized_title) >= 0
        and bool(_DIGIT_RE.search(normalized_title)) == bool(_DIGIT_RE.search(normalized_keyword))
    ):
        return 85

    # Token matching must use the original keyword. ``_normalize_text`` removes
    # spaces and punctuation, so normalized input would turn an English query
    # into one long token and lose useful partial matches.
    token_score = _keyword_token_hit_score(raw_keyword, raw_title)
    if token_score:
        return min(88, max(10, round(token_score * 1.1)))
    return 0


def _ranking_score(item: dict[str, Any], relevance_score: int) -> int:
    relevance = max(0, min(100, int(relevance_score)))
    try:
        preference_bonus = max(
            0,
            min(10, int(item.get("result_provider_priority_bonus") or 0)),
        )
    except (TypeError, ValueError):
        preference_bonus = 0

    # 网盘资源标题噪声极大（广告壳、译名差异、发行命名随意），精细的相关度
    # 分差并不可靠。相关度只压缩成粗档位，用来把特别低相关的结果挡在后面；
    # 档位之内由来源优先级决定先后，同优先级再按原始相关度和质量细分。
    if relevance >= 60:
        relevance_bucket = 2
    elif relevance >= 25:
        relevance_bucket = 1
    else:
        relevance_bucket = 0

    quality = item.get("quality_tags") or _quality_tags(str(item.get("title") or ""))
    quality_score = 0
    if "8k" in quality:
        quality_score += 22
    elif "4k" in quality:
        quality_score += 16
    elif "1080p" in quality:
        quality_score += 10
    elif "720p" in quality:
        quality_score += 5
    if "hdr" in quality:
        quality_score += 6

    provider_bonus = max(0, 20 - int(item.get("provider_priority") or 100))
    duplicate_bonus = min(20, (int(item.get("duplicate_count") or 1) - 1) * 5)
    metadata_bonus = int(bool(item.get("size") or item.get("size_text"))) * 4 + int(bool(item.get("datetime"))) * 2
    tie_breaker = min(99, quality_score + provider_bonus + duplicate_bonus + metadata_bonus)

    # Source availability is a filter/tie-breaker only. Transfer strategy and
    # destination are unknown until the server freezes an execution plan, so
    # search ranking must never infer route convenience.
    return (
        relevance_bucket * 1_000_000_000
        + preference_bonus * 10_000_000
        + int(bool(item.get("supported"))) * 1_000_000
        + relevance * 1_000
        + tie_breaker
    )


def _is_better_duplicate(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
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


_FULLWIDTH_FOLD = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_FOLD[0x3000] = 0x20
_UNICODE_ROMAN_FOLD = {
    ord("\u2170"): "1", ord("\u2171"): "2", ord("\u2172"): "3", ord("\u2173"): "4", ord("\u2174"): "5", ord("\u2175"): "6",
    ord("\u2176"): "7", ord("\u2177"): "8", ord("\u2178"): "9", ord("\u2179"): "10", ord("\u217a"): "11", ord("\u217b"): "12",
}
_CN_DIGIT_FOLD = {
    "\u4e00": "1", "\u4e8c": "2", "\u4e24": "2", "\u4e09": "3", "\u56db": "4", "\u4e94": "5",
    "\u516d": "6", "\u4e03": "7", "\u516b": "8", "\u4e5d": "9", "\u5341": "10",
}
_SEASON_CN_RE = re.compile(r"\u7b2c([0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})[\u5b63\u90e8\u96c6]")
_ROMAN_AFTER_CJK_RE = re.compile(r"(?<=[\u4e00-\u9fff])(?:viii|vii|xii|vi|iii|ii|iv|ix|xi)(?![a-z])")
_ROMAN_VALUE = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "xi": "11", "xii": "12",
}
_DIGIT_RE = re.compile(r"[0-9]")


def _season_repl(match: re.Match) -> str:
    raw = match.group(1)
    if raw.isdigit():
        return raw
    if len(raw) == 1 and raw in _CN_DIGIT_FOLD:
        return _CN_DIGIT_FOLD[raw]
    return match.group(0)


def _roman_repl(match: re.Match) -> str:
    return _ROMAN_VALUE[match.group(0)]


def _normalize_text(value: str) -> str:
    text = str(value or "").lower().translate(_FULLWIDTH_FOLD).translate(_UNICODE_ROMAN_FOLD)
    text = _SEASON_CN_RE.sub(_season_repl, text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return _ROMAN_AFTER_CJK_RE.sub(_roman_repl, text)


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
    if not normalized_title:
        return 0
    # CJK runs are scored by 2-gram coverage instead of whole-block equality,
    # so translation variants like "\u6c99\u4e18\uff1a\u7b2c\u4e8c\u90e8" vs "\u6c99\u4e182" keep partial credit.
    weights: dict[str, int] = {}
    for part in re.findall(r"[0-9a-z]+|[\u4e00-\u9fff]+", str(keyword).lower()):
        if len(part) >= 2 and part.isascii():
            weights[part] = len(part)
        elif len(part) >= 2:
            for gram in {part[i : i + 2] for i in range(len(part) - 1)}:
                weights[gram] = 2
        elif part.isdigit():
            weights[part] = 1
    if not weights:
        return 0
    matched_weight = sum(weight for token, weight in weights.items() if token in normalized_title)
    total_weight = sum(weights.values())
    return int(80 * matched_weight / total_weight) if total_weight else 0


_URL_RE = re.compile(
    r"(?:https?://|www\.)[\w\-./?=&%#:~+]+|\b[\w-]+\.(?:com|net|org|cc|xyz|me|top|club|vip|icu|info|site|online)(?:/[\w\-./?=&%#:~+]*)?",
    re.IGNORECASE,
)
_NOISY_BRACKET_RE = re.compile(r"[\u3010\[]([^\u3010\[\]\u3011]*)[\u3011\]]")
_BRACKET_NOISE_HINT_RE = re.compile(
    r"(?:www\.|https?://|\.com|\.net|\.cc|\.xyz|\u53d1\u5e03|\u5e7f\u544a|\u5b98\u7f51|\u57df\u540d|\u9632\u8d70\u4e22|\u6c38\u4e45|\u9ad8\u6e05\u5f71\u89c6|\u5f71\u89c6\u4e4b\u5bb6|\u8d44\u6e90\u7ad9)",
    re.IGNORECASE,
)
_PREFIX_LABEL_RE = re.compile(r"^\s*\u8d44\u6e90\u540d\u79f0\s*[\uff1a:]\s*")

# \u5f3a\u5783\u573e\u4fe1\u53f7\uff1a\u5fae\u4fe1\u5f15\u6d41\u8bdd\u672f\uff1b\u4e09\u4e2a\u4ee5\u4e0a"\uff08N\u96c6\uff09"\u6bb5\u843d\u8bf4\u660e\u662f\u591a\u5267\u5408\u96c6\u6253\u5305\u3002
_JUNK_PROMO_RE = re.compile(r"(?:\u6dfb\u52a0\u5fae\u4fe1|\u5fae\u4fe1\u53f7|\u52a0\u5fae\u4fe1|\u673a\u5668\u4eba\u7fa4)")
_EPISODE_COUNT_RE = re.compile(r"[\uff08(]\s*\d{1,4}\s*[\u96c6\u8bdd\u56de]\s*[\uff09)]")


def _is_junk_collection_title(variant: str) -> bool:
    text = str(variant or "")
    if _JUNK_PROMO_RE.search(text):
        return True
    return len(_EPISODE_COUNT_RE.findall(text)) >= 3


def _clean_title(title: str) -> str:
    """Strip ad shells (site prefix blocks, domains, "\u8d44\u6e90\u540d\u79f0\uff1a" labels) from a release name."""
    text = _PREFIX_LABEL_RE.sub("", str(title or "").strip())
    text = _URL_RE.sub(" ", text)

    def _drop_noisy(match: re.Match) -> str:
        inner = match.group(1) or ""
        return " " if _BRACKET_NOISE_HINT_RE.search(inner) else match.group(0)

    text = _NOISY_BRACKET_RE.sub(_drop_noisy, text)
    return re.sub(r"\s+", " ", text).strip(" \t-_|\u00b7\u2022\uff0c,")


def _strip_title(title: str) -> str:
    """Aggressively drop every bracketed block and URL, leaving the bare core title."""
    text = _PREFIX_LABEL_RE.sub("", str(title or "").strip())
    text = _URL_RE.sub(" ", text)
    text = _NOISY_BRACKET_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" \t-_|·•，,")


def _title_variants(title: str) -> list[str]:
    text = str(title or "").strip()
    if not text:
        return []
    variants = [text]
    for candidate in (_clean_title(text), _strip_title(text)):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants
