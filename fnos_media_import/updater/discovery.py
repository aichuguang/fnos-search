from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Any
from urllib.parse import urlparse

from ..classifiers.link_classifier import detect_link
from .matcher import episode_from_text


class UpdateDiscovery:
    def __init__(self, *, search_service: Any, quark_importer: Any, cloud139_importer: Any, routes: dict[str, Any], db: Any = None, cache_ttl_seconds: int = 21600, negative_cache_ttl_seconds: int = 600) -> None:
        self.search_service = search_service
        self.quark = quark_importer
        self.cloud139 = cloud139_importer
        self.routes = routes
        self.db = db
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds or 21600))
        self.negative_cache_ttl_seconds = max(60, int(negative_cache_ttl_seconds or 600))

    def discover(self, subscription: dict[str, Any], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enabled = [item for item in sources if item.get("enabled", True)]
        if not enabled:
            enabled = [{"type": "search", "name": "综合搜索", "enabled": True, "priority": 100, "options": {}}]
        candidates: list[dict[str, Any]] = []
        for source in sorted(enabled, key=lambda item: int(item.get("priority") or 100)):
            source_type = str(source.get("type") or "search").strip().lower()
            try:
                if source_type == "search":
                    candidates.extend(self._discover_search(subscription, source))
                elif source_type == "quark":
                    candidates.extend(self._discover_quark(subscription, source))
                elif source_type == "cloud139":
                    candidates.extend(self._discover_cloud139(subscription, source))
            except Exception as exc:  # noqa: BLE001
                candidates.append(
                    {
                        "title": source.get("name") or source_type,
                        "source_type": source_type,
                        "url": source.get("url") or "",
                        "password": source.get("password") or "",
                        "error": str(exc),
                        "source_id": source.get("id"),
                        "decision_hint": "error",
                    }
                )
        return candidates

    def _discover_search(self, subscription: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
        queries = self._queries(subscription)
        options = source.get("options") if isinstance(source.get("options"), dict) else {}
        result: list[dict[str, Any]] = []
        for query in queries[:8]:
            data = self.search_service.search(
                query,
                options={"save_resources": False, "async_poll": bool(options.get("async_poll", True)), "background": True},
            )
            for item in data.get("items") or []:
                url = str(item.get("url") or item.get("source_url") or "")
                if not url:
                    continue
                link = detect_link(url, self.routes, password=str(item.get("password") or ""))
                if str(link.source_type or "").strip().lower() in {"quark", "uc"} and self.quark:
                    expanded = self._expand_search_cloud_candidate(subscription, source, item, {"type": "quark", "url": link.url or url, "password": link.password or item.get("password") or ""})
                    if expanded:
                        result.extend(expanded)
                    continue
                if str(link.source_type or "").strip().lower() == "cloud139" and self.cloud139:
                    expanded = self._expand_search_cloud_candidate(subscription, source, item, {"type": "cloud139", "url": link.url or url, "password": link.password or item.get("password") or ""})
                    if expanded:
                        result.extend(expanded)
                    continue
                season, episode = episode_from_text(item.get("title"), _to_season(subscription.get("season")))
                normalized = {
                    "title": item.get("title") or query,
                    "source_type": link.source_type or item.get("source_type") or "unknown",
                    "url": link.url or url,
                    "password": link.password or item.get("password") or "",
                    "size_text": item.get("size_text") or item.get("size") or "",
                    "published_at": item.get("datetime") or item.get("created_at") or "",
                    "season": season,
                    "episode": episode,
                    "source_id": source.get("id"),
                    "raw": item,
                }
                if str(normalized["source_type"]).strip().lower() in {"cloud139", "quark", "uc"}:
                    normalized["decision_hint"] = "requires_preview"
                normalized["fingerprint"] = _fingerprint(normalized)
                normalized["import_payload"] = {
                    "title": normalized["title"],
                    "url": normalized["url"],
                    "password": normalized["password"],
                    "category": subscription.get("category") or "movie",
                }
                result.append(normalized)
        return _dedupe(result)

    def _expand_search_cloud_candidate(self, subscription: dict[str, Any], source: dict[str, Any], raw_item: dict[str, Any], cloud_source: dict[str, Any]) -> list[dict[str, Any]]:
        synthetic_source = {
            "id": source.get("id"),
            "type": cloud_source.get("type"),
            "name": raw_item.get("title") or source.get("name") or cloud_source.get("type"),
            "url": cloud_source.get("url") or "",
            "password": cloud_source.get("password") or "",
            "enabled": True,
            "priority": source.get("priority") or 100,
            "options": {"max_depth": 4, "max_items": 200},
        }
        try:
            if synthetic_source["type"] == "cloud139":
                items = self._discover_cloud139(subscription, synthetic_source)
            else:
                items = self._discover_quark(subscription, synthetic_source)
        except Exception as exc:  # noqa: BLE001
            return [
                {
                    "title": raw_item.get("title") or synthetic_source["name"] or "搜索候选",
                    "source_type": synthetic_source["type"],
                    "url": synthetic_source["url"],
                    "password": synthetic_source["password"],
                    "source_id": source.get("id"),
                    "raw": raw_item,
                    "error": f"搜索结果分享预览失败：{exc}",
                    "decision_hint": "error",
                }
            ]
        for item in items:
            item["source_id"] = source.get("id")
            item["search_title"] = raw_item.get("title") or ""
            item["search_raw"] = raw_item
        return items

    def _discover_quark(self, subscription: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(source.get("url") or "").strip()
        if not url:
            return []
        ok, data = self._cached_check("quark", url, lambda: self.quark.check_share(url, str(subscription.get("title") or "update_check")), force=bool(subscription.get("_force_preview_refresh")))
        if not ok:
            raise RuntimeError(_message(data, "Quark 分享检测失败"))
        options = source.get("options") if isinstance(source.get("options"), dict) else {}
        max_depth = int(options.get("max_depth") or 5)
        max_items = int(options.get("max_items") or 800)
        precise_fid = str(options.get("fid") or options.get("folder_fid") or "").strip()
        pwd_id = self.quark._extract_pwd_id(url) if hasattr(self.quark, "_extract_pwd_id") else ""
        stoken = str(self.quark._find_first_value(data, {"stoken", "share_stoken", "shareToken", "share_token"}) or "") if hasattr(self.quark, "_find_first_value") else ""
        rows = self.quark._extract_file_rows(data) if hasattr(self.quark, "_extract_file_rows") else []
        root_items = [self.quark._file_item(row) for row in rows] if hasattr(self.quark, "_file_item") else []
        if precise_fid:
            root_items = [item for item in root_items if item.get("fid") == precise_fid] or [{"fid": precise_fid, "name": source.get("name") or "指定目录", "is_dir": True}]
        result: list[dict[str, Any]] = []

        def walk(
            items: list[dict[str, Any]],
            depth: int,
            parent_fid: str = "",
            parent_name: str = "",
            parent_chain: list[str] | None = None,
            parent_season: int | None = None,
            parent_episode: int | None = None,
        ) -> None:
            if depth > max_depth or len(result) >= max_items:
                return
            parent_chain = parent_chain or []
            for item in items:
                if len(result) >= max_items:
                    return
                fid = str(item.get("fid") or "")
                name = str(item.get("name") or fid or "未命名文件")
                is_dir = bool(item.get("is_dir"))
                season, episode = episode_from_text(name, _to_season(subscription.get("season")))
                if season is None:
                    season = parent_season
                episode = episode or parent_episode

                # 追更必须基于已识别到的具体文件。若分享无法展开到可判断的新文件，
                # 说明本轮没有可靠更新证据，不生成“整分享默认更新”候选。
                if not is_dir:
                    candidate_url = url
                    selection: dict[str, Any] = {
                        "mode": "root_items",
                        "selected_items": [{"fid": fid, "name": name, "type": "file", "is_dir": False}],
                    }
                    if parent_fid and pwd_id:
                        # Quark 自动转存服务只能在“当前分享目录”的根层按文件名过滤。
                        # 因此深层文件不能用 root share + subdir_items 让导入阶段再递归，
                        # 而是把候选 URL 精准切到直接父目录，再用 root_items 只保存该文件。
                        candidate_url = _quark_subdir_share_url(url, pwd_id, parent_fid)
                    candidate = {
                        "title": name,
                        "source_type": "quark",
                        "url": candidate_url,
                        "password": source.get("password") or "",
                        "file_id": fid,
                        "season": season,
                        "episode": episode,
                        "source_id": source.get("id"),
                        "parent_name": "/".join(parent_chain) or parent_name,
                        "raw": {**item, "root_url": url, "parent_fid": parent_fid, "parent_chain": parent_chain},
                        "file_level": True,
                        "import_payload": {
                            "title": str(subscription.get("title") or name or source.get("name") or "未命名资源"),
                            "url": candidate_url,
                            "password": source.get("password") or "",
                            "category": subscription.get("category") or "movie",
                            "quark_selection": selection,
                        },
                    }
                    candidate["fingerprint"] = _fingerprint(candidate)
                    result.append(candidate)
                if is_dir and fid and depth < max_depth:
                    child_data = self.quark.list_files(pwd_id=pwd_id, fid=fid, stoken=stoken) if pwd_id else {}
                    child_rows = self.quark._extract_file_rows(child_data) if hasattr(self.quark, "_extract_file_rows") else []
                    child_items = [self.quark._file_item(row) for row in child_rows] if hasattr(self.quark, "_file_item") else []
                    walk(child_items, depth + 1, fid, name, [*parent_chain, name], season, episode)

        walk(root_items, 0)
        return _dedupe(result)

    def _discover_cloud139(self, subscription: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(source.get("url") or "").strip()
        if not url:
            return []
        password = str(source.get("password") or "")
        ok, data = self._cached_check("cloud139", url, lambda: self.cloud139.check_share(url, title=str(subscription.get("title") or "update_check"), password=password), force=bool(subscription.get("_force_preview_refresh")))
        if not ok:
            raise RuntimeError(_message(data, "139 分享检测失败"))
        items = _cloud139_items(data)
        options = source.get("options") if isinstance(source.get("options"), dict) else {}
        max_depth = int(options.get("max_depth") or 4)
        precise_fid = str(options.get("fid") or options.get("folder_id") or "").strip()
        max_items = int(options.get("max_items") or 800)
        # 官方分享概要的首项通常就是实际顶层目录，且可能完全不返回 level。
        # 不能把“缺少 level”按 0 处理，否则该目录会被误认为虚拟根节点并从
        # folder_items 中排除，最终不会展开其中与子目录并列的单集文件。
        root_item = next(
            (
                item
                for item in items
                if item.get("level") not in (None, "")
                and _safe_int(item.get("level"), -1) == 0
            ),
            None,
        )
        root_name = _cloud139_item_name(root_item) or str(subscription.get("title") or source.get("name") or "")
        result: list[dict[str, Any]] = []

        def add_candidate(
            *,
            title: str,
            file_id: str,
            season: int | None,
            episode: int | None,
            parent_name: str,
            raw: dict[str, Any],
            precision: str,
        ) -> None:
            if len(result) >= max_items:
                return
            if not _cloud139_item_is_file(raw):
                return
            selected_files: list[dict[str, Any]] = [
                {
                    "fid": file_id,
                    "id": file_id,
                    "name": title,
                    "type": "file",
                    "is_dir": False,
                    "path": _cloud139_item_path(raw),
                    "share_fid_token": str(raw.get("share_fid_token") or ""),
                }
            ]
            resource_title = str(subscription.get("title") or title or source.get("name") or "未命名资源")
            candidate = {
                "title": title,
                "source_type": "cloud139",
                "url": url,
                "password": password,
                "file_id": file_id,
                "season": season,
                "episode": episode,
                "source_id": source.get("id"),
                "parent_name": parent_name,
                "raw": raw,
                "precision": precision,
                "file_level": True,
                "import_payload": {
                    "title": resource_title,
                    "url": url,
                    "password": password,
                    "category": subscription.get("category") or "movie",
                    "cloud139_selection": {
                        "mode": "items",
                        "selected_files": selected_files,
                        "selected_folders": [],
                    },
                },
            }
            candidate["fingerprint"] = _fingerprint(candidate)
            result.append(candidate)

        def walk_scope(
            scope_fid: str,
            scope_name: str,
            children: list[dict[str, Any]],
            *,
            depth: int = 0,
            parent_chain: list[str] | None = None,
            parent_season: int | None = None,
            parent_episode: int | None = None,
        ) -> None:
            if depth > max_depth or len(result) >= max_items:
                return
            parent_chain = parent_chain or []
            for child in children:
                if len(result) >= max_items:
                    return
                child_id = _cloud139_item_id(child)
                child_name = _cloud139_item_name(child) or child_id or "未命名文件"
                season, episode = episode_from_text(child_name, _to_season(subscription.get("season")))
                if season is None:
                    season = parent_season
                episode = episode or parent_episode
                if _cloud139_item_is_file(child):
                    add_candidate(
                        title=child_name,
                        file_id=child_id,
                        season=season,
                        episode=episode,
                        parent_name="/".join([name for name in [root_name, scope_name, *parent_chain] if name]),
                        raw=child,
                        precision="exact_file_name",
                    )
                    continue
                if child_id and depth < max_depth and _cloud139_item_is_folder(child):
                    child_data = self.cloud139.list_files(url, child_id, password=password, title=child_name)
                    grand_children = _cloud139_child_items(child_data)
                    walk_scope(
                        scope_fid,
                        scope_name,
                        grand_children,
                        depth=depth + 1,
                        parent_chain=[*parent_chain, child_name],
                        parent_season=season,
                        parent_episode=episode,
                    )

        root_files_allowed = not precise_fid or precise_fid in {"-1", "root", "root-files"}
        # 部分 139 分享检测响应只返回目录概要，根节点缺少 hasRootFiles，
        # 但分享根目录实际存在单文件。此时也要补查 root，否则会漏掉与目录并列的新集。
        direct_root_files = [item for item in items if _cloud139_item_is_file(item)]
        should_probe_root = bool(
            root_files_allowed
            and root_item
            and (root_item.get("hasRootFiles") is True or not direct_root_files)
        )
        if should_probe_root:
            root_data = self.cloud139.list_files(url, "root", password=password, title=root_name or str(subscription.get("title") or "update_check"))
            root_files = [item for item in _cloud139_child_items(root_data) if _cloud139_item_is_file(item)]
            for file_item in root_files:
                file_name = _cloud139_item_name(file_item) or _cloud139_item_id(file_item) or "未命名文件"
                season, episode = episode_from_text(file_name, _to_season(subscription.get("season")))
                add_candidate(
                    title=file_name,
                    file_id=_cloud139_item_id(file_item),
                    season=season,
                    episode=episode,
                    parent_name=root_name,
                    raw=file_item,
                    precision="root_exact_file_name",
                )
        if root_files_allowed:
            for file_item in direct_root_files:
                file_name = _cloud139_item_name(file_item) or _cloud139_item_id(file_item) or "未命名文件"
                season, episode = episode_from_text(file_name, _to_season(subscription.get("season")))
                add_candidate(
                    title=file_name,
                    file_id=_cloud139_item_id(file_item),
                    season=season,
                    episode=episode,
                    parent_name=root_name,
                    raw=file_item,
                    precision="root_exact_file_name",
                )

        folder_items = [item for item in items if _cloud139_item_is_folder(item) and (_safe_int(item.get("level"), 0) > 0 or (_cloud139_item_id(item) and item is not root_item))]
        if precise_fid:
            folder_items = [item for item in folder_items if _cloud139_item_id(item) == precise_fid]
        if precise_fid and not folder_items and precise_fid not in {"-1", "root", "root-files"}:
            folder_items = [{"fid": precise_fid, "id": precise_fid, "name": source.get("name") or "指定目录", "type": "dir", "isFolder": True}]

        for item in folder_items:
            if len(result) >= max_items:
                break
            fid = _cloud139_item_id(item)
            if not fid:
                continue
            name = _cloud139_item_name(item) or fid or "未命名目录"
            season, episode = episode_from_text(name, _to_season(subscription.get("season")))
            before_count = len(result)
            child_data = self.cloud139.list_files(url, fid, password=password, title=name)
            children = _cloud139_child_items(child_data)
            if children:
                walk_scope(fid, name, children, parent_chain=[], parent_season=season, parent_episode=episode)
            if len(result) <= before_count and (episode or _cloud139_name_matches_subscription(name, subscription, root_name=root_name)):
                # 追更必须基于已识别到的具体文件。目录名命中但没有展开到文件时，
                # 不自动保存整个分享，避免把“没检测到更新”误判成需要更新。
                continue
        return _dedupe(result)

    def _queries(self, subscription: dict[str, Any]) -> list[str]:
        title = str(subscription.get("title") or "").strip()
        season = _to_season(subscription.get("season"))
        missing = [int(item) for item in subscription.get("_target_episode_numbers") or [] if _to_int(item)]
        aliases = [str(item).strip() for item in subscription.get("aliases") or [] if str(item).strip()]
        bases = [title, *aliases]
        result: list[str] = []
        for base in bases:
            if not base:
                continue
            if season is not None:
                result.append(f"{base} 第{season}季")
                result.append(f"{base} S{season:02d}")
            else:
                result.append(base)
            for episode in missing[:5]:
                result.append(f"{base} 第{episode}集")
                if season is not None:
                    result.append(f"{base} S{season:02d}E{episode:02d}")
        return list(dict.fromkeys(result or [title]))

    def _cached_check(self, source_type: str, url: str, loader: Any, *, force: bool = False) -> tuple[bool, dict[str, Any]]:
        if not self.db:
            return loader()
        try:
            cached = None if force else self.db.get_update_preview_cache(source_type, url)
        except Exception:
            cached = None
        if cached:
            raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), dict) else {}
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            if isinstance(data, dict):
                data = {**data, "_cache": {"hit": True, "updated_at": cached.get("updated_at")}}
                return bool(cached.get("ok")), data
        ok, data = loader()
        try:
            latest_season, latest_episode = _latest_episode_from_preview_items(_preview_items_for_cache(data))
            self.db.upsert_update_preview_cache(
                source_type=source_type,
                source_url=url,
                ok=bool(ok),
                message=_message(data, ""),
                items=_preview_items_for_cache(data),
                latest_season=latest_season,
                latest_episode=latest_episode,
                raw_data={"data": data},
                ttl_seconds=self.cache_ttl_seconds if ok else self.negative_cache_ttl_seconds,
            )
        except Exception:
            pass
        return ok, data


def _cloud139_items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    rows = body.get("items") if isinstance(body, dict) else None
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _cloud139_child_items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    candidates = data.get("items")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    candidates = body.get("items") if isinstance(body, dict) else None
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    result: list[dict[str, Any]] = []
    for key in ("files", "fileList", "coLst"):
        rows = body.get(key) if isinstance(body, dict) else None
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    result.append({**item, "type": "file", "isFolder": False})
    for key in ("folders", "folderList", "caLst"):
        rows = body.get(key) if isinstance(body, dict) else None
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    result.append({**item, "type": "dir", "isFolder": True})
    return result


def _cloud139_item_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("fid")
        or item.get("id")
        or item.get("folder_id")
        or item.get("fileId")
        or item.get("contentID")
        or item.get("contentId")
        or item.get("coID")
        or item.get("coId")
        or item.get("catalogID")
        or item.get("catalogId")
        or item.get("caID")
        or item.get("caId")
        or ""
    ).strip()


def _cloud139_item_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("name")
        or item.get("fileName")
        or item.get("contentName")
        or item.get("coName")
        or item.get("folderName")
        or item.get("catalogName")
        or item.get("caName")
        or ""
    ).strip()


def _cloud139_item_path(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("path") or item.get("contentPath") or item.get("coPath") or item.get("filePath") or "").strip()


def _cloud139_item_is_file(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = str(item.get("type") or "").strip().lower()
    if item_type in {"dir", "folder"}:
        return False
    if item.get("isFolder") is True or item.get("is_dir") is True or item.get("dir") is True or str(item.get("dir") or "").strip().lower() in {"1", "true", "yes"}:
        return False
    if item.get("catalogID") or item.get("catalogId") or item.get("caID") or item.get("caId") or item.get("folderName") or item.get("catalogName") or item.get("caName"):
        if not (item.get("contentID") or item.get("contentId") or item.get("coID") or item.get("coId")):
            return False
    if item_type == "file":
        return True
    if item.get("isFolder") is False or item.get("is_dir") is False or item.get("isFile") is True:
        return True
    return bool(item.get("contentID") or item.get("contentId") or item.get("coID") or item.get("coId") or item.get("fileName") or item.get("coName"))


def _cloud139_item_is_folder(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = str(item.get("type") or "").strip().lower()
    if item_type == "file" or item.get("isFolder") is False or item.get("is_dir") is False or item.get("isFile") is True:
        return False
    if item_type in {"dir", "folder"}:
        return True
    if item.get("isFolder") is True or item.get("is_dir") is True or item.get("dir") is True or str(item.get("dir") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    return bool(item.get("catalogID") or item.get("catalogId") or item.get("caID") or item.get("caId") or item.get("folderName") or item.get("catalogName") or item.get("caName"))


def _cloud139_name_matches_subscription(name: Any, subscription: dict[str, Any], *, root_name: str = "") -> bool:
    expected = _normalize_text(subscription.get("title"))
    aliases = [_normalize_text(item) for item in subscription.get("aliases") or []]
    haystack = _normalize_text(f"{name} {root_name}")
    return bool((expected and expected in haystack) or any(alias and alias in haystack for alias in aliases))


def _normalize_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _fingerprint(candidate: dict[str, Any]) -> str:
    raw = "|".join(str(candidate.get(key) or "") for key in ("source_type", "url", "file_id", "title", "size_text"))
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("fingerprint") or item.get("url") or item.get("title") or "")
        if key and key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _quark_subdir_share_url(original_url: str, pwd_id: str, fid: str) -> str:
    parsed = urlparse(str(original_url or "") if "://" in str(original_url or "") else f"https://{original_url}")
    scheme = parsed.scheme or "https"
    host = parsed.netloc or "pan.quark.cn"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}/s/{pwd_id}{query}#/list/share/{fid}"


def _preview_items_for_cache(data: Any) -> list[dict[str, Any]]:
    rows = _cloud139_child_items(data) or _cloud139_items(data)
    result: list[dict[str, Any]] = []
    for item in rows[:1000]:
        name = _cloud139_item_name(item) or str(item.get("title") or "") if isinstance(item, dict) else ""
        result.append(
            {
                "name": name,
                "fid": _cloud139_item_id(item),
                "type": "file" if _cloud139_item_is_file(item) else "dir",
                "size": item.get("size") if isinstance(item, dict) else None,
            }
        )
    return result


def _latest_episode_from_preview_items(items: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    best: tuple[int | None, int] | None = None
    for item in items:
        season, episode = episode_from_text(item.get("name"))
        if not episode:
            continue
        key = (season, episode)
        if best is None or (episode, season or 0) > (best[1], best[0] or 0):
            best = key
    return best if best else (None, None)


def _to_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _to_season(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _message(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("msg") or data.get("error") or fallback)
    return fallback
