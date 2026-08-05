from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .organizer.openlist_client import VIDEO_EXTENSIONS
from .public_web import _sixpan_parse_summary
from .services.import_staging_service import map_staging_path_to_openlist, staging_plan_from_job


def _join_virtual_path(*parts: Any) -> str:
    cleaned: list[str] = []
    for part in parts:
        text = str(part or "").replace("\\", "/").strip().strip("/")
        if not text:
            continue
        cleaned.extend(segment.strip(" .") for segment in text.split("/") if segment.strip(" ."))
    return "/" + "/".join(cleaned) if cleaned else "/"


def _strip_virtual_prefix(path: Any, prefix: Any) -> str:
    text = str(path or "").replace("\\", "/").strip().strip("/")
    root = str(prefix or "").replace("\\", "/").strip().strip("/")
    if root and (text == root or text.startswith(f"{root}/")):
        return text[len(root) :].strip("/")
    return text


def _clean_update_openlist_root(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if text.lower() in {"", "none", "null", "undefined", "/none", "/null", "/undefined"}:
        return ""
    return "/" + text.strip("/") if text.strip("/") else ""


def _is_update_season_dir_name(value: Any) -> bool:
    text = str(value or "").replace("\\", "/").strip().strip("/").split("/")[-1].strip()
    if not text:
        return False
    return bool(
        re.fullmatch(r"(?i)season\s*0*\d{1,2}", text)
        or re.fullmatch(r"(?i)s0*\d{1,2}", text)
        or re.fullmatch(r"第\s*(?:\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*季", text)
    )


def _resource_update_root(value: Any) -> str:
    root = _clean_update_openlist_root(value)
    while root and root != "/" and _is_update_season_dir_name(root):
        parent = "/" + "/".join(root.strip("/").split("/")[:-1])
        parent = _clean_update_openlist_root(parent)
        if not parent or parent == root:
            break
        root = parent
    return root


def _virtual_basename(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/").split("/")[-1].strip()


def _resource_root_for_import_mount(canonical_resource_root: Any, mount_category_root: Any) -> str:
    """把订阅识别出的资源目录名映射到当前入库线路的 OpenList 挂载下。

    例如订阅基线可能来自 /移动云/动漫/仙逆 (2023)，但磁链通过 6 盘入库时
    Organizer 实际可整理的目标根应是 /清云/动漫/仙逆 (2023)。
    """

    mount_root = _clean_update_openlist_root(mount_category_root)
    canonical_root = _resource_update_root(canonical_resource_root)
    leaf = _virtual_basename(canonical_root)
    if not mount_root or not leaf:
        return ""
    if leaf.casefold() == _virtual_basename(mount_root).casefold():
        return mount_root
    if _resource_update_root(mount_root).rstrip("/") == canonical_root.rstrip("/"):
        return canonical_root
    suffix = _resource_suffix_after_category_anchor(canonical_root, [_virtual_basename(mount_root)])
    return _join_virtual_path(mount_root, suffix or leaf)


def _rclone_organizer_target_plan(category_openlist_root: Any, preferred_root: Any = "") -> dict[str, Any]:
    """将 rclone 的扫描根与 Organizer 标准目标根解耦。

    普通入库只把深层目录作为本次资源的扫描范围，标准目标必须锚定分类根，
    这样 ``影视名 4K/影视名（年份）`` 等来源包装目录会在整理后被搬空清理。
    追更任务已有明确资源根时，则映射到当前入库挂载并直接写入该资源根。
    """

    category_root = _clean_update_openlist_root(category_openlist_root)
    canonical_root = _resource_update_root(preferred_root)
    if not canonical_root:
        return {
            "target_root_path": category_root,
            "target_root_is_resource": False,
        }
    resource_root = _resource_root_for_import_mount(canonical_root, category_root) or canonical_root
    return {
        "target_root_path": resource_root,
        "canonical_resource_root": resource_root,
        "target_root_is_resource": True,
    }


def _resource_suffix_after_category_anchor(path: Any, anchors: list[str]) -> str:
    parts = [item for item in str(path or "").replace("\\", "/").strip().strip("/").split("/") if item]
    normalized_anchors = {str(item or "").strip().casefold() for item in anchors if str(item or "").strip()}
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold() in normalized_anchors:
            return "/".join(parts[index + 1 :]).strip("/")
    return ""


def _map_cloud139_path_to_openlist(path: Any, mount_name: Any, official_root: Any) -> str:
    text = str(path or "").replace("\\", "/").strip().strip("/")
    mount = str(mount_name or "").replace("\\", "/").strip().strip("/")
    if not text:
        return ""
    if mount and (text == mount or text.startswith(f"{mount}/")):
        return _join_virtual_path(text)
    suffix = _strip_virtual_prefix(text, official_root)
    if mount and suffix:
        return _join_virtual_path(mount, suffix)
    return _join_virtual_path(suffix or text)


def _cloud139_real_folder_name(job: dict[str, Any]) -> str:
    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    save_data = raw_data.get("save") if isinstance(raw_data.get("save"), dict) else {}
    rows = save_data.get("data") if isinstance(save_data.get("data"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("realFolderName", "real_folder_name", "targetFolderName", "target_folder_name"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return ""


def _safe_virtual_segment(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text[:120].rstrip(" .-_") if len(text) > 120 else text


def _sixpan_scan_filters_from_job(job: dict[str, Any], *, root_path: str = "") -> dict[str, Any]:
    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    request_payload = raw_data.get("request") if isinstance(raw_data.get("request"), dict) else {}
    ignored = {str(item or "").strip() for item in (request_payload.get("ignore_files") or []) if str(item or "").strip()}
    parse_data = raw_data.get("parse") if isinstance(raw_data.get("parse"), dict) else {}
    items = (_sixpan_parse_summary(parse_data).get("items") or []) if parse_data else []
    expected_names: list[str] = []
    expected_paths: list[str] = []
    expected_count = 0
    for item in items:
        if not isinstance(item, dict) or item.get("directory"):
            continue
        identity = str(item.get("id") or item.get("identity") or item.get("path") or item.get("name") or "").strip()
        if identity and identity in ignored:
            continue
        media_type = str(item.get("media_type") or "").strip()
        if media_type and media_type != "video":
            continue
        path = str(item.get("path") or item.get("name") or "").replace("\\", "/").strip().strip("/")
        name = str(item.get("name") or path.rsplit("/", 1)[-1] or "").strip()
        expected_count += 1
        if name and name not in expected_names:
            expected_names.append(name)
        # 六盘的 ``path`` 是离线包内的源相对路径，不代表云盘最终落盘路径。
        # 任务根是独占目录时只用它做数量/文件名证据，避免云盘改名、包一层或
        # 扁平化后被错误地当成精确 OpenList 路径。
        confirmed_path = str(item.get("openlist_path") or item.get("target_path") or "").strip()
        scoped_path = (
            _scope_scan_filter_path(root_path, confirmed_path)
            if root_path and confirmed_path
            else ("" if root_path else _scope_scan_filter_path(root_path, path))
        )
        if scoped_path and scoped_path not in expected_paths:
            expected_paths.append(scoped_path)

    return {
        "expected_names": expected_names,
        "expected_paths": expected_paths,
        "expected_count": expected_count,
    }


def _cloud139_scan_filters_from_job(job: dict[str, Any], *, root_path: str = "") -> dict[str, Any]:
    """从 139 勾选结果中提取本次入库的视频文件名。

    单集追更或直接勾选文件时，139 可能把文件直接保存到分类根目录。
    如果没有文件证据，Organizer 会递归扫描整个影视分类，既慢又容易
    对 OpenList/底层云盘造成瞬时压力。
    """

    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    request_payload = raw_data.get("request") if isinstance(raw_data.get("request"), dict) else {}
    containers = [
        raw_data.get("selection") if isinstance(raw_data.get("selection"), dict) else {},
        request_payload.get("cloud139_selection") if isinstance(request_payload.get("cloud139_selection"), dict) else {},
    ]
    expected_names: list[str] = []
    expected_paths: list[str] = []
    expected_count = 0
    seen_items: set[str] = set()
    staging_plan = staging_plan_from_job(job)
    for container in containers:
        rows = []
        for key in ("selected_files", "files"):
            values = container.get(key) if isinstance(container.get(key), list) else []
            rows.extend(item for item in values if isinstance(item, dict))
        for item in rows:
            name = str(item.get("name") or item.get("fileName") or "").strip()
            if not name or Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            identity = str(
                item.get("fid")
                or item.get("id")
                or item.get("share_fid_token")
                or item.get("relative_path")
                or item.get("path")
                or item.get("target_path")
                or name
            ).strip()
            if identity and identity in seen_items:
                continue
            if identity:
                seen_items.add(identity)
            expected_count += 1
            if name not in expected_names:
                expected_names.append(name)
            for key in ("openlist_path", "target_path", "relative_path", "path"):
                path = str(item.get(key) or "").replace("\\", "/").strip()
                if root_path and key in {"relative_path", "path"}:
                    continue
                scoped_path = _scope_scan_filter_path(root_path, path)
                if not scoped_path and staging_plan:
                    scoped_path = _scope_scan_filter_path(
                        root_path,
                        map_staging_path_to_openlist(path, staging_plan),
                    )
                if scoped_path and scoped_path not in expected_paths:
                    expected_paths.append(scoped_path)
    return {
        "expected_names": expected_names,
        "expected_paths": expected_paths,
        "expected_count": expected_count,
    }


def _scope_scan_filter_path(root_path: Any, value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    if not path or "://" in path:
        return ""
    root = str(root_path or "").replace("\\", "/").strip()
    if not root:
        return path
    normalized_root = "/" + root.strip("/")
    if path.startswith("/"):
        normalized_path = "/" + path.strip("/")
        root_folded = normalized_root.casefold()
        path_folded = normalized_path.casefold()
        if path_folded == root_folded or path_folded.startswith(f"{root_folded}/"):
            return normalized_path
        return ""
    return f"{normalized_root}/{path.strip('/')}"


def _common_top_directory(paths: list[Any]) -> str:
    first_segments = []
    for value in paths:
        text = str(value or "").replace("\\", "/").strip().strip("/")
        if "/" not in text:
            return ""
        first = text.split("/", 1)[0].strip(" .")
        if not first:
            return ""
        first_segments.append(first)
    if not first_segments:
        return ""
    first = first_segments[0]
    return first if all(item == first for item in first_segments) else ""
