from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class MediaDashboardSources:
    libraries: dict[str, Any]
    refresh_libraries: dict[str, Any]
    running_tasks: dict[str, Any]


class MediaDashboardSourceService:
    """Loads independent FNOS dashboard sources with per-source isolation."""

    def load(self, fnos: Any) -> MediaDashboardSources:
        return MediaDashboardSources(
            libraries=self._safe_call(
                fnos.media_libraries,
                "飞牛媒体库列表获取异常",
                {"items": [], "summary": {}},
            ),
            refresh_libraries=self._safe_call(
                fnos.refresh_libraries,
                "飞牛媒体库刷新列表获取异常",
                {"items": []},
            ),
            running_tasks=self._safe_call(
                fnos.running_tasks,
                "飞牛刷新任务状态获取异常",
                {"items": []},
            ),
        )

    @staticmethod
    def _safe_call(
        callback: Callable[[], dict[str, Any]],
        error_prefix: str,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = callback()
            return result if isinstance(result, dict) else {"success": False, "message": "返回格式异常", **fallback}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"{error_prefix}：{exc}", **fallback}


class MediaLibraryItemBuilder:
    """Normalizes FNOS library and refresh-list records into one dashboard row."""

    def __init__(
        self,
        *,
        normalize_name: Callable[[Any], str],
        match_category: Callable[[dict[str, Any], dict[str, str]], str],
        media_target_path: Callable[[dict[str, Any]], str],
        target_hints: Callable[[dict[str, Any]], list[str]],
        match_target_dirs: Callable[[Any, list[str]], list[str]],
        configured_dirs: Callable[[dict[str, Any]], list[str]],
        absolute_dirs: Callable[[Any], list[str]],
        media_count: Callable[[dict[str, Any], str, dict[str, Any]], Any],
        task_matches: Callable[[dict[str, Any], str, str], bool],
        row_key: Callable[[str, str, str], str],
        type_label: Callable[[Any], str],
        asset_url: Callable[[Any, str], str],
        posters: Callable[[dict[str, Any]], list[Any]],
    ) -> None:
        self.normalize_name = normalize_name
        self.match_category = match_category
        self.media_target_path = media_target_path
        self.target_hints = target_hints
        self.match_target_dirs = match_target_dirs
        self.configured_dirs = configured_dirs
        self.absolute_dirs = absolute_dirs
        self.media_count = media_count
        self.task_matches = task_matches
        self.row_key = row_key
        self.type_label = type_label
        self.asset_url = asset_url
        self.posters = posters

    def build(
        self,
        item: dict[str, Any],
        *,
        refresh_item: dict[str, Any] | None,
        summary: dict[str, Any],
        running_items: list[dict[str, Any]],
        categories: dict[str, dict[str, Any]],
        category_index: dict[str, str],
        base_url: str,
    ) -> dict[str, Any]:
        refresh = refresh_item or {}
        guid = str(item.get("guid") or item.get("id") or refresh.get("guid") or refresh.get("id") or "").strip()
        title = str(
            item.get("title") or item.get("name") or refresh.get("name")
            or refresh.get("title") or item.get("label") or refresh.get("label")
            or guid or "未命名媒体库"
        ).strip()
        category_key = self.match_category(item, category_index)
        category = categories.get(category_key, {}) if category_key else {}
        hints = self.target_hints(category) if category_key else []
        raw_dirs = refresh.get("dir_list")
        dirs = self.match_target_dirs(raw_dirs, hints) or (self.configured_dirs(category) if category_key else [])
        tasks = [task for task in running_items if self.task_matches(task, guid, title)]
        return {
            "row_key": self.row_key(category_key, guid, title),
            "guid": guid,
            "title": title,
            "category": str(item.get("category") or refresh.get("category") or ""),
            "category_label": self.type_label(item.get("category") or refresh.get("category")),
            "view_type": item.get("view_type") or refresh.get("view_type"),
            "poster_type": item.get("poster_type") or refresh.get("poster_type"),
            "poster": self.asset_url(item.get("poster") or refresh.get("poster"), base_url),
            "posters": [self.asset_url(value, base_url) for value in self.posters(item or refresh)[:6]],
            "count": self.media_count(summary, guid, item),
            "matched_category_key": category_key,
            "matched_category_label": category.get("label") if category_key else "",
            "target_path": self.media_target_path(category) if category_key else "",
            "fnos_dir_list": dirs,
            "fnos_dir_source": "fnos_mdb_list" if self.absolute_dirs(raw_dirs) else "config",
            "fnos_dir_hints": hints,
            "refreshable": bool(guid),
            "dir_refresh": bool(dirs),
            "running": bool(tasks),
            "running_tasks": tasks[:3],
        }


class MediaDashboardBuilder:
    """Merges FNOS sources into the complete admin media dashboard model."""

    def __init__(
        self,
        *,
        source_loader: Any,
        item_builder: Any,
        refresh_indexes: Callable[[list[Any]], tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]],
        category_index: Callable[[dict[str, dict[str, Any]]], dict[str, str]],
        normalize_name: Callable[[Any], str],
        category_items: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        self.source_loader = source_loader
        self.item_builder = item_builder
        self.refresh_indexes = refresh_indexes
        self.category_index = category_index
        self.normalize_name = normalize_name
        self.category_items = category_items

    def build(self, fnos: Any, categories: dict[str, dict[str, Any]]) -> dict[str, Any]:
        sources = self.source_loader.load(fnos)
        libraries = sources.libraries
        refresh_result = sources.refresh_libraries
        running_result = sources.running_tasks
        summary = libraries.get("summary") if isinstance(libraries.get("summary"), dict) else {}
        raw_items = libraries.get("items") if isinstance(libraries.get("items"), list) else []
        refresh_items = refresh_result.get("items") if isinstance(refresh_result.get("items"), list) else []
        running_items = running_result.get("items") if isinstance(running_result.get("items"), list) else []
        by_guid, by_name = self.refresh_indexes(refresh_items)
        category_index = self.category_index(categories)
        base_url = str(getattr(fnos, "server_url", "") or "").rstrip("/")
        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            guid = str(raw_item.get("guid") or raw_item.get("id") or "").strip()
            name = raw_item.get("title") or raw_item.get("name") or raw_item.get("label")
            refresh_item = by_guid.get(guid) or by_name.get(self.normalize_name(name))
            items.append(self._item(raw_item, refresh_item, summary, running_items, categories, category_index, base_url))
        seen = {item.get("guid") for item in items if item.get("guid")}
        for refresh_item in refresh_items:
            if not isinstance(refresh_item, dict):
                continue
            guid = str(refresh_item.get("guid") or refresh_item.get("id") or "").strip()
            if guid in seen:
                continue
            items.append(self._item(refresh_item, refresh_item, summary, running_items, categories, category_index, base_url))
        describe = fnos.describe() if hasattr(fnos, "describe") else {}
        return {
            "success": bool(libraries.get("success") or refresh_result.get("success")),
            "message": str(libraries.get("message") or refresh_result.get("message") or ""),
            "configured": bool(describe.get("configured")),
            "fnos": describe,
            "summary": summary,
            "items": items,
            "categories": self.category_items(categories, items),
            "running": running_items,
            "diagnostics": {
                "libraries": {"success": bool(libraries.get("success")), "message": libraries.get("message") or ""},
                "refresh_libraries": {"success": bool(refresh_result.get("success")), "message": refresh_result.get("message") or ""},
                "summary": libraries.get("summary_response") or {},
                "running": {"success": bool(running_result.get("success")), "message": running_result.get("message") or ""},
            },
        }

    def _item(self, item, refresh, summary, running, categories, category_index, base_url):
        return self.item_builder.build(
            item,
            refresh_item=refresh,
            summary=summary,
            running_items=running,
            categories=categories,
            category_index=category_index,
            base_url=base_url,
        )


class MediaDashboardService:
    """领域门面：集中装配媒体仪表盘的数据加载、行构建与聚合流程。"""

    def __init__(
        self,
        *,
        normalize_name: Callable[[Any], str],
        match_category: Callable[[dict[str, Any], dict[str, str]], str],
        media_target_path: Callable[[dict[str, Any]], str],
        target_hints: Callable[[dict[str, Any]], list[str]],
        match_target_dirs: Callable[[Any, list[str]], list[str]],
        configured_dirs: Callable[[dict[str, Any]], list[str]],
        absolute_dirs: Callable[[Any], list[str]],
        media_count: Callable[[dict[str, Any], str, dict[str, Any]], Any],
        task_matches: Callable[[dict[str, Any], str, str], bool],
        row_key: Callable[[str, str, str], str],
        type_label: Callable[[Any], str],
        asset_url: Callable[[Any, str], str],
        posters: Callable[[dict[str, Any]], list[Any]],
        refresh_indexes: Callable[[list[Any]], tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]],
        category_index: Callable[[dict[str, dict[str, Any]]], dict[str, str]],
        category_items: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        item_builder = MediaLibraryItemBuilder(
            normalize_name=normalize_name,
            match_category=match_category,
            media_target_path=media_target_path,
            target_hints=target_hints,
            match_target_dirs=match_target_dirs,
            configured_dirs=configured_dirs,
            absolute_dirs=absolute_dirs,
            media_count=media_count,
            task_matches=task_matches,
            row_key=row_key,
            type_label=type_label,
            asset_url=asset_url,
            posters=posters,
        )
        self.builder = MediaDashboardBuilder(
            source_loader=MediaDashboardSourceService(),
            item_builder=item_builder,
            refresh_indexes=refresh_indexes,
            category_index=category_index,
            normalize_name=normalize_name,
            category_items=category_items,
        )

    def build(self, fnos: Any, categories: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return self.builder.build(fnos, categories)
