from __future__ import annotations

"""Canonical storage-target path helpers.

Storage destinations and OpenList mounts are V1 database resources.  This
module intentionally does not translate the pre-V1 ``cmcc_upload``,
``cloud139`` or category ``mobile_*`` fields into a synthetic destination.
Callers that still provide those fields receive a deterministic validation
error instead of silently selecting the historical mobile target.
"""

from typing import Any


_LEGACY_SIXPAN_PATH_FIELDS = frozenset(
    {"fnos_mount_name", "openlist_mount_name", "mount_name", "mount_path"}
)
_LEGACY_CATEGORY_PATH_FIELDS = frozenset(
    {
        "mobile_target_path",
        "cloud139_target_path",
        "cloud139_fnos_target_path",
        "cloud139_folder_id",
        "cmcc_parent_file_id",
        "cmcc_parent_path",
        "sixpan_fnos_target_path",
        "sixpan_fnos_dir_list",
    }
)


class LegacyStoragePathError(ValueError):
    """Raised when a removed pre-V1 path field is supplied."""


def normalize_storage_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def resolve_local_target(root: str, relative: str) -> str:
    """Resolve a V1 local destination below the controlled container mount."""

    root_norm = str(root or "").strip().replace("\\", "/")
    while "//" in root_norm:
        root_norm = root_norm.replace("//", "/")
    if not root_norm.startswith("/"):
        raise ValueError("root \u5fc5\u987b\u662f\u5bb9\u5668\u5185\u7edd\u5bf9\u8def\u5f84")
    root_norm = root_norm.rstrip("/")
    if not root_norm:
        raise ValueError("root \u4e0d\u80fd\u662f\u6587\u4ef6\u7cfb\u7edf\u6839\u76ee\u5f55")
    if "\x00" in root_norm or any(ord(char) < 32 for char in root_norm):
        raise ValueError("root \u5305\u542b\u975e\u6cd5\u63a7\u5236\u5b57\u7b26")
    if any(part in {"", ".", ".."} for part in root_norm.split("/")[1:]):
        raise ValueError("root \u5305\u542b .. \u6216\u975e\u6cd5\u8def\u5f84\u6bb5")
    if root_norm != "/storage-targets" and not root_norm.startswith(
        "/storage-targets/"
    ):
        raise ValueError("root \u5fc5\u987b\u4f4d\u4e8e /storage-targets \u6302\u8f7d\u76ee\u5f55\u5185")

    relative_text = str(relative or "").strip()
    if not relative_text:
        raise ValueError("relative \u4e0d\u80fd\u4e3a\u7a7a")
    if relative_text.startswith(("/", "\\")):
        raise ValueError("relative \u5fc5\u987b\u662f\u76f8\u5bf9\u8def\u5f84")
    if "\x00" in relative_text or any(ord(char) < 32 for char in relative_text):
        raise ValueError("relative \u5305\u542b\u975e\u6cd5\u63a7\u5236\u5b57\u7b26")
    relative_norm = normalize_storage_path(relative_text)
    if relative_norm in {"", ".", ".."} or any(
        part in {"", ".", ".."} for part in relative_norm.split("/")
    ):
        raise ValueError("relative \u5305\u542b .. \u6216\u975e\u6cd5\u8def\u5f84\u6bb5")
    return f"{root_norm}/{relative_norm}"


def _reject_legacy_category_fields(category: Any) -> dict[str, Any]:
    value = category if isinstance(category, dict) else {}
    invalid = sorted(_LEGACY_CATEGORY_PATH_FIELDS.intersection(value))
    if invalid:
        raise LegacyStoragePathError(
            "配置包含已删除的存储路径字段：" + ", ".join(invalid)
        )
    return value


def normalize_sixpan_path_sources(
    config: dict[str, Any],
    *,
    materialize_category_paths: bool,
) -> None:
    """Validate the canonical SixPan runtime section without deriving paths.

    ``materialize_category_paths`` remains in the signature for callers that
    have not yet been migrated, but it is deliberately ignored.  V1 target
    locators are the only source of destination paths.  Any removed global or
    category alias is rejected so malformed persisted/config-file data cannot
    be silently revived.
    """

    del materialize_category_paths
    if not isinstance(config, dict):
        raise LegacyStoragePathError("配置根必须是对象")

    sixpan = config.get("sixpan")
    if isinstance(sixpan, dict):
        invalid = sorted(_LEGACY_SIXPAN_PATH_FIELDS.intersection(sixpan))
        if invalid:
            raise LegacyStoragePathError(
                "六盘全局挂载路径已移除，请在存储目标中配置 locator：" + ", ".join(invalid)
            )

    categories = config.get("categories")
    if isinstance(categories, dict):
        for category_key, category in categories.items():
            if not isinstance(category, dict):
                continue
            invalid = sorted(_LEGACY_CATEGORY_PATH_FIELDS.intersection(category))
            if invalid:
                raise LegacyStoragePathError(
                    f"分类 {category_key} 包含已删除的存储路径字段：" + ", ".join(invalid)
                )


def _canonical_category_root(category: Any, *, organizer: bool = False) -> str:
    value = _reject_legacy_category_fields(category)
    if organizer:
        return normalize_storage_path(
            value.get("organizer_root")
            or value.get("openlist_root")
            or value.get("final_root")
        )
    return normalize_storage_path(
        value.get("final_root")
        or value.get("provider_path")
        or value.get("native_path")
    )


def cmcc_upload_root(category: dict[str, Any], cloud139_config: dict[str, Any] | None = None) -> str:
    """Return a V1 target category root.

    The second argument is retained only to avoid an import-time API break for
    code being migrated; its contents are never read.  Paths must come from a
    storage-target category locator, not from a global cloud139 section.
    """

    del cloud139_config
    return _canonical_category_root(category)


def upload_backend(config: dict[str, Any], cmcc_upload_config: dict[str, Any] | None = None) -> str:
    """Return the explicitly selected storage driver.

    ``cmcc_upload_config`` is intentionally ignored; credentials and target
    identity are resolved from V1 account/target bindings.
    """

    del cmcc_upload_config
    return str((config or {}).get("upload_backend") or "").strip().lower()


def rclone_upload_root(
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    del cloud139_config
    if str(backend or "").strip().lower() == "cmcc_api":
        return cmcc_upload_root(category)
    return _canonical_category_root(category)


def openlist_root_for_upload(
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    del backend, cloud139_config
    return _canonical_category_root(category, organizer=True)


def map_upload_path_to_openlist(
    path: Any,
    category: dict[str, Any],
    *,
    backend: str,
    cloud139_config: dict[str, Any] | None = None,
) -> str:
    """Map an upload path using V1 category roots only."""

    del backend, cloud139_config
    value = normalize_storage_path(path)
    if not value:
        return ""
    source_root = _canonical_category_root(category)
    visible_root = _canonical_category_root(category, organizer=True)
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


__all__ = [
    "LegacyStoragePathError",
    "cmcc_upload_root",
    "map_upload_path_to_openlist",
    "normalize_sixpan_path_sources",
    "normalize_storage_path",
    "resolve_local_target",
    "openlist_root_for_upload",
    "rclone_upload_root",
    "upload_backend",
]
