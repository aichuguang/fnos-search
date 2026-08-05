"""Lightweight hot-content source adapters.

The adapters intentionally only normalize public responses.  Fetching is kept
small and dependency-free so a source failure can be isolated by the caller.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

import requests

from ..http_client import HttpClient


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class HotItem:
    source: str
    source_id: str
    title: str
    media_type: str = "unknown"
    rank: int | None = None
    heat: float | None = None
    score: float | None = None
    year: int | None = None
    update_text: str = ""
    is_completed: bool | None = None
    image_url: str = ""
    actors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _text(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _flatten_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_flatten_text(child) for child in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(child) for child in value)
    return _text(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if "\u4e07" in text:
        number *= 10_000
    elif "\u4ebf" in text:
        number *= 100_000_000
    return number


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _year(mapping: Mapping[str, Any]) -> int | None:
    value = _first(mapping, "year", "releaseYear", "pubYear", "publishYear", "date", "releaseDate")
    text = _flatten_text(value) or _flatten_text(mapping)
    match = re.search(r"(?:19|20)\d{2}", text)
    return int(match.group(0)) if match else None


def _infer_type(mapping: Mapping[str, Any], text: str = "") -> str:
    value = " ".join(_flatten_text(_first(mapping, key)) for key in ("mediaType", "media_type", "type", "category", "subType", "contentType", "tags", "lines"))
    value = f"{value} {text}".lower()
    if any(token in value for token in ("\u7efc\u827a", "variety", "show")):
        return "variety"
    if any(token in value for token in ("\u52a8\u6f2b", "\u52a8\u753b", "\u5c11\u513f", "anime", "cartoon")):
        return "anime"
    if any(token in value for token in ("\u7535\u5f71", "movie", "film")):
        return "movie"
    if any(token in value for token in ("\u7535\u89c6\u5267", "\u7f51\u7edc\u5267", "\u5267\u96c6", "tv", "series", "drama")):
        return "tv"
    return "unknown"


def _list_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，|/、]", value) if part.strip()]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                item = _first(item, "name", "title", "content", "text")
            text = _text(item)
            if text:
                result.append(text)
        return result
    return []


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, Mapping):
        yield dict(value)
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _candidate(mapping: Mapping[str, Any], *, source: str, marker: str = "") -> HotItem | None:
    title = _text(_first(mapping, "title", "name", "word", "keyword", "videoName", "seriesName", "showName", "programName"))
    if not title or len(title) > 300:
        return None
    source_id = _text(_first(mapping, "source_id", "sourceId", "id", "videoId", "seriesId", "albumId", "qipuId", "docId", "cid", "contentId"))
    rank = _integer(_first(mapping, "rank", "rankNum", "rankNo", "ranking", "position", "index", "order", "top"))
    heat_value = _first(mapping, "heat", "hot", "hotScore", "currHeat", "scoreValue", "playCount", "viewCount")
    tag_rows = mapping.get("tags") if isinstance(mapping.get("tags"), list) else []
    if heat_value is None:
        heat_tag = next(
            (
                item.get("content")
                for item in tag_rows
                if isinstance(item, Mapping) and int(item.get("type") or 0) == 40
            ),
            None,
        )
        heat_value = heat_tag
    tag_texts = _list_text(_first(mapping, "tags", "tag", "labels"))
    if heat_value is None:
        heat_value = next((text for text in tag_texts if any(word in text for word in ("\u70ed\u5ea6", "hot"))), "")
    heat = _number(heat_value)
    score_value = _first(mapping, "rating", "score", "doubanScore")
    if score_value is None:
        score_value = next(
            (
                item.get("content")
                for item in tag_rows
                if isinstance(item, Mapping) and int(item.get("type") or 0) == 30
            ),
            None,
        )
    score = _number(score_value)
    if score is None:
        score = _number(next((text for text in tag_texts if any(word in text for word in ("\u8bc4\u5206", "rating"))), ""))
    update = _flatten_text(_first(mapping, "updateText", "update_text", "updateInfo", "releaseInfo", "specialTag", "subtitle", "desc", "description", "lbTexts"))
    if not rank:
        top = re.search(r"(?:top|榜\s*第?)\s*[-#：:]?\s*(\d+)", f"{marker} {update}", re.I)
        rank = int(top.group(1)) if top else None
    completed: bool | None = None
    if update:
        completed = bool(re.search(r"(?:全|完结|已完结|completed|finished)", update, re.I))
    image = _text(_first(mapping, "image_url", "imageUrl", "imgUrl", "image", "img", "cover", "coverUrl", "poster", "pic"))
    actors = _list_text(_first(mapping, "actors", "actor", "cast", "stars"))
    tags = _list_text(_first(mapping, "tags", "tag", "genres", "category"))
    return HotItem(
        source=source,
        source_id=source_id or title,
        title=title,
        media_type=_infer_type(mapping, marker),
        rank=rank,
        heat=heat,
        score=score,
        year=_year(mapping),
        update_text=update,
        is_completed=completed,
        image_url=image,
        actors=actors,
        tags=tags,
        raw_data=dict(mapping),
    )


def _dedupe(items: Iterable[HotItem]) -> list[HotItem]:
    seen: set[tuple[str, str]] = set()
    result: list[HotItem] = []
    for item in items:
        key = (item.source_id, item.title)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _limited(items: Iterable[HotItem], limit: int | None) -> list[HotItem]:
    values = _dedupe(items)
    values.sort(key=lambda item: (item.rank if item.rank and item.rank > 0 else float("inf"), item.title))
    if limit is None:
        return values
    return values[: max(1, int(limit))]


_TARGET_MEDIA_ORDER = ("tv", "movie", "variety", "anime")
_TARGET_MEDIA_LABELS = {
    "tv": "\u7535\u89c6\u5267",
    "movie": "\u7535\u5f71",
    "variety": "\u7efc\u827a",
    "anime": "\u52a8\u6f2b",
}


def _target_media_type(mapping: Mapping[str, Any], marker: str = "") -> str:
    classification = " ".join(
        _flatten_text(_first(mapping, key))
        for key in ("mediaType", "media_type", "type", "category", "subType", "contentType", "lines")
    ).lower()
    if "\u5c11\u513f" in f"{classification} {marker}".lower():
        return ""
    media_type = _infer_type(mapping, marker)
    return media_type if media_type in _TARGET_MEDIA_ORDER else ""


def _limited_by_category(
    items: Iterable[HotItem],
    *,
    per_category_limit: int = 20,
    total_limit: int | None = None,
) -> list[HotItem]:
    """Cap each supported media category before applying the source total cap."""

    per_category = max(1, int(per_category_limit or 20))
    buckets: dict[str, list[HotItem]] = {media_type: [] for media_type in _TARGET_MEDIA_ORDER}
    for item in _dedupe(items):
        if item.media_type in buckets:
            buckets[item.media_type].append(item)

    values: list[HotItem] = []
    for media_type in _TARGET_MEDIA_ORDER:
        rows = buckets[media_type]
        rows.sort(key=lambda item: (item.rank if item.rank and item.rank > 0 else float("inf"), item.title))
        values.extend(rows[:per_category])
    category_order = {media_type: index for index, media_type in enumerate(_TARGET_MEDIA_ORDER)}
    values.sort(
        key=lambda item: (
            item.rank if item.rank and item.rank > 0 else float("inf"),
            category_order.get(item.media_type, len(category_order)),
            item.title,
        )
    )
    if total_limit is not None:
        return values[: max(1, int(total_limit))]
    return values


def _rank_item_list(mapping: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = mapping.get("hotRankResult") or mapping.get("hot_rank_result")
    if not isinstance(result, Mapping):
        return []
    rows = result.get("rankItemList") or result.get("rank_item_list")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _tencent_rank_rows(payload: Any) -> list[dict[str, Any]]:
    """Read target category ranks from official nav tabs with overall-list fallback."""

    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    nav_items = data.get("navItemList") or data.get("nav_item_list")
    if not isinstance(nav_items, list):
        nav_items = []

    overall_rows: list[dict[str, Any]] = []
    category_rows: dict[str, list[dict[str, Any]]] = {}
    for nav in nav_items:
        if not isinstance(nav, Mapping):
            continue
        rows = _rank_item_list(nav)
        tab_id = str(nav.get("tabId") or nav.get("tab_id") or "")
        tab_name = _text(_first(nav, "tabName", "tab_name", "title", "name"))
        if tab_id == "0" or tab_name == "\u70ed\u641c":
            overall_rows = rows
            continue
        media_type = _target_media_type(nav, tab_name)
        if media_type and rows:
            category_rows[media_type] = rows

    if overall_rows or category_rows:
        normalized_rows: list[dict[str, Any]] = []
        for media_type in _TARGET_MEDIA_ORDER:
            rows = category_rows.get(media_type)
            if not rows:
                rows = [
                    row
                    for row in overall_rows
                    if _target_media_type(row, _flatten_text(row)) == media_type
                ]
            for rank, row in enumerate(rows, 1):
                normalized = dict(row)
                normalized.setdefault("overallRank", _integer(_first(row, "rank", "rankNum", "rankNo")))
                normalized["rank"] = rank
                normalized["mediaType"] = _TARGET_MEDIA_LABELS[media_type]
                normalized_rows.append(normalized)
        return normalized_rows

    # Compatibility with a controlled direct hotRankResult response shape.
    result = data.get("hotRankResult") or data.get("hot_rank_result")
    rows = result.get("rankItemList") if isinstance(result, Mapping) else None
    normalized_rows = []
    category_rank = {media_type: 0 for media_type in _TARGET_MEDIA_ORDER}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        media_type = _target_media_type(row, _flatten_text(row))
        if not media_type:
            continue
        category_rank[media_type] += 1
        normalized = dict(row)
        normalized["rank"] = category_rank[media_type]
        normalized["mediaType"] = _TARGET_MEDIA_LABELS[media_type]
        normalized_rows.append(normalized)
    return normalized_rows


class TencentHotSource:
    name = "tencent"
    endpoint = "https://pbaccess.video.qq.com/trpc.videosearch.hot_rank.HotRankServantHttp/HotRankHttp"

    def __init__(self, config: Mapping[str, Any] | None = None, http_client: HttpClient | None = None):
        config = config or {}
        self.endpoint = str(config.get("endpoint") or self.endpoint)
        self.data_version = str(config.get("data_version") or "25081802")
        # Tencent currently populates the TV tab directly while movie,
        # variety and anime are recovered from the overall hot list. Fetch
        # the complete overall list before applying the per-category cap.
        self.page_size = max(1, int(config.get("page_size") or 200))
        self.max_items = max(1, int(config.get("max_items") or self.page_size))
        self.max_items_per_category = max(1, int(config.get("max_items_per_category") or self.max_items))
        timeout = max(3, int(config.get("timeout") or 20))
        self.http = http_client or HttpClient(timeout=timeout, trust_env=False)
        if getattr(self.http, "session", None) is not None:
            self.http.session.trust_env = False

    @staticmethod
    def parse(payload: Any, limit: int | None = None, per_category_limit: int | None = None) -> list[HotItem]:
        rows = _tencent_rank_rows(payload)
        items: list[HotItem] = []
        for index, mapping in enumerate(rows, 1):
            normalized = dict(mapping)
            normalized.setdefault("rank", index)
            item = _candidate(
                normalized,
                source="tencent",
                marker=_flatten_text(normalized),
            )
            if item:
                items.append(item)
        category_limit = per_category_limit if per_category_limit is not None else limit if limit is not None else 20
        return _limited_by_category(items, per_category_limit=category_limit)

    def fetch(self) -> list[HotItem]:
        payload = {"pageNum": 0, "pageSize": self.page_size, "data_version": self.data_version, "client_type": 2}
        status, data = self.http.post_json(self.endpoint, payload)
        if status >= 400:
            raise RuntimeError(f"Tencent hot list HTTP {status}")
        return self.parse(data, limit=self.max_items, per_category_limit=self.max_items_per_category)


class IqiyiHotSource:
    name = "iqiyi"
    endpoint = "https://mesh.if.iqiyi.com/portal/lw/search/keywords/hotList"

    def __init__(self, config: Mapping[str, Any] | None = None, http_client: HttpClient | None = None):
        config = config or {}
        self.endpoint = str(config.get("endpoint") or self.endpoint)
        self.device_id = str(config.get("device_id") or "")
        self.version = str(config.get("version") or config.get("v") or "17.072.25808")
        timeout = max(3, int(config.get("timeout") or 20))
        self.max_items = max(1, int(config.get("max_items") or 20))
        self.max_items_per_category = max(1, int(config.get("max_items_per_category") or self.max_items))
        self.http = http_client or HttpClient(timeout=timeout, trust_env=False)
        if getattr(self.http, "session", None) is not None:
            self.http.session.trust_env = False

    @staticmethod
    def parse(payload: Any, limit: int | None = None) -> list[HotItem]:
        if not isinstance(payload, Mapping):
            return []
        groups = payload.get("hotQuery")
        if not isinstance(groups, list):
            data = payload.get("data")
            groups = data.get("hotQuery") if isinstance(data, Mapping) else None

        items: list[HotItem] = []
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, Mapping) or not isinstance(group.get("items"), list):
                    continue
                group_title = _text(group.get("title"))
                media_type = next(
                    (
                        canonical
                        for canonical, label in _TARGET_MEDIA_LABELS.items()
                        if group_title == label
                    ),
                    "",
                )
                if not media_type:
                    continue
                for rank, mapping in enumerate(group.get("items") or [], 1):
                    if not isinstance(mapping, Mapping):
                        continue
                    normalized = dict(mapping)
                    normalized["rank"] = rank
                    normalized["mediaType"] = _TARGET_MEDIA_LABELS[media_type]
                    item = _candidate(
                        normalized,
                        source="iqiyi",
                        marker=f"{group_title} {_flatten_text(normalized)}",
                    )
                    if item:
                        # The official group is authoritative. Individual rows
                        # can describe an animated movie/variety in their tags;
                        # those tags must not move the row into another board.
                        item.media_type = media_type
                        items.append(item)
        else:
            # Compatibility with the small fixture/legacy direct hot-list shape.
            data = payload.get("data")
            direct = data.get("hotList") if isinstance(data, Mapping) else payload.get("hotList")
            category_rank = {media_type: 0 for media_type in _TARGET_MEDIA_ORDER}
            for mapping in direct if isinstance(direct, list) else []:
                if not isinstance(mapping, Mapping):
                    continue
                media_type = _target_media_type(mapping, _flatten_text(mapping))
                if not media_type:
                    continue
                category_rank[media_type] += 1
                normalized = dict(mapping)
                normalized.setdefault("rank", category_rank[media_type])
                normalized["mediaType"] = _TARGET_MEDIA_LABELS[media_type]
                item = _candidate(normalized, source="iqiyi", marker=_flatten_text(normalized))
                if item:
                    items.append(item)
        return _limited_by_category(items, per_category_limit=limit if limit is not None else 20)

    def fetch(self) -> list[HotItem]:
        params = {"device_id": self.device_id, "v": self.version, "appMode": "", "src": ""}
        status, data = self.http.get_json(
            self.endpoint,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if status >= 400:
            raise RuntimeError(f"Iqiyi hot list HTTP {status}")
        return self.parse(data, limit=self.max_items_per_category)


def _extract_js_object(html: str, variable: str = "window.__INITIAL_DATA__") -> Any:
    match = re.search(re.escape(variable) + r"\s*=\s*", html)
    if not match:
        return None
    start = html.find("{", match.end())
    if start < 0:
        return None
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                text = html[start : index + 1]
                try:
                    return json.loads(text)
                except Exception:
                    try:
                        normalized = re.sub(r"\bundefined\b", "null", text)
                        try:
                            return json.loads(normalized)
                        except Exception:
                            normalized = re.sub(r"\btrue\b", "True", normalized, flags=re.I)
                            normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.I)
                            normalized = re.sub(r"\bnull\b", "None", normalized)
                            return ast.literal_eval(normalized)
                    except Exception:
                        return None
    return None


def _html_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


_YOUKU_TARGET_RANK = re.compile(
    r"(\u7535\u89c6\u5267|\u7535\u5f71|\u52a8\u6f2b|\u7efc\u827a)\u70ed\u5ea6\u699c\s*[\u00b7\u2022.:-]?\s*TOP\s*(\d+)",
    re.I,
)
_YOUKU_CATEGORY_RANK = re.compile(r"(\u7535\u89c6\u5267|\u7535\u5f71|\u52a8\u6f2b|\u7efc\u827a)\u70ed\u5ea6\u699c", re.I)
_YOUKU_TOP_RANK = re.compile(r"(?:TOP|\u7b2c)\s*[-#\uff1a:]?\s*(\d+)", re.I)


def _youku_item_rank_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _text(item.get("playPercentLabel")),
            _text(item.get("subtitle")),
            _flatten_text(item.get("reason")),
            _flatten_text(item.get("topLeftMark")),
            _flatten_text(item.get("lbTexts")),
        )
        if part
    )


def _youku_component_rank_text(component: Mapping[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _text(component.get("title")),
            _text(component.get("subTitle")),
            _text(component.get("typeName")),
            _flatten_text(component.get("reason")),
        )
        if part
    )


def _youku_rank_rows(data: Any) -> list[dict[str, Any]]:
    """Read ranked cards only from the live module/components/itemList path."""

    if not isinstance(data, Mapping):
        return []
    modules = data.get("moduleList")
    if not isinstance(modules, list):
        return []
    rows: list[dict[str, Any]] = []
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        components = module.get("components")
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, Mapping):
                continue
            items = component.get("itemList")
            if not isinstance(items, list):
                continue
            component_marker = _youku_component_rank_text(component)
            component_match = _YOUKU_CATEGORY_RANK.search(component_marker)
            for index, item in enumerate(items, 1):
                if not isinstance(item, Mapping):
                    continue
                label = _youku_item_rank_text(item)
                match = _YOUKU_TARGET_RANK.search(label)
                category_label = match.group(1) if match else component_match.group(1) if component_match else ""
                if not category_label:
                    continue
                rank_match = match or _YOUKU_TOP_RANK.search(label)
                rank = int(rank_match.group(2) if match else rank_match.group(1)) if rank_match else index
                normalized = dict(item)
                normalized["rank"] = rank
                normalized["mediaType"] = category_label
                normalized["rankLabel"] = label or component_marker
                rows.append(normalized)
    return rows


def _parse_youku_html_cards(html: str) -> list[HotItem]:
    items: list[HotItem] = []
    rank_pattern = _YOUKU_TARGET_RANK
    for match in rank_pattern.finditer(html):
        fragment = html[max(0, match.start() - 1200) : min(len(html), match.end() + 1200)]
        titles = re.findall(r"(?:title|alt)=[\"']([^\"']{1,300})[\"']", fragment, re.I)
        title = next((value.strip() for value in reversed(titles) if "TOP" not in value.upper()), "")
        if not title:
            heading = re.search(r"<(?:h[1-6]|a)[^>]*>([^<]{1,300})</", fragment, re.I)
            title = _html_text(heading.group(1)) if heading else ""
        if not title:
            continue
        source_match = re.search(r"(?:data-(?:id|vid)|video-id|id)=[\"']([^\"']+)[\"']", fragment, re.I)
        update_match = re.search(r"(?:\u66f4\u65b0\u81f3\s*\d+\s*[\u96c6\u671f\u8bdd]|\d+\s*[\u96c6\u671f\u8bdd]\s*\u5168|\u5df2\u5b8c\u7ed3)", _html_text(fragment))
        marker = _html_text(fragment)
        mapping = {
            "id": source_match.group(1) if source_match else title,
            "title": title,
            "rank": int(match.group(2)),
            "mediaType": match.group(1),
            "updateText": update_match.group(0) if update_match else "",
        }
        item = _candidate(mapping, source="youku", marker=marker)
        if item:
            items.append(item)
    return _dedupe(items)


class YoukuHotSource:
    name = "youku"
    endpoint = "https://www.youku.com/ku/webhome"

    def __init__(self, config: Mapping[str, Any] | None = None, http_client: HttpClient | None = None):
        config = config or {}
        self.endpoint = str(config.get("endpoint") or self.endpoint)
        self.timeout = int(config.get("timeout") or 20)
        self.max_items = max(1, int(config.get("max_items") or 20))
        self.max_items_per_category = max(1, int(config.get("max_items_per_category") or self.max_items))
        if http_client is not None and isinstance(getattr(http_client, "session", None), requests.Session):
            self.session = http_client.session
        elif isinstance(http_client, requests.Session):
            self.session = http_client
        else:
            self.session = requests.Session()
        self.session.trust_env = False

    @staticmethod
    def parse_html(html: str, limit: int | None = None, per_category_limit: int | None = None) -> list[HotItem]:
        category_limit = per_category_limit if per_category_limit is not None else limit if limit is not None else 20
        data = _extract_js_object(html)
        if data is None:
            return _limited_by_category(
                _parse_youku_html_cards(html),
                per_category_limit=category_limit,
            )
        items: list[HotItem] = []
        for mapping in _youku_rank_rows(data):
            marker = _text(mapping.get("rankLabel")) or _youku_item_rank_text(mapping)
            item = _candidate(mapping, source="youku", marker=marker)
            if item:
                items.append(item)
        return _limited_by_category(items, per_category_limit=category_limit)

    def fetch(self) -> list[HotItem]:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"}
        try:
            response = self.session.get(self.endpoint, timeout=self.timeout, headers=headers)
            if response.status_code >= 400:
                raise RuntimeError(f"Youku hot list HTTP {response.status_code}")
            return self.parse_html(
                response.text,
                limit=self.max_items,
                per_category_limit=self.max_items_per_category,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Youku hot list request failed: {exc}") from exc


__all__ = ["HotItem", "TencentHotSource", "IqiyiHotSource", "YoukuHotSource"]
