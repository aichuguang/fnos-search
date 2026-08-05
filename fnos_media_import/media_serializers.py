from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .media.fnos import FnosMediaRefresher
from .services.import_service import ImportService
from .services.media_dashboard_service import MediaDashboardService


def _build_media_dashboard(fnos: FnosMediaRefresher, categories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    service = MediaDashboardService(
        normalize_name=_normalize_media_name,
        match_category=_match_media_category,
        media_target_path=_media_target_path,
        target_hints=ImportService._fnos_target_hints,
        match_target_dirs=ImportService._match_fnos_target_dirs,
        configured_dirs=ImportService._fnos_dir_list,
        absolute_dirs=_absolute_media_dirs,
        media_count=_media_count,
        task_matches=_media_task_matches,
        row_key=_media_row_key,
        type_label=_fnos_media_type_label,
        asset_url=_media_asset_url,
        posters=_media_posters,
        refresh_indexes=_media_refresh_indexes,
        category_index=_media_category_index,
        category_items=_media_category_items,
    )
    return service.build(fnos, categories)


def _media_category_index(categories: dict[str, dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, category in categories.items():
        names = [key, category.get("label"), category.get("fnos_lib")]
        for value in names:
            for name in _media_library_names(value):
                normalized = _normalize_media_name(name)
                if normalized:
                    index[normalized] = key
    return index


def _media_refresh_indexes(items: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_guid: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        guid = str(item.get("guid") or item.get("id") or "").strip()
        if guid:
            by_guid[guid] = item
        for value in (item.get("name"), item.get("title"), item.get("label")):
            normalized = _normalize_media_name(value)
            if normalized:
                by_name[normalized] = item
    return by_guid, by_name


def _media_library_names(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[|,，、\n]+", text)
    return [part.strip() for part in parts if part.strip()]


def _normalize_media_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s_\-—/\\]+", "", text)


def _absolute_media_dirs(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").replace("|", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    result: list[str] = []
    for raw_item in raw_items:
        text = str(raw_item or "").strip()
        if not text:
            continue
        normalized = "/" + text.strip("/") if text.startswith("/") else text
        if not normalized.startswith("/"):
            continue
        if normalized not in result:
            result.append(normalized)
    return result


def _match_media_category(item: dict[str, Any], category_index: dict[str, str]) -> str:
    names = [
        item.get("title"),
        item.get("name"),
        item.get("label"),
    ]
    for value in names:
        normalized = _normalize_media_name(value)
        if normalized in category_index:
            return category_index[normalized]
    for value in names:
        normalized = _normalize_media_name(value)
        if not normalized:
            continue
        for candidate, key in category_index.items():
            if candidate and (candidate in normalized or normalized in candidate):
                return key
    return ""


def _media_count(summary: dict[str, Any], guid: str, item: dict[str, Any]) -> int:
    for value in (summary.get(guid), item.get("count"), item.get("total"), item.get("num")):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _media_posters(item: dict[str, Any]) -> list[Any]:
    posters = item.get("posters")
    if isinstance(posters, list):
        result = [value for value in posters if value]
    else:
        result = []
    poster = item.get("poster")
    if poster and poster not in result:
        result.insert(0, poster)
    return result


def _media_asset_url(value: Any, base_url: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    if not base_url:
        return text
    if text.startswith("/"):
        return f"{base_url}{text}"
    return f"{base_url}/{text}"


def _media_target_path(category: dict[str, Any]) -> str:
    return str(category.get("mobile_target_path") or category.get("cloud139_target_path") or category.get("quark_save_path") or "").strip()


def _media_task_matches(task: Any, guid: str, title: str) -> bool:
    if not isinstance(task, dict):
        return False
    task_guid = _media_task_guid(task)
    if guid and task_guid and task_guid == guid:
        return True
    try:
        text = json.dumps(task, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(task)
    if guid and guid in text:
        return True
    normalized_title = _normalize_media_name(title)
    return bool(normalized_title and len(normalized_title) > 1 and normalized_title in _normalize_media_name(text))


def _media_task_guid(task: dict[str, Any]) -> str:
    for key in ("guid", "library_guid", "mdb_guid", "media_guid", "mediadb_guid", "target_guid"):
        value = task.get(key)
        if value:
            return str(value)
    for key in ("data", "raw", "payload", "params", "args"):
        nested = task.get(key)
        if isinstance(nested, dict):
            value = _media_task_guid(nested)
            if value:
                return value
    return ""


def _media_row_key(category_key: str, guid: str, title: str) -> str:
    raw = str(category_key or guid or title or "library").strip()
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_")
    if safe:
        return safe[:72]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"library_{digest}"


def _media_category_items(categories: dict[str, dict[str, Any]], library_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category = {item.get("matched_category_key"): item for item in library_items if item.get("matched_category_key")}
    result: list[dict[str, Any]] = []
    for key, category in categories.items():
        matched = by_category.get(key) or {}
        dir_list = matched.get("fnos_dir_list") if isinstance(matched.get("fnos_dir_list"), list) else ImportService._fnos_dir_list(category)
        result.append(
            {
                "key": key,
                "label": category.get("label") or key,
                "fnos_lib": category.get("fnos_lib") or category.get("label") or key,
                "target_path": _media_target_path(category),
                "fnos_dir_list": dir_list,
                "fnos_dir_source": matched.get("fnos_dir_source") or ("config" if dir_list else ""),
                "fnos_dir_hints": matched.get("fnos_dir_hints") or ImportService._fnos_target_hints(category),
                "guid": matched.get("guid") or "",
                "library_title": matched.get("title") or "",
                "count": matched.get("count") or 0,
                "found": bool(matched),
                "refreshable": bool(matched.get("guid")),
                "running": bool(matched.get("running")),
            }
        )
    return result


def _fnos_media_type_label(value: Any) -> str:
    normalized = str(value or "").strip()
    labels = {
        "Movie": "电影库",
        "TV": "剧集库",
        "Mix": "混合库",
        "Video": "视频库",
    }
    return labels.get(normalized, normalized or "未知")
