from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlparse

from .config import (
    AppConfig,
    _deep_merge,
    _ensure_pansou_preferences,
    _ensure_storage_config,
    normalize_result_provider_badges,
)
from .constants import (
    CATEGORY_ANIME,
    CATEGORY_MOVIE,
    CATEGORY_OTHER,
    CATEGORY_TV,
    CATEGORY_VARIETY,
)
from .storage_paths import normalize_sixpan_path_sources


ADVANCED_CONFIG_KEY = "advanced_config"
ADVANCED_CONFIG_EXPORT_FORMAT = "fnos-media-import/advanced-config"
ADVANCED_CONFIG_EXPORT_VERSION = 1
_ADVANCED_DOCUMENT_METADATA_FIELDS = frozenset(
    {"format", "version", "stored", "effective", "exported_at", "source"}
)

CATEGORY_KEYS = [CATEGORY_MOVIE, CATEGORY_TV, CATEGORY_ANIME, CATEGORY_VARIETY, CATEGORY_OTHER]

# Fields from pre-V1 configuration documents.  New imports and runtime reads
# fail with an actionable path.  Existing database values are filtered only by
# the v29 offline migration so the running application never maintains a
# second, compatibility configuration model.
DEPRECATED_ADVANCED_FIELDS: dict[str, frozenset[str]] = {
    "quark": frozenset({"auto_save_url", "token", "check_before_save", "run_immediately"}),
    "cloud189": frozenset({"auto_save_url", "endpoint", "token", "timeout", "refresh_after_submit"}),
    "cmcc_upload": frozenset({
        "enabled", "backend", "mode", "rename_mode", "host", "auth_mode",
        "access_token", "authorization", "phone", "put_timeout",
    }),
    "cloud139": frozenset({
        "target_root_path", "cloud_root_path", "save_root_path", "root_path",
        "fnos_mount_name", "mount_name", "timeout", "check_before_save",
        "create_folder_if_missing", "refresh_after_submit", "refresh_delay_seconds",
        "mark_done_after_submit", "token", "endpoint", "auto_save_url",
    }),
    "openlist": frozenset({"base_url", "token"}),
    "sixpan": frozenset({
        "api_url", "endpoint", "host", "fnos_mount_name", "openlist_mount_name",
        "mount_name", "mount_path", "client_id", "client_secret", "oauth_client_id",
        "access_token", "refresh_token", "token",
    }),
    "categories": frozenset({
        "mobile_target_path", "cloud139_target_path", "cloud139_fnos_target_path",
        "cloud139_folder_id", "cmcc_parent_file_id", "cmcc_parent_path",
        "sixpan_fnos_target_path", "sixpan_mount_path", "sixpan_fnos_dir_list",
        "openlist_root_path", "quark_save_path", "sixpan_save_path",
    }),
}
ADVANCED_CONFIG_SECTIONS = (
    "pansou",
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
    "categories",
    "rclone",
    "storage",
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
    "phone",
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
        "result_provider_priority": "provider_priority",
        "result_provider_badges": "provider_badges",
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
        "connect_timeout": "float",
        "read_timeout": "float",
        "request_retries": "int",
        "retry_delay_seconds": "float",
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
        "callbacks": "list",
        "refresh_after_submit": "bool",
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
        "staging_retry_delay_seconds": "int",
        "staging_retry_max_delay_seconds": "int",
        "staging_retry_max_attempts": "int",
    },
}

CATEGORY_SCHEMA = {
    "fnos_lib": "str",
    "fnos_dir_list": "list",
}

# V1 targets and assignments are database resources.  The advanced storage
# section retains only process-level switches needed during startup.
STORAGE_TOP_FIELDS = frozenset({"enabled", "plan_version"})



def apply_persisted_config(app_config: AppConfig, settings: dict[str, Any]) -> AppConfig:
    """把数据库高级配置合并到运行时配置。

    加载顺序为：默认值 / config.yaml / .env -> 数据库高级配置。
    app.database_path、管理员登录账号等启动级配置不在这里覆盖，避免找不到数据库或锁死后台。
    """

    persisted = persisted_config_from_settings(settings)
    if not persisted:
        return app_config
    merged = _deep_merge(copy.deepcopy(app_config.raw), persisted)
    _ensure_storage_config(merged)
    _ensure_pansou_preferences(merged)
    return AppConfig(raw=merged, base_dir=app_config.base_dir)


def persisted_config_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get(ADVANCED_CONFIG_KEY)
    if not isinstance(raw, dict):
        return {}
    # The database stores the canonical V1 *stored section map*, not an
    # exported document envelope.  Refuse an accidentally persisted export
    # (or any pre-V1 wrapper) instead of silently filtering it to an empty
    # configuration and losing credentials at runtime.
    if _ADVANCED_DOCUMENT_METADATA_FIELDS.intersection(raw):
        raise ValueError(
            "数据库高级配置不是 V1 存储覆盖项；请清空该项并通过版本化配置文件重新录入"
        )
    _reject_deprecated_advanced_paths(raw)
    _reject_unknown_advanced_paths(raw)
    canonical = copy.deepcopy(raw)
    pansou = canonical.get("pansou")
    if isinstance(pansou, dict) and "result_provider_badges" in pansou:
        # A short-lived frontend build accepted arbitrary 2-5 character badge
        # labels. Keep startup compatible with those already stored values,
        # but disable the badge and fall back to a safe Chinese default. New
        # update/import payloads remain strict and reject the same value.
        pansou["result_provider_badges"] = normalize_result_provider_badges(
            pansou.get("result_provider_badges"),
            strict=True,
            coerce_invalid_text=True,
        )
    return sanitize_advanced_config(
        canonical,
        current={},
        preserve_secret_placeholders=False,
    )


def migrate_stored_advanced_config(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Filter a pre-V1 database override to the canonical V1 schema.

    This compatibility path is deliberately limited to values already stored
    in the application database.  Versioned import payloads continue to use
    strict validation and therefore cannot silently reintroduce removed keys.
    Returned diagnostics contain paths only and never configuration values.
    """

    if not isinstance(raw, dict):
        return {}, []
    if _ADVANCED_DOCUMENT_METADATA_FIELDS.intersection(raw):
        raise ValueError(
            "数据库高级配置不是 V1 存储覆盖项；请清空该项并通过版本化配置文件重新录入"
        )
    cleaned = _filter_known_advanced_config(raw)
    return cleaned, _removed_config_paths(raw, cleaned)


def _removed_config_paths(
    original: dict[str, Any],
    cleaned: dict[str, Any],
    prefix: str = "",
) -> list[str]:
    removed: list[str] = []
    for key, value in original.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in cleaned:
            removed.append(path)
            continue
        cleaned_value = cleaned.get(key)
        if isinstance(value, dict) and isinstance(cleaned_value, dict):
            removed.extend(_removed_config_paths(value, cleaned_value, path))
    return removed


def normalize_advanced_config_payload(payload: dict[str, Any], current_stored: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("高级配置格式不正确")
    current = current_stored or {}
    if not isinstance(current, dict):
        raise ValueError("数据库高级配置必须是对象")
    _reject_deprecated_advanced_paths(current)
    _reject_unknown_advanced_paths(current)
    if current:
        current = persisted_config_from_settings({ADVANCED_CONFIG_KEY: current})
    requested_mode = str(payload.get("mode") or "").strip().lower()
    # Imports and updates use one versioned document shape.  Bare section
    # dictionaries were accepted by the pre-V1 UI and are intentionally
    # rejected rather than guessed as a merge patch.  Reject an ambiguous
    # payload that mixes the wrapper and document fields; otherwise a caller
    # could accidentally update a different section than the one it showed.
    has_document_fields = bool({"format", "version"}.intersection(payload))
    has_wrapper = "config" in payload
    if has_document_fields and has_wrapper:
        raise ValueError("高级配置请求不能同时包含顶层版本字段和 config 包装层")
    if has_wrapper:
        unknown_wrapper_fields = sorted(
            set(payload) - {"config", "mode", "scope", "clear_secrets", "source"}
        )
        if unknown_wrapper_fields:
            preview = "、".join(unknown_wrapper_fields[:8])
            suffix = " 等" if len(unknown_wrapper_fields) > 8 else ""
            raise ValueError(f"高级配置请求包含不支持的字段：{preview}{suffix}")
    config_payload = payload if has_document_fields else payload.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("高级配置内容必须是对象")
    if "format" not in config_payload or "version" not in config_payload:
        raise ValueError(
            "高级配置必须是带 format 和 version 的版本化配置文件；请先导出当前配置"
        )
    export_format = str(config_payload.get("format") or "").strip()
    if export_format != ADVANCED_CONFIG_EXPORT_FORMAT:
        raise ValueError("配置文件格式标识不受支持")
    unknown_document_fields = sorted(set(config_payload) - _ADVANCED_DOCUMENT_METADATA_FIELDS)
    if unknown_document_fields:
        preview = "、".join(unknown_document_fields[:8])
        suffix = " 等" if len(unknown_document_fields) > 8 else ""
        raise ValueError(f"配置文件包含不支持的 envelope 字段：{preview}{suffix}")
    if isinstance(config_payload.get("version"), bool):
        raise ValueError("配置文件版本不正确")
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
    for scope_name in ("stored", "effective"):
        if scope_name in config_payload and not isinstance(config_payload[scope_name], dict):
            raise ValueError(f"配置文件中的 {scope_name} 必须是对象")
    config_payload = config_payload.get(import_scope)
    if not isinstance(config_payload, dict):
        raise ValueError(f"配置文件缺少可导入的 {import_scope} 配置")

    mode = requested_mode or "replace"
    if mode not in {"merge", "replace"}:
        raise ValueError("配置保存模式只能是 merge 或 replace")

    _reject_deprecated_advanced_paths(config_payload)
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
    allowed_clear = _allowed_secret_paths()
    invalid_clear_paths = sorted(set(clear_secrets) - allowed_clear)
    if invalid_clear_paths:
        preview = "、".join(invalid_clear_paths[:8])
        raise ValueError(f"不能清除非密钥字段：{preview}")

    normalized = sanitize_advanced_config(
        config_payload,
        current=current if mode == "merge" else {},
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
        return "invalid"
    requested = str(payload.get("mode") or "").strip().lower()
    if requested in {"merge", "replace"}:
        return requested
    config_payload = payload if {"format", "version"}.intersection(payload) else payload.get("config")
    if isinstance(config_payload, dict) and {
        "format", "version"
    }.issubset(config_payload):
        return "replace"
    return "invalid"


def _reject_deprecated_advanced_paths(config: dict[str, Any]) -> None:
    """Raise for removed pre-V1 fields instead of silently filtering them."""

    if not isinstance(config, dict):
        return
    invalid: list[str] = []
    for section, fields in DEPRECATED_ADVANCED_FIELDS.items():
        value = config.get(section)
        if not isinstance(value, dict):
            if value is not None and section not in SECTION_SCHEMA:
                invalid.append(section)
            continue
        # A removed section (quark/cloud189/cmcc_upload/cloud139) is invalid
        # even when an unknown field is supplied; report the concrete fields
        # where possible for a useful migration error.
        if section not in SECTION_SCHEMA and section not in {"categories"}:
            invalid.extend(f"{section}.{field}" for field in value if field in fields)
            if not any(field in fields for field in value):
                invalid.append(section)
            continue
        if section == "categories":
            for category_key, category in value.items():
                if not isinstance(category, dict):
                    continue
                invalid.extend(
                    f"categories.{category_key}.{field}"
                    for field in category
                    if field in fields
                )
            continue
        invalid.extend(f"{section}.{field}" for field in value if field in fields)

    storage = config.get("storage")
    if isinstance(storage, dict):
        # Targets, default selection and their locators are V1 database
        # resources.  Reject the entire pre-V1 declarative registry rather
        # than accepting a subset and accidentally creating a second source
        # of truth.
        for field in ("targets", "default_target"):
            if field in storage:
                invalid.append(f"storage.{field}")
    if "routes" in config:
        invalid.append("routes")
    if invalid:
        preview = "、".join(sorted(set(invalid))[:8])
        suffix = " 等" if len(set(invalid)) > 8 else ""
        raise ValueError(f"配置包含已删除的旧版字段：{preview}{suffix}")


def _reject_unknown_advanced_paths(config: dict[str, Any]) -> None:
    """Reject unknown fields in a newly submitted V1 configuration document."""

    if not isinstance(config, dict):
        return
    unknown_paths = _unknown_advanced_config_paths(config)
    if not unknown_paths:
        return
    preview = "、".join(unknown_paths[:8])
    suffix = " 等" if len(unknown_paths) > 8 else ""
    raise ValueError(f"配置包含不支持的字段：{preview}{suffix}")


def _unknown_advanced_config_paths(config: dict[str, Any]) -> list[str]:
    allowed_sections = set(SECTION_SCHEMA) | {"categories", "storage"}
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

    unknown.extend(_validate_storage_config_paths(config))
    return sorted(set(unknown))


def _validate_storage_config_paths(config: dict[str, Any]) -> list[str]:
    """Validate only process-level storage switches.

    Provider targets, locators and assignments are database V1 resources; an
    advanced-config document may not carry a declarative ``targets`` map.
    """

    unknown: list[str] = []
    storage = config.get("storage")
    if storage is None:
        return unknown
    if not isinstance(storage, dict):
        return ["storage"]
    for field in storage:
        if field not in STORAGE_TOP_FIELDS:
            unknown.append(f"storage.{field}")
    plan_version = storage.get("plan_version")
    if plan_version is not None:
        try:
            parsed_plan_version = int(plan_version)
        except (TypeError, ValueError):
            unknown.append("storage.plan_version")
        else:
            if parsed_plan_version != 1:
                unknown.append("storage.plan_version")
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
    return result
def sanitize_advanced_config(
    payload: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
    preserve_secret_placeholders: bool = False,
) -> dict[str, Any]:
    _reject_deprecated_advanced_paths(payload)
    _reject_deprecated_advanced_paths(current or {})
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

    storage_payload = payload.get("storage")
    if isinstance(storage_payload, dict):
        current_storage = result.get("storage") if isinstance(result.get("storage"), dict) else {}
        result["storage"] = _sanitize_storage_config(
            storage_payload,
            current=current_storage,
        )

    result = _prune_empty_sections(result)
    normalize_sixpan_path_sources(result, materialize_category_paths=False)
    return _prune_empty_sections(result)


def _sanitize_storage_config(
    storage_payload: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """清洗仅用于启动的 storage 开关；目标注册表由 V1 数据库维护。"""

    storage_result: dict[str, Any] = copy.deepcopy(current) if isinstance(current, dict) else {}
    if "enabled" in storage_payload:
        storage_result["enabled"] = _to_bool(storage_payload.get("enabled"), True)
    if "plan_version" in storage_payload:
        storage_result["plan_version"] = _to_int(storage_payload.get("plan_version"), 1)
    return storage_result


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

    storage = config.get("storage")
    if isinstance(storage, dict):
        storage_result = {
            field: copy.deepcopy(storage[field])
            for field in STORAGE_TOP_FIELDS
            if field in storage
        }
        if storage_result:
            result["storage"] = storage_result

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
    if field_type == "provider_priority":
        return _to_provider_priority(value), True
    if field_type == "provider_badges":
        return normalize_result_provider_badges(value, strict=True), True
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
    return list(dict.fromkeys(result))


def _to_provider_priority(value: Any) -> list[str]:
    fallback = ["quark", "mobile", "tianyi", "magnet"]
    values = _to_list(value)
    result = [item.lower() for item in values if item.lower() in fallback]
    result = list(dict.fromkeys(result))
    result.extend(item for item in fallback if item not in result)
    return result


def _prune_empty_sections(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            cleaned = _prune_empty_sections(value)
            if cleaned:
                result[key] = cleaned
        elif value is not None:
            result[key] = value
    return result
