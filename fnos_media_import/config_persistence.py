from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig, _deep_merge
from .constants import (
    CATEGORY_ANIME,
    CATEGORY_LABELS,
    CATEGORY_MOVIE,
    CATEGORY_OTHER,
    CATEGORY_TV,
    CATEGORY_VARIETY,
    ROUTE_CLOUD139_DIRECT,
    ROUTE_CLOUD189_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
)
from .storage_paths import normalize_sixpan_path_sources


ADVANCED_CONFIG_KEY = "advanced_config"
ADVANCED_CONFIG_EXPORT_FORMAT = "fnos-media-import/advanced-config"
ADVANCED_CONFIG_EXPORT_VERSION = 1

CATEGORY_KEYS = [CATEGORY_MOVIE, CATEGORY_TV, CATEGORY_ANIME, CATEGORY_VARIETY, CATEGORY_OTHER]
ROUTE_DEFAULTS = {
    "quark": ROUTE_QUARK_TO_MOBILE,
    "uc": ROUTE_QUARK_TO_MOBILE,
    "cloud139": ROUTE_CLOUD139_DIRECT,
    "cloud189": ROUTE_CLOUD189_DIRECT,
    "magnet": ROUTE_SIXPAN_OFFLINE,
    "torrent": ROUTE_SIXPAN_OFFLINE,
}

ADVANCED_CONFIG_SECTIONS = (
    "pansou",
    "quark",
    "cloud139",
    "cmcc_upload",
    "openlist",
    "tmdb",
    "ai",
    "content_review",
    "btbtla",
    "update_scheduler",
    "hot_discovery",
    "organizer",
    "sixpan",
    "fnos",
    "routes",
    "categories",
    "rclone",
)

SENSITIVE_FIELDS = {
    "password",
    "token",
    "default_token",
    "api_key",
    "secret",
    "access_token",
    "refresh_token",
    "client_secret",
    "secret_key",
    "channel_base64",
    "trace_base64",
    "app_key",
    "proxy_url",
}
SECRET_PLACEHOLDERS = {"", "***", "******", "已配置，留空不修改"}
MASKED_SECRET_PLACEHOLDERS = SECRET_PLACEHOLDERS - {""}


def _allowed_secret_paths() -> set[str]:
    return {
        f"{section}.{field}"
        for section, schema in SECTION_SCHEMA.items()
        for field, field_type in schema.items()
        if field_type == "secret"
    }

SECTION_SCHEMA: dict[str, dict[str, str]] = {
    "pansou": {
        "base_url": "str",
        "username": "str",
        "password": "secret",
        "default_token": "secret",
        "cloud_types": "cloud_types",
        "res": "str",
        "src": "str",
        "conc": "int",
        "refresh": "bool",
        "channels": "list",
        "plugins": "list",
        "filter_include": "list",
        "filter_exclude": "list",
        "async_poll_enabled": "bool",
        "async_poll_interval_seconds": "float",
        "async_poll_max_rounds": "int",
        "async_poll_stable_rounds": "int",
        "timeout": "int",
    },
    "quark": {
        "auto_save_url": "str",
        "token": "secret",
        "check_before_save": "bool",
        "run_immediately": "bool",
    },
    "cloud139": {
        "check_before_save": "bool",
        "create_folder_if_missing": "bool",
        "refresh_after_submit": "bool",
        "refresh_delay_seconds": "int",
        "mark_done_after_submit": "bool",
    },
    "cmcc_upload": {
        "enabled": "bool",
        "backend": "str",
        "mode": "str",
        "rename_mode": "str",
        "host": "str",
        "auth_mode": "str",
        "access_token": "secret",
        "phone": "str",
        "put_timeout": "int",
    },
    "update_scheduler": {
        "enabled": "bool",
        "interval_seconds": "int",
        "run_lease_seconds": "int",
        "max_subscriptions_per_tick": "int",
        "max_episodes_per_run": "int",
        "coalesce_missed_runs": "bool",
        "preview_cache_ttl_seconds": "int",
        "negative_preview_cache_ttl_seconds": "int",
        "snapshot_ttl_seconds": "int",
        "empty_retry_enabled": "bool",
        "empty_retry_interval_minutes": "int",
        "empty_retry_max_attempts": "int",
        "empty_retry_max_window_hours": "int",
        "failure_retry_interval_minutes": "int",
        "pending_import_check_interval_minutes": "int",
        "source_health_warn_threshold": "int",
        "tmdb_probe_lead_minutes": "int",
    },
    "hot_discovery": {
        "enabled": "bool",
        "run_at": "daily_time",
        "timezone": "str",
        "timeout": "int",
        "max_items_per_source": "int",
        "tencent_enabled": "bool",
        "tencent_endpoint": "str",
        "tencent_data_version": "str",
        "iqiyi_enabled": "bool",
        "iqiyi_endpoint": "str",
        "iqiyi_device_id": "str",
        "iqiyi_version": "str",
        "youku_enabled": "bool",
        "youku_url": "str",
    },
    "openlist": {
        "base_url": "str",
        "token": "secret",
        "timeout": "int",
        "batch_timeout": "int",
        "list_refresh_default": "bool",
        "verify_tls": "bool",
        "use_env_proxy": "bool",
    },
    "tmdb": {
        "token": "secret",
        "language": "str",
        "use_env_proxy": "bool",
        "proxy_enabled": "bool",
        "proxy_url": "secret",
    },
    "ai": {
        "enabled": "bool",
        "api_style": "str",
        "base_url": "str",
        "api_key": "secret",
        "model": "str",
        "timeout": "int",
    },
    "content_review": {
        "enabled": "bool",
        "keyword_enabled": "bool",
        "keyword_file": "str",
        "keywords": "list",
        "min_keyword_length": "int",
        "bt_ai_enabled": "bool",
        "bt_ai_pass_score": "int",
        "bt_ai_retries": "int",
        "bt_parse_failure_review": "bool",
        "bt_ai_failure_review": "bool",
        "max_files_for_ai": "int",
    },
    "btbtla": {
        "base_url": "str",
        "timeout": "int",
        "max_results": "int",
        "max_detail_resources": "int",
        "request_retries": "int",
        "retry_delay_seconds": "float",
        "verify_tls": "bool",
        "use_env_proxy": "bool",
        "proxy_enabled": "bool",
        "proxy_url": "secret",
        "user_agent": "str",
    },
    "organizer": {
        "enabled": "bool",
        "staging_enabled": "bool",
        "staging_dir_name": "str",
        "stable_window_seconds": "int",
        "auto_apply_confidence": "int",
        "max_scan_depth": "int",
        "max_files_per_task": "int",
        "bulk_operations_enabled": "bool",
        "regex_rename_min_items": "int",
        "bulk_reconcile_timeout_seconds": "int",
        "refresh_fnos_after_apply": "bool",
        "refresh_delay_seconds": "int",
        "cleanup_empty_dirs": "bool",
        "strm_refresh_after_apply": "bool",
        "strm_cleanup_old_before_refresh": "bool",
        "strm_refresh_prefix": "str",
        "strm_refresh_prefix_movie": "str",
        "strm_refresh_prefix_tv": "str",
        "strm_refresh_prefix_anime": "str",
        "strm_refresh_prefix_variety": "str",
        "local_strm_root": "str",
    },
    "sixpan": {
        "host": "str",
        "fnos_mount_name": "str",
        "openlist_mount_name": "str",
        "mount_name": "str",
        "mount_path": "str",
        "client_id": "str",
        "client_secret": "secret",
        "access_token": "secret",
        "refresh_token": "secret",
        "timeout": "int",
        "verify_tls": "bool",
        "parse_before_add": "bool",
        "parse_required": "bool",
        "parse_cache_ttl_seconds": "int",
        "parse_cache_max_entries": "int",
        "poll_enabled": "bool",
        "poll_interval_seconds": "int",
        "task_poll_limit": "int",
        "task_max_pages": "int",
        "task_missing_poll_limit": "int",
        "task_unknown_poll_limit": "int",
        "submitted_timeout_seconds": "int",
        "success_statuses": "list",
        "failed_statuses": "list",
        "running_statuses": "list",
    },
    "fnos": {
        "server_url": "str",
        "username": "str",
        "password": "secret",
        "api_key": "secret",
        "secret": "secret",
        "token": "secret",
    },
    "rclone": {
        "enabled": "bool",
        "remote_name": "str",
        "upload_backend": "str",
        "auto_interval_minutes": "int",
        "staging_retry_delay_seconds": "int",
        "staging_retry_max_delay_seconds": "int",
        "staging_retry_max_attempts": "int",
    },
}

CATEGORY_SCHEMA = {
    "label": "str",
    "quark_save_path": "str",
    "sixpan_save_path": "str",
    "sixpan_fnos_target_path": "str",
    "sixpan_mount_path": "str",
    "sixpan_fnos_dir_list": "list",
    "mobile_target_path": "str",
    "cloud139_target_path": "str",
    "cloud139_fnos_target_path": "str",
    "cloud139_folder_id": "str",
    "cmcc_parent_file_id": "str",
    "cmcc_parent_path": "str",
    "openlist_root_path": "str",
    "strm_fnos_dir_list": "list",
    "fnos_lib": "str",
    "fnos_dir_list": "list",
}


def apply_persisted_config(app_config: AppConfig, settings: dict[str, Any]) -> AppConfig:
    """把数据库高级配置合并到运行时配置。

    加载顺序为：默认值 / config.yaml / .env -> 数据库高级配置。
    app.database_path、管理员登录账号等启动级配置不在这里覆盖，避免找不到数据库或锁死后台。
    """

    persisted = persisted_config_from_settings(settings)
    if not persisted:
        return app_config
    merged = _deep_merge(copy.deepcopy(app_config.raw), persisted)
    _ensure_derived_config(merged)
    return AppConfig(raw=merged, base_dir=app_config.base_dir)


def persisted_config_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get(ADVANCED_CONFIG_KEY)
    if not isinstance(raw, dict):
        return {}
    return sanitize_advanced_config(raw, current={}, preserve_secret_placeholders=False)


def normalize_advanced_config_payload(payload: dict[str, Any], current_stored: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("高级配置格式不正确")
    requested_mode = str(payload.get("mode") or "").strip().lower()
    config_payload = payload.get("config", payload)
    if not isinstance(config_payload, dict):
        raise ValueError("高级配置内容必须是对象")

    is_export_document = "format" in config_payload or "version" in config_payload
    if is_export_document:
        export_format = str(config_payload.get("format") or "").strip()
        if export_format != ADVANCED_CONFIG_EXPORT_FORMAT:
            raise ValueError("配置文件格式标识不受支持")
        try:
            export_version = int(config_payload.get("version"))
        except (TypeError, ValueError) as exc:
            raise ValueError("配置文件版本不正确") from exc
        if export_version != ADVANCED_CONFIG_EXPORT_VERSION:
            raise ValueError(
                f"配置文件版本 {export_version} 不受支持，当前仅支持版本 {ADVANCED_CONFIG_EXPORT_VERSION}"
            )
        import_scope = str(payload.get("scope") or "stored").strip().lower()
        if import_scope not in {"stored", "effective"}:
            raise ValueError("配置导入范围只能是 stored 或 effective")
        config_payload = config_payload.get(import_scope)
        if not isinstance(config_payload, dict):
            raise ValueError(f"配置文件缺少可导入的 {import_scope} 配置")

    mode = requested_mode or ("replace" if is_export_document else "merge")
    if mode not in {"merge", "replace"}:
        raise ValueError("配置保存模式只能是 merge 或 replace")

    unknown_paths = _unknown_advanced_config_paths(config_payload)
    if unknown_paths:
        preview = "、".join(unknown_paths[:8])
        suffix = " 等" if len(unknown_paths) > 8 else ""
        raise ValueError(f"配置包含不支持的字段：{preview}{suffix}")
    if mode == "replace":
        masked_paths = _masked_secret_paths(config_payload)
        if masked_paths:
            preview = "、".join(masked_paths[:8])
            suffix = " 等" if len(masked_paths) > 8 else ""
            raise ValueError(f"覆盖导入不能使用脱敏占位符，请提供真实密钥或空值以清除：{preview}{suffix}")

    clear_secrets = payload.get("clear_secrets", [])
    if clear_secrets in (None, ""):
        clear_secrets = []
    if not isinstance(clear_secrets, list) or any(not isinstance(path, str) for path in clear_secrets):
        raise ValueError("待清除的密钥字段格式不正确")
    invalid_clear_paths = sorted(set(clear_secrets) - _allowed_secret_paths())
    if invalid_clear_paths:
        preview = "、".join(invalid_clear_paths[:8])
        raise ValueError(f"不能清除非密钥字段：{preview}")

    normalized = sanitize_advanced_config(
        config_payload,
        current=(current_stored or {}) if mode == "merge" else {},
        preserve_secret_placeholders=mode == "merge",
    )
    for path in clear_secrets:
        section, field = path.split(".", 1)
        section_config = normalized.get(section)
        if isinstance(section_config, dict):
            section_config.pop(field, None)
    normalized = _prune_empty_sections(normalized)
    _validate_btbtla_proxy(normalized)
    _validate_tmdb_proxy(normalized)
    return normalized


def advanced_config_payload_mode(payload: dict[str, Any]) -> str:
    """Return the effective persistence mode without exposing configuration values."""

    if not isinstance(payload, dict):
        return "merge"
    requested = str(payload.get("mode") or "").strip().lower()
    if requested in {"merge", "replace"}:
        return requested
    config_payload = payload.get("config")
    if isinstance(config_payload, dict) and ("format" in config_payload or "version" in config_payload):
        return "replace"
    return "merge"


def _unknown_advanced_config_paths(config: dict[str, Any]) -> list[str]:
    allowed_sections = set(SECTION_SCHEMA) | {"routes", "categories"}
    unknown: list[str] = []
    for section in config:
        if section not in allowed_sections:
            unknown.append(str(section))

    for section, schema in SECTION_SCHEMA.items():
        section_payload = config.get(section)
        if not isinstance(section_payload, dict):
            if section_payload is not None:
                unknown.append(section)
            continue
        unknown.extend(f"{section}.{field}" for field in section_payload if field not in schema)

    routes = config.get("routes")
    if isinstance(routes, dict):
        for source_type, item in routes.items():
            if source_type not in ROUTE_DEFAULTS:
                unknown.append(f"routes.{source_type}")
                continue
            if not isinstance(item, dict):
                unknown.append(f"routes.{source_type}")
                continue
            unknown.extend(
                f"routes.{source_type}.{field}"
                for field in item
                if field not in {"enabled", "route"}
            )
    elif routes is not None:
        unknown.append("routes")

    categories = config.get("categories")
    if isinstance(categories, dict):
        for category_key, item in categories.items():
            if category_key not in CATEGORY_KEYS:
                unknown.append(f"categories.{category_key}")
                continue
            if not isinstance(item, dict):
                unknown.append(f"categories.{category_key}")
                continue
            unknown.extend(
                f"categories.{category_key}.{field}"
                for field in item
                if field not in CATEGORY_SCHEMA
            )
    elif categories is not None:
        unknown.append("categories")
    return sorted(set(unknown))


def _masked_secret_paths(config: dict[str, Any]) -> list[str]:
    masked: list[str] = []
    for section, schema in SECTION_SCHEMA.items():
        section_payload = config.get(section)
        if not isinstance(section_payload, dict):
            continue
        for field, field_type in schema.items():
            if field_type != "secret" or field not in section_payload:
                continue
            if str(section_payload.get(field) or "").strip() in MASKED_SECRET_PLACEHOLDERS:
                masked.append(f"{section}.{field}")
    return masked


def _validate_btbtla_proxy(config: dict[str, Any]) -> None:
    btbtla = config.get("btbtla") if isinstance(config.get("btbtla"), dict) else {}
    if not _to_bool(btbtla.get("proxy_enabled"), False):
        return
    proxy_url = str(btbtla.get("proxy_url") or "").strip()
    if not proxy_url:
        # 地址可能来自环境变量；数据库补丁中没有明文时由运行时配置继续校验。
        return
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("BT 独立代理地址必须使用 http、https、socks5 或 socks5h 格式")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("BT 独立代理端口必须是 1-65535 之间的数字") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("BT 独立代理端口必须是 1-65535 之间的数字")


def _validate_tmdb_proxy(config: dict[str, Any]) -> None:
    tmdb = config.get("tmdb") if isinstance(config.get("tmdb"), dict) else {}
    if not _to_bool(tmdb.get("proxy_enabled"), False):
        return
    proxy_url = str(tmdb.get("proxy_url") or "").strip()
    if not proxy_url:
        # 地址可能来自环境变量；数据库补丁中没有明文时由运行时配置继续校验。
        return
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("TMDB 独立代理地址必须使用 http、https、socks5 或 socks5h 格式")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("TMDB 独立代理端口必须是 1-65535 之间的数字") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("TMDB 独立代理端口必须是 1-65535 之间的数字")


def advanced_config_response(effective_config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    stored = persisted_config_from_settings(settings)
    effective = advanced_config_subset(effective_config)
    return {
        "config": redact_advanced_config(effective),
        "stored": redact_advanced_config(stored),
        "meta": {
            "database_configured": bool(stored),
            "stored_sections": sorted(stored.keys()),
            "config_key": ADVANCED_CONFIG_KEY,
            "restart_required": False,
            "note": "除启动级配置外，高级配置保存后会写入数据库并立即重载运行时服务。",
        },
    }


def advanced_config_subset(config: dict[str, Any]) -> dict[str, Any]:
    result = _filter_known_advanced_config(config)
    for section in ADVANCED_CONFIG_SECTIONS:
        result.setdefault(section, {})
    result.setdefault("routes", {})
    for key, route in ROUTE_DEFAULTS.items():
        result["routes"].setdefault(key, {"enabled": False, "route": route})
    result.setdefault("categories", {})
    for key in CATEGORY_KEYS:
        category = result["categories"].setdefault(key, {})
        category.setdefault("label", CATEGORY_LABELS.get(key, key))
    return result


def sanitize_advanced_config(
    payload: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
    preserve_secret_placeholders: bool = False,
) -> dict[str, Any]:
    result = _filter_known_advanced_config(current or {})

    for section, schema in SECTION_SCHEMA.items():
        section_payload = payload.get(section)
        if not isinstance(section_payload, dict):
            continue
        section_result = result.setdefault(section, {})
        if not isinstance(section_result, dict):
            section_result = {}
            result[section] = section_result
        for field, field_type in schema.items():
            if field not in section_payload:
                continue
            converted, should_set = _convert_value(section_payload.get(field), field_type, preserve_secret_placeholders)
            if should_set:
                section_result[field] = converted

    routes_payload = payload.get("routes")
    if isinstance(routes_payload, dict):
        route_result = result.setdefault("routes", {})
        if not isinstance(route_result, dict):
            route_result = {}
            result["routes"] = route_result
        for source_type, default_route in ROUTE_DEFAULTS.items():
            item_payload = routes_payload.get(source_type)
            if not isinstance(item_payload, dict):
                continue
            item_result = route_result.setdefault(source_type, {})
            if not isinstance(item_result, dict):
                item_result = {}
                route_result[source_type] = item_result
            if "enabled" in item_payload:
                item_result["enabled"] = _to_bool(item_payload.get("enabled"), False)
            if "route" in item_payload:
                route_value = str(item_payload.get("route") or default_route).strip() or default_route
                item_result["route"] = route_value
            else:
                item_result.setdefault("route", default_route)

    categories_payload = payload.get("categories")
    if isinstance(categories_payload, dict):
        categories_result = result.setdefault("categories", {})
        if not isinstance(categories_result, dict):
            categories_result = {}
            result["categories"] = categories_result
        for category_key in CATEGORY_KEYS:
            category_payload = categories_payload.get(category_key)
            if not isinstance(category_payload, dict):
                continue
            category_result = categories_result.setdefault(category_key, {})
            if not isinstance(category_result, dict):
                category_result = {}
                categories_result[category_key] = category_result
            for field, field_type in CATEGORY_SCHEMA.items():
                if field not in category_payload:
                    continue
                converted, should_set = _convert_value(category_payload.get(field), field_type, preserve_secret_placeholders)
                if should_set:
                    category_result[field] = converted
            category_result.setdefault("label", CATEGORY_LABELS.get(category_key, category_key))

    result = _prune_empty_sections(result)
    normalize_sixpan_path_sources(result, materialize_category_paths=False)
    return _prune_empty_sections(result)


def _filter_known_advanced_config(config: dict[str, Any]) -> dict[str, Any]:
    """只保留后台会展示和允许持久化的字段，避免旧版高级配置越积越乱。"""

    result: dict[str, Any] = {}
    if not isinstance(config, dict):
        return result

    for section, schema in SECTION_SCHEMA.items():
        value = config.get(section)
        if not isinstance(value, dict):
            continue
        filtered = {field: copy.deepcopy(value[field]) for field in schema if field in value}
        if filtered:
            result[section] = filtered

    routes = config.get("routes")
    if isinstance(routes, dict):
        route_result: dict[str, Any] = {}
        for source_type, default_route in ROUTE_DEFAULTS.items():
            item = routes.get(source_type)
            if not isinstance(item, dict):
                continue
            route_result[source_type] = {
                "enabled": _to_bool(item.get("enabled"), False),
                "route": str(item.get("route") or default_route).strip() or default_route,
            }
        if route_result:
            result["routes"] = route_result

    categories = config.get("categories")
    if isinstance(categories, dict):
        categories_result: dict[str, Any] = {}
        for category_key in CATEGORY_KEYS:
            item = categories.get(category_key)
            if not isinstance(item, dict):
                continue
            filtered = {field: copy.deepcopy(item[field]) for field in CATEGORY_SCHEMA if field in item}
            if filtered:
                categories_result[category_key] = filtered
        if categories_result:
            result["categories"] = categories_result

    return result


def redact_advanced_config(config: dict[str, Any]) -> dict[str, Any]:
    def redact(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {child_key: redact(child_value, child_key) for child_key, child_value in value.items()}
        if key.lower() in SENSITIVE_FIELDS:
            return "***" if str(value or "").strip() else ""
        return value

    return redact(copy.deepcopy(config))


def _convert_value(value: Any, field_type: str, preserve_secret_placeholders: bool) -> tuple[Any, bool]:
    if field_type == "secret":
        text = str(value or "").strip()
        if preserve_secret_placeholders and text in SECRET_PLACEHOLDERS:
            return "", False
        return text, True
    if field_type == "bool":
        return _to_bool(value, False), True
    if field_type == "int":
        return _to_int(value, 0), True
    if field_type == "float":
        return _to_float(value, 0.0), True
    if field_type == "list":
        return _to_list(value), True
    if field_type == "cloud_types":
        return _to_cloud_types(value), True
    if field_type == "daily_time":
        text = str(value or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            raise ValueError("每日执行时间必须使用有效的 HH:MM 格式")
        return text, True
    return str(value or "").strip(), True


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = re.split(r"[,，\n|]+", str(value or ""))
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _to_cloud_types(value: Any) -> list[str]:
    allowed = {"quark", "tianyi", "mobile", "magnet"}
    values = _to_list(value)
    result = [item.lower() for item in values if item.lower() in allowed]
    return result or ["quark", "tianyi", "mobile", "magnet"]


def _prune_empty_sections(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            cleaned = _prune_empty_sections(value)
            if cleaned:
                result[key] = cleaned
        elif value not in (None, "", []):
            result[key] = value
    return result


def _ensure_derived_config(config: dict[str, Any]) -> None:
    sixpan = config.get("sixpan")
    if isinstance(sixpan, dict):
        if sixpan.get("api_url") and not sixpan.get("endpoint"):
            sixpan["endpoint"] = sixpan.get("api_url")
    normalize_sixpan_path_sources(config, materialize_category_paths=True)

    routes = config.setdefault("routes", {})
    if isinstance(routes, dict):
        for key, route in ROUTE_DEFAULTS.items():
            item = routes.setdefault(key, {})
            if isinstance(item, dict):
                item.setdefault("route", route)
