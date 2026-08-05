from __future__ import annotations

from typing import Any


def normalize_storage_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def normalize_sixpan_path_sources(
    config: dict[str, Any],
    *,
    materialize_category_paths: bool,
) -> None:
    """Keep one SixPan mount source and derive per-category OpenList paths."""

    sixpan = config.get("sixpan")
    if not isinstance(sixpan, dict):
        sixpan = {}
        config["sixpan"] = sixpan
    mount_name = normalize_storage_path(
        sixpan.get("fnos_mount_name")
        or sixpan.get("openlist_mount_name")
        or sixpan.get("mount_name")
        or sixpan.get("mount_path")
    )
    if mount_name:
        sixpan["fnos_mount_name"] = mount_name
    for legacy_key in ("openlist_mount_name", "mount_name", "mount_path"):
        sixpan.pop(legacy_key, None)

    categories = config.get("categories")
    if not isinstance(categories, dict):
        return
    for category in categories.values():
        if not isinstance(category, dict):
            continue
        if materialize_category_paths:
            category_dir = normalize_storage_path(
                category.get("sixpan_save_path") or category.get("label")
            )
            derived = "/".join(part for part in (mount_name, category_dir) if part)
            if derived:
                category["sixpan_fnos_target_path"] = f"/{derived}"
            else:
                category.pop("sixpan_fnos_target_path", None)
        else:
            category.pop("sixpan_fnos_target_path", None)


def cmcc_upload_root(category: dict[str, Any], cloud139_config: dict[str, Any] | None = None) -> str:
    """Return the real CMCC cloud directory used by API uploads.

    CMCC API paths and OpenList mount paths are different namespaces.  An
    explicit ``cmcc_parent_path`` remains a supported override; otherwise API
    uploads share the same official directory as 139 direct saves.
    """

    official = normalize_storage_path(category.get("cloud139_target_path"))
    official_root = normalize_storage_path((cloud139_config or {}).get("target_root_path"))
    official_folded = official.casefold()
    root_folded = official_root.casefold()
    if official_root and official and official_folded != root_folded and not official_folded.startswith(f"{root_folded}/"):
        return f"{official_root}/{official}"
    # A full category path saved by the settings page is authoritative.  The
    # legacy CMCC override is still useful when the category only contains its
    # default leaf (for example just "电影").
    if official and "/" in official:
        return official
    explicit = normalize_storage_path(category.get("cmcc_parent_path"))
    if explicit:
        return explicit
    if official:
        return official
    if official_root:
        label = normalize_storage_path(category.get("label"))
        return f"{official_root}/{label}" if label else official_root
    return normalize_storage_path(category.get("mobile_target_path") or category.get("label"))


def upload_backend(config: dict[str, Any], cmcc_upload_config: dict[str, Any] | None = None) -> str:
    cmcc = cmcc_upload_config or {}
    enabled = str(cmcc.get("enabled", True)).strip().lower()
    fallback = "webdav" if enabled in {"0", "false", "no", "off"} else "cmcc_api"
    return str(config.get("upload_backend") or cmcc.get("backend") or fallback).strip().lower()


def rclone_upload_root(
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    if str(backend or "").strip().lower() == "cmcc_api":
        return cmcc_upload_root(category, cloud139_config)
    return normalize_storage_path(category.get("mobile_target_path") or category.get("label"))


def openlist_root_for_upload(
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    if str(backend or "").strip().lower() != "cmcc_api":
        return normalize_storage_path(
            category.get("openlist_root_path")
            or category.get("mobile_openlist_root_path")
            or category.get("mobile_target_path")
        )

    configured = normalize_storage_path(
        category.get("cloud139_fnos_target_path")
        or category.get("mobile_openlist_root_path")
        or category.get("openlist_root_path")
    )
    if configured:
        return configured

    cloud139 = cloud139_config or {}
    mount_name = normalize_storage_path(cloud139.get("fnos_mount_name") or cloud139.get("mount_name"))
    if not mount_name:
        return ""
    official = cmcc_upload_root(category, cloud139)
    official_root = normalize_storage_path(cloud139.get("target_root_path"))
    suffix = official
    official_folded = official.casefold()
    root_folded = official_root.casefold()
    if official_root and (official_folded == root_folded or official_folded.startswith(f"{root_folded}/")):
        suffix = official[len(official_root) :].strip("/")
    return "/".join(part for part in (mount_name, suffix) if part)


def map_upload_path_to_openlist(
    path: Any,
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    """Map an actual upload path to the path exposed by OpenList."""

    value = normalize_storage_path(path)
    if not value:
        return ""
    source_root = rclone_upload_root(category, backend=backend, cloud139_config=cloud139_config)
    visible_root = openlist_root_for_upload(category, backend=backend, cloud139_config=cloud139_config)
    if not source_root or not visible_root:
        return f"/{value}"
    value_folded = value.casefold()
    source_folded = source_root.casefold()
    if value_folded == source_folded:
        return f"/{visible_root}"
    if value_folded.startswith(f"{source_folded}/"):
        suffix = value[len(source_root) + 1 :]
        return f"/{visible_root}/{suffix}"
    return f"/{value}"
