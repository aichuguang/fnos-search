from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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


BASE_DIR = Path(__file__).resolve().parent.parent
CMCC_DEFAULT_HOST = "https://personal-kd-njs.yun.139.com/hcy"
CMCC_DEFAULT_APP_ID = "2046144659771756544"
CMCC_DEFAULT_APP_NAME = "MClaw空间"
CMCC_DEFAULT_ACCESS_TOKEN_FILE = ""
CMCC_DEFAULT_CHANNEL_BASE64 = "J/0voJP3L9EKbQyz0uxQHkBZjjbZrNBJFp9XbT4/b9U="
CMCC_DEFAULT_TRACE_BASE64 = "7WokJK5IZVhod3UBMECBRA=="


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_csv(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


def _cloud_type_values(value: Any, default: list[str] | None = None) -> list[str]:
    allowed = {"quark", "tianyi", "mobile", "magnet"}
    items = value if isinstance(value, list) else str(value or "").split(",")
    result = []
    for item in items:
        text = str(item or "").strip().lower()
        if text in allowed and text not in result:
            result.append(text)
    return result or list(default or ["quark", "tianyi", "mobile", "magnet"])


def _env_cloud_types(name: str, default: list[str] | None = None) -> list[str]:
    return _cloud_type_values(_env_csv(name, default), default)


def _load_dotenv(path: Path) -> None:
    """加载简单 .env 文件。

    不覆盖已存在环境变量，方便 Docker / systemd 注入。
    """

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _default_config() -> dict[str, Any]:
    return {
        "app": {
            "name": "飞牛影视统一入库维护系统",
            "host": "0.0.0.0",
            "port": 5251,
            "debug": False,
            "database_path": "data/app.db",
            "secret_key": os.getenv("APP_SECRET_KEY", "change-me-in-production"),
        },
        "admin": {
            "username": os.getenv("ADMIN_USERNAME", "admin"),
            "password": os.getenv("ADMIN_PASSWORD", "admin"),
        },
        "submission": {
            "mode": os.getenv("SUBMIT_MODE", "auto"),
        },
        "public": {
            "allow_anonymous_search": _env_bool("PUBLIC_ALLOW_ANONYMOUS_SEARCH", True),
            "request_query_enabled": _env_bool("PUBLIC_REQUEST_QUERY_ENABLED", True),
            "hide_full_links": _env_bool("PUBLIC_HIDE_FULL_LINKS", True),
        },
        "security": {
            "strict": _env_bool("SECURITY_STRICT", _env_bool("FNOS_SECURITY_STRICT", False)),
            "trusted_proxy_count": _env_int("SECURITY_TRUSTED_PROXY_COUNT", 0),
            "admin_login_rate_limit": _env_int("SECURITY_ADMIN_LOGIN_RATE_LIMIT", 10),
            "session_cookie_secure": _env_bool("SECURITY_SESSION_COOKIE_SECURE", False),
            "session_cookie_samesite": os.getenv("SECURITY_SESSION_COOKIE_SAMESITE", "Lax"),
            "session_lifetime_seconds": _env_int("SECURITY_SESSION_LIFETIME_SECONDS", 43200),
            "csrf_enabled": _env_bool("SECURITY_CSRF_ENABLED", False),
            "rate_limit_enabled": _env_bool("SECURITY_RATE_LIMIT_ENABLED", True),
            "rate_limit_window_seconds": _env_int("SECURITY_RATE_LIMIT_WINDOW_SECONDS", 60),
            "public_search_rate_limit": _env_int("SECURITY_PUBLIC_SEARCH_RATE_LIMIT", 20),
            "public_detect_rate_limit": _env_int("SECURITY_PUBLIC_DETECT_RATE_LIMIT", 40),
            "public_submit_rate_limit": _env_int("SECURITY_PUBLIC_SUBMIT_RATE_LIMIT", 10),
            "public_request_rate_limit": _env_int("SECURITY_PUBLIC_REQUEST_RATE_LIMIT", 60),
            "public_captcha_rate_limit": _env_int("SECURITY_PUBLIC_CAPTCHA_RATE_LIMIT", 20),
            "max_keyword_length": _env_int("SECURITY_MAX_KEYWORD_LENGTH", 80),
            "max_title_length": _env_int("SECURITY_MAX_TITLE_LENGTH", 300),
            "max_request_body_bytes": _env_int("SECURITY_MAX_REQUEST_BODY_BYTES", 8 * 1024 * 1024),
            "max_public_json_bytes": _env_int("SECURITY_MAX_PUBLIC_JSON_BYTES", 256 * 1024),
            "max_url_length": _env_int("SECURITY_MAX_URL_LENGTH", 2048),
            "max_password_length": _env_int("SECURITY_MAX_PASSWORD_LENGTH", 32),
            "max_note_length": _env_int("SECURITY_MAX_NOTE_LENGTH", 500),
            "max_token_length": _env_int("SECURITY_MAX_TOKEN_LENGTH", 80),
            "duplicate_check_enabled": _env_bool("SECURITY_DUPLICATE_CHECK_ENABLED", True),
            "duplicate_window_minutes": _env_int("SECURITY_DUPLICATE_WINDOW_MINUTES", 1440),
            "ip_hash_salt": os.getenv("SECURITY_IP_HASH_SALT") or os.getenv("APP_SECRET_KEY", "change-me-in-production"),
            "allowed_url_schemes": _env_csv("SECURITY_ALLOWED_URL_SCHEMES", ["http", "https", "magnet"]),
            "allowed_share_domains": _env_csv("SECURITY_ALLOWED_SHARE_DOMAINS", []),
            "block_private_hosts": _env_bool("SECURITY_BLOCK_PRIVATE_HOSTS", True),
            "captcha_enabled": _env_bool("SECURITY_CAPTCHA_ENABLED", False),
            "captcha_ttl_seconds": _env_int("SECURITY_CAPTCHA_TTL_SECONDS", 300),
        },
        "pansou": {
            "base_url": os.getenv("PANSOU_BASE_URL", "http://127.0.0.1:8055"),
            "username": os.getenv("PANSOU_USERNAME", ""),
            "password": os.getenv("PANSOU_PASSWORD", ""),
            "default_token": os.getenv("PANSOU_DEFAULT_TOKEN", ""),
            "cloud_types": _env_cloud_types("PANSOU_CLOUD_TYPES", ["quark", "tianyi", "mobile", "magnet"]),
            "res": os.getenv("PANSOU_RES", "merge"),
            "src": os.getenv("PANSOU_SRC", "all"),
            "conc": _env_int("PANSOU_CONC", 10),
            "refresh": _env_bool("PANSOU_REFRESH", False),
            "channels": _env_csv("PANSOU_CHANNELS", []),
            "plugins": _env_csv("PANSOU_PLUGINS", []),
            "filter_include": _env_csv("PANSOU_FILTER_INCLUDE", []),
            "filter_exclude": _env_csv("PANSOU_FILTER_EXCLUDE", []),
            "async_poll_enabled": _env_bool("PANSOU_ASYNC_POLL_ENABLED", True),
            "async_poll_interval_seconds": _env_float("PANSOU_ASYNC_POLL_INTERVAL_SECONDS", 0.8),
            "async_poll_max_rounds": _env_int("PANSOU_ASYNC_POLL_MAX_ROUNDS", 2),
            "async_poll_stable_rounds": _env_int("PANSOU_ASYNC_POLL_STABLE_ROUNDS", 1),
            "timeout": _env_int("PANSOU_TIMEOUT", 20),
            "verify_tls": _env_bool("PANSOU_VERIFY_TLS", True),
        },
        "btbtla": {
            "base_url": os.getenv("BTBTLA_BASE_URL", "https://www.btbtla.com"),
            "timeout": _env_int("BTBTLA_TIMEOUT", 15),
            "max_results": _env_int("BTBTLA_MAX_RESULTS", 20),
            "max_detail_resources": _env_int("BTBTLA_MAX_DETAIL_RESOURCES", 80),
            "request_retries": _env_int("BTBTLA_REQUEST_RETRIES", 4),
            "retry_delay_seconds": _env_float("BTBTLA_RETRY_DELAY_SECONDS", 0.4),
            "verify_tls": _env_bool("BTBTLA_VERIFY_TLS", False),
            "use_env_proxy": _env_bool("BTBTLA_USE_ENV_PROXY", False),
            "proxy_enabled": _env_bool("BTBTLA_PROXY_ENABLED", False),
            "proxy_url": os.getenv("BTBTLA_PROXY_URL", ""),
            "user_agent": os.getenv(
                "BTBTLA_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            ),
        },
        "search": {
            "providers": {
                "pansou": {
                    "enabled": _env_bool("SEARCH_PANSOU_ENABLED", True),
                    "priority": _env_int("SEARCH_PANSOU_PRIORITY", 10),
                },
                "btbtla": {
                    "enabled": _env_bool("SEARCH_BTBTLA_ENABLED", True),
                    "priority": _env_int("SEARCH_BTBTLA_PRIORITY", 30),
                }
            },
            "aliases": {},
        },
        "content_review": {
            "enabled": _env_bool("CONTENT_REVIEW_ENABLED", True),
            "keyword_enabled": _env_bool("CONTENT_REVIEW_KEYWORD_ENABLED", True),
            "keyword_file": os.getenv("CONTENT_REVIEW_KEYWORD_FILE", "data/sensitive_words.txt"),
            "keywords": _env_csv("CONTENT_REVIEW_KEYWORDS", []),
            "min_keyword_length": _env_int("CONTENT_REVIEW_MIN_KEYWORD_LENGTH", 2),
            "bt_ai_enabled": _env_bool("CONTENT_REVIEW_BT_AI_ENABLED", True),
            "bt_ai_pass_score": _env_int("CONTENT_REVIEW_BT_AI_PASS_SCORE", 75),
            "bt_ai_retries": _env_int("CONTENT_REVIEW_BT_AI_RETRIES", 2),
            "bt_parse_failure_review": _env_bool("CONTENT_REVIEW_BT_PARSE_FAILURE_REVIEW", True),
            "bt_ai_failure_review": _env_bool("CONTENT_REVIEW_BT_AI_FAILURE_REVIEW", True),
            "max_files_for_ai": _env_int("CONTENT_REVIEW_MAX_FILES_FOR_AI", 80),
        },
        "quark": {
            "auto_save_url": os.getenv("QUARK_AUTO_SAVE_URL", "http://127.0.0.1:8015"),
            "token": os.getenv("QUARK_AUTO_SAVE_TOKEN", ""),
            "timeout": 20,
            "check_before_save": True,
            "run_immediately": True,
        },
        "cloud139": {
            "target_root_path": os.getenv("CLOUD139_TARGET_ROOT_PATH", os.getenv("CLOUD139_CLOUD_ROOT_PATH", "")),
            "fnos_mount_name": os.getenv("CLOUD139_FNOS_MOUNT_NAME", os.getenv("CLOUD139_MOUNT_NAME", "")),
            "timeout": 20,
            "check_before_save": _env_bool("CLOUD139_CHECK_BEFORE_SAVE", True),
            "create_folder_if_missing": _env_bool("CLOUD139_CREATE_FOLDER_IF_MISSING", True),
            "refresh_after_submit": _env_bool("CLOUD139_REFRESH_AFTER_SUBMIT", True),
            "refresh_delay_seconds": _env_int("CLOUD139_REFRESH_DELAY_SECONDS", 90),
            # 139 官方分享转存接口创建的是云端批处理任务，本项目没有独立的外部
            # worker 可持续追踪该任务；保存接口成功返回后默认视为已提交成功并进入
            # done，避免访客端长期停留在“处理中”。如需严格轮询可显式设为 false。
            "mark_done_after_submit": _env_bool("CLOUD139_MARK_DONE_AFTER_SUBMIT", True),
        },
        "cmcc_upload": {
            "enabled": _env_bool("CMCC_UPLOAD_ENABLED", True),
            "backend": os.getenv("RCLONE_UPLOAD_BACKEND", os.getenv("CMCC_UPLOAD_BACKEND", "cmcc_api")),
            "mode": os.getenv("CMCC_UPLOAD_MODE", "rapid_first"),
            "rename_mode": os.getenv("CMCC_UPLOAD_RENAME_MODE", "auto_rename"),
            "host": os.getenv("CMCC_HOST", os.getenv("CMCC_WEB_HOST", os.getenv("CM_CLOUD_HOST", CMCC_DEFAULT_HOST))),
            "auth_mode": os.getenv("CMCC_AUTH_MODE", "web_basic"),
            "access_token": os.getenv("CMCC_ACCESS_TOKEN", os.getenv("CM_CLOUD_ACCESS_TOKEN", "")),
            "phone": os.getenv("CMCC_PHONE", os.getenv("CM_CLOUD_PHONE", "")),
            "put_timeout": _env_int("CMCC_UPLOAD_PUT_TIMEOUT", 7200),
        },
        "openlist": {
            "base_url": os.getenv("OPENLIST_BASE_URL", "http://127.0.0.1:5244"),
            "token": os.getenv("OPENLIST_TOKEN", ""),
            "timeout": _env_int("OPENLIST_TIMEOUT", 30),
            "batch_timeout": _env_int("OPENLIST_BATCH_TIMEOUT", 600),
            "list_refresh_default": _env_bool("OPENLIST_LIST_REFRESH_DEFAULT", False),
            "verify_tls": _env_bool("OPENLIST_VERIFY_TLS", True),
            "use_env_proxy": _env_bool("OPENLIST_USE_ENV_PROXY", False),
        },
        "tmdb": {
            "token": os.getenv("TMDB_TOKEN", ""),
            "language": os.getenv("TMDB_LANGUAGE", "zh-CN"),
            "use_env_proxy": _env_bool("TMDB_USE_ENV_PROXY", True),
            "proxy_enabled": _env_bool("TMDB_PROXY_ENABLED", False),
            "proxy_url": os.getenv("TMDB_PROXY_URL", ""),
        },
        "ai": {
            "enabled": _env_bool("AI_ENABLED", False),
            "api_style": os.getenv("AI_API_STYLE", "auto"),
            "base_url": os.getenv("AI_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")),
            "api_key": os.getenv("AI_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            "model": os.getenv("AI_MODEL", "gpt-4.1-mini"),
            "timeout": _env_int("AI_TIMEOUT", 45),
        },
        "organizer": {
            "enabled": _env_bool("ORGANIZER_ENABLED", False),
            "staging_enabled": _env_bool("ORGANIZER_STAGING_ENABLED", True),
            "staging_dir_name": os.getenv("ORGANIZER_STAGING_DIR_NAME", "_入库暂存"),
            "stable_window_seconds": _env_int("ORGANIZER_STABLE_WINDOW_SECONDS", 90),
            "auto_apply_confidence": _env_int("ORGANIZER_AUTO_APPLY_CONFIDENCE", 85),
            "max_scan_depth": _env_int("ORGANIZER_MAX_SCAN_DEPTH", 8),
            "max_files_per_task": _env_int("ORGANIZER_MAX_FILES_PER_TASK", 500),
            "bulk_operations_enabled": _env_bool("ORGANIZER_BULK_OPERATIONS_ENABLED", True),
            "regex_rename_min_items": _env_int("ORGANIZER_REGEX_RENAME_MIN_ITEMS", 10),
            "bulk_reconcile_timeout_seconds": _env_int("ORGANIZER_BULK_RECONCILE_TIMEOUT_SECONDS", 120),
            "lock_wait_seconds": _env_int("ORGANIZER_LOCK_WAIT_SECONDS", 900),
            "lock_poll_seconds": _env_int("ORGANIZER_LOCK_POLL_SECONDS", 2),
            "refresh_fnos_after_apply": _env_bool("ORGANIZER_REFRESH_FNOS_AFTER_APPLY", False),
            "refresh_delay_seconds": _env_int("ORGANIZER_REFRESH_DELAY_SECONDS", 60),
            "cleanup_empty_dirs": _env_bool("ORGANIZER_CLEANUP_EMPTY_DIRS", False),
            "strm_refresh_after_apply": _env_bool("ORGANIZER_STRM_REFRESH_AFTER_APPLY", True),
            "strm_cleanup_old_before_refresh": _env_bool("ORGANIZER_STRM_CLEANUP_OLD_BEFORE_REFRESH", False),
            "strm_refresh_prefix": os.getenv("ORGANIZER_STRM_REFRESH_PREFIX", ""),
            "strm_refresh_prefix_movie": os.getenv("ORGANIZER_STRM_REFRESH_PREFIX_MOVIE", ""),
            "strm_refresh_prefix_tv": os.getenv("ORGANIZER_STRM_REFRESH_PREFIX_TV", ""),
            "strm_refresh_prefix_anime": os.getenv("ORGANIZER_STRM_REFRESH_PREFIX_ANIME", ""),
            "strm_refresh_prefix_variety": os.getenv("ORGANIZER_STRM_REFRESH_PREFIX_VARIETY", ""),
            "local_strm_root": os.getenv("ORGANIZER_LOCAL_STRM_ROOT", ""),
        },
        "update_scheduler": {
            "enabled": _env_bool("UPDATE_SCHEDULER_ENABLED", True),
            "interval_seconds": _env_int("UPDATE_SCHEDULER_INTERVAL_SECONDS", 60),
            "run_lease_seconds": _env_int("UPDATE_RUN_LEASE_SECONDS", 120),
            "max_subscriptions_per_tick": _env_int("UPDATE_SCHEDULER_MAX_SUBSCRIPTIONS_PER_TICK", 5),
            "max_episodes_per_run": _env_int("UPDATE_SCHEDULER_MAX_EPISODES_PER_RUN", 10),
            "coalesce_missed_runs": _env_bool("UPDATE_SCHEDULER_COALESCE_MISSED_RUNS", True),
            "preview_cache_ttl_seconds": _env_int("UPDATE_PREVIEW_CACHE_TTL_SECONDS", 6 * 60 * 60),
            "negative_preview_cache_ttl_seconds": _env_int("UPDATE_NEGATIVE_PREVIEW_CACHE_TTL_SECONDS", 10 * 60),
            "snapshot_ttl_seconds": _env_int("UPDATE_SNAPSHOT_TTL_SECONDS", 30 * 60),
            "empty_retry_enabled": _env_bool("UPDATE_EMPTY_RETRY_ENABLED", True),
            "empty_retry_interval_minutes": _env_int("UPDATE_EMPTY_RETRY_INTERVAL_MINUTES", 30),
            "empty_retry_max_attempts": _env_int("UPDATE_EMPTY_RETRY_MAX_ATTEMPTS", 4),
            "empty_retry_max_window_hours": _env_int("UPDATE_EMPTY_RETRY_MAX_WINDOW_HOURS", 12),
            "failure_retry_interval_minutes": _env_int("UPDATE_FAILURE_RETRY_INTERVAL_MINUTES", 30),
            "pending_import_check_interval_minutes": _env_int("UPDATE_PENDING_IMPORT_CHECK_INTERVAL_MINUTES", 30),
            "source_health_warn_threshold": _env_int("UPDATE_SOURCE_HEALTH_WARN_THRESHOLD", 4),
            "tmdb_probe_lead_minutes": _env_int("UPDATE_TMDB_PROBE_LEAD_MINUTES", 0),
        },
        "hot_discovery": {
            "enabled": _env_bool("HOT_DISCOVERY_ENABLED", False),
            "run_at": os.getenv("HOT_DISCOVERY_RUN_AT", "08:30"),
            "timezone": os.getenv("HOT_DISCOVERY_TIMEZONE", "Asia/Shanghai"),
            "timeout": _env_int("HOT_DISCOVERY_TIMEOUT", 20),
            "max_items_per_source": _env_int("HOT_DISCOVERY_MAX_ITEMS_PER_SOURCE", 20),
            "tencent_enabled": _env_bool("HOT_DISCOVERY_TENCENT_ENABLED", True),
            "tencent_endpoint": os.getenv("HOT_DISCOVERY_TENCENT_ENDPOINT", "https://pbaccess.video.qq.com/trpc.videosearch.hot_rank.HotRankServantHttp/HotRankHttp"),
            "tencent_data_version": os.getenv("HOT_DISCOVERY_TENCENT_DATA_VERSION", "25081802"),
            "iqiyi_enabled": _env_bool("HOT_DISCOVERY_IQIYI_ENABLED", True),
            "iqiyi_endpoint": os.getenv("HOT_DISCOVERY_IQIYI_ENDPOINT", "https://mesh.if.iqiyi.com/portal/lw/search/keywords/hotList"),
            "iqiyi_device_id": os.getenv("HOT_DISCOVERY_IQIYI_DEVICE_ID", ""),
            "iqiyi_version": os.getenv("HOT_DISCOVERY_IQIYI_VERSION", "17.072.25808"),
            "youku_enabled": _env_bool("HOT_DISCOVERY_YOUKU_ENABLED", True),
            "youku_url": os.getenv("HOT_DISCOVERY_YOUKU_URL", "https://www.youku.com/ku/webhome"),
        },
        "cloud189": {
            "auto_save_url": os.getenv("CLOUD189_AUTO_SAVE_URL", ""),
            "endpoint": os.getenv("CLOUD189_AUTO_SAVE_URL", ""),
            "token": os.getenv("CLOUD189_AUTO_SAVE_TOKEN", ""),
            "timeout": 20,
            "refresh_after_submit": True,
        },
        "sixpan": {
            "api_url": os.getenv("SIXPAN_API_URL", os.getenv("SIXPAN_HOST", "openapi.2dland.cn")),
            "endpoint": os.getenv("SIXPAN_API_URL", os.getenv("SIXPAN_HOST", "openapi.2dland.cn")),
            "host": os.getenv("SIXPAN_HOST", os.getenv("SIXPAN_API_URL", "openapi.2dland.cn")),
            "fnos_mount_name": os.getenv("SIXPAN_FNOS_MOUNT_NAME", os.getenv("SIXPAN_OPENLIST_MOUNT_NAME", os.getenv("SIXPAN_MOUNT_NAME", "\u6e05\u4e91"))),
            "client_id": os.getenv("SIXPAN_CLIENT_ID", ""),
            "client_secret": os.getenv("SIXPAN_CLIENT_SECRET", ""),
            "access_token": os.getenv("SIXPAN_ACCESS_TOKEN", os.getenv("SIXPAN_TOKEN", "")),
            "refresh_token": os.getenv("SIXPAN_REFRESH_TOKEN", ""),
            "token": os.getenv("SIXPAN_ACCESS_TOKEN", os.getenv("SIXPAN_TOKEN", "")),
            "timeout": _env_int("SIXPAN_TIMEOUT", 20),
            "verify_tls": _env_bool("SIXPAN_VERIFY_TLS", True),
            "parse_before_add": _env_bool("SIXPAN_PARSE_BEFORE_ADD", True),
            "parse_required": _env_bool("SIXPAN_PARSE_REQUIRED", False),
            "parse_cache_ttl_seconds": _env_int("SIXPAN_PARSE_CACHE_TTL_SECONDS", 300),
            "parse_cache_max_entries": _env_int("SIXPAN_PARSE_CACHE_MAX_ENTRIES", 128),
            "poll_enabled": _env_bool("SIXPAN_POLL_ENABLED", True),
            "poll_interval_seconds": _env_int("SIXPAN_POLL_INTERVAL_SECONDS", 60),
            "task_poll_limit": _env_int("SIXPAN_TASK_POLL_LIMIT", 200),
            "task_max_pages": _env_int("SIXPAN_TASK_MAX_PAGES", 200),
            "task_missing_poll_limit": _env_int("SIXPAN_TASK_MISSING_POLL_LIMIT", 5),
            "task_unknown_poll_limit": _env_int("SIXPAN_TASK_UNKNOWN_POLL_LIMIT", 5),
            "submitted_timeout_seconds": _env_int("SIXPAN_SUBMITTED_TIMEOUT_SECONDS", 604800),
            "success_statuses": _env_csv("SIXPAN_SUCCESS_STATUSES", ["completed", "done", "success", "finished", "3", "4", "100"]),
            "failed_statuses": _env_csv("SIXPAN_FAILED_STATUSES", ["failed", "error", "fail", "-1", "5", "6"]),
            "running_statuses": _env_csv(
                "SIXPAN_RUNNING_STATUSES",
                ["created", "new", "pending", "waiting", "queued", "ready", "running", "downloading", "processing", "retrying", "0", "1", "2"],
            ),
            "callbacks": _env_csv("SIXPAN_CALLBACK_URLS", []),
            "refresh_after_submit": False,
        },
        "fnos": {
            "server_url": os.getenv("FNOS_SERVER_URL", os.getenv("FNOS_BASE_URL", "")),
            "username": os.getenv("FNOS_USERNAME", ""),
            "password": os.getenv("FNOS_PASSWORD", ""),
            "api_key": os.getenv("FNOS_API_KEY", ""),
            "secret": os.getenv("FNOS_SECRET", os.getenv("FNOS_SECRET_STRING", "")),
            "app_name": os.getenv("FNOS_APP_NAME", "trimemedia-web"),
            "token": os.getenv("FNOS_TOKEN", ""),
            "token_cache_path": os.getenv("FNOS_TOKEN_CACHE_PATH", "data/fnos_token.json"),
            "sign_mode": os.getenv("FNOS_SIGN_MODE", "auto"),
            "timeout": _env_int("FNOS_REFRESH_TIMEOUT", 20),
            "max_auth_retries": _env_int("FNOS_MAX_AUTH_RETRIES", 3),
            "retry_delay": _env_int("FNOS_RETRY_DELAY", 1),
            "scan_dedupe_seconds": _env_int("FNOS_SCAN_DEDUPE_SECONDS", 45),
            "log_enabled": _env_bool("FNOS_REFRESH_LOG_ENABLED", True),
            "log_path": os.getenv("FNOS_REFRESH_LOG_PATH", "data/fnos_refresh.log"),
            "verify_ssl": _env_bool("FNOS_VERIFY_SSL", False),
        },
        "rclone": {
            "enabled": True,
            "script_path": "scripts/fnos_rclone_worker.sh",
            "shell": os.getenv("RCLONE_SCRIPT_SHELL", "sh"),
            "python_bin": os.getenv("RCLONE_PYTHON_BIN", "python"),
            "container_name": os.getenv("RCLONE_CONTAINER_NAME", "rclone-server"),
            "exec_user": os.getenv("RCLONE_EXEC_USER", f"{os.getenv('APP_UID', '10001')}:{os.getenv('APP_GID', '10001')}"),
            "remote_name": os.getenv("RCLONE_REMOTE_NAME", "MP"),
            "upload_backend": os.getenv("RCLONE_UPLOAD_BACKEND", os.getenv("CMCC_UPLOAD_BACKEND", "cmcc_api")),
            "local_temp": os.getenv("RCLONE_LOCAL_TEMP", "/temp/fnos-media-import"),
            "buffer_size": os.getenv("RCLONE_BUFFER_SIZE", "128M"),
            "download_multi_thread": int(os.getenv("RCLONE_DOWNLOAD_MULTI_THREAD", "16")),
            "upload_multi_thread": int(os.getenv("RCLONE_UPLOAD_MULTI_THREAD", "1")),
            "max_retries": int(os.getenv("RCLONE_MAX_RETRIES", "3")),
            "download_max_retries": int(os.getenv("RCLONE_DOWNLOAD_MAX_RETRIES", os.getenv("RCLONE_MAX_RETRIES", "3"))),
            "upload_max_retries": int(os.getenv("RCLONE_UPLOAD_MAX_RETRIES", "1")),
            "retry_delay": int(os.getenv("RCLONE_RETRY_DELAY", "5")),
            "download_retries": int(os.getenv("RCLONE_DOWNLOAD_RETRIES", "2")),
            "upload_retries": int(os.getenv("RCLONE_UPLOAD_RETRIES", "1")),
            "low_level_retries": int(os.getenv("RCLONE_LOW_LEVEL_RETRIES", "3")),
            "timeout": os.getenv("RCLONE_TIMEOUT", "10m"),
            "connect_timeout": os.getenv("RCLONE_CONNECT_TIMEOUT", "30s"),
            "replace_size_mismatch": os.getenv("RCLONE_REPLACE_SIZE_MISMATCH", "true"),
            "source_settle_seconds": int(os.getenv("RCLONE_SOURCE_SETTLE_SECONDS", "30")),
            "manual_source_settle_seconds": int(os.getenv("RCLONE_MANUAL_SOURCE_SETTLE_SECONDS", "0")),
            "source_settle_rounds": int(os.getenv("RCLONE_SOURCE_SETTLE_ROUNDS", "2")),
            "source_appear_wait_seconds": int(os.getenv("RCLONE_SOURCE_APPEAR_WAIT_SECONDS", "180")),
            "source_appear_poll_seconds": int(os.getenv("RCLONE_SOURCE_APPEAR_POLL_SECONDS", "15")),
            "history_repair_interval_seconds": int(os.getenv("RCLONE_HISTORY_REPAIR_INTERVAL_SECONDS", "300")),
            "staging_retry_delay_seconds": int(os.getenv("RCLONE_STAGING_RETRY_DELAY_SECONDS", "30")),
            "staging_retry_max_delay_seconds": int(os.getenv("RCLONE_STAGING_RETRY_MAX_DELAY_SECONDS", "300")),
            "staging_retry_max_attempts": int(os.getenv("RCLONE_STAGING_RETRY_MAX_ATTEMPTS", "8")),
            "refresh_in_worker": os.getenv("RCLONE_REFRESH_IN_WORKER", "false"),
            "temp_retention_hours": int(os.getenv("RCLONE_TEMP_RETENTION_HOURS", "72")),
            "temp_cleanup_timeout": int(os.getenv("RCLONE_TEMP_CLEANUP_TIMEOUT", "20")),
            "upload_max_duration": os.getenv("RCLONE_UPLOAD_MAX_DURATION", "3h"),
            "download_max_duration": os.getenv("RCLONE_DOWNLOAD_MAX_DURATION", "3h"),
            "upload_stall_timeout": int(os.getenv("RCLONE_UPLOAD_STALL_TIMEOUT", "600")),
            "upload_complete_stall_timeout": int(os.getenv("RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT", "120")),
            "upload_pending_verify_seconds": int(os.getenv("RCLONE_UPLOAD_PENDING_VERIFY_SECONDS", "600")),
            "upload_pending_verify_poll_seconds": int(os.getenv("RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS", "30")),
            "upload_pending_ttl": int(os.getenv("RCLONE_UPLOAD_PENDING_TTL", "7200")),
            "delete_partial_on_upload_fail": os.getenv("RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL", "true"),
            "verify_size_retries": int(os.getenv("RCLONE_VERIFY_SIZE_RETRIES", "6")),
            "verify_size_delay": int(os.getenv("RCLONE_VERIFY_SIZE_DELAY", "5")),
            "local_oversize_bytes": int(os.getenv("RCLONE_LOCAL_OVERSIZE_BYTES", str(10 * 1024 * 1024))),
            "lock_file": os.getenv("RCLONE_LOCK_FILE", "/tmp/fnos_media_import_rclone.lock"),
            "auto_interval_minutes": int(os.getenv("RCLONE_AUTO_INTERVAL_MINUTES", "0")),
            "log_lines": 500,
            "app_callback_url": os.getenv("RCLONE_APP_CALLBACK_URL", ""),
        },
        "categories": {
            CATEGORY_MOVIE: {
                "label": CATEGORY_LABELS[CATEGORY_MOVIE],
                "quark_save_path": "/离线下载/电影",
                "sixpan_save_path": os.getenv("SIXPAN_SAVE_MOVIE_PATH", "/电影"),
                "sixpan_fnos_target_path": os.getenv("SIXPAN_FNOS_MOVIE_PATH", ""),
                "sixpan_fnos_dir_list": _env_csv("SIXPAN_FNOS_MOVIE_DIR_LIST", []),
                "mobile_target_path": "移动云盘A/电影",
                "cloud139_target_path": os.getenv("CLOUD139_TARGET_MOVIE_PATH", CATEGORY_LABELS[CATEGORY_MOVIE]),
                "cloud139_fnos_target_path": os.getenv("CLOUD139_FNOS_MOVIE_PATH", ""),
                "cloud139_fnos_dir_list": _env_csv("CLOUD139_FNOS_MOVIE_DIR_LIST", []),
                "cloud139_folder_id": os.getenv("CLOUD139_TARGET_MOVIE_FOLDER_ID", ""),
                "cmcc_parent_file_id": os.getenv("CMCC_TARGET_MOVIE_PARENT_FILE_ID", ""),
                "cmcc_parent_path": os.getenv("CMCC_TARGET_MOVIE_PARENT_PATH", ""),
                "fnos_lib": "电影",
                "fnos_dir_list": _env_csv("FNOS_MDB_MOVIE_DIR_LIST", _env_csv("FNOS_MOVIE_DIR_LIST", [])),
            },
            CATEGORY_TV: {
                "label": CATEGORY_LABELS[CATEGORY_TV],
                "quark_save_path": "/离线下载/电视剧",
                "sixpan_save_path": os.getenv("SIXPAN_SAVE_TV_PATH", "/电视剧"),
                "sixpan_fnos_target_path": os.getenv("SIXPAN_FNOS_TV_PATH", ""),
                "sixpan_fnos_dir_list": _env_csv("SIXPAN_FNOS_TV_DIR_LIST", []),
                "mobile_target_path": "移动云盘A/电视剧",
                "cloud139_target_path": os.getenv("CLOUD139_TARGET_TV_PATH", CATEGORY_LABELS[CATEGORY_TV]),
                "cloud139_fnos_target_path": os.getenv("CLOUD139_FNOS_TV_PATH", ""),
                "cloud139_fnos_dir_list": _env_csv("CLOUD139_FNOS_TV_DIR_LIST", []),
                "cloud139_folder_id": os.getenv("CLOUD139_TARGET_TV_FOLDER_ID", ""),
                "cmcc_parent_file_id": os.getenv("CMCC_TARGET_TV_PARENT_FILE_ID", ""),
                "cmcc_parent_path": os.getenv("CMCC_TARGET_TV_PARENT_PATH", ""),
                "fnos_lib": "电视剧",
                "fnos_dir_list": _env_csv("FNOS_MDB_TV_DIR_LIST", _env_csv("FNOS_TV_DIR_LIST", [])),
            },
            CATEGORY_ANIME: {
                "label": CATEGORY_LABELS[CATEGORY_ANIME],
                "quark_save_path": "/离线下载/动漫",
                "sixpan_save_path": os.getenv("SIXPAN_SAVE_ANIME_PATH", "/动漫"),
                "sixpan_fnos_target_path": os.getenv("SIXPAN_FNOS_ANIME_PATH", ""),
                "sixpan_fnos_dir_list": _env_csv("SIXPAN_FNOS_ANIME_DIR_LIST", []),
                "mobile_target_path": "移动云盘A/动漫",
                "cloud139_target_path": os.getenv("CLOUD139_TARGET_ANIME_PATH", CATEGORY_LABELS[CATEGORY_ANIME]),
                "cloud139_fnos_target_path": os.getenv("CLOUD139_FNOS_ANIME_PATH", ""),
                "cloud139_fnos_dir_list": _env_csv("CLOUD139_FNOS_ANIME_DIR_LIST", []),
                "cloud139_folder_id": os.getenv("CLOUD139_TARGET_ANIME_FOLDER_ID", ""),
                "cmcc_parent_file_id": os.getenv("CMCC_TARGET_ANIME_PARENT_FILE_ID", ""),
                "cmcc_parent_path": os.getenv("CMCC_TARGET_ANIME_PARENT_PATH", ""),
                "fnos_lib": "动漫",
                "fnos_dir_list": _env_csv("FNOS_MDB_ANIME_DIR_LIST", _env_csv("FNOS_ANIME_DIR_LIST", [])),
            },
            CATEGORY_VARIETY: {
                "label": CATEGORY_LABELS[CATEGORY_VARIETY],
                "quark_save_path": "/离线下载/综艺",
                "sixpan_save_path": os.getenv("SIXPAN_SAVE_VARIETY_PATH", "/综艺"),
                "sixpan_fnos_target_path": os.getenv("SIXPAN_FNOS_VARIETY_PATH", ""),
                "sixpan_fnos_dir_list": _env_csv("SIXPAN_FNOS_VARIETY_DIR_LIST", []),
                "mobile_target_path": "移动云盘A/综艺",
                "cloud139_target_path": os.getenv("CLOUD139_TARGET_VARIETY_PATH", CATEGORY_LABELS[CATEGORY_VARIETY]),
                "cloud139_fnos_target_path": os.getenv("CLOUD139_FNOS_VARIETY_PATH", ""),
                "cloud139_fnos_dir_list": _env_csv("CLOUD139_FNOS_VARIETY_DIR_LIST", []),
                "cloud139_folder_id": os.getenv("CLOUD139_TARGET_VARIETY_FOLDER_ID", ""),
                "cmcc_parent_file_id": os.getenv("CMCC_TARGET_VARIETY_PARENT_FILE_ID", ""),
                "cmcc_parent_path": os.getenv("CMCC_TARGET_VARIETY_PARENT_PATH", ""),
                "fnos_lib": "综艺",
                "fnos_dir_list": _env_csv("FNOS_MDB_VARIETY_DIR_LIST", _env_csv("FNOS_VARIETY_DIR_LIST", [])),
            },
            CATEGORY_OTHER: {
                "label": CATEGORY_LABELS[CATEGORY_OTHER],
                "quark_save_path": "/离线下载/其他",
                "sixpan_save_path": os.getenv("SIXPAN_SAVE_OTHER_PATH", "/其他"),
                "sixpan_fnos_target_path": os.getenv("SIXPAN_FNOS_OTHER_PATH", ""),
                "sixpan_fnos_dir_list": _env_csv("SIXPAN_FNOS_OTHER_DIR_LIST", []),
                "mobile_target_path": "移动云盘A/其他",
                "cloud139_target_path": os.getenv("CLOUD139_TARGET_OTHER_PATH", CATEGORY_LABELS[CATEGORY_OTHER]),
                "cloud139_fnos_target_path": os.getenv("CLOUD139_FNOS_OTHER_PATH", ""),
                "cloud139_fnos_dir_list": _env_csv("CLOUD139_FNOS_OTHER_DIR_LIST", []),
                "cloud139_folder_id": os.getenv("CLOUD139_TARGET_OTHER_FOLDER_ID", ""),
                "cmcc_parent_file_id": os.getenv("CMCC_TARGET_OTHER_PARENT_FILE_ID", ""),
                "cmcc_parent_path": os.getenv("CMCC_TARGET_OTHER_PARENT_PATH", ""),
                "fnos_lib": "其他",
                "fnos_dir_list": _env_csv("FNOS_MDB_OTHER_DIR_LIST", _env_csv("FNOS_OTHER_DIR_LIST", [])),
            },
        },
        "routes": {
            "quark": {"enabled": True, "route": ROUTE_QUARK_TO_MOBILE},
            "uc": {"enabled": False, "route": ROUTE_QUARK_TO_MOBILE},
            "cloud139": {"enabled": _env_bool("CLOUD139_ROUTE_ENABLED", False), "route": ROUTE_CLOUD139_DIRECT},
            "cloud189": {"enabled": False, "route": ROUTE_CLOUD189_DIRECT},
            "magnet": {"enabled": _env_bool("MAGNET_ROUTE_ENABLED", _env_bool("SIXPAN_ROUTE_ENABLED", True)), "route": ROUTE_SIXPAN_OFFLINE},
            "torrent": {"enabled": _env_bool("TORRENT_ROUTE_ENABLED", _env_bool("SIXPAN_ROUTE_ENABLED", True)), "route": ROUTE_SIXPAN_OFFLINE},
        },
    }


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """让环境变量最终生效。

    这样 Docker 部署时即使挂载了空白的 config.yaml，也可以直接通过 .env
    注入敏感配置，不会被示例配置里的空字符串覆盖。
    """

    env_map = {
        ("pansou", "base_url"): "PANSOU_BASE_URL",
        ("pansou", "username"): "PANSOU_USERNAME",
        ("pansou", "password"): "PANSOU_PASSWORD",
        ("pansou", "default_token"): "PANSOU_DEFAULT_TOKEN",
        ("pansou", "cloud_types"): "PANSOU_CLOUD_TYPES",
        ("pansou", "res"): "PANSOU_RES",
        ("pansou", "src"): "PANSOU_SRC",
        ("pansou", "conc"): "PANSOU_CONC",
        ("pansou", "refresh"): "PANSOU_REFRESH",
        ("pansou", "channels"): "PANSOU_CHANNELS",
        ("pansou", "plugins"): "PANSOU_PLUGINS",
        ("pansou", "filter_include"): "PANSOU_FILTER_INCLUDE",
        ("pansou", "filter_exclude"): "PANSOU_FILTER_EXCLUDE",
        ("pansou", "async_poll_enabled"): "PANSOU_ASYNC_POLL_ENABLED",
        ("pansou", "async_poll_interval_seconds"): "PANSOU_ASYNC_POLL_INTERVAL_SECONDS",
        ("pansou", "async_poll_max_rounds"): "PANSOU_ASYNC_POLL_MAX_ROUNDS",
        ("pansou", "async_poll_stable_rounds"): "PANSOU_ASYNC_POLL_STABLE_ROUNDS",
        ("pansou", "timeout"): "PANSOU_TIMEOUT",
        ("pansou", "verify_tls"): "PANSOU_VERIFY_TLS",
        ("btbtla", "base_url"): "BTBTLA_BASE_URL",
        ("btbtla", "timeout"): "BTBTLA_TIMEOUT",
        ("btbtla", "max_results"): "BTBTLA_MAX_RESULTS",
        ("btbtla", "max_detail_resources"): "BTBTLA_MAX_DETAIL_RESOURCES",
        ("btbtla", "request_retries"): "BTBTLA_REQUEST_RETRIES",
        ("btbtla", "retry_delay_seconds"): "BTBTLA_RETRY_DELAY_SECONDS",
        ("btbtla", "verify_tls"): "BTBTLA_VERIFY_TLS",
        ("btbtla", "use_env_proxy"): "BTBTLA_USE_ENV_PROXY",
        ("btbtla", "proxy_enabled"): "BTBTLA_PROXY_ENABLED",
        ("btbtla", "proxy_url"): "BTBTLA_PROXY_URL",
        ("btbtla", "user_agent"): "BTBTLA_USER_AGENT",
        ("search", "providers", "pansou", "enabled"): "SEARCH_PANSOU_ENABLED",
        ("search", "providers", "pansou", "priority"): "SEARCH_PANSOU_PRIORITY",
        ("search", "providers", "btbtla", "enabled"): "SEARCH_BTBTLA_ENABLED",
        ("search", "providers", "btbtla", "priority"): "SEARCH_BTBTLA_PRIORITY",
        ("content_review", "enabled"): "CONTENT_REVIEW_ENABLED",
        ("content_review", "keyword_enabled"): "CONTENT_REVIEW_KEYWORD_ENABLED",
        ("content_review", "keyword_file"): "CONTENT_REVIEW_KEYWORD_FILE",
        ("content_review", "keywords"): "CONTENT_REVIEW_KEYWORDS",
        ("content_review", "min_keyword_length"): "CONTENT_REVIEW_MIN_KEYWORD_LENGTH",
        ("content_review", "bt_ai_enabled"): "CONTENT_REVIEW_BT_AI_ENABLED",
        ("content_review", "bt_ai_pass_score"): "CONTENT_REVIEW_BT_AI_PASS_SCORE",
        ("content_review", "bt_ai_retries"): "CONTENT_REVIEW_BT_AI_RETRIES",
        ("content_review", "bt_parse_failure_review"): "CONTENT_REVIEW_BT_PARSE_FAILURE_REVIEW",
        ("content_review", "bt_ai_failure_review"): "CONTENT_REVIEW_BT_AI_FAILURE_REVIEW",
        ("content_review", "max_files_for_ai"): "CONTENT_REVIEW_MAX_FILES_FOR_AI",
        ("quark", "auto_save_url"): "QUARK_AUTO_SAVE_URL",
        ("quark", "token"): "QUARK_AUTO_SAVE_TOKEN",
        ("cloud139", "target_root_path"): "CLOUD139_TARGET_ROOT_PATH",
        ("cloud139", "fnos_mount_name"): "CLOUD139_FNOS_MOUNT_NAME",
        ("cloud139", "check_before_save"): "CLOUD139_CHECK_BEFORE_SAVE",
        ("cloud139", "create_folder_if_missing"): "CLOUD139_CREATE_FOLDER_IF_MISSING",
        ("cloud139", "refresh_after_submit"): "CLOUD139_REFRESH_AFTER_SUBMIT",
        ("cloud139", "refresh_delay_seconds"): "CLOUD139_REFRESH_DELAY_SECONDS",
        ("cloud139", "mark_done_after_submit"): "CLOUD139_MARK_DONE_AFTER_SUBMIT",
        ("cmcc_upload", "enabled"): "CMCC_UPLOAD_ENABLED",
        ("cmcc_upload", "backend"): "RCLONE_UPLOAD_BACKEND",
        ("cmcc_upload", "mode"): "CMCC_UPLOAD_MODE",
        ("cmcc_upload", "rename_mode"): "CMCC_UPLOAD_RENAME_MODE",
        ("cmcc_upload", "host"): "CMCC_HOST",
        ("cmcc_upload", "auth_mode"): "CMCC_AUTH_MODE",
        ("cmcc_upload", "access_token"): "CM_CLOUD_ACCESS_TOKEN",
        ("cmcc_upload", "phone"): "CM_CLOUD_PHONE",
        ("cmcc_upload", "put_timeout"): "CMCC_UPLOAD_PUT_TIMEOUT",
        ("openlist", "base_url"): "OPENLIST_BASE_URL",
        ("openlist", "token"): "OPENLIST_TOKEN",
        ("openlist", "timeout"): "OPENLIST_TIMEOUT",
        ("openlist", "batch_timeout"): "OPENLIST_BATCH_TIMEOUT",
        ("openlist", "list_refresh_default"): "OPENLIST_LIST_REFRESH_DEFAULT",
        ("openlist", "verify_tls"): "OPENLIST_VERIFY_TLS",
        ("openlist", "use_env_proxy"): "OPENLIST_USE_ENV_PROXY",
        ("tmdb", "token"): "TMDB_TOKEN",
        ("tmdb", "language"): "TMDB_LANGUAGE",
        ("tmdb", "use_env_proxy"): "TMDB_USE_ENV_PROXY",
        ("tmdb", "proxy_enabled"): "TMDB_PROXY_ENABLED",
        ("tmdb", "proxy_url"): "TMDB_PROXY_URL",
        ("ai", "enabled"): "AI_ENABLED",
        ("ai", "api_style"): "AI_API_STYLE",
        ("ai", "base_url"): "AI_BASE_URL",
        ("ai", "api_key"): "AI_API_KEY",
        ("ai", "model"): "AI_MODEL",
        ("ai", "timeout"): "AI_TIMEOUT",
        ("organizer", "enabled"): "ORGANIZER_ENABLED",
        ("organizer", "staging_enabled"): "ORGANIZER_STAGING_ENABLED",
        ("organizer", "staging_dir_name"): "ORGANIZER_STAGING_DIR_NAME",
        ("organizer", "stable_window_seconds"): "ORGANIZER_STABLE_WINDOW_SECONDS",
        ("organizer", "auto_apply_confidence"): "ORGANIZER_AUTO_APPLY_CONFIDENCE",
        ("organizer", "max_scan_depth"): "ORGANIZER_MAX_SCAN_DEPTH",
        ("organizer", "max_files_per_task"): "ORGANIZER_MAX_FILES_PER_TASK",
        ("organizer", "bulk_operations_enabled"): "ORGANIZER_BULK_OPERATIONS_ENABLED",
        ("organizer", "regex_rename_min_items"): "ORGANIZER_REGEX_RENAME_MIN_ITEMS",
        ("organizer", "bulk_reconcile_timeout_seconds"): "ORGANIZER_BULK_RECONCILE_TIMEOUT_SECONDS",
        ("organizer", "lock_wait_seconds"): "ORGANIZER_LOCK_WAIT_SECONDS",
        ("organizer", "lock_poll_seconds"): "ORGANIZER_LOCK_POLL_SECONDS",
        ("organizer", "refresh_fnos_after_apply"): "ORGANIZER_REFRESH_FNOS_AFTER_APPLY",
        ("organizer", "refresh_delay_seconds"): "ORGANIZER_REFRESH_DELAY_SECONDS",
        ("organizer", "cleanup_empty_dirs"): "ORGANIZER_CLEANUP_EMPTY_DIRS",
        ("organizer", "strm_refresh_after_apply"): "ORGANIZER_STRM_REFRESH_AFTER_APPLY",
        ("organizer", "strm_cleanup_old_before_refresh"): "ORGANIZER_STRM_CLEANUP_OLD_BEFORE_REFRESH",
        ("organizer", "strm_refresh_prefix"): "ORGANIZER_STRM_REFRESH_PREFIX",
        ("organizer", "strm_refresh_prefix_movie"): "ORGANIZER_STRM_REFRESH_PREFIX_MOVIE",
        ("organizer", "strm_refresh_prefix_tv"): "ORGANIZER_STRM_REFRESH_PREFIX_TV",
        ("organizer", "strm_refresh_prefix_anime"): "ORGANIZER_STRM_REFRESH_PREFIX_ANIME",
        ("organizer", "strm_refresh_prefix_variety"): "ORGANIZER_STRM_REFRESH_PREFIX_VARIETY",
        ("organizer", "local_strm_root"): "ORGANIZER_LOCAL_STRM_ROOT",
        ("update_scheduler", "enabled"): "UPDATE_SCHEDULER_ENABLED",
        ("update_scheduler", "interval_seconds"): "UPDATE_SCHEDULER_INTERVAL_SECONDS",
        ("update_scheduler", "run_lease_seconds"): "UPDATE_RUN_LEASE_SECONDS",
        ("update_scheduler", "max_subscriptions_per_tick"): "UPDATE_SCHEDULER_MAX_SUBSCRIPTIONS_PER_TICK",
        ("update_scheduler", "max_episodes_per_run"): "UPDATE_SCHEDULER_MAX_EPISODES_PER_RUN",
        ("update_scheduler", "coalesce_missed_runs"): "UPDATE_SCHEDULER_COALESCE_MISSED_RUNS",
        ("update_scheduler", "preview_cache_ttl_seconds"): "UPDATE_PREVIEW_CACHE_TTL_SECONDS",
        ("update_scheduler", "negative_preview_cache_ttl_seconds"): "UPDATE_NEGATIVE_PREVIEW_CACHE_TTL_SECONDS",
        ("update_scheduler", "snapshot_ttl_seconds"): "UPDATE_SNAPSHOT_TTL_SECONDS",
        ("update_scheduler", "empty_retry_enabled"): "UPDATE_EMPTY_RETRY_ENABLED",
        ("update_scheduler", "empty_retry_interval_minutes"): "UPDATE_EMPTY_RETRY_INTERVAL_MINUTES",
        ("update_scheduler", "empty_retry_max_attempts"): "UPDATE_EMPTY_RETRY_MAX_ATTEMPTS",
        ("update_scheduler", "empty_retry_max_window_hours"): "UPDATE_EMPTY_RETRY_MAX_WINDOW_HOURS",
        ("update_scheduler", "failure_retry_interval_minutes"): "UPDATE_FAILURE_RETRY_INTERVAL_MINUTES",
        ("update_scheduler", "pending_import_check_interval_minutes"): "UPDATE_PENDING_IMPORT_CHECK_INTERVAL_MINUTES",
        ("update_scheduler", "source_health_warn_threshold"): "UPDATE_SOURCE_HEALTH_WARN_THRESHOLD",
        ("hot_discovery", "enabled"): "HOT_DISCOVERY_ENABLED",
        ("hot_discovery", "run_at"): "HOT_DISCOVERY_RUN_AT",
        ("hot_discovery", "timezone"): "HOT_DISCOVERY_TIMEZONE",
        ("hot_discovery", "timeout"): "HOT_DISCOVERY_TIMEOUT",
        ("hot_discovery", "max_items_per_source"): "HOT_DISCOVERY_MAX_ITEMS_PER_SOURCE",
        ("hot_discovery", "tencent_enabled"): "HOT_DISCOVERY_TENCENT_ENABLED",
        ("hot_discovery", "tencent_endpoint"): "HOT_DISCOVERY_TENCENT_ENDPOINT",
        ("hot_discovery", "tencent_data_version"): "HOT_DISCOVERY_TENCENT_DATA_VERSION",
        ("hot_discovery", "iqiyi_enabled"): "HOT_DISCOVERY_IQIYI_ENABLED",
        ("hot_discovery", "iqiyi_endpoint"): "HOT_DISCOVERY_IQIYI_ENDPOINT",
        ("hot_discovery", "iqiyi_device_id"): "HOT_DISCOVERY_IQIYI_DEVICE_ID",
        ("hot_discovery", "iqiyi_version"): "HOT_DISCOVERY_IQIYI_VERSION",
        ("hot_discovery", "youku_enabled"): "HOT_DISCOVERY_YOUKU_ENABLED",
        ("hot_discovery", "youku_url"): "HOT_DISCOVERY_YOUKU_URL",
        ("routes", "cloud139", "enabled"): "CLOUD139_ROUTE_ENABLED",
        ("routes", "magnet", "enabled"): "MAGNET_ROUTE_ENABLED",
        ("routes", "torrent", "enabled"): "TORRENT_ROUTE_ENABLED",
        ("cloud189", "auto_save_url"): "CLOUD189_AUTO_SAVE_URL",
        ("cloud189", "endpoint"): "CLOUD189_AUTO_SAVE_URL",
        ("cloud189", "token"): "CLOUD189_AUTO_SAVE_TOKEN",
        ("sixpan", "api_url"): "SIXPAN_API_URL",
        ("sixpan", "endpoint"): "SIXPAN_API_URL",
        ("sixpan", "host"): "SIXPAN_HOST",
        ("sixpan", "fnos_mount_name"): "SIXPAN_FNOS_MOUNT_NAME",
        ("sixpan", "client_id"): "SIXPAN_CLIENT_ID",
        ("sixpan", "client_secret"): "SIXPAN_CLIENT_SECRET",
        ("sixpan", "access_token"): "SIXPAN_ACCESS_TOKEN",
        ("sixpan", "refresh_token"): "SIXPAN_REFRESH_TOKEN",
        ("sixpan", "token"): "SIXPAN_ACCESS_TOKEN",
        ("sixpan", "timeout"): "SIXPAN_TIMEOUT",
        ("sixpan", "verify_tls"): "SIXPAN_VERIFY_TLS",
        ("sixpan", "parse_before_add"): "SIXPAN_PARSE_BEFORE_ADD",
        ("sixpan", "parse_required"): "SIXPAN_PARSE_REQUIRED",
        ("sixpan", "parse_cache_ttl_seconds"): "SIXPAN_PARSE_CACHE_TTL_SECONDS",
        ("sixpan", "parse_cache_max_entries"): "SIXPAN_PARSE_CACHE_MAX_ENTRIES",
        ("sixpan", "poll_enabled"): "SIXPAN_POLL_ENABLED",
        ("sixpan", "poll_interval_seconds"): "SIXPAN_POLL_INTERVAL_SECONDS",
        ("sixpan", "task_poll_limit"): "SIXPAN_TASK_POLL_LIMIT",
        ("sixpan", "task_max_pages"): "SIXPAN_TASK_MAX_PAGES",
        ("sixpan", "task_missing_poll_limit"): "SIXPAN_TASK_MISSING_POLL_LIMIT",
        ("sixpan", "task_unknown_poll_limit"): "SIXPAN_TASK_UNKNOWN_POLL_LIMIT",
        ("sixpan", "submitted_timeout_seconds"): "SIXPAN_SUBMITTED_TIMEOUT_SECONDS",
        ("sixpan", "running_statuses"): "SIXPAN_RUNNING_STATUSES",
        ("fnos", "server_url"): "FNOS_SERVER_URL",
        ("fnos", "username"): "FNOS_USERNAME",
        ("fnos", "password"): "FNOS_PASSWORD",
        ("fnos", "api_key"): "FNOS_API_KEY",
        ("fnos", "secret"): "FNOS_SECRET",
        ("fnos", "app_name"): "FNOS_APP_NAME",
        ("fnos", "token"): "FNOS_TOKEN",
        ("fnos", "token_cache_path"): "FNOS_TOKEN_CACHE_PATH",
        ("fnos", "sign_mode"): "FNOS_SIGN_MODE",
        ("fnos", "timeout"): "FNOS_REFRESH_TIMEOUT",
        ("fnos", "max_auth_retries"): "FNOS_MAX_AUTH_RETRIES",
        ("fnos", "retry_delay"): "FNOS_RETRY_DELAY",
        ("fnos", "verify_ssl"): "FNOS_VERIFY_SSL",
        ("app", "secret_key"): "APP_SECRET_KEY",
        ("admin", "username"): "ADMIN_USERNAME",
        ("admin", "password"): "ADMIN_PASSWORD",
        ("submission", "mode"): "SUBMIT_MODE",
        ("public", "allow_anonymous_search"): "PUBLIC_ALLOW_ANONYMOUS_SEARCH",
        ("public", "request_query_enabled"): "PUBLIC_REQUEST_QUERY_ENABLED",
        ("public", "hide_full_links"): "PUBLIC_HIDE_FULL_LINKS",
        ("security", "rate_limit_enabled"): "SECURITY_RATE_LIMIT_ENABLED",
        ("security", "rate_limit_window_seconds"): "SECURITY_RATE_LIMIT_WINDOW_SECONDS",
        ("security", "public_search_rate_limit"): "SECURITY_PUBLIC_SEARCH_RATE_LIMIT",
        ("security", "public_detect_rate_limit"): "SECURITY_PUBLIC_DETECT_RATE_LIMIT",
        ("security", "public_submit_rate_limit"): "SECURITY_PUBLIC_SUBMIT_RATE_LIMIT",
        ("security", "public_request_rate_limit"): "SECURITY_PUBLIC_REQUEST_RATE_LIMIT",
        ("security", "public_captcha_rate_limit"): "SECURITY_PUBLIC_CAPTCHA_RATE_LIMIT",
        ("security", "max_keyword_length"): "SECURITY_MAX_KEYWORD_LENGTH",
        ("security", "max_title_length"): "SECURITY_MAX_TITLE_LENGTH",
        ("security", "max_request_body_bytes"): "SECURITY_MAX_REQUEST_BODY_BYTES",
        ("security", "max_public_json_bytes"): "SECURITY_MAX_PUBLIC_JSON_BYTES",
        ("security", "max_url_length"): "SECURITY_MAX_URL_LENGTH",
        ("security", "max_password_length"): "SECURITY_MAX_PASSWORD_LENGTH",
        ("security", "max_note_length"): "SECURITY_MAX_NOTE_LENGTH",
        ("security", "max_token_length"): "SECURITY_MAX_TOKEN_LENGTH",
        ("security", "duplicate_check_enabled"): "SECURITY_DUPLICATE_CHECK_ENABLED",
        ("security", "duplicate_window_minutes"): "SECURITY_DUPLICATE_WINDOW_MINUTES",
        ("security", "ip_hash_salt"): "SECURITY_IP_HASH_SALT",
        ("security", "allowed_url_schemes"): "SECURITY_ALLOWED_URL_SCHEMES",
        ("security", "allowed_share_domains"): "SECURITY_ALLOWED_SHARE_DOMAINS",
        ("security", "block_private_hosts"): "SECURITY_BLOCK_PRIVATE_HOSTS",
        ("security", "captcha_enabled"): "SECURITY_CAPTCHA_ENABLED",
        ("security", "captcha_ttl_seconds"): "SECURITY_CAPTCHA_TTL_SECONDS",
        ("rclone", "shell"): "RCLONE_SCRIPT_SHELL",
        ("rclone", "python_bin"): "RCLONE_PYTHON_BIN",
        ("rclone", "container_name"): "RCLONE_CONTAINER_NAME",
        ("rclone", "exec_user"): "RCLONE_EXEC_USER",
        ("rclone", "remote_name"): "RCLONE_REMOTE_NAME",
        ("rclone", "upload_backend"): "RCLONE_UPLOAD_BACKEND",
        ("rclone", "local_temp"): "RCLONE_LOCAL_TEMP",
        ("rclone", "buffer_size"): "RCLONE_BUFFER_SIZE",
        ("rclone", "download_multi_thread"): "RCLONE_DOWNLOAD_MULTI_THREAD",
        ("rclone", "upload_multi_thread"): "RCLONE_UPLOAD_MULTI_THREAD",
        ("rclone", "max_retries"): "RCLONE_MAX_RETRIES",
        ("rclone", "download_max_retries"): "RCLONE_DOWNLOAD_MAX_RETRIES",
        ("rclone", "upload_max_retries"): "RCLONE_UPLOAD_MAX_RETRIES",
        ("rclone", "retry_delay"): "RCLONE_RETRY_DELAY",
        ("rclone", "download_retries"): "RCLONE_DOWNLOAD_RETRIES",
        ("rclone", "upload_retries"): "RCLONE_UPLOAD_RETRIES",
        ("rclone", "low_level_retries"): "RCLONE_LOW_LEVEL_RETRIES",
        ("rclone", "timeout"): "RCLONE_TIMEOUT",
        ("rclone", "connect_timeout"): "RCLONE_CONNECT_TIMEOUT",
        ("rclone", "replace_size_mismatch"): "RCLONE_REPLACE_SIZE_MISMATCH",
        ("rclone", "source_settle_seconds"): "RCLONE_SOURCE_SETTLE_SECONDS",
        ("rclone", "manual_source_settle_seconds"): "RCLONE_MANUAL_SOURCE_SETTLE_SECONDS",
        ("rclone", "source_settle_rounds"): "RCLONE_SOURCE_SETTLE_ROUNDS",
        ("rclone", "source_appear_wait_seconds"): "RCLONE_SOURCE_APPEAR_WAIT_SECONDS",
        ("rclone", "source_appear_poll_seconds"): "RCLONE_SOURCE_APPEAR_POLL_SECONDS",
        ("rclone", "history_repair_interval_seconds"): "RCLONE_HISTORY_REPAIR_INTERVAL_SECONDS",
        ("rclone", "staging_retry_delay_seconds"): "RCLONE_STAGING_RETRY_DELAY_SECONDS",
        ("rclone", "staging_retry_max_delay_seconds"): "RCLONE_STAGING_RETRY_MAX_DELAY_SECONDS",
        ("rclone", "staging_retry_max_attempts"): "RCLONE_STAGING_RETRY_MAX_ATTEMPTS",
        ("rclone", "refresh_in_worker"): "RCLONE_REFRESH_IN_WORKER",
        ("rclone", "temp_retention_hours"): "RCLONE_TEMP_RETENTION_HOURS",
        ("rclone", "temp_cleanup_timeout"): "RCLONE_TEMP_CLEANUP_TIMEOUT",
        ("rclone", "upload_max_duration"): "RCLONE_UPLOAD_MAX_DURATION",
        ("rclone", "download_max_duration"): "RCLONE_DOWNLOAD_MAX_DURATION",
        ("rclone", "upload_stall_timeout"): "RCLONE_UPLOAD_STALL_TIMEOUT",
        ("rclone", "upload_complete_stall_timeout"): "RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT",
        ("rclone", "upload_pending_verify_seconds"): "RCLONE_UPLOAD_PENDING_VERIFY_SECONDS",
        ("rclone", "upload_pending_verify_poll_seconds"): "RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS",
        ("rclone", "upload_pending_ttl"): "RCLONE_UPLOAD_PENDING_TTL",
        ("rclone", "delete_partial_on_upload_fail"): "RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL",
        ("rclone", "verify_size_retries"): "RCLONE_VERIFY_SIZE_RETRIES",
        ("rclone", "verify_size_delay"): "RCLONE_VERIFY_SIZE_DELAY",
        ("rclone", "local_oversize_bytes"): "RCLONE_LOCAL_OVERSIZE_BYTES",
        ("rclone", "lock_file"): "RCLONE_LOCK_FILE",
        ("rclone", "auto_interval_minutes"): "RCLONE_AUTO_INTERVAL_MINUTES",
        ("rclone", "app_callback_url"): "RCLONE_APP_CALLBACK_URL",
    }
    for path, env_name in env_map.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            current = config
            for section in path[:-1]:
                current = current.setdefault(section, {})
            current[path[-1]] = _cloud_type_values(value) if env_name == "PANSOU_CLOUD_TYPES" else value
    cmcc_alias_env_map = {
        ("cmcc_upload", "backend"): "CMCC_UPLOAD_BACKEND",
        ("cmcc_upload", "host"): "CM_CLOUD_HOST",
        ("cmcc_upload", "access_token"): "CMCC_ACCESS_TOKEN",
        ("cmcc_upload", "phone"): "CMCC_PHONE",
    }
    for path, env_name in cmcc_alias_env_map.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            current = config
            for section in path[:-1]:
                current = current.setdefault(section, {})
            current[path[-1]] = value
    cloud139_root_path = os.getenv("CLOUD139_TARGET_ROOT_PATH") or os.getenv("CLOUD139_CLOUD_ROOT_PATH")
    if cloud139_root_path:
        config.setdefault("cloud139", {})["target_root_path"] = cloud139_root_path
    sixpan_mount_name = os.getenv("SIXPAN_FNOS_MOUNT_NAME") or os.getenv("SIXPAN_OPENLIST_MOUNT_NAME") or os.getenv("SIXPAN_MOUNT_NAME")
    if sixpan_mount_name:
        config.setdefault("sixpan", {})["fnos_mount_name"] = sixpan_mount_name
    category_env_map = {
        CATEGORY_MOVIE: (
            "RCLONE_SRC_MOVIE_DIR",
            "RCLONE_DST_MOVIE_DIR",
            "SIXPAN_SAVE_MOVIE_PATH",
            "SIXPAN_FNOS_MOVIE_PATH",
            "SIXPAN_FNOS_MOVIE_DIR_LIST",
            "CLOUD139_TARGET_MOVIE_PATH",
            "CLOUD139_TARGET_MOVIE_FOLDER_ID",
            "CLOUD139_FNOS_MOVIE_PATH",
            "CLOUD139_FNOS_MOVIE_DIR_LIST",
            "FNOS_MDB_MOVIE_DIR_LIST",
            "FNOS_MOVIE_DIR_LIST",
        ),
        CATEGORY_TV: (
            "RCLONE_SRC_TV_DIR",
            "RCLONE_DST_TV_DIR",
            "SIXPAN_SAVE_TV_PATH",
            "SIXPAN_FNOS_TV_PATH",
            "SIXPAN_FNOS_TV_DIR_LIST",
            "CLOUD139_TARGET_TV_PATH",
            "CLOUD139_TARGET_TV_FOLDER_ID",
            "CLOUD139_FNOS_TV_PATH",
            "CLOUD139_FNOS_TV_DIR_LIST",
            "FNOS_MDB_TV_DIR_LIST",
            "FNOS_TV_DIR_LIST",
        ),
        CATEGORY_ANIME: (
            "RCLONE_SRC_ANIME_DIR",
            "RCLONE_DST_ANIME_DIR",
            "SIXPAN_SAVE_ANIME_PATH",
            "SIXPAN_FNOS_ANIME_PATH",
            "SIXPAN_FNOS_ANIME_DIR_LIST",
            "CLOUD139_TARGET_ANIME_PATH",
            "CLOUD139_TARGET_ANIME_FOLDER_ID",
            "CLOUD139_FNOS_ANIME_PATH",
            "CLOUD139_FNOS_ANIME_DIR_LIST",
            "FNOS_MDB_ANIME_DIR_LIST",
            "FNOS_ANIME_DIR_LIST",
        ),
        CATEGORY_VARIETY: (
            "RCLONE_SRC_VARIETY_DIR",
            "RCLONE_DST_VARIETY_DIR",
            "SIXPAN_SAVE_VARIETY_PATH",
            "SIXPAN_FNOS_VARIETY_PATH",
            "SIXPAN_FNOS_VARIETY_DIR_LIST",
            "CLOUD139_TARGET_VARIETY_PATH",
            "CLOUD139_TARGET_VARIETY_FOLDER_ID",
            "CLOUD139_FNOS_VARIETY_PATH",
            "CLOUD139_FNOS_VARIETY_DIR_LIST",
            "FNOS_MDB_VARIETY_DIR_LIST",
            "FNOS_VARIETY_DIR_LIST",
        ),
        CATEGORY_OTHER: (
            "RCLONE_SRC_OTHER_DIR",
            "RCLONE_DST_OTHER_DIR",
            "SIXPAN_SAVE_OTHER_PATH",
            "SIXPAN_FNOS_OTHER_PATH",
            "SIXPAN_FNOS_OTHER_DIR_LIST",
            "CLOUD139_TARGET_OTHER_PATH",
            "CLOUD139_TARGET_OTHER_FOLDER_ID",
            "CLOUD139_FNOS_OTHER_PATH",
            "CLOUD139_FNOS_OTHER_DIR_LIST",
            "FNOS_MDB_OTHER_DIR_LIST",
            "FNOS_OTHER_DIR_LIST",
        ),
    }
    for (
        category_key,
        (
            src_env,
            dst_env,
            sixpan_save_env,
            sixpan_fnos_path_env,
            sixpan_fnos_dirs_env,
            cloud139_path_env,
            cloud139_folder_id_env,
            cloud139_fnos_path_env,
            cloud139_fnos_dirs_env,
            fnos_dirs_env,
            fnos_dirs_alt_env,
        ),
    ) in category_env_map.items():
        category = config.setdefault("categories", {}).setdefault(category_key, {})
        src_value = _clean_remote_dir(os.getenv(src_env))
        dst_value = _clean_remote_dir(os.getenv(dst_env))
        sixpan_save_value = _clean_remote_dir(os.getenv(sixpan_save_env))
        sixpan_fnos_path_value = str(os.getenv(sixpan_fnos_path_env) or "").strip()
        sixpan_fnos_dir_list = _env_csv(sixpan_fnos_dirs_env, [])
        cloud139_path_value = _clean_remote_dir(os.getenv(cloud139_path_env))
        cloud139_folder_id = str(os.getenv(cloud139_folder_id_env) or "").strip()
        cloud139_fnos_path_value = _clean_remote_dir(os.getenv(cloud139_fnos_path_env))
        cloud139_fnos_dir_list = _env_csv(cloud139_fnos_dirs_env, [])
        fnos_dir_list = _env_csv(fnos_dirs_env, _env_csv(fnos_dirs_alt_env, []))
        if src_value:
            category["quark_save_path"] = f"/{src_value}"
        if dst_value:
            category["mobile_target_path"] = dst_value
        if sixpan_save_value:
            category["sixpan_save_path"] = f"/{sixpan_save_value.strip('/')}"
        if sixpan_fnos_path_value:
            category["sixpan_fnos_target_path"] = sixpan_fnos_path_value
        if sixpan_fnos_dir_list:
            category["sixpan_fnos_dir_list"] = sixpan_fnos_dir_list
        if cloud139_path_value:
            category["cloud139_target_path"] = cloud139_path_value
        if cloud139_folder_id:
            category["cloud139_folder_id"] = cloud139_folder_id
        if cloud139_fnos_path_value:
            category["cloud139_fnos_target_path"] = cloud139_fnos_path_value
        if cloud139_fnos_dir_list:
            category["cloud139_fnos_dir_list"] = cloud139_fnos_dir_list
        if fnos_dir_list:
            category["fnos_dir_list"] = fnos_dir_list
    cmcc_category_env_map = {
        CATEGORY_MOVIE: ("CMCC_TARGET_MOVIE_PARENT_FILE_ID", "CMCC_TARGET_MOVIE_PARENT_PATH"),
        CATEGORY_TV: ("CMCC_TARGET_TV_PARENT_FILE_ID", "CMCC_TARGET_TV_PARENT_PATH"),
        CATEGORY_ANIME: ("CMCC_TARGET_ANIME_PARENT_FILE_ID", "CMCC_TARGET_ANIME_PARENT_PATH"),
        CATEGORY_VARIETY: ("CMCC_TARGET_VARIETY_PARENT_FILE_ID", "CMCC_TARGET_VARIETY_PARENT_PATH"),
        CATEGORY_OTHER: ("CMCC_TARGET_OTHER_PARENT_FILE_ID", "CMCC_TARGET_OTHER_PARENT_PATH"),
    }
    for category_key, (parent_id_env, parent_path_env) in cmcc_category_env_map.items():
        category = config.setdefault("categories", {}).setdefault(category_key, {})
        parent_id = str(os.getenv(parent_id_env) or "").strip()
        parent_path = _clean_remote_dir(os.getenv(parent_path_env))
        if parent_id:
            category["cmcc_parent_file_id"] = parent_id
        if parent_path:
            category["cmcc_parent_path"] = f"/{parent_path}"
    return config


def _clean_remote_dir(value: str | None) -> str:
    return str(value or "").strip().strip("/")


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any] = field(default_factory=dict)
    base_dir: Path = BASE_DIR

    @property
    def app_name(self) -> str:
        return str(self.raw["app"]["name"])

    @property
    def host(self) -> str:
        return str(self.raw["app"].get("host", "0.0.0.0"))

    @property
    def port(self) -> int:
        return int(self.raw["app"].get("port", 5251))

    @property
    def debug(self) -> bool:
        return bool(self.raw["app"].get("debug", False))

    @property
    def database_path(self) -> Path:
        configured = Path(str(self.raw["app"].get("database_path", "data/app.db")))
        if configured.is_absolute():
            return configured
        return self.base_dir / configured

    @property
    def categories(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("categories", {})

    def category(self, category_key: str) -> dict[str, Any]:
        categories = self.categories
        return categories.get(category_key) or categories[CATEGORY_MOVIE]

    def route_config(self, source_type: str) -> dict[str, Any]:
        return self.raw.get("routes", {}).get(source_type, {"enabled": False, "route": "unsupported"})


def load_config(config_path: str | os.PathLike[str] | None = None) -> AppConfig:
    _load_dotenv(BASE_DIR / ".env")

    default = _default_config()
    resolved_path = Path(config_path) if config_path else BASE_DIR / "config" / "config.yaml"
    if not resolved_path.is_absolute():
        resolved_path = BASE_DIR / resolved_path

    if resolved_path.exists():
        try:
            config_text = resolved_path.read_text(encoding="utf-8")
        except PermissionError as exc:
            raise PermissionError(
                f"无法读取配置文件 {resolved_path}。请检查容器启动日志中的 bind mount "
                "权限准备错误，并确认当前镜像已重新构建。"
            ) from exc
        user_config = yaml.safe_load(config_text) or {}
        merged = _deep_merge(default, user_config)
    else:
        merged = default

    merged = _apply_env_overrides(merged)
    if not merged.get("rclone", {}).get("app_callback_url"):
        port = int(merged.get("app", {}).get("port", 5251) or 5251)
        merged.setdefault("rclone", {})["app_callback_url"] = f"http://127.0.0.1:{port}/api/callback/rclone"

    normalize_sixpan_path_sources(merged, materialize_category_paths=True)

    return AppConfig(raw=merged)
