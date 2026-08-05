from __future__ import annotations

import atexit
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import string
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import urllib3
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .classifiers.link_classifier import detect_link
from .config import load_config
from .config_persistence import (
    ADVANCED_CONFIG_KEY,
    advanced_config_response,
    apply_persisted_config,
    normalize_advanced_config_payload,
)
from .content_guard import BT_SOURCE_TYPES, evaluate_submission_content_risk
from .constants import (
    APP_NAME,
    CATEGORY_LABELS,
    JOB_CANCELLED,
    JOB_CONFIRMING,
    JOB_DONE,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_REVIEW,
    JOB_STATUS_LABELS,
    JOB_SUBMITTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
    JOB_WAITING_TRANSFER,
)
from .database import Database
from .time_utils import utc_now_iso, utc_now_iso_offset
from .importers.cloud139 import Cloud139Importer
from .importers.generic import GenericWebhookImporter
from .importers.quark import QuarkImporter
from .importers.sixpan import SixPanOfflineImporter
from .media.fnos import FnosMediaRefresher
from .blueprints import AdaptersRouteContext, AdminShellRouteContext, AuthRouteContext, CallbackRouteContext, CloudCompatRouteContext, DiagnosticsRouteContext, JobsRouteContext, LegacyApiRouteContext, MediaRouteContext, OrganizerRouteContext, PublicRouteContext, RcloneRouteContext, RequestsRouteContext, SettingsRouteContext, SixPanRouteContext, SystemRouteContext, UpdatesRouteContext, create_adapters_blueprint, create_admin_shell_blueprint, create_auth_blueprint, create_callbacks_blueprint, create_cloud_compat_blueprint, create_diagnostics_blueprint, create_jobs_blueprint, create_legacy_api_blueprint, create_media_blueprint, create_organizer_blueprint, create_public_blueprint, create_rclone_blueprint, create_requests_blueprint, create_settings_blueprint, create_sixpan_blueprint, create_system_blueprint, create_updates_blueprint, preserve_legacy_endpoints
from .organizer.openlist_client import VIDEO_EXTENSIONS
from .organizer.service import OrganizerService
from .providers.btbtla import BtbtlaClient
from .providers.hot_sources import IqiyiHotSource, TencentHotSource, YoukuHotSource
from .services.import_service import ImportService
from .services.import_staging_service import map_staging_path_to_openlist, staging_plan_from_job
from .services.job_service import JobService
from .services.rclone_service import RcloneService
from .services.rclone_webdav_config_service import RcloneWebdavConfigError, RcloneWebdavConfigService
from .storage_paths import map_upload_path_to_openlist, openlist_root_for_upload, upload_backend
from .services.rclone_history_repair_worker import RcloneHistoryRepairWorker
from .services.rclone_worker_runtime import RcloneWorkerRuntime
from .services.search_cache_maintenance_worker import SearchCacheMaintenanceWorker
from .services.event_retention_worker import EventRetentionWorker
from .services.callback_service import CallbackDependencies, RcloneCallbackService
from .services.admin_profile_service import AdminProfileService
from .services.public_search_service import PublicSearchDependencies, PublicSearchService
from .services.public_submission_service import PublicSubmissionDependencies, PublicSubmissionService
from .services.guest_request_admin_service import GuestRequestAdminDependencies, GuestRequestAdminService
from .services.job_admin_query_service import JobAdminQueryDependencies, JobAdminQueryService
from .services.job_admin_command_service import JobAdminCommandDependencies, JobAdminCommandService
from .services.job_cancellation_service import JobCancellationDependencies, JobCancellationService
from .services.request_review_command_service import RequestReviewCommandDependencies, RequestReviewCommandService
from .services.request_approval_service import RequestApprovalDependencies, RequestApprovalService
from .services.admin_dashboard_service import AdminDashboardDependencies, AdminDashboardService
from .services.media_admin_service import (
    MediaAdminCommandDependencies,
    MediaAdminCommandService,
    MediaAdminQueryDependencies,
    MediaAdminQueryService,
)
from .services.rclone_admin_service import RcloneAdminQueryDependencies, RcloneAdminQueryService
from .services.rclone_file_retry_service import RcloneFileRetryDependencies, RcloneFileRetryService
from .services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
    OrganizerAdminQueryDependencies,
    OrganizerAdminQueryService,
)
from .services.rclone_admin_service import RcloneAdminCommandDependencies, RcloneAdminCommandService
from .services.system_diagnostics_service import SystemDiagnosticsDependencies, SystemDiagnosticsService
from .services.external_diagnostics_service import ExternalDiagnosticsDependencies, ExternalDiagnosticsService
from .services.btbtla_proxy_diagnostics_service import BtbtlaProxyDiagnosticsService
from .services.public_resource_service import PublicResourceDependencies, PublicResourceService
from .services.settings_service import SettingsDependencies, SettingsService
from .repositories.admin_profile_repository import AdminProfileRepository
from .services.search_service import SearchService
from .services.update_scheduler import UpdateScheduler
from .services.update_scheduler_runtime import UpdateSchedulerRuntime
from .services.trending_discovery_service import TrendingDiscoveryService
from .services.trending_discovery_scheduler import TrendingDiscoveryScheduler
from .services.trending_initial_import_service import TrendingInitialImportError, TrendingInitialImportService
from .services.sixpan_polling_runtime import SixPanPollingRuntime
from .services.sixpan_offline_sync_service import SixPanOfflineSyncService
from .services.import_completion_dispatcher import ImportCompletionDispatcher
from .services.organizer_dispatch_service import OrganizerDispatchService
from .services.job_completion_info_service import JobCompletionInfoService
from .services.runtime_reload_service import RuntimeReloadService, finalize_organizer_runtime_transition
from .services.public_submission_preparation_service import (
    PublicSubmissionPreparationError,
    PublicSubmissionPreparationService,
)
from .services.public_import_job_coordinator import PublicImportJobCoordinator
from .services.public_submission_decision_service import PublicSubmissionDecisionService
from .services.public_submission_intake_service import PublicSubmissionIntakeService
from .services.security_status_service import SecurityStatusService
from .services.rate_limit_service import RateLimitService
from .services.public_resource_detail_service import PublicResourceDetailService
from .services.media_dashboard_service import (
    MediaDashboardService,
)
from .services.durable_worker_runtime import DurableWorkerRuntime
from .services.worker_task_dispatcher import WorkerTaskDispatcher
from .services.worker_queue_diagnostics_service import WorkerQueueDiagnosticsService
from .services.notification_settings_service import NotificationSettingsService
from .notifications import events as notification_events
from .notifications import config as notification_config
from .notifications import secrets as notification_secrets
from .notifications.emitter import emit_notification
from .notifications.scheduler import NotificationDigestScheduler
from .notifications.transitions import emit_organizer_review_required
from .notifications.worker import make_notification_deliver_handler
from .services.public_submission_preflight_service import PublicSubmissionPreflightService
from .services.public_manual_preview_service import PublicManualPreviewService
from .services.public_bt_resolve_service import PublicBtResolveService
from .services.public_sixpan_preview_service import PublicSixpanPreviewService
from .services.update_service import UpdateService
from .runtime import RuntimeServices, RuntimeSnapshot, install_request_runtime
from .process_role import resolve_process_role, role_runs
from .runtime_builder import RuntimeBuilder, RuntimeRetirementQueue, rclone_runtime_config
from .blueprints.trending import TrendingRouteContext, create_trending_blueprint

# --- 兼容 re-export：以下纯 helper 的实现已迁至 media_serializers / path_planning /
# public_web / web_input 四个模块，这里保留名字供 create_app 闭包与外部测试导入。 ---
from .media_serializers import (
    _absolute_media_dirs,
    _build_media_dashboard,
    _fnos_media_type_label,
    _match_media_category,
    _media_asset_url,
    _media_category_index,
    _media_category_items,
    _media_count,
    _media_library_names,
    _media_posters,
    _media_refresh_indexes,
    _media_row_key,
    _media_target_path,
    _media_task_guid,
    _media_task_matches,
    _normalize_media_name,
)
from .path_planning import (
    _clean_update_openlist_root,
    _cloud139_real_folder_name,
    _cloud139_scan_filters_from_job,
    _common_top_directory,
    _is_update_season_dir_name,
    _join_virtual_path,
    _map_cloud139_path_to_openlist,
    _resource_root_for_import_mount,
    _resource_suffix_after_category_anchor,
    _resource_update_root,
    _rclone_organizer_target_plan,
    _safe_virtual_segment,
    _scope_scan_filter_path,
    _sixpan_scan_filters_from_job,
    _strip_virtual_prefix,
    _virtual_basename,
)
from .public_web import (
    _adapter_placeholders,
    _auto_submit_allowed,
    _btbtla_inspection_summary,
    _captcha_hash,
    _category_source_text,
    _category_suggestion,
    _cloud139_inspection_summary,
    _detail_capability_for_source,
    _elapsed_ms,
    _extract_file_rows,
    _extract_quark_pwd_id,
    _find_first_value,
    _format_size,
    _guest_safe_job_result,
    _hash_client_ip,
    _hash_password,
    _inspect_public_resource,
    _is_cloud139_public_item,
    _mask_share_url,
    _new_public_id,
    _new_request_token,
    _new_simple_captcha,
    _normalize_submission_mode,
    _preflight_public_submission,
    _public_adapter_capabilities,
    _public_cached_item,
    _public_categories,
    _public_file_item,
    _public_request_message_for_status,
    _public_request_payload,
    _public_request_response,
    _public_resource_child_files,
    _public_resource_detail,
    _public_resource_item,
    _public_result_key,
    _public_routes,
    _public_security_config,
    _public_status,
    _public_submit_message,
    _quark_inspection_summary,
    _random_code,
    _rclone_callback_level,
    _safe_public_cloud139_selection,
    _safe_public_quark_selection,
    _safe_public_sixpan_selection,
    _safe_public_string_list,
    _safe_search_preview,
    _short_text,
    _sixpan_default_selected,
    _sixpan_media_type,
    _sixpan_parse_file_item,
    _sixpan_parse_summary,
    _submission_mode,
    _verify_password_hash,
    _verify_public_captcha,
)
from .web_input import (
    PublicInputError,
    _clip_text,
    _config_bool,
    _config_int,
    _csv_values,
    _default_secret_key,
    _extract_url_candidate,
    _limited_text,
    _merge_raw_data,
    _payload_bool,
    _public_import_compensation_retry_job_id,
    _public_import_worker_result,
    _read_jsonl_tail,
    _recent_business_events,
    _redact_config,
    _safe_int,
    _safe_int_value,
    _sanitize_sources,
    _setting_bool,
    _strict_security_enabled,
    _task_log_summaries,
    _validate_public_host,
    _validate_public_url,
    _video_file_paths,
    _worker_dispatch_enabled_for_role,
)

__all__ = [
    "PublicInputError",
    "_absolute_media_dirs",
    "_adapter_placeholders",
    "_auto_submit_allowed",
    "_btbtla_inspection_summary",
    "_build_media_dashboard",
    "_captcha_hash",
    "_category_source_text",
    "_category_suggestion",
    "_clean_update_openlist_root",
    "_clip_text",
    "_cloud139_inspection_summary",
    "_cloud139_real_folder_name",
    "_cloud139_scan_filters_from_job",
    "_common_top_directory",
    "_config_bool",
    "_config_int",
    "_csv_values",
    "_default_secret_key",
    "_detail_capability_for_source",
    "_elapsed_ms",
    "_extract_file_rows",
    "_extract_quark_pwd_id",
    "_extract_url_candidate",
    "_find_first_value",
    "_fnos_media_type_label",
    "_format_size",
    "_guest_safe_job_result",
    "_hash_client_ip",
    "_hash_password",
    "_inspect_public_resource",
    "_is_cloud139_public_item",
    "_is_update_season_dir_name",
    "_join_virtual_path",
    "_limited_text",
    "_map_cloud139_path_to_openlist",
    "_mask_share_url",
    "_match_media_category",
    "_media_asset_url",
    "_media_category_index",
    "_media_category_items",
    "_media_count",
    "_media_library_names",
    "_media_posters",
    "_media_refresh_indexes",
    "_media_row_key",
    "_media_target_path",
    "_media_task_guid",
    "_media_task_matches",
    "_merge_raw_data",
    "_new_public_id",
    "_new_request_token",
    "_new_simple_captcha",
    "_normalize_media_name",
    "_normalize_submission_mode",
    "_payload_bool",
    "_preflight_public_submission",
    "_public_adapter_capabilities",
    "_public_cached_item",
    "_public_categories",
    "_public_file_item",
    "_public_import_compensation_retry_job_id",
    "_public_import_worker_result",
    "_public_request_message_for_status",
    "_public_request_payload",
    "_public_request_response",
    "_public_resource_child_files",
    "_public_resource_detail",
    "_public_resource_item",
    "_public_result_key",
    "_public_routes",
    "_public_security_config",
    "_public_status",
    "_public_submit_message",
    "_quark_inspection_summary",
    "_random_code",
    "_rclone_callback_level",
    "_rclone_organizer_target_plan",
    "_read_jsonl_tail",
    "_recent_business_events",
    "_redact_config",
    "_resource_root_for_import_mount",
    "_resource_suffix_after_category_anchor",
    "_resource_update_root",
    "_safe_int",
    "_safe_int_value",
    "_safe_public_cloud139_selection",
    "_safe_public_quark_selection",
    "_safe_public_sixpan_selection",
    "_safe_public_string_list",
    "_safe_search_preview",
    "_safe_virtual_segment",
    "_sanitize_sources",
    "_scope_scan_filter_path",
    "_setting_bool",
    "_short_text",
    "_sixpan_default_selected",
    "_sixpan_media_type",
    "_sixpan_parse_file_item",
    "_sixpan_parse_summary",
    "_sixpan_scan_filters_from_job",
    "_strict_security_enabled",
    "_strip_virtual_prefix",
    "_submission_mode",
    "_task_log_summaries",
    "_validate_public_host",
    "_validate_public_url",
    "_verify_password_hash",
    "_verify_public_captcha",
    "_video_file_paths",
    "_virtual_basename",
    "_worker_dispatch_enabled_for_role",
    "create_app",
]


ADMIN_PROFILE_KEY = "admin.profile"
SITE_BRANDING_KEY = "site.branding"


class _MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._fnos_memory_log = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            item = {
                "created_at": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage(),
                "line": line,
            }
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                item["exception"] = formatter.formatException(record.exc_info)
            with self._lock:
                self.records.append(item)
        except Exception:
            self.handleError(record)

    def tail(self, limit: int = 300, logger_prefix: str = "") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.records)
        if logger_prefix:
            items = [item for item in items if str(item.get("logger") or "").startswith(logger_prefix)]
        return items[-max(1, int(limit)) :]


def _install_file_log_handler(root_logger: logging.Logger, base_dir: Path) -> None:
    """Add a rotating file handler so application logs survive restarts.

    Uses ``LOG_FILE`` when set, otherwise ``<base_dir>/logs/app.log``
    (``/app/logs`` in the container is bind-mounted to the host).  Idempotent:
    any existing handler on the same resolved path is replaced first.
    """

    from logging.handlers import RotatingFileHandler

    log_file = os.getenv("LOG_FILE", "")
    if not log_file:
        log_file = str(base_dir / "logs" / "app.log")
    log_path = Path(log_file)
    file_handler: RotatingFileHandler | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root_logger.addHandler(file_handler)
    except Exception as exc:  # noqa: BLE001
        # 文件不可创建、不可写或 handler 注册失败时，保留 stdout 与内存日志，
        # 不能让可选的持久日志阻断应用启动。
        if file_handler is not None:
            try:
                root_logger.removeHandler(file_handler)
            except Exception:  # noqa: BLE001
                pass
            try:
                file_handler.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            root_logger.warning("持久日志不可用，已降级为控制台/内存日志：%s（%s）", log_path, exc)
        except Exception:  # noqa: BLE001
            pass
        return

    for existing in list(root_logger.handlers):
        if existing is not file_handler and isinstance(existing, RotatingFileHandler):
            try:
                if Path(existing.baseFilename).resolve() == log_path.resolve():
                    root_logger.removeHandler(existing)
                    try:
                        existing.close()
                    except OSError:
                        pass
            except (OSError, ValueError):
                continue


def _resolve_secret_key(config_value: str, db: Database) -> str:
    """解析应用签名密钥：显式配置优先；否则用 DB 持久化的随机 key；都没有则生成并持久化。

    默认值 change-me-in-production 是公开已知的，会让攻击者可伪造签名 session 提权
    admin，因此弱默认值一律替换为随机 key（持久化到 app_settings，重启保持稳定）。
    """
    configured = str(config_value or "").strip()
    if configured and not _default_secret_key(configured):
        return configured
    settings = db.get_app_settings() if callable(getattr(db, "get_app_settings", None)) else {}
    persisted = str((settings or {}).get("app.secret_key") or "").strip()
    if persisted and not _default_secret_key(persisted):
        return persisted
    generated = secrets.token_hex(32)
    if callable(getattr(db, "set_app_settings", None)):
        db.set_app_settings({"app.secret_key": generated})
    return generated


def _hot_title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _hot_identity_year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _hot_identity_category(item: dict[str, Any]) -> str:
    raw = str(item.get("category") or item.get("media_type") or "").strip().lower()
    aliases = {
        "movie": "movie",
        "film": "movie",
        "tv": "tv",
        "series": "tv",
        "anime": "anime",
        "animation": "anime",
        "variety": "variety",
        "show": "variety",
    }
    return aliases.get(raw, "")


def _hot_record_year(item: dict[str, Any]) -> str:
    for key in ("year", "tmdb_year", "release_year", "canonical_year"):
        year = _hot_identity_year(item.get(key))
        if year:
            return year
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (raw_data, raw_data.get("metadata"), raw_data.get("tmdb")):
        if not isinstance(container, dict):
            continue
        for key in ("year", "tmdb_year", "release_year", "canonical_year"):
            year = _hot_identity_year(container.get(key))
            if year:
                return year
    return _hot_identity_year(item.get("title"))


def _hot_record_tmdb_id(item: dict[str, Any]) -> int:
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (item, raw_data, raw_data.get("subscription_identity"), raw_data.get("tmdb")):
        if not isinstance(container, dict):
            continue
        try:
            tmdb_id = int(container.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            continue
        if tmdb_id > 0:
            return tmdb_id
    return 0


def _hot_record_season(item: dict[str, Any]) -> int | None:
    raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
    for container in (item, raw_data, raw_data.get("subscription_identity"), raw_data.get("tmdb")):
        if not isinstance(container, dict):
            continue
        value = container.get("season")
        if value in (None, ""):
            continue
        try:
            season = int(value)
        except (TypeError, ValueError):
            continue
        if season >= 0:
            return season
    return None


def _hot_identity_matches(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_tmdb_id = _hot_record_tmdb_id(candidate)
    existing_tmdb_id = _hot_record_tmdb_id(existing)
    same_tmdb_identity = bool(candidate_tmdb_id and candidate_tmdb_id == existing_tmdb_id)
    if candidate_tmdb_id and existing_tmdb_id:
        if candidate_tmdb_id != existing_tmdb_id:
            return False
    elif _hot_title_key(candidate.get("title")) != _hot_title_key(existing.get("title")):
        return False
    candidate_category = _hot_identity_category(candidate)
    existing_category = _hot_identity_category(existing)
    if not candidate_category or not existing_category or candidate_category != existing_category:
        return False
    candidate_season = _hot_record_season(candidate)
    existing_season = _hot_record_season(existing)
    if candidate_season != existing_season:
        return False
    candidate_year = _hot_record_year(candidate)
    existing_year = _hot_record_year(existing)
    if not same_tmdb_identity:
        if bool(candidate_year) != bool(existing_year):
            return False
        if candidate_year != existing_year:
            return False
    return True


def _find_hot_existing_subscription(
    item: dict[str, Any],
    subscriptions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return one unambiguous existing subscription for a hot candidate.

    Title/category alone cannot choose between remakes or multiple seasonal
    subscriptions.  Ambiguous matches deliberately fall through to the TMDB
    identity flow, which binds by ``tmdb_id/category/season`` atomically.
    """

    matches = [
        row
        for row in subscriptions
        if str(row.get("status") or "enabled").strip().lower() != "archived"
        and _hot_identity_matches(item, row)
    ]
    return matches[0] if len(matches) == 1 else None


def create_app(config_path: str | None = None, process_role: str | None = None) -> Flask:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    app_config = load_config(config_path)
    active_process_role = resolve_process_role(process_role)
    process_owner_id = f"{os.getenv('HOSTNAME') or os.getenv('COMPUTERNAME') or 'host'}:{os.getpid()}:{secrets.token_hex(6)}"
    db = Database(app_config.database_path)
    admin_config = app_config.raw.get("admin", {}) if isinstance(app_config.raw.get("admin"), dict) else {}
    admin_profile_service = AdminProfileService(
        AdminProfileRepository(db.app_settings),
        admin_config,
        app_config.base_dir / "static" / "uploads",
        _hash_password,
        _verify_password_hash,
    )
    db.init_schema()
    app_config = apply_persisted_config(app_config, db.get_app_settings())

    def _store_sixpan_tokens(tokens: dict[str, Any]) -> bool:
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not access_token and not refresh_token:
            return False

        def merge_tokens(current: Any, _existed: bool) -> dict[str, Any]:
            stored = json.loads(json.dumps(current, ensure_ascii=False)) if isinstance(current, dict) else {}
            sixpan_config = stored.setdefault("sixpan", {})
            if not isinstance(sixpan_config, dict):
                sixpan_config = {}
                stored["sixpan"] = sixpan_config
            if access_token:
                sixpan_config["access_token"] = access_token
            if refresh_token:
                sixpan_config["refresh_token"] = refresh_token
            return stored

        atomic_update = getattr(db, "update_app_setting_atomic", None)
        if callable(atomic_update):
            atomic_update(ADVANCED_CONFIG_KEY, merge_tokens)
        else:  # pragma: no cover - Database always supports the atomic path
            settings = db.get_app_settings()
            current = settings.get(ADVANCED_CONFIG_KEY) if isinstance(settings.get(ADVANCED_CONFIG_KEY), dict) else {}
            db.set_app_settings({ADVANCED_CONFIG_KEY: merge_tokens(current, ADVANCED_CONFIG_KEY in settings)})
        return True

    runtime_builder = RuntimeBuilder(db, _store_sixpan_tokens, owner_id=process_owner_id)
    runtime_build = runtime_builder.build(
        app_config,
        recover_background=role_runs(active_process_role, "worker"),
    )
    runtime_retirement = RuntimeRetirementQueue(
        grace_seconds=max(30, _config_int(app_config.raw.get("app", {}), "runtime_retire_grace_seconds", 300))
    )
    atexit.register(runtime_retirement.close_all)
    pansou = runtime_build.pansou
    btbtla = runtime_build.btbtla
    quark_importer = runtime_build.quark_importer
    cloud139_importer = runtime_build.cloud139_importer
    generic_importers = runtime_build.generic_importers
    fnos = runtime_build.fnos
    search_service = runtime_build.search_service
    import_service = runtime_build.import_service
    organizer_service = runtime_build.organizer_service
    btbtla_proxy_diagnostics_service = BtbtlaProxyDiagnosticsService(
        current_config=lambda: app_config.raw.get("btbtla", {}),
        current_routes=lambda: app_config.raw.get("routes", {}),
    )

    job_service = JobService(db)
    rclone_service = RcloneService(
        rclone_runtime_config(app_config),
        app_config.base_dir,
        app_config.raw["fnos"],
        db=db,
        categories=app_config.categories,
        cmcc_upload_config=app_config.raw.get("cmcc_upload", {}),
        cloud139_config=app_config.raw.get("cloud139", {}),
        owner_id=process_owner_id,
    )
    rclone_webdav_config_service = RcloneWebdavConfigService(lambda: rclone_service.config)
    update_service = UpdateService(
        db,
        app_config.raw,
        app_config.categories,
        search_service,
        import_service,
        quark_importer,
        cloud139_importer,
        import_handler=lambda result, reason: _auto_start_rclone_for_import(result, reason),
        owner_id=process_owner_id,
    )
    update_scheduler_config = app_config.raw.get("update_scheduler", {}) if isinstance(app_config.raw.get("update_scheduler"), dict) else {}
    try:
        update_scheduler_interval = max(30, min(3600, int(update_scheduler_config.get("interval_seconds") or 60)))
    except (TypeError, ValueError):
        update_scheduler_interval = 60
    update_scheduler = UpdateScheduler(
        update_service,
        interval_seconds=update_scheduler_interval,
        enabled=_config_bool(update_scheduler_config, "enabled", True),
        max_subscriptions_per_tick=_config_int(update_scheduler_config, "max_subscriptions_per_tick", 5),
        coalesce_missed_runs=_config_bool(update_scheduler_config, "coalesce_missed_runs", True),
        owner_id=process_owner_id,
    )
    update_scheduler_runtime = UpdateSchedulerRuntime(update_scheduler)
    atexit.register(update_scheduler_runtime.shutdown)
    hot_config = app_config.raw.get("hot_discovery", {}) if isinstance(app_config.raw.get("hot_discovery"), dict) else {}
    hot_timeout = max(3, _config_int(hot_config, "timeout", 20))
    hot_max_items = max(1, _config_int(hot_config, "max_items_per_source", 20))
    hot_sources: list[Any] = []
    if _config_bool(hot_config, "tencent_enabled", True):
        hot_sources.append(
            TencentHotSource(
                {
                    "endpoint": hot_config.get("tencent_endpoint"),
                    "data_version": hot_config.get("tencent_data_version"),
                    "page_size": max(200, hot_max_items * 10),
                    "max_items": hot_max_items,
                    "timeout": hot_timeout,
                }
            )
        )
    if _config_bool(hot_config, "iqiyi_enabled", True):
        iqiyi_device_id = str(hot_config.get("iqiyi_device_id") or "").strip()
        if not iqiyi_device_id:
            app_secret = str((app_config.raw.get("app", {}) or {}).get("secret_key") or "")
            iqiyi_device_id = hashlib.sha256(f"{app_secret}:{process_owner_id.split(':', 1)[0]}".encode("utf-8")).hexdigest()[:32]
        hot_sources.append(
            IqiyiHotSource(
                {
                    "endpoint": hot_config.get("iqiyi_endpoint"),
                    "device_id": iqiyi_device_id,
                    "version": hot_config.get("iqiyi_version"),
                    "timeout": hot_timeout,
                    "max_items": hot_max_items,
                }
            )
        )
    if _config_bool(hot_config, "youku_enabled", True):
        hot_sources.append(YoukuHotSource({
            "endpoint": hot_config.get("youku_url"),
            "timeout": hot_timeout,
            "max_items": hot_max_items,
        }))

    def _hot_media_exists(item: dict[str, Any]) -> bool:
        title = str(item.get("title") or "").strip()
        key = _hot_title_key(title)
        if not key:
            return False
        return any(
            _hot_identity_matches(item, job)
            for job in db.list_jobs(limit=100, status=JOB_DONE, keyword=title)
        )

    def _hot_existing_subscription(item: dict[str, Any]) -> dict[str, Any] | None:
        if not _hot_title_key(item.get("title")):
            return None
        return _find_hot_existing_subscription(
            item,
            db.list_update_subscriptions(limit=5000, include_sources=False),
        )

    def _hot_task_exists(item: dict[str, Any]) -> bool:
        if _hot_existing_subscription(item):
            return True
        key = _hot_title_key(item.get("title"))
        if not key:
            return False
        return any(
            _hot_identity_matches(item, job)
            for job in db.list_jobs(limit=100, keyword=str(item.get("title") or ""))
            if str(job.get("status") or "") not in {JOB_DONE, JOB_FAILED, JOB_CANCELLED}
        )

    trending_service = TrendingDiscoveryService(
        sources=hot_sources,
        repository=db,
        media_exists=_hot_media_exists,
        task_exists=_hot_task_exists,
        owner_id=process_owner_id,
        max_items_per_source=hot_max_items,
    )
    trending_scheduler = TrendingDiscoveryScheduler(
        service=trending_service,
        database=db,
        owner_id=process_owner_id,
        enabled=_config_bool(hot_config, "enabled", False),
        run_at=str(hot_config.get("run_at") or "08:30"),
        timezone_name=str(hot_config.get("timezone") or "Asia/Shanghai"),
        process_role=active_process_role,
    )
    atexit.register(trending_scheduler.shutdown)
    runtime_services = RuntimeServices(
        RuntimeSnapshot(
            config=app_config,
            database=db,
            pansou=pansou,
            btbtla=btbtla,
            quark_importer=quark_importer,
            cloud139_importer=cloud139_importer,
            generic_importers=generic_importers,
            fnos=fnos,
            search_service=search_service,
            import_service=import_service,
            organizer_service=organizer_service,
            job_service=job_service,
            rclone_service=rclone_service,
            update_service=update_service,
            update_scheduler=update_scheduler,
        )
    )
    worker_task_dispatcher = WorkerTaskDispatcher(
        repository=db.worker_tasks,
        enabled=lambda: _worker_dispatch_enabled_for_role(
            app_config.raw.get("worker", {}) if isinstance(app_config.raw.get("worker"), dict) else {},
            active_process_role,
        ),
        config_revision=lambda: runtime_services.revision,
    )
    def _dispatch_rclone_history_repair(limit: int) -> dict[str, Any]:
        queued = worker_task_dispatcher.rclone_repair(
            limit=limit,
            recovery_key=process_owner_id,
        )
        return queued or rclone_service.repair_waiting_jobs_from_history(limit)

    history_repair_worker = RcloneHistoryRepairWorker(
        database=db,
        owner_id=process_owner_id,
        repair=_dispatch_rclone_history_repair,
        log=rclone_service._append_log,
        limit=50,
        interval_seconds=max(
            30,
            _config_int(
                app_config.raw.get("rclone", {}) if isinstance(app_config.raw.get("rclone"), dict) else {},
                "history_repair_interval_seconds",
                300,
            ),
        ),
    )
    rclone_worker_runtime = RcloneWorkerRuntime(
        rclone_service=rclone_service,
        history_repair=history_repair_worker,
    )
    atexit.register(rclone_worker_runtime.shutdown)
    search_cache_maintenance_worker = SearchCacheMaintenanceWorker(
        database=db,
        owner_id=f"{process_owner_id}-search-cache",
        log=logging.getLogger("fnos_media_import.search_cache").info,
    )
    atexit.register(search_cache_maintenance_worker.shutdown)
    event_retention_worker = EventRetentionWorker(
        database=db,
        owner_id=f"{process_owner_id}-events",
        log=logging.getLogger("fnos_media_import.events").info,
        retention_days=_safe_int(os.getenv("EVENT_RETENTION_DAYS"), 90, 1, 3650),
    )
    atexit.register(event_retention_worker.shutdown)

    def _worker_rclone_repair(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        return rclone_service.repair_waiting_jobs_from_history(
            limit=_safe_int(payload.get("limit"), 50, 1, 500)
        )

    def _worker_organizer_process(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_int(payload.get("task_id"), 0, 1, 999999999)
        if not task_id:
            raise ValueError("Organizer Worker 任务缺少 task_id")
        return organizer_service.process_task_from_worker(
            task_id,
            auto_apply=_payload_bool(payload, "auto_apply", True),
            respect_schedule=_payload_bool(payload, "respect_schedule", True),
        )

    def _worker_organizer_apply(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_int(payload.get("task_id"), 0, 1, 999999999)
        if not task_id:
            raise ValueError("Organizer Apply Worker 任务缺少 task_id")
        apply_from_worker = getattr(organizer_service, "apply_task_from_worker", None)
        if callable(apply_from_worker):
            return apply_from_worker(task_id)
        return organizer_service.apply_task(task_id)

    def _worker_media_refresh(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        library = str(payload.get("library") or "").strip()
        if not library:
            raise ValueError("媒体刷新 Worker 任务缺少 library")
        guid = str(payload.get("guid") or "").strip()
        if guid:
            return fnos.refresh_guid(guid, library=library, dir_list=payload.get("dir_list"))
        return fnos.refresh(library, dir_list=payload.get("dir_list"))

    def _worker_media_category_refresh(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        category = str(payload.get("category") or "").strip()
        if not category:
            raise ValueError("媒体分类刷新 Worker 任务缺少 category")
        return import_service.refresh_media(category)

    def _worker_import_retry(payload: dict[str, Any], _task: dict[str, Any]) -> dict[str, Any]:
        job_id = _safe_int(payload.get("job_id"), 0, 1, 999999999)
        if not job_id:
            raise ValueError("Import Retry Worker 任务缺少 job_id")
        result = import_service.retry_job(job_id)
        rclone_start = _auto_start_rclone_for_import(
            result,
            str(payload.get("reason") or f"worker_retry:{job_id}"),
        )
        return {**result, "rclone_start": rclone_start}

    def _worker_public_import_create(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        request_id = _safe_int(payload.get("guest_request_id"), 0, 1, 999999999)
        request_token = str(payload.get("request_token") or "").strip()
        submit_payload = payload.get("submit_payload") if isinstance(payload.get("submit_payload"), dict) else {}
        request_updates = payload.get("request_updates") if isinstance(payload.get("request_updates"), dict) else {}
        if not request_id or not request_token or not submit_payload:
            raise ValueError("Public Import Worker 任务参数不完整")
        attempts = _safe_int(task.get("attempts"), 1, 1, 999)
        max_attempts = _safe_int(task.get("max_attempts"), 3, 1, 999)
        submit_payload = {
            **submit_payload,
            "config_revision": _safe_int(task.get("config_revision"), 1, 1, 999999999),
            "executor_id": str(task.get("owner_id") or process_owner_id),
        }
        outcome = public_import_job_coordinator.execute_inline(
            guest_request_id=request_id,
            request_token=request_token,
            submit_payload=submit_payload,
            mark_failure=attempts >= max_attempts,
            request_updates=request_updates,
            compensation_retry_job_id=_public_import_compensation_retry_job_id(task),
        )
        return _public_import_worker_result(outcome)

    notification_deliver_handler = make_notification_deliver_handler(db)
    notification_digest_scheduler = NotificationDigestScheduler(
        database=db,
        owner_id=f"{process_owner_id}-digest",
        log=logging.getLogger("fnos_media_import.notifications").info,
    )

    def _emit_configured_notification(
        database: Any,
        event_type: str,
        context: dict[str, Any],
        *,
        idempotency_key: str,
        connection: Any,
        include_admin: bool = True,
    ) -> list[dict[str, Any]]:
        config = notification_config.read_config(database)
        configured = notification_config.resolve_channels(config, event_type)
        emitted: list[dict[str, Any]] = []
        admin_channels = [
            channel
            for channel in configured
            if channel != notification_events.CHANNEL_GUEST_EMAIL
        ]
        if include_admin and admin_channels:
            result = emit_notification(
                database,
                event_type,
                context,
                idempotency_key=idempotency_key,
                connection=connection,
                channels_override=admin_channels,
            )
            if result is not None:
                emitted.append(result)

        request_id = _safe_int_value(context.get("request_id"), 0)
        if notification_events.CHANNEL_GUEST_EMAIL in configured and request_id:
            subscription = connection.execute(
                """
                SELECT verified_at, opted_out_at
                FROM guest_notification_subscriptions WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
            if subscription and subscription["verified_at"] and not subscription["opted_out_at"]:
                result = emit_notification(
                    database,
                    event_type,
                    context,
                    idempotency_key=f"{idempotency_key}:guest:{request_id}",
                    connection=connection,
                    channels_override=[notification_events.CHANNEL_GUEST_EMAIL],
                )
                if result is not None:
                    emitted.append(result)
        return emitted

    def _emit_job_status_notification(
        connection: Any,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        status = str(current.get("status") or "").strip().lower()
        if status == JOB_REVIEW:
            emit_organizer_review_required(
                db,
                connection,
                current,
                _emit_configured_notification,
            )
            return
        if status not in {JOB_DONE, JOB_FAILED}:
            return
        previous_status = str(previous.get("status") or "").strip().lower()
        event_type = (
            notification_events.EVENT_JOB_DONE
            if status == JOB_DONE
            else notification_events.EVENT_JOB_FAILED
        )
        job_id = int(current["id"])
        transition_ref = (
            str(job_id)
            if status == JOB_DONE
            else f"{job_id}:{previous_status}->{status}:{current.get('updated_at') or ''}"
        )
        base_key = notification_events.idempotency_key(event_type, transition_ref)
        base_context = {
            "job_id": job_id,
            "title": str(current.get("title") or "未命名资源"),
            "stage": previous_status or "unknown",
            "error": str(current.get("error_message") or ""),
        }
        _emit_configured_notification(
            db,
            event_type,
            base_context,
            idempotency_key=base_key,
            connection=connection,
        )
        linked_requests = connection.execute(
            """
            SELECT id, request_token, title
            FROM guest_requests WHERE job_id=? ORDER BY id ASC
            """,
            (job_id,),
        ).fetchall()
        for linked in linked_requests:
            _emit_configured_notification(
                db,
                event_type,
                {
                    **base_context,
                    "request_id": int(linked["id"]),
                    "request_token": str(linked["request_token"] or ""),
                    "title": str(linked["title"] or base_context["title"]),
                },
                idempotency_key=base_key,
                connection=connection,
                include_admin=False,
            )

    db.job_commands.set_status_transition_emitter(_emit_job_status_notification)

    durable_worker_runtime = DurableWorkerRuntime(
        repository=db.worker_tasks,
        owner_id=f"{process_owner_id}-durable",
        handlers={
            "rclone_repair": _worker_rclone_repair,
            "organizer_process": _worker_organizer_process,
            "organizer_apply": _worker_organizer_apply,
            "media_refresh": _worker_media_refresh,
            "media_category_refresh": _worker_media_category_refresh,
            "import_retry": _worker_import_retry,
            "public_import_create": _worker_public_import_create,
            "notification_deliver": notification_deliver_handler,
        },
        poll_seconds=_config_int(app_config.raw.get("worker", {}), "poll_seconds", 1),
        lease_seconds=_config_int(app_config.raw.get("worker", {}), "lease_seconds", 120),
        retry_delay_seconds=_config_int(app_config.raw.get("worker", {}), "retry_delay_seconds", 30),
        retention_days=_config_int(app_config.raw.get("worker", {}), "retention_days", 7),
        cleanup_interval_seconds=_config_int(app_config.raw.get("worker", {}), "cleanup_interval_seconds", 3600),
        log=logging.getLogger("fnos_media_import.worker").info,
    )
    atexit.register(durable_worker_runtime.shutdown)
    atexit.register(notification_digest_scheduler.shutdown)
    worker_queue_diagnostics = WorkerQueueDiagnosticsService(
        repository=db.worker_tasks,
        runtime=durable_worker_runtime,
        dispatch_enabled=worker_task_dispatcher.enabled,
        runtime_required=lambda: role_runs(active_process_role, "worker"),
    )

    import_completion_dispatcher = ImportCompletionDispatcher(
        database=db,
        rclone_service=rclone_service,
        category=lambda key: app_config.category(key) if key in app_config.categories else {},
        enqueue_organizer=lambda result, reason: _enqueue_organizer_from_completed_import(result, reason),
    )

    def _start_rclone_for_job(job: dict[str, Any], reason: str) -> dict[str, Any] | None:
        return import_completion_dispatcher.start_rclone_for_job(job, reason)

    def _job_uses_rclone_staging(job: dict[str, Any]) -> bool:
        return import_completion_dispatcher.uses_rclone_staging(job)

    def _auto_start_rclone_for_import(result: dict[str, Any], reason: str) -> dict[str, Any] | None:
        return import_completion_dispatcher.dispatch(result, reason)

    if role_runs(active_process_role, "scheduler"):
        update_scheduler_runtime.start()
        trending_scheduler.start()
        notification_digest_scheduler.start()

    def _organizer_plan_for_rclone_completed_item(item: dict[str, Any]) -> dict[str, Any] | None:
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        staging_plan = staging_plan_from_job(job)
        category_key = str(item.get("category") or job.get("category") or "").strip()
        category = app_config.category(category_key) if category_key in app_config.categories else {}
        update_payload_extra = _update_organizer_payload_extra(job)
        preferred_root = str(
            update_payload_extra.get("target_root_path")
            or update_payload_extra.get("canonical_resource_root")
            or ""
        ).strip()
        root_path = _rclone_completed_openlist_root(
            category_key,
            category,
            item,
            preferred_root=preferred_root,
        )
        target_paths = [
            str(value or "").strip()
            for value in (item.get("target_paths") or [])
            if str(value or "").strip()
        ]
        video_target_paths = _video_file_paths(target_paths)
        openlist_transport_paths = [
            path
            for path in (
                _map_category_path_to_openlist(path, category, staging_plan)
                for path in target_paths
            )
            if path
        ]
        openlist_target_paths = [
            path
            for path in (
                _map_category_path_to_openlist(path, category, staging_plan)
                for path in video_target_paths
            )
            if path
        ]
        source_paths = [
            str(value or "").strip()
            for value in (item.get("source_paths") or [])
            if str(value or "").strip()
        ]
        video_source_paths = _video_file_paths(source_paths)
        expected_target_paths = _dedupe_strings(openlist_target_paths or video_target_paths)
        base_scan_filters = {
            "expected_paths": expected_target_paths,
            "expected_names": _dedupe_strings([
                Path(path.replace("\\", "/")).name
                for path in expected_target_paths
                if path
            ]),
            "expected_count": len(expected_target_paths),
        }
        scan_filters = _merge_scan_filters(
            base_scan_filters,
            update_payload_extra.get("scan_filters")
            if isinstance(update_payload_extra.get("scan_filters"), dict)
            else {},
        )
        category_openlist_root = str(staging_plan.get("openlist_final_category_root") or "").strip() or _rclone_category_openlist_root(category)
        target_plan = _rclone_organizer_target_plan(category_openlist_root, preferred_root)
        return {
            "root_path": root_path,
            "payload_extra": {
                **update_payload_extra,
                # rclone 搬运 manifest 覆盖视频、字幕、NFO 等全部文件；
                # Organizer 的 scan_videos 完整性只能使用视频子集。
                "target_paths": openlist_target_paths or video_target_paths,
                "raw_target_paths": target_paths,
                "transport_target_paths": _dedupe_strings(openlist_transport_paths),
                "source_paths": video_source_paths,
                "raw_source_paths": source_paths,
                "transport_file_count": len(_dedupe_strings(target_paths)),
                "video_file_count": len(expected_target_paths),
                "scan_filters": scan_filters,
                "staging_plan": staging_plan,
                **target_plan,
            },
        }

    def _enqueue_organizer_from_rclone_completed_items(
        category_refresh: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return organizer_dispatch_service.enqueue_rclone_completed_items(category_refresh, payload)

    def _rclone_completed_openlist_root(category_key: str, category: dict[str, Any], item: dict[str, Any], *, preferred_root: str = "") -> str:
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        staging_plan = staging_plan_from_job(job)
        staged_job_root = _safe_openlist_path(staging_plan.get("openlist_job_root"))
        if staged_job_root:
            return staged_job_root
        target_paths = [str(value or "").strip() for value in (item.get("target_paths") or []) if str(value or "").strip()]
        if not target_paths:
            return _safe_openlist_path(preferred_root)
        mapped_roots = []
        for target_path in target_paths:
            root = organizer_service._root_path_from_target(category_key, target_path)  # noqa: SLF001
            mapped = _map_category_path_to_openlist(root, category, staging_plan)
            if mapped:
                mapped_roots.append(mapped)
        common_root = _safe_openlist_path(_common_virtual_path(mapped_roots))
        category_root = _rclone_category_openlist_root(category)
        preferred = _resource_root_for_import_mount(preferred_root, category_root) or _safe_openlist_path(preferred_root)
        if preferred and (not common_root or _same_virtual_path(common_root, category_root)):
            return preferred
        if common_root:
            return common_root
        return category_root

    def _map_category_path_to_openlist(path: str, category: dict[str, Any], staging_plan: dict[str, Any] | None = None) -> str:
        value = _safe_openlist_path(path)
        if not value:
            return ""
        staged = _safe_openlist_path(map_staging_path_to_openlist(value, staging_plan))
        if staged:
            return staged
        if _is_quark_staging_path(value, category):
            return ""
        return _safe_openlist_path(
            map_upload_path_to_openlist(
                value,
                category,
                backend=upload_backend(rclone_service.config, rclone_service.cmcc_upload_config),
                cloud139_config=rclone_service.cloud139_config,
            )
        )

    def _rclone_category_openlist_root(category: dict[str, Any]) -> str:
        root = openlist_root_for_upload(
            category,
            backend=upload_backend(rclone_service.config, rclone_service.cmcc_upload_config),
            cloud139_config=rclone_service.cloud139_config,
        )
        return _safe_openlist_path(root) or _category_openlist_root(category)

    def _safe_openlist_path(value: Any) -> str:
        text = str(value or "").replace("\\", "/").strip()
        if not text or "://" in text:
            return ""
        normalized = text.strip("/").strip()
        lowered = normalized.lower()
        if not normalized or lowered in {"none", "null", "undefined"} or "/none" in f"/{lowered}" or lowered.endswith("/none"):
            return ""
        return "/" + normalized

    def _category_openlist_root(category: dict[str, Any]) -> str:
        for key in ("openlist_root_path", "cloud139_fnos_target_path", "mobile_openlist_root_path", "mobile_target_path", "sixpan_fnos_target_path"):
            value = _safe_openlist_path(category.get(key))
            if value:
                return value
        return _safe_openlist_path(category.get("label"))

    def _same_virtual_path(left: Any, right: Any) -> bool:
        left_norm = _normalize_virtual_compare(left)
        right_norm = _normalize_virtual_compare(right)
        return bool(left_norm and right_norm and left_norm == right_norm)

    def _job_update_context(job: dict[str, Any] | None) -> dict[str, Any]:
        raw_data = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
        request_payload = raw_data.get("request") if isinstance(raw_data.get("request"), dict) else {}
        context = request_payload.get("update_context") if isinstance(request_payload.get("update_context"), dict) else {}
        organizer_context = request_payload.get("organizer_context") if isinstance(request_payload.get("organizer_context"), dict) else {}
        merged = {**context, **organizer_context}
        canonical_root = _resource_update_root(
            merged.get("canonical_openlist_root")
            or merged.get("canonical_resource_root")
            or merged.get("target_root_path")
            or merged.get("resource_root_path")
        )
        if canonical_root:
            merged["canonical_openlist_root"] = canonical_root
            merged["canonical_resource_root"] = canonical_root
            merged["target_root_path"] = canonical_root
            merged["target_root_is_resource"] = True
        scan_filters = merged.get("scan_filters") if isinstance(merged.get("scan_filters"), dict) else {}
        merged["scan_filters"] = _normalize_scan_filters(scan_filters)
        return merged

    def _normalize_scan_filters(filters: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(filters, dict):
            return {"expected_names": [], "expected_paths": [], "expected_count": 0}
        expected_count = _safe_int(filters.get("expected_count"), 0, 0, 1000000)
        return {
            "expected_names": _dedupe_strings([str(item or "").strip() for item in (filters.get("expected_names") or []) if str(item or "").strip()]),
            "expected_paths": _dedupe_strings([str(item or "").strip() for item in (filters.get("expected_paths") or []) if str(item or "").strip()]),
            "expected_count": expected_count,
        }

    def _merge_scan_filters(*filters_list: dict[str, Any]) -> dict[str, Any]:
        names: list[str] = []
        paths: list[str] = []
        expected_count = 0
        for filters in filters_list:
            normalized = _normalize_scan_filters(filters)
            names.extend(normalized.get("expected_names") or [])
            paths.extend(normalized.get("expected_paths") or [])
            expected_count = max(expected_count, int(normalized.get("expected_count") or 0))
        normalized_names = _dedupe_strings(names)
        normalized_paths = _dedupe_strings(paths)
        return {
            "expected_names": normalized_names,
            "expected_paths": normalized_paths,
            "expected_count": max(expected_count, len(normalized_paths), len(normalized_names)),
        }

    def _update_organizer_payload_extra(job: dict[str, Any] | None) -> dict[str, Any]:
        context = _job_update_context(job)
        canonical_root = str(context.get("canonical_openlist_root") or "").strip()
        if not canonical_root:
            return {}
        return {
            "update_context": context,
            "target_root_path": canonical_root,
            "canonical_resource_root": canonical_root,
            "target_root_is_resource": True,
            "allow_same_root_task": True,
            "scan_filters": context.get("scan_filters") or {},
        }

    def _normalize_virtual_compare(value: Any) -> str:
        return str(value or "").strip().replace("\\", "/").strip("/").casefold()

    def _virtual_path_under(path: Any, root: Any) -> bool:
        normalized_path = _normalize_virtual_compare(path)
        normalized_root = _normalize_virtual_compare(root)
        if not normalized_path or not normalized_root:
            return False
        return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")

    def _is_quark_staging_path(path: Any, category: dict[str, Any]) -> bool:
        return _virtual_path_under(path, category.get("quark_save_path"))

    def _common_virtual_path(paths: list[str]) -> str:
        cleaned = [str(path or "").strip().strip("/") for path in paths if str(path or "").strip().strip("/")]
        if not cleaned:
            return ""
        split_paths = [path.split("/") for path in cleaned]
        common: list[str] = []
        for parts in zip(*split_paths):
            first = parts[0]
            if all(part == first for part in parts):
                common.append(first)
            else:
                break
        return "/" + "/".join(common) if common else ""

    def _dedupe_strings(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    def _path_health_checks(
        *,
        official_save_path: str = "",
        official_save_label: str = "官方网盘保存路径",
        rclone_target_path: str = "",
        rclone_target_required: bool = False,
        openlist_visible_path: str = "",
        organizer_scan_path: str = "",
        organized_target_path: str = "",
        openlist_required: bool = True,
        organizer_required: bool = True,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def _add(
            name: str,
            label: str,
            value: str,
            required: bool = True,
            *,
            success_message: str = "路径正常",
            empty_message: str = "路径为空",
        ) -> None:
            text = str(value or "").strip()
            # 可选路径尚未由后续阶段生成时不是异常，也不应出现在黄色告警中。
            # 等 Organizer 真正产出目标目录后再展示该检查项。
            if not required and not text:
                return
            invalid = _is_invalid_virtual_path(text)
            checks.append(
                {
                    "name": name,
                    "label": label,
                    "path": text,
                    "success": bool(text and not invalid) if required else not invalid,
                    "message": success_message if text and not invalid else (empty_message if not text else "路径异常，疑似 None 或 /None"),
                }
            )

        _add("official_save_path", official_save_label, official_save_path)
        if rclone_target_required or str(rclone_target_path or "").strip():
            _add(
                "rclone_target_path",
                "rclone 搬运目标路径",
                rclone_target_path,
                required=rclone_target_required,
                success_message="rclone 将按后台分类配置搬运到该目录",
                empty_message="分类 mobile_target_path 未配置，rclone 目标不明确",
            )
        _add("openlist_visible_path", "OpenList 可见路径", openlist_visible_path, required=openlist_required)
        _add("organizer_scan_path", "Organizer 扫描路径", organizer_scan_path, required=organizer_required)
        _add("organized_target_path", "标准整理目标路径", organized_target_path, required=False)
        if not organizer_service.enabled:
            checks.append({"name": "organizer_enabled", "label": "Organizer", "success": False, "message": "Organizer 未启用，不能确认完整整理入库"})
        if not organizer_service.openlist.configured:
            checks.append({"name": "openlist_configured", "label": "OpenList", "success": False, "message": "OpenList 未配置，不能确认标准目录"})
        return checks

    def _is_invalid_virtual_path(value: Any) -> bool:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            return True
        normalized = text.strip("/").strip().lower()
        return normalized in {"none", "null", "undefined"} or "/none" in f"/{normalized}" or normalized.endswith("/none")

    def _job_completion_info(job: dict[str, Any]) -> dict[str, Any]:
        return job_completion_info_service.build(job)

    def _decorate_job_completion(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(job, dict):
            return job
        return {**job, **_job_completion_info(job)}

    def _set_job_completion_stage(job: dict[str, Any], status: str, stage: str, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = _safe_int(job.get("id"), 0, 1, 999999999)
        if not job_id:
            return job
        info = _job_completion_info(job)
        extra_data = extra or {}
        official_save_path = str(extra_data.get("official_save_path") or info.get("official_save_path") or "")
        openlist_visible_path = str(extra_data.get("openlist_visible_path") or info.get("openlist_visible_path") or "")
        organizer_scan_path = str(extra_data.get("organizer_scan_path") or info.get("organizer_scan_path") or "")
        if organizer_scan_path and not openlist_visible_path:
            openlist_visible_path = organizer_scan_path
        patch = {
            "completion": {
                "stage": stage,
                "official_save_path": official_save_path,
                "openlist_visible_path": openlist_visible_path,
                "organizer_scan_path": organizer_scan_path,
                "organized_target_path": str(extra_data.get("organized_target_path") or info.get("organized_target_path") or ""),
                "checks": info.get("completion_checks") or [],
                "message": message,
                **extra_data,
            }
        }
        expected_status = str(job.get("status") or "").strip()
        if not expected_status or not db.update_job_if_status(
            job_id,
            {expected_status},
            status=status,
            error_message="" if status not in {JOB_FAILED, JOB_REVIEW} else message,
            raw_data=_merge_raw_data(job.get("raw_data"), patch),
        ):
            return db.get_job(job_id) or job
        db.add_event(job_id, "info" if status not in {JOB_FAILED, JOB_REVIEW} else "warn", message, patch)
        _sync_guest_requests_for_job(job_id, status, {"completion_stage": stage})
        return db.get_job(job_id) or {**job, "status": status}

    def _sixpan_openlist_plan_for_job(job: dict[str, Any], category: dict[str, Any]) -> dict[str, Any]:
        staging_plan = staging_plan_from_job(job)
        if staging_plan:
            scan_root = _safe_openlist_path(staging_plan.get("openlist_job_root"))
            final_root = _safe_openlist_path(staging_plan.get("openlist_final_category_root"))
            return {
                "root_path": scan_root,
                "target_root_path": final_root,
                "scan_filters": _sixpan_scan_filters_from_job(job, root_path=scan_root),
                "mount_name": "",
                "save_path": str(staging_plan.get("provider_target_path") or job.get("target_path") or ""),
                "common_dir": "",
                "staging_plan": staging_plan,
            }
        sixpan_config = app_config.raw.get("sixpan", {}) if isinstance(app_config.raw.get("sixpan"), dict) else {}
        mount_name = str(
            sixpan_config.get("openlist_mount_name")
            or sixpan_config.get("fnos_mount_name")
            or sixpan_config.get("mount_name")
            or sixpan_config.get("mount_path")
            or "清云"
        ).strip()
        save_path = str(job.get("target_path") or category.get("sixpan_save_path") or category.get("label") or "").strip()
        category_root = _join_virtual_path(mount_name, save_path)
        filters = _sixpan_scan_filters_from_job(job)
        common_dir = _common_top_directory(filters.get("expected_paths") or [])
        if common_dir:
            scan_root = _join_virtual_path(category_root, common_dir)
        elif filters.get("expected_paths"):
            scan_root = category_root
        else:
            title_dir = _safe_virtual_segment(job.get("title") or "")
            scan_root = _join_virtual_path(category_root, title_dir) if title_dir else ""
        return {
            "root_path": scan_root,
            "target_root_path": category_root,
            "scan_filters": filters,
            "mount_name": mount_name,
            "save_path": save_path,
            "common_dir": common_dir,
        }

    def _cloud139_openlist_plan_for_job(job: dict[str, Any], category: dict[str, Any], directory_plan: dict[str, Any]) -> dict[str, Any]:
        """把 139 官方保存路径映射成 OpenList 可见路径。

        139 直转有两套路径：
        - 官方云端保存路径：博客/电影
        - OpenList 挂载路径：移动云2/电影

        Organizer 只能扫描 OpenList API，所以这里不能直接使用 job.target_path
        或 directory_plan.target_path 里的官方路径。
        """

        staging_plan = staging_plan_from_job(job)
        if staging_plan:
            job_root = _safe_openlist_path(staging_plan.get("openlist_job_root"))
            final_root = _safe_openlist_path(staging_plan.get("openlist_final_category_root"))
            native_target = str(directory_plan.get("target_path") or job.get("target_path") or "").strip()
            mapped_target = _safe_openlist_path(map_staging_path_to_openlist(native_target, staging_plan))
            scan_root = mapped_target if mapped_target and _virtual_path_under(mapped_target, job_root) else job_root
            return {
                "root_path": scan_root,
                "target_root_path": final_root,
                "mount_name": "",
                "official_root": "",
                "real_folder_name": _cloud139_real_folder_name(job),
                "staging_plan": staging_plan,
                "scan_filters": _cloud139_scan_filters_from_job(job, root_path=job_root),
            }

        cloud139_config = app_config.raw.get("cloud139", {}) if isinstance(app_config.raw.get("cloud139"), dict) else {}
        official_root = str(cloud139_config.get("target_root_path") or "").strip().strip("/")
        mount_name = str(cloud139_config.get("fnos_mount_name") or "").strip().strip("/")
        category_root = str(category.get("cloud139_fnos_target_path") or category.get("openlist_root_path") or "").strip()
        real_folder_name = _cloud139_real_folder_name(job)
        title_dir = _safe_virtual_segment(job.get("title") or directory_plan.get("resource_name") or "")
        resource_dir_applied = not str(directory_plan.get("resource_dir_applied")).lower() == "false"
        if category_root:
            real_leaf = _safe_virtual_segment(str(real_folder_name or "").replace("\\", "/").strip("/").split("/")[-1])
            if resource_dir_applied and (real_leaf or title_dir):
                scan_root = _join_virtual_path(category_root, real_leaf or title_dir)
            else:
                scan_root = _join_virtual_path(category_root)
            return {
                "root_path": scan_root,
                "target_root_path": _join_virtual_path(category_root),
                "mount_name": "",
                "official_root": "",
                "real_folder_name": real_folder_name,
            }
        if not category_root:
            suffix_source = str(category.get("cloud139_target_path") or directory_plan.get("target_path") or job.get("target_path") or category.get("label") or "").strip()
            suffix = _strip_virtual_prefix(suffix_source, official_root)
            category_root = _join_virtual_path(mount_name, suffix) if mount_name else str(category.get("mobile_target_path") or "").strip()
        if not category_root:
            category_root = str(category.get("mobile_target_path") or category.get("label") or "").strip()

        mapped_real_folder = _map_cloud139_path_to_openlist(real_folder_name, mount_name, official_root)
        if mapped_real_folder:
            scan_root = mapped_real_folder
        else:
            target_path = _map_cloud139_path_to_openlist(
                str(directory_plan.get("target_path") or job.get("target_path") or ""),
                mount_name,
                official_root,
            )
            if target_path and title_dir and resource_dir_applied:
                scan_root = target_path
            elif category_root and title_dir:
                scan_root = _join_virtual_path(category_root, title_dir)
            else:
                scan_root = target_path or category_root
        return {
            "root_path": scan_root,
            "target_root_path": category_root,
            "mount_name": mount_name,
            "official_root": official_root,
            "real_folder_name": real_folder_name,
        }

    job_completion_info_service = JobCompletionInfoService(
        category=lambda key: app_config.category(key) if key in app_config.categories else {},
        uses_rclone_staging=_job_uses_rclone_staging,
        is_staging_path=_is_quark_staging_path,
        common_virtual_path=_common_virtual_path,
        rclone_completed_root=_rclone_completed_openlist_root,
        cloud139_plan=_cloud139_openlist_plan_for_job,
        sixpan_plan=_sixpan_openlist_plan_for_job,
        map_category_path=_map_category_path_to_openlist,
        path_health_checks=_path_health_checks,
    )

    def _organizer_plan_for_completed_import(job: dict[str, Any]) -> dict[str, Any] | None:
        source_type = str(job.get("source_type") or "").strip().lower()
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        directory_plan = raw_data.get("directory_plan") if isinstance(raw_data.get("directory_plan"), dict) else {}
        update_payload_extra = _update_organizer_payload_extra(job)
        payload_extra: dict[str, Any] = dict(update_payload_extra)
        if source_type in BT_SOURCE_TYPES:
            category_key = str(job.get("category") or "").strip()
            category = app_config.category(category_key) if category_key in app_config.categories else {}
            if not category:
                return None
            sixpan_plan = _sixpan_openlist_plan_for_job(job, category)
            root_path = str(sixpan_plan.get("root_path") or "").strip()
            sixpan_resource_root = _resource_root_for_import_mount(
                update_payload_extra.get("target_root_path") or update_payload_extra.get("canonical_resource_root"),
                sixpan_plan.get("target_root_path"),
            )
            payload_extra = {
                **update_payload_extra,
                "sixpan_openlist": sixpan_plan,
                "staging_plan": sixpan_plan.get("staging_plan") or {},
                "target_root_path": sixpan_resource_root or sixpan_plan.get("target_root_path") or "",
                "canonical_resource_root": sixpan_resource_root or update_payload_extra.get("canonical_resource_root") or "",
                "scan_filters": _merge_scan_filters(
                    sixpan_plan.get("scan_filters") if isinstance(sixpan_plan.get("scan_filters"), dict) else {},
                    update_payload_extra.get("scan_filters") if isinstance(update_payload_extra.get("scan_filters"), dict) else {},
                ),
                "allow_same_root_task": True,
            }
        elif source_type == "cloud139":
            category_key = str(job.get("category") or "").strip()
            category = app_config.category(category_key) if category_key in app_config.categories else {}
            if not category:
                return None
            cloud139_plan = _cloud139_openlist_plan_for_job(job, category, directory_plan)
            root_path = str(
                cloud139_plan.get("root_path")
                if cloud139_plan.get("staging_plan")
                else update_payload_extra.get("target_root_path") or cloud139_plan.get("root_path") or ""
            ).strip()
            cloud139_scan_filters = (
                cloud139_plan.get("scan_filters")
                if isinstance(cloud139_plan.get("scan_filters"), dict)
                else _cloud139_scan_filters_from_job(job)
            )
            payload_extra = {
                **update_payload_extra,
                "cloud139_openlist": cloud139_plan,
                "staging_plan": cloud139_plan.get("staging_plan") or {},
                "target_root_path": update_payload_extra.get("target_root_path") or cloud139_plan.get("target_root_path") or "",
                "scan_filters": _merge_scan_filters(
                    cloud139_scan_filters,
                    update_payload_extra.get("scan_filters") if isinstance(update_payload_extra.get("scan_filters"), dict) else {}
                ),
            }
        elif not directory_plan:
            return None
        else:
            root_path = str(directory_plan.get("target_path") or job.get("target_path") or "").strip()
        return {
            "root_path": root_path,
            "directory_plan": directory_plan,
            "payload_extra": payload_extra,
        }

    organizer_dispatch_service = OrganizerDispatchService(
        database=db,
        organizer=organizer_service,
        resolve_plan=_organizer_plan_for_completed_import,
        resolve_rclone_plan=_organizer_plan_for_rclone_completed_item,
        set_completion_stage=_set_job_completion_stage,
        invalid_virtual_path=_is_invalid_virtual_path,
        dispatch_process=(
            worker_task_dispatcher.organizer_process
            if not role_runs(active_process_role, "worker")
            else None
        ),
    )

    def _enqueue_organizer_from_completed_import(result: dict[str, Any], reason: str) -> dict[str, Any] | None:
        return organizer_dispatch_service.enqueue_completed_import(result, reason)

    rclone_service.set_run_ready_handler(
        _enqueue_organizer_from_rclone_completed_items,
        direct_handler=_enqueue_organizer_from_completed_import,
    )

    sixpan_offline_sync = SixPanOfflineSyncService(
        database=db,
        importer=lambda: generic_importers.get("sixpan"),
        poll_limit=lambda: _safe_int(
            app_config.raw.get("sixpan", {}).get("task_poll_limit"),
            200,
            1,
            1000,
        ),
        category=lambda key: app_config.category(key) if key in app_config.categories else {},
        enqueue_organizer=_enqueue_organizer_from_completed_import,
        record_completed=lambda job_id, category, target_path, **kwargs: import_service.record_sixpan_completed(
            job_id,
            category,
            target_path,
            **kwargs,
        ),
        sync_guest_requests=lambda *args, **kwargs: _sync_guest_requests_for_job(*args, **kwargs),
    )

    sixpan_polling_runtime = SixPanPollingRuntime(
        database=db,
        owner_id=process_owner_id,
        poll_once=sixpan_offline_sync.sync,
        interval_seconds=lambda: _safe_int(
            app_config.raw.get("sixpan", {}).get("poll_interval_seconds"),
            60,
            10,
            3600,
        ),
        log=rclone_service._append_log,
    )
    if role_runs(active_process_role, "scheduler"):
        sixpan_polling_runtime.start()
    atexit.register(sixpan_polling_runtime.shutdown)

    app = Flask(
        __name__,
        template_folder=str(app_config.base_dir / "templates"),
        static_folder=str(app_config.base_dir / "static"),
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("fnos_media_import").setLevel(logging.INFO)
    app.logger.setLevel(logging.INFO)
    system_log_handler = _MemoryLogHandler(maxlen=2000)
    system_log_handler.setLevel(logging.INFO)
    system_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_fnos_memory_log", False):
            root_logger.removeHandler(handler)
    root_logger.addHandler(system_log_handler)
    _install_file_log_handler(root_logger, app_config.base_dir)
    app.secret_key = _resolve_secret_key(str(app_config.raw.get("app", {}).get("secret_key") or ""), db)
    app_config.raw.setdefault("app", {})["secret_key"] = app.secret_key
    if _strict_security_enabled(app_config.raw) and _default_secret_key(app.secret_key):
        raise RuntimeError("正式环境应用签名密钥初始化失败")
    app.config["APP_CONFIG"] = app_config
    app.config["DATABASE"] = db
    app.config["PROCESS_ROLE"] = active_process_role
    security_config = app_config.raw.get("security", {})
    if _default_secret_key(str(security_config.get("ip_hash_salt") or "")):
        security_config["ip_hash_salt"] = app.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=str(security_config.get("session_cookie_samesite") or "Lax"),
        SESSION_COOKIE_SECURE=_config_bool(security_config, "session_cookie_secure", False),
        PERMANENT_SESSION_LIFETIME=max(300, _config_int(security_config, "session_lifetime_seconds", 43200)),
        MAX_CONTENT_LENGTH=max(1, _config_int(security_config, "max_request_body_bytes", 8 * 1024 * 1024)),
    )
    app.extensions["runtime_services"] = runtime_services
    app.extensions["durable_worker_runtime"] = durable_worker_runtime
    app.extensions["search_cache_maintenance_worker"] = search_cache_maintenance_worker
    app.extensions["trending_discovery_service"] = trending_service
    app.extensions["trending_discovery_scheduler"] = trending_scheduler
    install_request_runtime(app, runtime_services)
    trending_initial_import_service = TrendingInitialImportService(
        repository=db,
        search_service=lambda: search_service,
        import_service=lambda: import_service,
        get_resource=db.get_resource,
        get_cached_resource=db.get_search_cache,
        find_resource_by_url=db.find_resource_by_url,
        search_resources=lambda **kwargs: _public_search_application(admin=True).search(
            kwargs["keyword"],
            sources=kwargs.get("sources") or [],
            token=kwargs.get("token") or "",
            options=kwargs.get("options") or {},
            hide_full_links=False,
            trace_id=kwargs.get("trace_id") or "-",
            cache_keyword=kwargs.get("cache_keyword"),
        ),
        get_job=db.get_job,
        categories=lambda: app_config.categories,
        runtime_revision=lambda: runtime_services.revision,
        executor_id=lambda: active_process_role,
        start_import=lambda result, reason: _auto_start_rclone_for_import(result, reason),
        sanitize_string_list=lambda value: _safe_public_string_list(value, max_items=2000, max_length=512),
        sanitize_quark_selection=_safe_public_quark_selection,
        sanitize_cloud139_selection=_safe_public_cloud139_selection,
    )
    app.extensions["trending_initial_import_service"] = trending_initial_import_service
    rate_limit_service = RateLimitService(
        repository=db.rate_limits,
        enabled=lambda: _config_bool(security_config, "rate_limit_enabled", True),
        window_seconds=lambda: _config_int(security_config, "rate_limit_window_seconds", 60),
    )

    @app.before_request
    def enforce_public_json_body_limit():
        if not request.path.startswith("/api/public/") or not request.is_json:
            return None
        max_bytes = max(1, _config_int(security_config, "max_public_json_bytes", 256 * 1024))
        # Also constrain chunked requests where Content-Length is unavailable;
        # Flask will raise RequestEntityTooLarge while parsing the body.
        request.max_content_length = min(int(app.config.get("MAX_CONTENT_LENGTH") or max_bytes), max_bytes)
        if request.content_length is not None and request.content_length > max_bytes:
            return jsonify(
                {
                    "success": False,
                    "error_code": "request_too_large",
                    "message": "请求体过大",
                    "max_bytes": max_bytes,
                }
            ), 413
        return None

    runtime_reload_service = RuntimeReloadService(
        load_config=lambda: apply_persisted_config(load_config(config_path), db.get_app_settings()),
        builder=runtime_builder,
        runtime_services=runtime_services,
        retirement=runtime_retirement,
        database=db,
        job_service=job_service,
        rclone_service=rclone_service,
        update_service=update_service,
        update_scheduler=update_scheduler,
        trending_scheduler=trending_scheduler,
        config_bool=_config_bool,
        config_int=_config_int,
        rollback_logger=lambda: app.logger.exception("runtime reload rollback failed"),
        advanced_config_key=ADVANCED_CONFIG_KEY,
    )

    def _reload_runtime_config() -> dict[str, Any]:
        nonlocal app_config, pansou, btbtla, quark_importer, cloud139_importer, generic_importers, fnos
        nonlocal search_service, import_service, security_config
        nonlocal organizer_service, runtime_build

        previous_build = runtime_build
        outcome = runtime_reload_service.reload(app_config, previous_build)
        app_config = outcome.config
        runtime_build = outcome.build
        security_config = outcome.security_config
        pansou = runtime_build.pansou
        btbtla = runtime_build.btbtla
        quark_importer = runtime_build.quark_importer
        cloud139_importer = runtime_build.cloud139_importer
        generic_importers = runtime_build.generic_importers
        fnos = runtime_build.fnos
        search_service = runtime_build.search_service
        import_service = runtime_build.import_service
        organizer_service = runtime_build.organizer_service
        app.config["APP_CONFIG"] = app_config
        finalize_organizer_runtime_transition(
            dispatcher=organizer_dispatch_service,
            previous_build=previous_build,
            candidate_build=runtime_build,
            retirement=runtime_retirement,
            activate_background=role_runs(active_process_role, "worker"),
        )
        return outcome.response

    app.extensions["reload_runtime_config"] = _reload_runtime_config

    def _persist_sixpan_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
        updated = _store_sixpan_tokens(tokens)
        if not updated:
            return {"updated": False, "reload": {}}
        reload_result = _reload_runtime_config()
        return {"updated": True, "reload": reload_result}

    def _sixpan_oauth_state_message(state: str, authorized: bool = False) -> str:
        normalized = str(state or "").strip().upper()
        if authorized:
            return "六盘授权成功，token 已保存到数据库"
        if normalized == "AUTHORIZATION_SUCCESS":
            return "六盘授权成功，但接口未返回 token，请重试检查或重新授权"
        if normalized == "AUTHORIZATION_TOKEN_CREATED":
            return "六盘授权已生成 token，正在保存"
        if normalized == "AUTHORIZATION_PENDING_LOGIN":
            return "等待你在六盘授权页面登录"
        if normalized == "AUTHORIZATION_PENDING_CONFIRMATION":
            return "等待你在六盘授权页面确认授权"
        if normalized:
            return f"六盘授权状态：{normalized}"
        return "六盘授权状态未知"

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not _is_admin_logged_in():
                return jsonify({"success": False, "message": "管理员未登录"}), 401
            return view(*args, **kwargs)

        return wrapper

    def _is_admin_logged_in() -> bool:
        return bool(session.get("admin_logged_in"))

    def _page_args(default_per_page: int = 50, max_per_page: int = 200) -> tuple[int, int, int]:
        page = _safe_int(request.args.get("page"), 1, 1, 999999)
        per_page = _safe_int(request.args.get("per_page") or request.args.get("limit"), default_per_page, 1, max_per_page)
        return page, per_page, (page - 1) * per_page

    def _page_meta(total: int, page: int, per_page: int) -> dict[str, Any]:
        total = max(0, int(total or 0))
        per_page = max(1, int(per_page or 1))
        pages = max(1, (total + per_page - 1) // per_page)
        return {"page": page, "per_page": per_page, "total": total, "pages": pages, "has_prev": page > 1, "has_next": page < pages}

    def _admin_profile(settings: dict[str, Any] | None = None) -> dict[str, Any]:
        return admin_profile_service.profile()

    def _verify_admin_password(username: str, password: str) -> tuple[bool, str]:
        return admin_profile_service.verify_password(username, password)

    def _current_admin_password_ok(password: str) -> bool:
        username = str(session.get("admin_username") or _admin_profile().get("username") or "")
        ok, _ = _verify_admin_password(username, str(password or ""))
        return ok

    security_status_service = SecurityStatusService(
        raw_config=lambda: app_config.raw,
        settings=db.get_app_settings,
        strict_enabled=_strict_security_enabled,
        default_secret=_default_secret_key,
        docker_socket_mounted=lambda: Path("/var/run/docker.sock").exists(),
        admin_profile_key=ADMIN_PROFILE_KEY,
    )

    def _build_security_status() -> dict[str, Any]:
        return security_status_service.build()

    def _rate_limit(action: str, config_key: str):
        limit = _config_int(security_config, config_key, 0)
        client_key = _hash_client_ip(_client_ip(), str(security_config.get("ip_hash_salt") or app.secret_key or ""))
        bucket_key = f"{action}:{client_key}"
        decision = rate_limit_service.check(bucket_key, limit=limit)
        if decision is not None and not decision.allowed:
            retry_after = decision.retry_after
            return (
                jsonify(
                    {
                        "success": False,
                        "message": f"请求过于频繁，请 {retry_after} 秒后再试",
                        "retry_after": retry_after,
                    }
                ),
                429,
            )
        return None

    def _client_ip() -> str:
        remote_addr = str(request.remote_addr or "").strip()
        trusted_proxy_count = max(0, _config_int(security_config, "trusted_proxy_count", 0))
        if trusted_proxy_count <= 0:
            return remote_addr
        forwarded = [item.strip() for item in str(request.headers.get("X-Forwarded-For") or "").split(",") if item.strip()]
        if len(forwarded) < trusted_proxy_count:
            return remote_addr
        return forwarded[-trusted_proxy_count]

    def index():
        return redirect("/submit", code=302)

    def submit_page():
        profile = _admin_profile()
        return render_template(
            "submit.html",
            app_name=app_config.app_name or APP_NAME,
            categories=app_config.categories,
            site_logo_url=profile.get("logo_url") or "",
        )

    def request_status_page(token: str):
        profile = _admin_profile()
        return render_template(
            "request_status.html",
            app_name=app_config.app_name or APP_NAME,
            request_token=token,
            site_logo_url=profile.get("logo_url") or "",
        )

    def admin_login_page():
        profile = _admin_profile()
        return render_template("admin_login.html", app_name=app_config.app_name or APP_NAME, site_logo_url=profile.get("logo_url") or "")

    def admin_page():
        profile = _admin_profile()
        return render_template(
            "admin.html",
            app_name=app_config.app_name or APP_NAME,
            categories=app_config.categories,
            status_labels=JOB_STATUS_LABELS,
            admin_profile=profile,
            site_logo_url=profile.get("logo_url") or "",
        )

    def api_admin_profile():
        return jsonify({"success": True, "profile": admin_profile_service.profile()})

    def api_admin_profile_update():
        payload = request.get_json(silent=True) or {}
        current_username = str(session.get("admin_username") or admin_profile_service.profile().get("username") or "")
        result, status_code = admin_profile_service.update_profile(payload, current_username)
        if result.get("success"):
            session["admin_username"] = result["profile"]["username"]
        return jsonify(result), status_code

    def api_admin_profile_avatar():
        result, status_code = admin_profile_service.save_avatar(request.files.get("avatar"))
        return jsonify(result), status_code

    def api_admin_site_logo():
        result, status_code = admin_profile_service.save_logo(request.files.get("logo"))
        return jsonify(result), status_code

    def _sync_guest_request_status(item: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
        request_status = str(item.get("status") or "")
        if request_status in {"rejected", "cancelled", "unsupported"} or not item.get("job_id"):
            return item
        job = job if job is not None else db.get_job(int(item["job_id"]))
        if not job:
            return item
        job = _reconcile_cloud139_submitted_job(job, "guest_request_status_sync")
        job_status = str(job.get("status") or "")
        public_status = _public_status(job_status)
        if not job_status or (job_status == request_status and str(item.get("public_status") or "") == public_status):
            return item
        if job_status != request_status:
            db.add_guest_request_event(
                int(item["id"]),
                "info",
                "系统同步关联正式任务状态",
                {"job_id": job.get("id"), "job_status": job_status, "previous_status": request_status},
            )
        db.update_guest_request(
            int(item["id"]),
            status=job_status,
            public_status=public_status,
            raw_data=_merge_raw_data(item.get("raw_data"), {"status_sync": {"job_id": job.get("id"), "job_status": job_status}}),
        )
        return db.get_guest_request(int(item["id"])) or item

    def _sync_guest_request_statuses(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        job_ids = []
        for item in items:
            if not item.get("job_id") or str(item.get("status") or "") in {"rejected", "cancelled", "unsupported"}:
                continue
            job_id = _safe_int(item.get("job_id"), 0, 1, 999999999)
            if job_id:
                job_ids.append(job_id)
        jobs_by_id = db.get_jobs_by_ids(job_ids)
        return [
            _sync_guest_request_status(item, jobs_by_id.get(_safe_int(item.get("job_id"), 0, 1, 999999999))) if item.get("job_id") else item
            for item in items
        ]

    def _sync_guest_requests_for_job(job_id: int, job_status: str, raw: dict[str, Any] | None = None) -> None:
        if not job_id:
            return
        public_status = _public_status(job_status)
        for item in db.list_guest_requests_by_job(job_id):
            request_id = _safe_int(item.get("id"), 0, 0, 999999999)
            if not request_id:
                continue
            current = str(item.get("status") or "")
            if current in {"rejected", "cancelled", "unsupported"}:
                continue
            if current != job_status:
                db.add_guest_request_event(
                    request_id,
                    "info",
                    "系统同步关联正式任务状态",
                    {"job_id": job_id, "job_status": job_status, "previous_status": current, **(raw or {})},
                )
            db.update_guest_request(
                request_id,
                status=job_status,
                public_status=public_status,
                raw_data=_merge_raw_data(item.get("raw_data"), {"status_sync": {"job_id": job_id, "job_status": job_status, **(raw or {})}}),
            )

    def _cloud139_submitted_can_mark_done(job: dict[str, Any]) -> bool:
        if not isinstance(job, dict):
            return False
        if str(job.get("status") or "").strip() != JOB_SUBMITTED:
            return False
        if str(job.get("source_type") or "").strip().lower() != "cloud139":
            return False
        if str(job.get("target_route") or "").strip() != "cloud139_direct":
            return False
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        if str(raw_data.get("provider") or "").strip() != "cmcc_native":
            return False
        if not isinstance(raw_data.get("save"), dict):
            return False
        if raw_data.get("error") or str(job.get("error_message") or "").strip():
            return False
        return True

    def _reconcile_cloud139_submitted_job(job: dict[str, Any], reason: str) -> dict[str, Any]:
        """兼容旧配置创建的 139 submitted 任务。

        139 官方分享转存是云端批处理，接口成功返回 save 结果后只能证明
        “提交成功”，不能证明 OpenList 已可见、Organizer 已整理、标准目录已有
        视频文件。因此旧 submitted 任务只能补到等待 OpenList/Organizer 确认，
        不能再直接补标最终完成。
        """

        if not _cloud139_submitted_can_mark_done(job):
            return job
        job_id = _safe_int(job.get("id"), 0, 1, 999999999)
        if not job_id:
            return job
        latest_before_update = db.get_job(job_id)
        if isinstance(latest_before_update, dict):
            if str(latest_before_update.get("status") or "").strip() != JOB_SUBMITTED:
                return latest_before_update
            latest_raw = latest_before_update.get("raw_data") if isinstance(latest_before_update.get("raw_data"), dict) else {}
            if isinstance(latest_raw.get("cloud139_status_reconciled"), dict):
                return latest_before_update
            job = latest_before_update
        raw_data = _merge_raw_data(
            job.get("raw_data"),
            {
                "cloud139_status_reconciled": {
                    "from": JOB_SUBMITTED,
                    "to": JOB_WAITING_OPENLIST,
                    "reason": reason,
                    "message": "139 官方直转接口已返回保存任务，等待 OpenList 可见和 Organizer 整理确认。",
                }
            },
        )
        if not db.update_job_if_status(
            job_id,
            {JOB_SUBMITTED},
            status=JOB_WAITING_OPENLIST,
            error_message="",
            raw_data=raw_data,
        ):
            return db.get_job(job_id) or job
        db.add_event(
            job_id,
            "info",
            "139 官方直转已提交成功，等待 OpenList 可见和 Organizer 整理确认",
            {"reason": reason, "target_path": job.get("target_path"), "external_task_id": job.get("external_task_id")},
        )
        latest = db.get_job(job_id) or {**job, "status": JOB_WAITING_OPENLIST, "raw_data": raw_data}
        _sync_guest_requests_for_job(job_id, JOB_WAITING_OPENLIST, {"cloud139_status_reconciled": True, "reason": reason})
        organizer_result = _enqueue_organizer_from_completed_import({"success": True, "job": latest}, f"cloud139_reconcile:{reason}")
        if isinstance(organizer_result, dict):
            latest = db.get_job(job_id) or latest
        return latest

    def _effective_settings() -> dict[str, Any]:
        public_defaults = app_config.raw.get("public", {})
        stored = db.get_app_settings()
        public_settings = {
            "allow_anonymous_search": _setting_bool(stored, "public.allow_anonymous_search", _config_bool(public_defaults, "allow_anonymous_search", True)),
            "request_query_enabled": _setting_bool(stored, "public.request_query_enabled", _config_bool(public_defaults, "request_query_enabled", True)),
            "hide_full_links": _setting_bool(stored, "public.hide_full_links", _config_bool(public_defaults, "hide_full_links", True)),
        }
        stored_mode = stored.get("submission.mode")
        submission_mode = _normalize_submission_mode(stored_mode if stored_mode not in (None, "") else app_config.raw.get("submission", {}).get("mode"))
        return {
            "public": public_settings,
            "submission": {"mode": submission_mode},
        }

    def _current_public_settings() -> dict[str, bool]:
        return _effective_settings()["public"]

    def _current_submission_mode() -> str:
        return str(_effective_settings()["submission"]["mode"])

    def _admin_data_status() -> dict[str, Any]:
        try:
            db.get_app_settings()
            database_status = {"healthy": True}
        except Exception as exc:  # noqa: BLE001
            database_status = {"healthy": False, "error": str(exc)}
        try:
            usage = shutil.disk_usage(db.path.parent)
            free_ratio = (usage.free / usage.total) if usage.total else 0.0
            storage_status = {
                "healthy": usage.free >= 1024**3 and free_ratio >= 0.05,
                "warning": usage.free < 5 * 1024**3 or free_ratio < 0.15,
                "critical": usage.free < 1024**3 or free_ratio < 0.05,
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "free_ratio": round(free_ratio, 4),
            }
        except Exception as exc:  # noqa: BLE001
            storage_status = {"healthy": False, "error": str(exc)}
        return {"database": database_status, "storage": storage_status}

    def _admin_organizer_status() -> dict[str, Any]:
        status = organizer_service.status()
        status["counts"] = {
            key: db.count_organizer_tasks(status=key)
            for key in ("failed", "executing", "waiting_openlist", "stabilizing", "pending")
        }
        return status

    def _admin_system_status() -> dict[str, Any]:
        data_status = _admin_data_status()
        return {
            "ok": bool((data_status.get("database") or {}).get("healthy")),
            "database": "ok" if (data_status.get("database") or {}).get("healthy") else "error",
            "rclone": rclone_service.status(),
            "organizer": _admin_organizer_status(),
            "update_scheduler": update_scheduler.status(),
            "trending_discovery": trending_scheduler.status(),
            "worker_queue": worker_queue_diagnostics.status(),
            "data": data_status,
        }

    def api_admin_dashboard():
        service = AdminDashboardService(
            AdminDashboardDependencies(
                jobs=db.job_queries,
                requests=db.guest_request_queries,
                reconcile_job=_reconcile_cloud139_submitted_job,
                decorate_job=_decorate_job_completion,
                sync_requests=_sync_guest_request_statuses,
                system_status=_admin_system_status,
            )
        )
        return jsonify({"success": True, "summary": service.summary(limit=200)})

    def _guest_request_admin_application() -> GuestRequestAdminService:
        return GuestRequestAdminService(
            GuestRequestAdminDependencies(
                requests=db.guest_request_queries,
                jobs=db.job_queries,
                sync_one=_sync_guest_request_status,
                sync_many=_sync_guest_request_statuses,
            )
        )

    def _request_review_command_application() -> RequestReviewCommandService:
        return RequestReviewCommandService(
            RequestReviewCommandDependencies(
                requests=db.guest_request_queries,
                commands=db.guest_request_commands,
                jobs=db.job_queries,
                merge_raw_data=_merge_raw_data,
                db=db,
                emit_notification=_emit_configured_notification,
            )
        )

    def _request_approval_application() -> RequestApprovalService:
        return RequestApprovalService(
            RequestApprovalDependencies(
                requests=db.guest_request_queries,
                commands=db.guest_request_commands,
                jobs=db.job_queries,
                coordinate_import=lambda **kwargs: public_import_job_coordinator.execute(**kwargs),
                public_status=_public_status,
                safe_result=_guest_safe_job_result,
                merge_raw_data=_merge_raw_data,
                sanitize_string_list=lambda value: _safe_public_string_list(value, max_items=2000, max_length=512),
                sanitize_quark_selection=_safe_public_quark_selection,
                sanitize_cloud139_selection=_safe_public_cloud139_selection,
                sanitize_sixpan_selection=_safe_public_sixpan_selection,
                category_label=lambda key: app_config.category(key).get("label", key),
                db=db,
                emit_notification=_emit_configured_notification,
            )
        )

    def api_admin_requests():
        page, per_page, offset = _page_args(20, 200)
        status = request.args.get("status") or None
        result = _guest_request_admin_application().list_requests(status=status, limit=per_page, offset=offset)
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def api_admin_request_detail(request_id: int):
        result, status_code = _guest_request_admin_application().detail(request_id)
        return jsonify(result), status_code

    def api_admin_request_approve(request_id: int):
        payload = request.get_json(silent=True) or {}
        result, status_code = _request_approval_application().approve(request_id, payload, admin=session.get("admin_username"))
        return jsonify(result), status_code

    def api_admin_request_reject(request_id: int):
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "管理员拒绝该提交")
        decision, status_code = _request_review_command_application().reject(
            request_id, reason=reason, admin=session.get("admin_username"), force=_payload_bool(payload, "force", False)
        )
        if status_code != 200:
            return jsonify(decision), status_code
        item = decision["request"]
        linked_job = decision["linked_job"]
        request_worker_cancel = db.worker_tasks.cancel_related(
            guest_request_id=request_id,
            reason=f"访客提交 #{request_id} 已拒绝：{reason}",
        )
        cancel_result = None
        if linked_job:
            cancel_result = _cancel_job_and_cleanup(
                linked_job,
                reason=f"关联访客提交已拒绝：{reason}",
                payload=payload,
                request_item=item,
                cleanup_default=_payload_bool(payload, "cleanup", True),
                stop_running_default=False,
            )
        return jsonify({"success": True, "message": "已拒绝提交", "request": db.get_guest_request(request_id), "job_cancel": cancel_result, "worker_cancel": request_worker_cancel})

    def api_admin_request_cancel(request_id: int):
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "管理员取消该提交")
        decision, status_code = _request_review_command_application().cancel(
            request_id, reason=reason, admin=session.get("admin_username"), force=_payload_bool(payload, "force", False)
        )
        if status_code != 200:
            return jsonify(decision), status_code
        item = decision["request"]
        linked_job = decision["linked_job"]
        request_worker_cancel = db.worker_tasks.cancel_related(
            guest_request_id=request_id,
            reason=f"访客提交 #{request_id} 已取消：{reason}",
        )
        cancel_result = None
        if linked_job:
            cancel_result = _cancel_job_and_cleanup(
                linked_job,
                reason=f"关联访客提交已取消：{reason}",
                payload=payload,
                request_item=item,
                cleanup_default=_payload_bool(payload, "cleanup", True),
                stop_running_default=True,
            )
        return jsonify({"success": True, "message": "已取消提交", "request": db.get_guest_request(request_id), "job_cancel": cancel_result, "worker_cancel": request_worker_cancel})

    def api_admin_jobs():
        page, per_page, offset = _page_args(20, 200)
        status = request.args.get("status") or None
        category = request.args.get("category") or None
        source_type = request.args.get("source_type") or None
        keyword = request.args.get("keyword") or request.args.get("q") or None
        result = _job_admin_query_application().list_jobs(
            limit=per_page, offset=offset, status=status, category=category, source_type=source_type, keyword=keyword
        )
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def _job_admin_query_application() -> JobAdminQueryService:
        return JobAdminQueryService(
            JobAdminQueryDependencies(
                jobs=db.job_queries,
                load_detail=job_service.get_job_with_events,
                reconcile=_reconcile_cloud139_submitted_job,
                decorate=_decorate_job_completion,
            )
        )

    def api_admin_job_detail(job_id: int):
        result, status_code = _job_admin_query_application().detail(job_id)
        return jsonify(result), status_code

    def api_admin_job_retry(job_id: int):
        return jsonify(_job_admin_command_application().retry(job_id))

    def api_admin_job_delete(job_id: int):
        result, status_code = _job_admin_command_application().delete(job_id)
        return jsonify(result), status_code

    def _job_admin_command_application() -> JobAdminCommandService:
        return JobAdminCommandService(
            JobAdminCommandDependencies(
                imports=import_service,
                jobs=db,
                auto_start_rclone=_auto_start_rclone_for_import,
            )
        )

    def api_admin_job_cancel(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"success": False, "message": "任务不存在"}), 404
        payload = request.get_json(silent=True) or {}
        if str(job.get("status") or "") in {"done", "success"}:
            return jsonify({"success": False, "message": "任务已完成，不能取消；如需删除已入库资源请走人工清理"}), 400
        reason = str(payload.get("reason") or "管理员取消任务")
        result = _cancel_job_and_cleanup(
            job,
            reason=reason,
            payload=payload,
            request_item=None,
            cleanup_default=_payload_bool(payload, "cleanup", True),
            stop_running_default=True,
        )
        if result.get("cancelled") is False:
            return jsonify({
                "success": False,
                "message": result.get("message") or "任务状态已变化，取消未执行",
                "job": result.get("job") or db.get_job(job_id),
                "cleanup": result.get("cleanup"),
            }), 409
        for guest_request in db.list_guest_requests_by_job(job_id):
            request_id = guest_request.get("id")
            if not request_id:
                continue
            db.add_guest_request_event(int(request_id), "warn", f"关联任务已取消：{reason}", {"job_id": job_id})
            db.update_guest_request(
                int(request_id),
                status=JOB_CANCELLED,
                public_status="已取消",
                raw_data=_merge_raw_data(guest_request.get("raw_data"), {"cancelled_by": session.get("admin_username"), "reason": reason, "job_id": job_id}),
            )
        return jsonify({"success": True, "message": result.get("message") or "已取消任务", "job": db.get_job(job_id), "cleanup": result.get("cleanup")})

    def _job_cancellation_service() -> JobCancellationService:
        return JobCancellationService(
            JobCancellationDependencies(
                jobs=db,
                cleaner=rclone_service,
                merge_raw_data=_merge_raw_data,
                payload_bool=_payload_bool,
                cancelled_status=JOB_CANCELLED,
                worker_tasks=db.worker_tasks,
                organizer=organizer_service,
                sixpan_importer=lambda: generic_importers.get("sixpan"),
            )
        )

    def _cancel_job_and_cleanup(
        job: dict[str, Any],
        *,
        reason: str,
        payload: dict[str, Any],
        request_item: dict[str, Any] | None,
        cleanup_default: bool,
        stop_running_default: bool,
    ) -> dict[str, Any]:
        return _job_cancellation_service().cancel(
            job,
            reason=reason,
            payload=payload,
            request_item=request_item,
            cleanup_default=cleanup_default,
            stop_running_default=stop_running_default,
            admin_username=session.get("admin_username"),
        )

    def api_admin_jobs_batch_retry():
        payload = request.get_json(silent=True) or {}
        result, status_code = _job_admin_command_application().batch_retry(payload.get("job_ids") or [])
        return jsonify(result), status_code

    def api_admin_media_libraries():
        return jsonify(_media_admin_query_application().libraries())

    def api_admin_media_running():
        return jsonify(_media_admin_query_application().running())

    def api_admin_media_refresh_logs():
        limit = _safe_int(request.args.get("limit"), 100, 1, 1000)
        return jsonify(_media_admin_query_application().refresh_logs(limit))

    def _media_admin_query_application() -> MediaAdminQueryService:
        return MediaAdminQueryService(
            MediaAdminQueryDependencies(
                client=fnos,
                categories=app_config.categories,
                build_dashboard=_build_media_dashboard,
                read_log_tail=_read_jsonl_tail,
            )
        )

    def api_admin_system_logs():
        limit = _safe_int(request.args.get("limit"), 300, 1, 2000)
        logger_prefix = str(request.args.get("logger") or "").strip()
        return jsonify(_system_diagnostics_application().logs(limit=limit, logger_prefix=logger_prefix))

    def api_admin_system_events():
        page = _safe_int(request.args.get("page"), 1, 1, 1000000)
        per_page = _safe_int(request.args.get("per_page") or request.args.get("limit"), 50, 1, 500)
        keyword = str(request.args.get("keyword") or "").strip()
        source = str(request.args.get("source") or "").strip().lower()
        raw_job_id = str(request.args.get("job_id") or "").strip()
        parsed_job_id = _safe_int_value(raw_job_id, 0)
        job_id = parsed_job_id if parsed_job_id > 0 else None
        return jsonify(
            _system_diagnostics_application().events(
                page=page,
                per_page=per_page,
                keyword=keyword,
                source=source,
                job_id=job_id,
            )
        )

    def api_admin_task_logs():
        page = _safe_int(request.args.get("page"), 1, 1, 1000000)
        per_page = _safe_int(request.args.get("per_page"), 20, 1, 200)
        return jsonify(
            _system_diagnostics_application().task_logs(
                page=page,
                per_page=per_page,
                keyword=str(request.args.get("keyword") or "").strip(),
                status=str(request.args.get("status") or "").strip().lower(),
                date_from=str(request.args.get("date_from") or "").strip(),
                date_to=str(request.args.get("date_to") or "").strip(),
            )
        )

    def _system_diagnostics_application() -> SystemDiagnosticsService:
        return SystemDiagnosticsService(
            SystemDiagnosticsDependencies(
                logs=system_log_handler,
                database=db,
                recent_events=_recent_business_events,
                task_log_summaries=_task_log_summaries,
            )
        )

    def api_admin_media_refresh():
        payload = request.get_json(silent=True) or {}
        service = MediaAdminCommandService(
            MediaAdminCommandDependencies(
                imports=import_service,
                client=fnos,
                directory_required_message=ImportService.FNOS_DIR_REQUIRED_MESSAGE,
                worker_dispatcher=worker_task_dispatcher,
            )
        )
        return jsonify(service.refresh(payload))

    def api_admin_rclone_status():
        return jsonify(_rclone_admin_query_application().status())

    def api_admin_rclone_start():
        payload = request.get_json(silent=True) or {}
        return jsonify(_rclone_admin_command_application().start(payload))

    def api_admin_rclone_stop():
        return jsonify(_rclone_admin_command_application().stop())

    def api_admin_rclone_logs():
        limit = _safe_int(request.args.get("limit"), 200, 1, 1000)
        return jsonify(_rclone_admin_query_application().logs(limit))

    def api_admin_rclone_runs():
        page, per_page, offset = _page_args(20, 100)
        result = _rclone_admin_query_application().runs(limit=per_page, offset=offset)
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def api_admin_rclone_events():
        limit = _safe_int(request.args.get("limit"), 200, 1, 1000)
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999)
        return jsonify(_rclone_admin_query_application().events(run_id=run_id or None, limit=limit))

    def api_admin_rclone_file_events():
        page, per_page, offset = _page_args(50, 200)
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999)
        job_id = _safe_int(request.args.get("job_id"), 0, 0, 999999999)
        status = str(request.args.get("status") or "").strip() or None
        category = str(request.args.get("category") or "").strip() or None
        result = _rclone_admin_query_application().file_events(run_id=run_id or None, job_id=job_id or None, status=status, category=category, limit=per_page, offset=offset)
        return jsonify(
            {
                "success": True,
                "items": result["items"],
                "pagination": _page_meta(result["total"], page, per_page),
            }
        )

    def _rclone_admin_query_application() -> RcloneAdminQueryService:
        return RcloneAdminQueryService(RcloneAdminQueryDependencies(rclone=rclone_service, counts=db))

    def api_admin_rclone_file_retry(event_id: int):
        payload = request.get_json(silent=True) or {}
        service = RcloneFileRetryService(RcloneFileRetryDependencies(database=db, runner=rclone_service))
        result, status_code = service.retry(event_id, force=bool(payload.get("force")))
        return jsonify(result), status_code

    def api_admin_rclone_check():
        return jsonify(_rclone_admin_command_application().check())

    def _rclone_webdav_response(action: Callable[[], dict[str, Any]]):
        try:
            result = action()
        except RcloneWebdavConfigError as exc:
            return jsonify({"success": False, "message": str(exc)}), exc.status_code
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response, 200

    def api_admin_rclone_webdav_config():
        return _rclone_webdav_response(
            lambda: rclone_webdav_config_service.status(request.args.get("remote_name"))
        )

    def api_admin_rclone_webdav_config_update():
        payload = request.get_json(silent=True)
        return _rclone_webdav_response(
            lambda: rclone_webdav_config_service.save({} if payload is None else payload)
        )

    def api_admin_rclone_webdav_config_test():
        payload = request.get_json(silent=True) or {}
        return _rclone_webdav_response(
            lambda: rclone_webdav_config_service.test(payload.get("remote_name"))
        )

    def _rclone_admin_command_application() -> RcloneAdminCommandService:
        return RcloneAdminCommandService(RcloneAdminCommandDependencies(rclone=rclone_service))

    def api_admin_organizer_tasks():
        page, per_page, offset = _page_args(50, 200)
        status = str(request.args.get("status") or "").strip() or None
        result = _organizer_admin_query_application().tasks(limit=per_page, offset=offset, status=status)
        return jsonify({"success": True, "status": result["status"], "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def api_admin_organizer_scan():
        payload = request.get_json(silent=True) or {}
        result, status_code = _organizer_admin_command_application().scan(payload)
        return jsonify(result), status_code

    def api_admin_organizer_task_detail(task_id: int):
        result, status_code = _organizer_admin_query_application().detail(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_rebuild(task_id: int):
        result, status_code = _organizer_admin_command_application().rebuild(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_mapping_update(task_id: int, mapping_id: int):
        payload = request.get_json(silent=True) or {}
        result, status_code = _organizer_admin_command_application().update_mapping(task_id, mapping_id, payload)
        return jsonify(result), status_code

    def api_admin_organizer_mappings_batch_update(task_id: int):
        payload = request.get_json(silent=True) or {}
        result, status_code = _organizer_admin_command_application().batch_update_mappings(task_id, payload)
        return jsonify(result), status_code

    def api_admin_organizer_approve(task_id: int):
        result, status_code = _organizer_admin_command_application().approve(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_apply(task_id: int):
        result, status_code = _organizer_admin_command_application().apply(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_skip(task_id: int):
        result, status_code = _organizer_admin_command_application().skip(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_retry(task_id: int):
        result, status_code = _organizer_admin_command_application().retry(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_delete(task_id: int):
        result, status_code = _organizer_admin_command_application().delete(task_id)
        return jsonify(result), status_code

    def api_admin_organizer_runs():
        page, per_page, offset = _page_args(30, 100)
        result = _organizer_admin_query_application().runs(limit=per_page, offset=offset)
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def _organizer_admin_query_application() -> OrganizerAdminQueryService:
        return OrganizerAdminQueryService(OrganizerAdminQueryDependencies(organizer=organizer_service, counts=db))

    def api_admin_organizer_rollback(run_id: int):
        result, status_code = _organizer_admin_command_application().rollback(run_id)
        return jsonify(result), status_code

    def _organizer_admin_command_application() -> OrganizerAdminCommandService:
        return OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=organizer_service,
                worker_dispatcher=worker_task_dispatcher,
            )
        )

    def api_admin_update_subscriptions():
        page, per_page, offset = _page_args(50, 200)
        status = request.args.get("status") or None
        result = update_service.list_subscriptions(page=page, per_page=per_page, status=status)
        items = result.get("items") or []
        total = int(result.get("total") or 0)
        return jsonify({"success": True, "items": items, "pagination": _page_meta(total, page, per_page), "scheduler": update_scheduler.status()})

    def api_admin_update_subscription_create():
        payload = request.get_json(silent=True) or {}
        try:
            item = update_service.create_subscription(payload)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "item": item, "message": "定时追更订阅已创建"})

    def api_admin_update_subscription_detail(subscription_id: int):
        try:
            update_service.sync_subscription_completion(subscription_id)
        except Exception:  # noqa: BLE001
            app.logger.debug("sync update subscription completion failed", exc_info=True)
        item = update_service.get_subscription(subscription_id)
        if not item:
            return jsonify({"success": False, "message": "追更订阅不存在"}), 404
        runs = db.list_update_runs(subscription_id=subscription_id, limit=3)
        candidates = update_service.filter_display_candidates(db.list_update_candidates(subscription_id=subscription_id, limit=100))[:30]
        events = db.list_update_events(subscription_id=subscription_id, limit=20)
        root = ""
        raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
        if raw_data:
            resolution = raw_data.get("canonical_root_resolution") if isinstance(raw_data.get("canonical_root_resolution"), dict) else {}
            root = _resource_update_root(
                raw_data.get("canonical_openlist_root")
                or resolution.get("canonical_openlist_root")
                or raw_data.get("existing_openlist_root")
                or raw_data.get("openlist_path")
            )
        if not root:
            category = app_config.category(str(item.get("category") or "movie")) if str(item.get("category") or "") in app_config.categories else {}
            root = _clean_update_openlist_root(category.get("openlist_root_path") or category.get("fnos_target_path") or category.get("cloud139_fnos_target_path") or category.get("mobile_target_path") or "")
        snapshot = db.get_update_path_snapshot(subscription_id, root) if root else None
        return jsonify({"success": True, "item": item, "runs": runs, "candidates": candidates, "events": events, "snapshot": snapshot})

    def api_admin_update_subscription_update(subscription_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            item = update_service.update_subscription(subscription_id, payload)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "item": item, "message": "定时追更订阅已更新"})

    def api_admin_update_subscription_delete(subscription_id: int):
        try:
            return jsonify(update_service.delete_subscription(subscription_id))
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 404

    def api_admin_update_subscription_run(subscription_id: int):
        return jsonify(update_service.run_subscription(subscription_id, trigger_type="manual"))

    def api_admin_update_subscription_refresh_snapshot(subscription_id: int):
        return jsonify(update_service.refresh_snapshot(subscription_id))

    def api_admin_update_subscription_preview(subscription_id: int):
        return jsonify(update_service.preview_sources(subscription_id))

    def api_admin_update_subscription_pause(subscription_id: int):
        item = update_service.set_status(subscription_id, "paused")
        return jsonify({"success": True, "item": item, "message": "已暂停追更"})

    def api_admin_update_subscription_enable(subscription_id: int):
        item = update_service.set_status(subscription_id, "enabled")
        return jsonify({"success": True, "item": item, "message": "已启用追更"})

    def api_admin_update_runs():
        page, per_page, offset = _page_args(50, 200)
        subscription_id = _safe_int(request.args.get("subscription_id"), 0, 0, 999999999) or None
        items = db.list_update_runs(subscription_id=subscription_id, limit=per_page, offset=offset)
        total = db.count_update_runs(subscription_id=subscription_id)
        return jsonify({"success": True, "items": items, "pagination": _page_meta(total, page, per_page)})

    def api_admin_update_run_detail(run_id: int):
        item = db.get_update_run(run_id)
        if not item:
            return jsonify({"success": False, "message": "追更运行记录不存在"}), 404
        events = db.list_update_events(run_id=run_id, limit=100)
        candidates = update_service.filter_display_candidates(db.list_update_candidates(run_id=run_id, limit=200))[:100]
        return jsonify({"success": True, "item": item, "events": events, "candidates": candidates})

    def api_admin_update_candidates():
        page, per_page, offset = _page_args(50, 200)
        subscription_id = _safe_int(request.args.get("subscription_id"), 0, 0, 999999999) or None
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999) or None
        decision = request.args.get("decision") or None
        items = db.list_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision, limit=per_page, offset=offset)
        total = db.count_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision)
        return jsonify({"success": True, "items": items, "pagination": _page_meta(total, page, per_page)})

    def api_admin_update_candidate_import(candidate_id: int):
        return jsonify(update_service.import_candidate(candidate_id, reason=f"admin_update_candidate:{candidate_id}"))

    def api_admin_update_candidate_reject(candidate_id: int):
        payload = request.get_json(silent=True) or {}
        return jsonify(update_service.reject_candidate(candidate_id, reason=str(payload.get("reason") or "管理员拒绝候选")))

    def api_admin_update_scheduler_run_due():
        payload = request.get_json(silent=True) or {}
        limit = _safe_int(payload.get("limit"), 10, 1, 100)
        return jsonify(update_scheduler.run_due(limit=limit))

    def api_admin_update_scheduler_status():
        return jsonify({"success": True, "status": update_scheduler.status()})

    def api_admin_trending_status():
        pending_candidates = db.list_trending_candidates(status="importing", limit=200, offset=0)
        trending_initial_import_service.reconcile_candidates(pending_candidates)
        status = trending_scheduler.status()
        latest = status.get("latest_run") if isinstance(status.get("latest_run"), dict) else {}
        summary = latest.get("summary") if isinstance(latest.get("summary"), dict) else {}
        source_rows = summary.get("sources") if isinstance(summary.get("sources"), list) else []
        status["sources"] = {
            str(item.get("source") or "unknown"): item
            for item in source_rows
            if isinstance(item, dict)
        }
        status["counts"] = {
            "discovered": db.count_trending_candidates(status="discovered"),
            "already_exists": db.count_trending_candidates(status="already_exists"),
            "task_exists": db.count_trending_candidates(status="task_exists"),
            "ignored": db.count_trending_candidates(status="ignored"),
            "importing": db.count_trending_candidates(status="importing"),
            "imported": db.count_trending_candidates(status="imported"),
            "import_failed": db.count_trending_candidates(status="import_failed"),
            "today": int(latest.get("candidate_count") or 0),
        }
        return jsonify({"success": True, "status": status})

    def api_admin_trending_run():
        result = trending_scheduler.run_now()
        return jsonify({
            **result,
            "message": "热榜发现完成" if result.get("success") else "热榜发现失败，请查看来源错误",
        }), 200 if result.get("success") else 502

    def api_admin_trending_runs():
        page, per_page, offset = _page_args(30, 100)
        items = db.list_trending_runs(limit=per_page, offset=offset)
        total = db.count_trending_runs() if callable(getattr(db, "count_trending_runs", None)) else offset + len(items)
        return jsonify({"success": True, "items": items, "pagination": _page_meta(total, page, per_page)})

    def api_admin_trending_candidates():
        grouped = str(request.args.get("group_by") or request.args.get("grouped") or "").strip().lower() in {
            "1", "true", "media_type", "category"
        }
        if grouped:
            groups = trending_service.grouped_candidates(
                status=request.args.get("status") or None,
                source=request.args.get("source") or None,
            )
            for group in groups.values():
                group["items"] = trending_initial_import_service.reconcile_candidates(group.get("items") or [])
            return jsonify({
                "success": True,
                "grouped": True,
                "groups": groups,
                "total": sum(int(group.get("count") or 0) for group in groups.values()),
            })
        page, per_page, _offset = _page_args(50, 200)
        result = trending_service.list_candidates(
            page=page,
            per_page=per_page,
            status=request.args.get("status") or None,
            media_type=request.args.get("media_type") or None,
            source=request.args.get("source") or None,
        )
        return jsonify({
            "success": True,
            "items": trending_initial_import_service.reconcile_candidates(result.get("items") or []),
            "pagination": _page_meta(int(result.get("total") or 0), page, per_page),
        })

    def api_admin_trending_candidate_detail(candidate_id: int):
        item = db.get_trending_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "error_code": "not_found", "message": "热榜候选不存在"}), 404
        return jsonify({"success": True, "item": trending_initial_import_service.reconcile_candidate(item)})

    def api_admin_trending_candidate_search(candidate_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            sources = _sanitize_sources(payload.get("sources"))
            token = _limited_text(payload.get("token"), "临时 Token", 512)
            result = trending_initial_import_service.search(
                candidate_id,
                sources=sources,
                token=token,
                refresh=_payload_bool(payload, "refresh", False),
                keyword=_limited_text(payload.get("keyword"), "\u641c\u7d22\u5173\u952e\u8bcd", 300),
            )
            return jsonify(result)
        except (TrendingInitialImportError, PublicInputError) as exc:
            return jsonify({"success": False, "message": str(exc)}), getattr(exc, "status_code", 400)
        except Exception:  # noqa: BLE001
            app.logger.exception("trending candidate search failed: candidate_id=%s", candidate_id)
            return jsonify({"success": False, "message": "\u641c\u7d22\u8d44\u6e90\u5931\u8d25"}), 502

    def api_admin_trending_candidate_resource_detail(candidate_id: int, public_id: str):
        try:
            trending_initial_import_service.cached_resource_for_candidate(candidate_id, public_id)
        except TrendingInitialImportError as exc:
            return jsonify({"success": False, "message": str(exc)}), exc.status_code
        result, status_code = _public_resource_application().detail(public_id, hide_full_links=False)
        return jsonify(result), status_code

    def api_admin_trending_candidate_resource_files(candidate_id: int, public_id: str):
        try:
            trending_initial_import_service.cached_resource_for_candidate(candidate_id, public_id)
            fid = _limited_text(request.args.get("fid"), "\u76ee\u5f55 ID", 256, required=True)
        except (TrendingInitialImportError, PublicInputError) as exc:
            return jsonify({"success": False, "message": str(exc)}), getattr(exc, "status_code", 400)
        result, status_code = _public_resource_application().files(public_id, fid=fid)
        return jsonify(result), status_code

    def api_admin_trending_candidate_import(candidate_id: int):
        payload = request.get_json(silent=True) or {}
        prepared_payload = {
            "public_id": str(payload.get("public_id") or "").strip(),
            "resource_id": payload.get("resource_id"),
            "category": str(payload.get("category") or "").strip(),
            "quark_selection": _safe_public_quark_selection(payload.get("quark_selection")),
            "cloud139_selection": _safe_public_cloud139_selection(payload.get("cloud139_selection")),
            "ignore_files": _safe_public_string_list(payload.get("ignore_files"), max_items=2000, max_length=512),
        }
        if prepared_payload["ignore_files"]:
            prepared_payload["sixpan_selection"] = {"ignore_files": prepared_payload["ignore_files"]}
        try:
            result = trending_initial_import_service.create_initial_import(candidate_id, prepared_payload)
            return jsonify(result), 200 if result.get("success", True) else 502
        except TrendingInitialImportError as exc:
            return jsonify({"success": False, "message": str(exc)}), exc.status_code
        except Exception:  # noqa: BLE001
            app.logger.exception("trending candidate initial import failed: candidate_id=%s", candidate_id)
            return jsonify({"success": False, "message": "\u521b\u5efa\u70ed\u699c\u9996\u6b21\u5165\u5e93\u4efb\u52a1\u5931\u8d25"}), 500

    def api_admin_trending_candidate_ignore(candidate_id: int):
        item = db.get_trending_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        if str(item.get("status") or "") == "importing":
            return jsonify({"success": False, "message": "首入库任务正在执行，暂不能忽略该候选"}), 409
        payload = request.get_json(silent=True) or {}
        db.update_trending_candidate(
            candidate_id,
            status="ignored",
            ignore_reason=str(payload.get("reason") or "管理员忽略"),
        )
        return jsonify({"success": True, "item": db.get_trending_candidate(candidate_id), "message": "热榜候选已忽略"})

    def api_admin_trending_candidate_restore(candidate_id: int):
        item = db.get_trending_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        media_exists = _hot_media_exists(item)
        status = "already_exists" if media_exists else "task_exists" if _hot_task_exists(item) else "discovered"
        db.update_trending_candidate(
            candidate_id,
            status=status,
            media_exists=media_exists,
            ignore_reason=None,
            ignore_until=None,
        )
        return jsonify({"success": True, "item": db.get_trending_candidate(candidate_id), "message": "热榜候选已恢复"})

    def api_admin_trending_candidate_subscribe(candidate_id: int):
        item = db.get_trending_candidate(candidate_id)
        if not item:
            return jsonify({"success": False, "message": "热榜候选不存在"}), 404
        # 持久绑定必须以 TMDB/category/season 为身份。标题匹配只用于
        # discovery 展示 task_exists，不能在这里直接绑定同名翻拍或不同季。
        result, status_code = _create_subscription_from_hot_candidate(
            item,
            update_service.tmdb,
            lambda payload: update_service.create_subscription_from_trending_candidate(candidate_id, payload),
        )
        result["item"] = db.get_trending_candidate(candidate_id)
        return jsonify(result), status_code

    def api_admin_openlist_test():
        return jsonify(_external_diagnostics_application().openlist_test())

    def api_admin_btbtla_proxy_test():
        payload = request.get_json(silent=True) or {}
        return jsonify(btbtla_proxy_diagnostics_service.test(payload))

    def api_admin_openlist_dirs():
        path = _clean_update_openlist_root(request.args.get("path") or "/")
        if not path:
            path = "/"
        result, status_code = _external_diagnostics_application().openlist_dirs(path)
        return jsonify(result), status_code

    def api_admin_tmdb_test():
        return jsonify(_external_diagnostics_application().tmdb_test())

    def api_admin_tmdb_search():
        query = str(request.args.get("query") or "").strip()
        media_type = str(request.args.get("media_type") or "tv").strip() or "tv"
        return jsonify(_external_diagnostics_application().tmdb_search(query, media_type))

    def api_admin_tmdb_detail(media_type: str, tmdb_id: int):
        season = _safe_int_value(request.args.get("season"), 0)
        result, status_code = _external_diagnostics_application().tmdb_detail(media_type, tmdb_id, season)
        return jsonify(result), status_code

    def api_admin_ai_test():
        payload = request.get_json(silent=True) or {}
        return jsonify(_external_diagnostics_application().ai_test(payload))

    def _external_diagnostics_application() -> ExternalDiagnosticsService:
        return ExternalDiagnosticsService(ExternalDiagnosticsDependencies(organizer=organizer_service))

    settings_service = SettingsService(SettingsDependencies(db=db, raw_config=lambda: app_config.raw, redact_config=_redact_config, advanced_response=advanced_config_response, normalize_advanced=normalize_advanced_config_payload, advanced_key=ADVANCED_CONFIG_KEY, reload_runtime=_reload_runtime_config, effective_settings=_effective_settings, payload_bool=_payload_bool, search_providers=search_service.describe_providers))
    notification_settings_service = NotificationSettingsService(
        db=db,
        public_base_url=lambda: request.host_url.rstrip("/"),
    )

    def _settings_response(result):
        body, status_code = result
        return jsonify(body), status_code

    def api_admin_config():
        return _settings_response(settings_service.config())

    def api_admin_maintenance_history_summary():
        return _settings_response(settings_service.history_summary())

    def api_admin_maintenance_cleanup_history():
        return _settings_response(settings_service.cleanup_history(request.get_json(silent=True) or {}))

    def api_admin_advanced_config():
        return _settings_response(settings_service.advanced())

    def api_admin_advanced_config_update():
        payload = request.get_json(silent=True) or {}
        result = settings_service.update_advanced(payload)
        body, status_code = result
        if status_code == 200 and str(payload.get("source") or "").strip().lower() == "import":
            app.logger.info(
                "advanced config import completed: admin=%s mode=%s sections=%s",
                str(session.get("admin_username") or "admin"),
                str(body.get("mode") or "merge"),
                len((body.get("meta") or {}).get("stored_sections") or []),
            )
        return _settings_response(result)

    def api_admin_advanced_config_export():
        payload = request.get_json(silent=True) or {}
        if not _payload_bool(payload, "confirm", False):
            return jsonify({"success": False, "message": "请先确认导出包含敏感密钥的高级配置"}), 400
        body, status_code = settings_service.export_advanced()
        response = jsonify(body)
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        if status_code == 200:
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            app.logger.info(
                "advanced config export completed: admin=%s stored_sections=%s",
                str(session.get("admin_username") or "admin"),
                len(meta.get("stored_sections") or []),
            )
        return response, status_code

    def api_admin_settings():
        return _settings_response(settings_service.settings())

    def api_admin_settings_update():
        return _settings_response(settings_service.update_settings(request.get_json(silent=True) or {}))

    def api_admin_settings_update_all():
        return _settings_response(settings_service.update_all(request.get_json(silent=True) or {}))

    def _notification_response(result: tuple[dict[str, Any], int]):
        body, status_code = result
        response = jsonify(body)
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response, status_code

    def api_admin_notifications_config():
        return _notification_response(notification_settings_service.config())

    def api_admin_notifications_update():
        payload = request.get_json(silent=True)
        return _notification_response(
            notification_settings_service.update({} if payload is None else payload)
        )

    def api_admin_notifications_test():
        payload = request.get_json(silent=True)
        return _notification_response(
            notification_settings_service.test({} if payload is None else payload)
        )

    def api_admin_notifications_deliveries():
        return _notification_response(
            notification_settings_service.deliveries(request.args.to_dict())
        )

    def api_admin_notifications_retry(task_id: int):
        return _notification_response(notification_settings_service.retry(task_id))

    def api_admin_search_providers():
        settings = db.get_app_settings()
        return jsonify(
            {
                "success": True,
                "items": search_service.describe_providers(),
                "aliases": settings.get("search.aliases") or {},
            }
        )

    def api_admin_search_providers_update():
        payload = request.get_json(silent=True) or {}
        provider_payload = payload.get("providers", payload.get("items", {}))
        current = db.get_app_settings().get("search.providers")
        provider_settings = current if isinstance(current, dict) else {}

        if isinstance(provider_payload, list):
            iterable = provider_payload
        elif isinstance(provider_payload, dict):
            iterable = [{"key": key, **value} for key, value in provider_payload.items() if isinstance(value, dict)]
        else:
            return jsonify({"success": False, "message": "搜索源设置格式不正确"}), 400

        known = {item["key"] for item in search_service.describe_providers()}
        for item in iterable:
            key = str(item.get("key") or "").strip()
            if key not in known:
                return jsonify({"success": False, "message": f"未知搜索源：{key}"}), 400
            existing = provider_settings.get(key) if isinstance(provider_settings.get(key), dict) else {}
            provider_settings[key] = {
                "enabled": _payload_bool(item, "enabled", _setting_bool(existing, "enabled", True)),
                "priority": _safe_int(item.get("priority", existing.get("priority", 100)), 100, 1, 999),
            }

        db.set_app_settings({"search.providers": provider_settings})
        return jsonify({"success": True, "message": "搜索源设置已保存", "items": search_service.describe_providers()})

    def api_admin_search_aliases():
        aliases = db.get_app_settings().get("search.aliases") or {}
        return jsonify({"success": True, "aliases": aliases})

    def api_admin_search_aliases_update():
        payload = request.get_json(silent=True) or {}
        aliases = payload.get("aliases", payload)
        if not isinstance(aliases, dict):
            return jsonify({"success": False, "message": "别名词库格式不正确"}), 400
        normalized_aliases: dict[str, list[str]] = {}
        for title, values in aliases.items():
            key = str(title or "").strip()
            if not key:
                continue
            if isinstance(values, str):
                items = [part.strip() for part in re.split(r"[,，\n]", values) if part.strip()]
            elif isinstance(values, list):
                items = [str(part).strip() for part in values if str(part).strip()]
            else:
                continue
            normalized_aliases[key] = items[:20]
        db.set_app_settings({"search.aliases": normalized_aliases})
        return jsonify({"success": True, "message": "搜索别名词库已保存", "aliases": normalized_aliases})

    def api_admin_adapters():
        return jsonify({"success": True, "items": _adapter_placeholders(app_config.raw, generic_importers, cloud139_importer=cloud139_importer)})

    def api_admin_adapter_probe(adapter_key: str):
        adapters = {item["key"]: item for item in _adapter_placeholders(app_config.raw, generic_importers, cloud139_importer=cloud139_importer)}
        item = adapters.get(adapter_key)
        if not item:
            return jsonify({"success": False, "message": "线路适配器不存在"}), 404
        importer = cloud139_importer if adapter_key == "cloud139" else generic_importers.get(adapter_key)
        if importer and hasattr(importer, "probe"):
            result = importer.probe()
            return jsonify(
                {
                    "success": True,
                    "adapter": item,
                    "probe": {
                        "ok": result.ok,
                        "status": result.status,
                        "message": result.message,
                        "details": result.details,
                    },
                }
            )
        return jsonify(
            {
                "success": True,
                "adapter": item,
                "probe": {
                    "ok": bool(item.get("configured")),
                    "status": "placeholder",
                    "message": "对接入口已预留，后续接入专用适配器后再启用真实探测",
                },
            }
        )

    def api_public_config():
        settings = _effective_settings()
        return jsonify(
            {
                "success": True,
                "app_name": app_config.app_name,
                "categories": _public_categories(app_config.categories),
                "routes": _public_routes(app_config.raw.get("routes", {})),
                "category_labels": CATEGORY_LABELS,
                "security": _public_security_config(security_config),
                "public": settings["public"],
                "submission": settings["submission"],
                "search": {"providers": search_service.describe_providers()},
                "adapters": _public_adapter_capabilities(app_config.raw),
                "notifications": {
                    "guest_email_available": _guest_email_subscription_available()
                },
            }
        )

    def api_public_trending():
        groups = trending_service.grouped_candidates(limit=25)
        latest = trending_service.status().get("latest_run") or {}

        def public_item(item: dict[str, Any]) -> dict[str, Any]:
            raw_data = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
            source_items = raw_data.get("source_items") if isinstance(raw_data.get("source_items"), list) else []
            source_values = raw_data.get("sources") if isinstance(raw_data.get("sources"), list) else source_items
            source_rows = [
                {
                    "source": str(source_item.get("source") or "").strip(),
                    "rank": source_item.get("rank"),
                }
                for source_item in source_values
                if isinstance(source_item, dict) and str(source_item.get("source") or "").strip()
            ]
            image_url = str(item.get("image_url") or "").strip()
            if not image_url:
                image_url = next(
                    (
                        str(source_item.get("image_url") or "").strip()
                        for source_item in source_items
                        if isinstance(source_item, dict) and str(source_item.get("image_url") or "").strip()
                    ),
                    "",
                )
            media_exists = bool(item.get("media_exists")) or str(item.get("status") or "") == "already_exists"
            if not media_exists:
                media_exists = bool(_hot_media_exists(item))
            return {
                "title": str(item.get("title") or "").strip(),
                "year": item.get("year"),
                "media_type": str(item.get("media_type") or "unknown"),
                "rank": int(item.get("rank") or raw_data.get("category_rank") or 0),
                "heat": item.get("heat"),
                "score": item.get("score"),
                "image_url": image_url,
                "sources": source_rows,
                "platform_ranks": item.get("platform_ranks") if isinstance(item.get("platform_ranks"), dict) else raw_data.get("platform_ranks") or {},
                "media_exists": media_exists,
                "availability_text": "\u5df2\u7ecf\u53ef\u4ee5\u89c2\u770b\u4e86" if media_exists else "\u641c\u7d22\u8d44\u6e90",
            }

        public_groups: dict[str, dict[str, Any]] = {}
        for media_type in ("tv", "movie", "variety", "anime"):
            rows = groups.get(media_type, {}).get("items") or []
            items = [public_item(item) for item in rows if str(item.get("title") or "").strip()]
            public_groups[media_type] = {"items": items[:25], "count": min(25, len(items))}
        return jsonify(
            {
                "success": True,
                "groups": public_groups,
                "updated_at": latest.get("finished_at") or latest.get("started_at") or "",
            }
        )

    def _public_submission_application() -> PublicSubmissionService:
        return PublicSubmissionService(
            PublicSubmissionDependencies(
                queries=db.guest_request_queries,
                commands=db.guest_request_commands,
                sync_request=_sync_guest_request_status,
                public_status=_public_status,
                public_request=_public_request_response,
                db=db,
                emit_notification=_emit_configured_notification,
            )
        )

    def api_public_captcha():
        limited = _rate_limit("public_captcha", "public_captcha_rate_limit")
        if limited:
            return limited
        if not _config_bool(security_config, "captcha_enabled", False):
            return jsonify({"success": True, "enabled": False})
        challenge = _new_simple_captcha(security_config, str(app.secret_key or ""))
        session["public_captcha_hash"] = challenge["answer_hash"]
        session["public_captcha_expires_at"] = int(time.time()) + challenge["expires_in_seconds"]
        return jsonify(
            {
                "success": True,
                "enabled": True,
                "provider": "simple",
                "question": challenge["question"],
                "expires_in_seconds": challenge["expires_in_seconds"],
            }
        )

    def _public_search_application(*, admin: bool = False) -> PublicSearchService:
        def present(item: dict[str, Any], *, public_id: str, hide_full_links: bool) -> dict[str, Any]:
            projected = _public_resource_item(
                item,
                public_id=public_id,
                hide_full_links=False if admin else hide_full_links,
            )
            if admin and item.get("resource_id"):
                projected["resource_id"] = int(item["resource_id"])
            return projected

        return PublicSearchService(
            PublicSearchDependencies(
                search=search_service,
                cache=db,
                new_public_id=_new_public_id,
                present_item=present,
                log_info=app.logger.info,
            )
        )

    def api_public_search():
        route_started = time.perf_counter()
        trace_id = secrets.token_hex(4)
        public_settings = _current_public_settings()
        if not public_settings.get("allow_anonymous_search", True):
            return jsonify({"success": False, "message": "匿名搜索已关闭，请使用链接提交"}), 403
        limited = _rate_limit("public_search", "public_search_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        try:
            keyword = _limited_text(payload.get("keyword") or payload.get("kw"), "搜索关键词", _config_int(security_config, "max_keyword_length", 80), required=True)
            sources = _sanitize_sources(payload.get("sources"))
            token = _limited_text(payload.get("token"), "临时 Token", 512)
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        search_options = {
            "async_poll": _payload_bool(payload, "async_poll", False),
            "trace_id": trace_id,
            "save_resources": not _payload_bool(payload, "background", False),
        }
        if "refresh" in payload:
            search_options["refresh"] = _payload_bool(payload, "refresh", False)
        app.logger.info(
            "search_trace=%s stage=public_route_start keyword=%r background=%s async_poll=%s",
            trace_id,
            _short_text(keyword),
            _payload_bool(payload, "background", False),
            search_options["async_poll"],
        )
        result = _public_search_application().search(
            keyword,
            sources=sources,
            token=token,
            options=search_options,
            hide_full_links=public_settings.get("hide_full_links", True),
            trace_id=trace_id,
        )
        app.logger.info(
            "search_trace=%s stage=public_route_done total_ms=%.1f items=%d",
            trace_id,
            _elapsed_ms(route_started),
            len(result["items"]),
        )
        return jsonify(result)

    def api_public_detect():
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        try:
            url = _validate_public_url(payload.get("url"), security_config)
            password = _limited_text(payload.get("password"), "提取码", _config_int(security_config, "max_password_length", 32))
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        link = detect_link(url, app_config.raw.get("routes", {}), password=password)
        public_link = {
            "source_type": link.source_type,
            "supported": link.supported,
            "reason": "已识别资源类型，提交时会检测可用性" if link.supported else "当前资源暂不支持自动入库",
            "route": link.route,
            "detail_capability": _detail_capability_for_source(link.source_type, cloud139_importer=cloud139_importer, sixpan_importer=generic_importers.get("sixpan")),
        }
        return jsonify({"success": True, "link": public_link})

    public_manual_preview_service = PublicManualPreviewService(
        cache=db,
        new_public_id=_new_public_id,
        detect_link=detect_link,
        routes=lambda: app_config.raw.get("routes", {}),
        resource_detail=_public_resource_detail,
        present_item=_public_resource_item,
        detail_capability=_detail_capability_for_source,
        quark_importer=lambda: quark_importer,
        cloud139_importer=lambda: cloud139_importer,
        sixpan_importer=lambda: generic_importers.get("sixpan"),
        hide_full_links=lambda: _current_public_settings().get("hide_full_links", True),
    )

    def api_public_manual_preview():
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        try:
            url = _validate_public_url(payload.get("url"), security_config)
            password = _limited_text(payload.get("password"), "提取码", _config_int(security_config, "max_password_length", 32))
            title = _limited_text(payload.get("title") or payload.get("preferred_title"), "资源标题", _config_int(security_config, "max_title_length", 300), required=True)
            category_key = _limited_text(payload.get("category"), "资源分类", 40, required=True)
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if category_key not in app_config.categories:
            return jsonify({"success": False, "message": "资源分类不存在"}), 400

        return jsonify(
            public_manual_preview_service.preview(
                url=url,
                password=password,
                title=title,
            )
        )

    def api_public_resource_detail(public_id: str):
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        try:
            public_id = _limited_text(public_id, "资源编号", _config_int(security_config, "max_token_length", 80), required=True)
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        result, status_code = _public_resource_application().detail(
            public_id, hide_full_links=_current_public_settings().get("hide_full_links", True)
        )
        return jsonify(result), status_code

    def api_public_resource_files(public_id: str):
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        try:
            public_id = _limited_text(public_id, "资源编号", _config_int(security_config, "max_token_length", 80), required=True)
            fid = _limited_text(request.args.get("fid"), "目录 ID", 256, required=True)
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        result, status_code = _public_resource_application().files(public_id, fid=fid)
        return jsonify(result), status_code

    def _public_resource_application() -> PublicResourceService:
        return PublicResourceService(
            PublicResourceDependencies(
                cache_get=db.get_search_cache,
                routes=app_config.raw.get("routes", {}),
                quark_importer=quark_importer,
                cloud139_importer=cloud139_importer,
                sixpan_importer=generic_importers.get("sixpan"),
                btbtla_client=btbtla,
                build_detail=_public_resource_detail,
                child_files=_public_resource_child_files,
            )
        )

    public_sixpan_preview_service = PublicSixpanPreviewService(
        cache=db,
        importer=lambda: generic_importers.get("sixpan"),
        validate_url=lambda value: _validate_public_url(value, security_config),
        detect_link=detect_link,
        routes=lambda: app_config.raw.get("routes", {}),
        summarize=_sixpan_parse_summary,
    )

    def api_public_sixpan_parse():
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        try:
            public_id = _limited_text(payload.get("public_id") or payload.get("resource_id"), "资源编号", _config_int(security_config, "max_token_length", 80))
            title = _limited_text(payload.get("title"), "资源标题", _config_int(security_config, "max_title_length", 300))
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc), "items": []}), 400
        result, status_code = public_sixpan_preview_service.preview(
            public_id=public_id,
            url=payload.get("url"),
            title=title,
            password=str(payload.get("password") or ""),
        )
        return jsonify(result), status_code

    public_bt_resolve_service = PublicBtResolveService(
        cache=db,
        btbtla=lambda: btbtla,
        present_item=_public_resource_item,
        resource_detail=_public_resource_detail,
        routes=lambda: app_config.raw.get("routes", {}),
        quark_importer=lambda: quark_importer,
        cloud139_importer=lambda: cloud139_importer,
        sixpan_importer=lambda: generic_importers.get("sixpan"),
        hide_full_links=lambda: _current_public_settings().get("hide_full_links", True),
    )

    def api_public_btbtla_resolve():
        limited = _rate_limit("public_detect", "public_detect_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        try:
            public_id = _limited_text(payload.get("public_id") or payload.get("resource_id"), "资源编号", _config_int(security_config, "max_token_length", 80), required=True)
            resource_id = _limited_text(payload.get("resource_id") or payload.get("download_id"), "下载资源 ID", 40)
            resource_url = _limited_text(payload.get("resource_url"), "下载页地址", _config_int(security_config, "max_url_length", 2048))
            resource_title = _limited_text(payload.get("resource_title") or payload.get("title"), "资源标题", _config_int(security_config, "max_title_length", 300))
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        result, status_code = public_bt_resolve_service.resolve(
            public_id=public_id,
            resource_id=resource_id,
            resource_url=resource_url,
            resource_title=resource_title,
        )
        return jsonify(result), status_code

    def _submit_content_guard_for_public_submission(link: Any, submit_payload: dict[str, Any], preflight: dict[str, Any] | None = None) -> dict[str, Any]:
        source_type = str(getattr(link, "source_type", "") or "").strip().lower()
        title = str(submit_payload.get("title") or ("BT 入库资源" if source_type in BT_SOURCE_TYPES else "未命名资源"))
        ignore_files = _safe_public_string_list(submit_payload.get("ignore_files"), max_items=2000, max_length=512)
        parse_error = ""
        sixpan_selection = submit_payload.get("sixpan_selection") if isinstance(submit_payload.get("sixpan_selection"), dict) else {}
        if source_type in BT_SOURCE_TYPES and str(sixpan_selection.get("parse_status") or "").strip().lower() == "parse_failed":
            parse_error = str(sixpan_selection.get("parse_error") or "提交前内容预览解析失败").strip()
        files: list[dict[str, Any]] = _preflight_guard_files(preflight)
        if source_type in BT_SOURCE_TYPES:
            importer = generic_importers.get("sixpan")
            if parse_error:
                pass
            elif importer and getattr(importer, "configured", False):
                try:
                    parse_data = importer.parse_resource(title=title, source_url=getattr(link, "url", ""), source_type=source_type)
                    parse_summary = _sixpan_parse_summary(parse_data)
                    files = [item for item in parse_summary.get("items") or [] if isinstance(item, dict)]
                except Exception as exc:  # noqa: BLE001
                    parse_error = str(exc)
            else:
                parse_error = "六盘解析服务未配置，无法确认 BT/磁链内容"

        return evaluate_submission_content_risk(
            config=app_config.raw,
            title=title,
            source_type=source_type,
            source_url=str(getattr(link, "url", "") or submit_payload.get("url") or ""),
            category=str(submit_payload.get("category") or ""),
            note=str(submit_payload.get("note") or ""),
            files=files,
            ignore_files=ignore_files,
            parse_error=parse_error,
        )

    def _preflight_guard_files(preflight: dict[str, Any] | None) -> list[dict[str, Any]]:
        inspection = preflight.get("inspection") if isinstance(preflight, dict) and isinstance(preflight.get("inspection"), dict) else {}
        rows = inspection.get("items") if isinstance(inspection, dict) else []
        if not isinstance(rows, list):
            return []
        files: list[dict[str, Any]] = []
        for row in rows[:200]:
            if not isinstance(row, dict):
                continue
            files.append(
                {
                    "id": row.get("id") or row.get("fid") or row.get("path") or row.get("name"),
                    "identity": row.get("id") or row.get("fid") or row.get("path") or row.get("name"),
                    "name": row.get("name") or row.get("title") or row.get("path") or "",
                    "path": row.get("path") or row.get("name") or row.get("title") or "",
                    "size": row.get("size") or 0,
                    "size_text": row.get("size_text") or row.get("size") or "",
                    "directory": bool(row.get("directory") or row.get("is_dir")),
                    "media_type": row.get("media_type") or "",
                }
            )
        return files

    public_submission_preparation = PublicSubmissionPreparationService(
        search_cache=db.get_search_cache,
        categories=lambda: app_config.categories,
        category=lambda key: app_config.category(key),
        routes=lambda: app_config.raw.get("routes", {}),
        limited_text=_limited_text,
        validate_url=_validate_public_url,
        safe_string_list=_safe_public_string_list,
        safe_quark_selection=_safe_public_quark_selection,
        safe_cloud139_selection=_safe_public_cloud139_selection,
        detect_link=detect_link,
        security_config=lambda: security_config,
        config_int=_config_int,
    )

    def _cancel_unbound_public_import_job(
        job: dict[str, Any],
        *,
        reason: str,
        request_item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return _job_cancellation_service().cancel(
            job,
            reason=reason,
            payload={"cleanup": False, "stop_running": True},
            request_item=request_item,
            cleanup_default=False,
            stop_running_default=True,
            admin_username=None,
        )

    public_import_job_coordinator = PublicImportJobCoordinator(
        import_service=lambda: import_service,
        submission_service=_public_submission_application,
        runtime_revision=lambda: runtime_services.revision,
        executor_id=lambda: active_process_role,
        start_rclone=_auto_start_rclone_for_import,
        public_status=_public_status,
        safe_result=_guest_safe_job_result,
        warn=lambda message, args: app.logger.warning(message, *args),
        cancel_unbound_job=_cancel_unbound_public_import_job,
        worker_dispatcher=worker_task_dispatcher,
    )

    public_submission_decision = PublicSubmissionDecisionService(
        submission_service=_public_submission_application,
        get_request=db.get_guest_request,
        add_event=db.add_guest_request_event,
        update_request=db.update_guest_request,
        merge_raw_data=_merge_raw_data,
        public_request=_public_request_response,
        auto_submit_allowed=_auto_submit_allowed,
    )

    def _guest_email_subscription_available() -> bool:
        try:
            key = notification_secrets.key_bytes()
        except ValueError:
            return False
        return (
            notification_config.guest_email_available(notification_config.read_config(db))
            and key is not None
            and len(key) in {16, 24, 32}
        )

    def _prepare_guest_notification(
        payload: dict[str, Any],
        request_token: str,
    ) -> Callable[[Any, int], None] | None:
        if payload.get("notification_email_enabled") is not True:
            return None
        email = str(payload.get("notification_email") or "").strip().lower()
        if not _guest_email_subscription_available():
            raise ValueError("邮件进度通知当前不可用，请取消勾选后重试")
        if (
            len(email) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", email)
        ):
            raise ValueError("请输入有效的通知邮箱")

        verification_token = secrets.token_urlsafe(32)
        unsubscribe_token = secrets.token_urlsafe(32)
        encrypted_email = notification_secrets.store(email)
        encrypted_verification = notification_secrets.store(verification_token)
        encrypted_unsubscribe = notification_secrets.store(unsubscribe_token)
        encryption_key = notification_secrets.key_bytes()
        if encryption_key is None:  # 已由可用性检查拦截，仅保留类型收窄。
            raise ValueError("通知加密密钥不可用")

        def _create_subscription(connection: Any, request_id: int) -> None:
            db.create_guest_notification_subscription(
                request_id=request_id,
                email_encrypted=encrypted_email,
                email_hash=hmac.new(
                    encryption_key, email.encode("utf-8"), hashlib.sha256
                ).hexdigest(),
                verification_token_encrypted=encrypted_verification,
                verification_token_hash=hashlib.sha256(
                    verification_token.encode("utf-8")
                ).hexdigest(),
                verification_expires_at=utc_now_iso_offset(hours=24),
                unsubscribe_token_encrypted=encrypted_unsubscribe,
                unsubscribe_token_hash=hashlib.sha256(
                    unsubscribe_token.encode("utf-8")
                ).hexdigest(),
                connection=connection,
            )
            emit_notification(
                db,
                notification_events.EVENT_GUEST_EMAIL_VERIFY,
                {
                    "request_id": request_id,
                    "request_token": request_token,
                    "title": str(payload.get("title") or "未命名资源"),
                },
                idempotency_key=notification_events.idempotency_key(
                    notification_events.EVENT_GUEST_EMAIL_VERIFY, request_id
                ),
                connection=connection,
                channels_override=[notification_events.CHANNEL_GUEST_EMAIL],
            )

        return _create_subscription

    public_submission_intake = PublicSubmissionIntakeService(
        submission_service=_public_submission_application,
        preflight=lambda prepared: _preflight_public_submission(
            prepared.link,
            title=str(prepared.payload.get("title") or ""),
            raw=(
                prepared.cached.get("raw_data")
                if prepared.cached and isinstance(prepared.cached.get("raw_data"), dict)
                else {}
            ),
            quark_importer=quark_importer,
            cloud139_importer=cloud139_importer,
            sixpan_importer=generic_importers.get("sixpan"),
        ),
        new_token=_new_request_token,
        cached_item=_public_cached_item,
        request_payload=_public_request_payload,
        duplicate_minutes=lambda: _config_int(
            security_config, "duplicate_window_minutes", 1440
        ),
        duplicate_enabled=lambda: _config_bool(
            security_config, "duplicate_check_enabled", True
        ),
        prepare_notification=lambda payload, request_token: _prepare_guest_notification(
            payload, request_token
        ),
    )

    def api_public_submit():
        limited = _rate_limit("public_submit", "public_submit_rate_limit")
        if limited:
            return limited
        payload = request.get_json(silent=True) or {}
        captcha_ok, captcha_message = _verify_public_captcha(
            payload, security_config, _client_ip(), str(app.secret_key or "")
        )
        if not captcha_ok:
            return jsonify({"success": False, "message": captcha_message}), 400
        try:
            prepared_submission = public_submission_preparation.prepare(payload)
        except (PublicInputError, PublicSubmissionPreparationError) as exc:
            return jsonify({"success": False, "message": str(exc)}), getattr(exc, "status_code", 400)

        submit_payload = prepared_submission.payload
        link = prepared_submission.link
        try:
            intake = public_submission_intake.begin(
                prepared_submission,
                client_ip_hash=_hash_client_ip(
                    _client_ip(),
                    str(security_config.get("ip_hash_salt") or app.secret_key or ""),
                ),
                user_agent=_clip_text(request.headers.get("User-Agent"), 200),
            )
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if intake.response is not None:
            return jsonify(intake.response)
        preflight = intake.preflight
        request_token = intake.request_token
        guest_request_id = intake.guest_request_id
        submit_mode = _current_submission_mode()
        content_guard = (
            _submit_content_guard_for_public_submission(link, submit_payload, preflight)
            if link.supported
            else {}
        )
        decision_response = public_submission_decision.decide(
            guest_request_id=guest_request_id,
            request_token=request_token,
            link=link,
            submit_mode=submit_mode,
            content_guard=content_guard,
        )
        if decision_response is not None:
            return jsonify(decision_response)

        import_outcome = public_import_job_coordinator.execute(
            guest_request_id=guest_request_id,
            request_token=request_token,
            submit_payload=submit_payload,
        )
        result = import_outcome.result
        public_status = import_outcome.public_status
        bound_request = import_outcome.bound_request
        return jsonify(
            {
                "success": bool(result.get("success", True)),
                "message": _public_submit_message(result),
                "request_token": request_token,
                "status": public_status,
                "request": _public_request_response(bound_request or db.get_guest_request(guest_request_id)),
            }
        )

    def api_public_request(token: str):
        query_enabled = _current_public_settings().get("request_query_enabled", True)
        if not query_enabled:
            result, status_code = _public_submission_application().get_public_request(token, query_enabled=False)
            return jsonify(result), status_code
        limited = _rate_limit("public_request", "public_request_rate_limit")
        if limited:
            return limited
        try:
            token = _limited_text(token, "提交编号", _config_int(security_config, "max_token_length", 80), required=True)
        except PublicInputError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        result, status_code = _public_submission_application().get_public_request(
            token,
            query_enabled=query_enabled,
        )
        return jsonify(result), status_code

    def _public_notification_confirmation(token: str, *, action: str):
        try:
            _limited_text(token, "通知令牌", 160, required=True)
        except PublicInputError:
            return redirect("/submit?notification=invalid", code=302)
        verify = action == "verify"
        response = app.make_response(
            render_template(
                "notification_action.html",
                title="确认接收邮件通知" if verify else "确认停止邮件通知",
                action_label="确认接收" if verify else "停止接收",
                action_kind=action,
            )
        )
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def api_public_notification_verify_confirm(token: str):
        return _public_notification_confirmation(token, action="verify")

    def api_public_notification_verify(token: str):
        try:
            token = _limited_text(token, "验证令牌", 160, required=True)
        except PublicInputError:
            return redirect("/submit?notification=invalid", code=302)
        subscription = db.verify_guest_notification_subscription(token)
        if not subscription:
            return redirect("/submit?notification=invalid", code=302)
        request_token = str(subscription.get("request_token") or "")
        return redirect(
            f"/request/{request_token}?{urlencode({'notification': 'verified'})}",
            code=302,
        )

    def api_public_notification_unsubscribe_confirm(token: str):
        return _public_notification_confirmation(token, action="unsubscribe")

    def api_public_notification_unsubscribe(token: str):
        try:
            token = _limited_text(token, "退订令牌", 160, required=True)
        except PublicInputError:
            return redirect("/submit?notification=invalid", code=302)
        subscription = db.opt_out_guest_notification_subscription(token)
        if not subscription:
            return redirect("/submit?notification=invalid", code=302)
        request_token = str(subscription.get("request_token") or "")
        return redirect(
            f"/request/{request_token}?{urlencode({'notification': 'unsubscribed'})}",
            code=302,
        )

    def public_config():
        settings = _effective_settings()
        return jsonify(
            {
                "app_name": app_config.app_name,
                "categories": app_config.categories,
                "routes": _public_routes(app_config.raw.get("routes", {})),
                "public": settings["public"],
                "submission": settings["submission"],
                "search": {"providers": search_service.describe_providers()},
                "category_labels": CATEGORY_LABELS,
            }
        )

    def api_search():
        route_started = time.perf_counter()
        trace_id = secrets.token_hex(4)
        payload = request.get_json(silent=True) or {}
        keyword = str(payload.get("keyword") or payload.get("kw") or "").strip()
        if not keyword:
            return jsonify({"success": False, "message": "缺少搜索关键词"}), 400
        sources = payload.get("sources")
        token = str(payload.get("token") or "").strip()
        if sources is not None and not isinstance(sources, list):
            sources = [str(sources)]
        search_options = {
            "async_poll": _payload_bool(payload, "async_poll", False),
            "trace_id": trace_id,
            "save_resources": not _payload_bool(payload, "background", False),
        }
        if "refresh" in payload:
            search_options["refresh"] = _payload_bool(payload, "refresh", False)
        app.logger.info(
            "search_trace=%s stage=admin_route_start keyword=%r background=%s async_poll=%s",
            trace_id,
            _short_text(keyword),
            _payload_bool(payload, "background", False),
            search_options["async_poll"],
        )
        result = search_service.search(keyword, sources=sources, token=token, options=search_options)
        app.logger.info(
            "search_trace=%s stage=admin_route_done total_ms=%.1f items=%d",
            trace_id,
            _elapsed_ms(route_started),
            len(result.get("items") or []),
        )
        return jsonify({"success": True, "items": result["items"], "raw": result["raw"]})

    def api_detect():
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url") or "").strip()
        if not url:
            return jsonify({"success": False, "message": "缺少资源链接"}), 400
        password = str(payload.get("password") or "").strip()
        link = detect_link(url, app_config.raw.get("routes", {}), password=password)
        return jsonify({"success": True, "link": link.to_dict()})

    def api_import():
        payload = request.get_json(silent=True) or {}
        if not str(payload.get("url") or payload.get("source_url") or "").strip():
            return jsonify({"success": False, "message": "缺少资源链接"}), 400
        result = import_service.create_import_job(payload)
        _auto_start_rclone_for_import(result, "api_import")
        return jsonify({"success": bool(result.get("success", True)), **result})

    def api_jobs():
        page, per_page, offset = _page_args(100, 500)
        status = request.args.get("status") or None
        category = request.args.get("category") or None
        source_type = request.args.get("source_type") or None
        keyword = request.args.get("keyword") or request.args.get("q") or None
        result = _job_admin_query_application().list_jobs(limit=per_page, offset=offset, reconcile_reason="api_jobs_list", status=status, category=category, source_type=source_type, keyword=keyword)
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def api_job_detail(job_id: int):
        result, status_code = _job_admin_query_application().detail(job_id, reconcile_reason="api_job_detail")
        return jsonify(result), status_code

    def api_job_retry(job_id: int):
        return jsonify(_job_admin_command_application().retry(job_id, reason=f"api_retry:{job_id}"))

    def api_media_refresh():
        payload = request.get_json(silent=True) or {}
        category = str(payload.get("category") or "movie")
        return jsonify(MediaAdminCommandService(MediaAdminCommandDependencies(imports=import_service, client=fnos, directory_required_message=ImportService.FNOS_DIR_REQUIRED_MESSAGE, worker_dispatcher=worker_task_dispatcher)).refresh({"category": category}))

    def api_rclone_status():
        return jsonify(_rclone_admin_query_application().status())

    def api_rclone_start():
        payload = request.get_json(silent=True) or {}
        return jsonify(_rclone_admin_command_application().start(payload, default_reason="manual"))

    def api_rclone_stop():
        return jsonify(_rclone_admin_command_application().stop())

    def api_rclone_logs():
        limit = _safe_int(request.args.get("limit"), 200, 1, 1000)
        return jsonify(_rclone_admin_query_application().logs(limit))

    def api_rclone_runs():
        page, per_page, offset = _page_args(50, 200)
        result = _rclone_admin_query_application().runs(limit=per_page, offset=offset)
        return jsonify({"success": True, "items": result["items"], "pagination": _page_meta(result["total"], page, per_page)})

    def api_rclone_events():
        limit = _safe_int(request.args.get("limit"), 200, 1, 1000)
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999)
        return jsonify(_rclone_admin_query_application().events(run_id=run_id or None, limit=limit))

    def api_rclone_file_events():
        page, per_page, offset = _page_args(200, 1000)
        run_id = _safe_int(request.args.get("run_id"), 0, 0, 999999999)
        job_id = _safe_int(request.args.get("job_id"), 0, 0, 999999999)
        status = str(request.args.get("status") or "").strip() or None
        category = str(request.args.get("category") or "").strip() or None
        result = _rclone_admin_query_application().file_events(run_id=run_id or None, job_id=job_id or None, status=status, category=category, limit=per_page, offset=offset)
        return jsonify(
            {
                "success": True,
                "items": result["items"],
                "pagination": _page_meta(result["total"], page, per_page),
            }
        )

    def api_rclone_check():
        return jsonify(_rclone_admin_command_application().check())

    def api_quark_check():
        payload = request.get_json(silent=True) or {}
        share_url = str(payload.get("shareurl") or payload.get("url") or "").strip()
        if not share_url:
            return jsonify({"success": False, "message": "缺少夸克分享链接"}), 400
        title = str(payload.get("title") or payload.get("taskname") or "temp_check")
        ok, data = quark_importer.check_share(share_url, title)
        return jsonify({"success": ok, "data": data})

    def api_quark_file_list():
        payload = request.get_json(silent=True) or {}
        pwd_id = str(payload.get("pwd_id") or "").strip()
        if not pwd_id:
            return jsonify({"success": False, "message": "缺少夸克分享标识 pwd_id"}), 400
        data = quark_importer.list_files(
            pwd_id=pwd_id,
            fid=str(payload.get("fid") or ""),
            stoken=str(payload.get("stoken") or ""),
        )
        return jsonify(data)

    def api_cloud139_check():
        payload = request.get_json(silent=True) or {}
        share_url = str(payload.get("shareurl") or payload.get("url") or "").strip()
        if not share_url:
            return jsonify({"success": False, "message": "缺少139云盘分享链接"}), 400
        title = str(payload.get("title") or payload.get("taskname") or "temp_check")
        password = str(payload.get("password") or payload.get("pwd") or "").strip()
        ok, data = cloud139_importer.check_share(share_url, title=title, password=password)
        return jsonify({"success": ok, "data": data})

    def api_cloud139_file_list():
        payload = request.get_json(silent=True) or {}
        share_url = str(payload.get("shareurl") or payload.get("url") or "").strip()
        folder_id = str(payload.get("fid") or payload.get("folder_id") or payload.get("catalog_id") or "").strip()
        if not share_url or not folder_id:
            return jsonify({"success": False, "message": "缺少139云盘分享链接或目录标识"}), 400
        data = cloud139_importer.list_files(
            share_url=share_url,
            fid=folder_id,
            password=str(payload.get("password") or payload.get("pwd") or ""),
            title=str(payload.get("title") or payload.get("taskname") or "temp_fetch_list"),
        )
        return jsonify(data)

    def api_admin_sixpan_tasks():
        importer = generic_importers.get("sixpan")
        if not importer or not getattr(importer, "configured", False):
            return jsonify({"success": False, "message": "六盘离线适配器未配置", "items": []}), 400
        limit = _safe_int(request.args.get("limit"), 100, 1, 500)
        try:
            items = importer.list_tasks(limit=limit)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"六盘任务列表读取失败：{exc}", "items": []}), 502
        return jsonify({"success": True, "items": items})

    def api_admin_sixpan_probe():
        importer = generic_importers.get("sixpan")
        if not importer or not hasattr(importer, "probe"):
            return jsonify({"success": False, "message": "六盘离线适配器不可用"}), 400
        result = importer.probe()
        return jsonify({"success": result.ok, "status": result.status, "message": result.message, "details": result.details})

    def api_admin_sixpan_oauth_device_code():
        payload = request.get_json(silent=True) or {}
        credentials = payload.get("credentials")
        save_body: dict[str, Any] = {}
        if isinstance(credentials, dict):
            credential_patch = {
                "client_id": str(credentials.get("client_id") or "").strip(),
                "client_secret": str(credentials.get("client_secret") or "").strip(),
            }
            save_body, save_status = settings_service.update_advanced(
                {"config": {"sixpan": credential_patch}}
            )
            if save_status != 200:
                return jsonify(save_body), save_status

        importer = generic_importers.get("sixpan")
        if not importer or not getattr(importer, "auth_configured", False):
            return jsonify({"success": False, "message": "请填写六盘 ClientID/ClientSecret"}), 400
        try:
            auth = importer.start_device_authorization(
                device=str(payload.get("device") or "fnos-media-import/1.0"),
                scope=str(payload.get("scope") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"六盘授权入口创建失败：{exc}"}), 502

        device_code = str(auth.get("device_code") or auth.get("deviceCode") or "").strip()
        user_code = str(auth.get("user_code") or auth.get("userCode") or "").strip()
        verification_uri = str(auth.get("verification_uri") or auth.get("verificationUri") or "").strip()
        interval = _safe_int(auth.get("interval"), 5, 1, 120)
        expires_in = _safe_int(auth.get("expires_in") or auth.get("expiresIn"), 0, 0, 86400)
        if not device_code:
            return jsonify({"success": False, "message": "六盘授权接口未返回 device_code", "raw": _redact_config(auth)}), 502

        state = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "interval": interval,
            "expires_in": expires_in,
            "created_at": utc_now_iso(),
        }
        db.set_app_settings({"sixpan.oauth.device_code": state})
        return jsonify(
            {
                "success": True,
                "message": "六盘授权入口已创建，请打开授权链接并输入验证码",
                "auth": {**state, "device_code": "***"},
                "raw": _redact_config(auth),
                **{key: save_body[key] for key in ("config", "stored", "meta") if key in save_body},
            }
        )

    def api_admin_sixpan_oauth_device_code_check():
        importer = generic_importers.get("sixpan")
        if not importer or not getattr(importer, "auth_configured", False):
            return jsonify({"success": False, "message": "请先保存六盘 ClientID/ClientSecret"}), 400
        payload = request.get_json(silent=True) or {}
        settings = db.get_app_settings()
        saved_state = settings.get("sixpan.oauth.device_code") if isinstance(settings.get("sixpan.oauth.device_code"), dict) else {}
        device_code = str(payload.get("device_code") or saved_state.get("device_code") or "").strip()
        user_code = str(payload.get("user_code") or saved_state.get("user_code") or "").strip()
        if not device_code:
            return jsonify({"success": False, "message": "没有可检查的六盘 device_code，请先点击开始授权"}), 400

        try:
            state = importer.check_device_authorization(device_code=device_code, user_code=user_code)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"success": False, "message": f"六盘授权状态检查失败：{exc}"}), 502

        tokens = importer.extract_tokens(state) if hasattr(importer, "extract_tokens") else {}
        status_text = str(state.get("status") or "").strip()
        authorized = bool(tokens.get("access_token") or tokens.get("refresh_token"))
        persist_result = _persist_sixpan_tokens(tokens) if authorized else {"updated": False, "reload": {}}
        sanitized_state = _redact_config(state)
        next_state = dict(saved_state)
        next_state.update(
            {
                "device_code": device_code,
                "user_code": user_code,
                "status": status_text,
                "checked_at": utc_now_iso(),
            }
        )
        db.set_app_settings({"sixpan.oauth.device_code": next_state})
        return jsonify(
            {
                "success": True,
                "authorized": authorized,
                "status": status_text,
                "message": _sixpan_oauth_state_message(status_text, authorized),
                "state": sanitized_state,
                "token_saved": bool(persist_result.get("updated")),
                "reload": persist_result.get("reload") or {},
            }
        )

    def api_admin_sixpan_sync():
        result = sixpan_offline_sync.sync(trigger="admin_manual")
        return jsonify(result)

    def api_admin_sixpan_retry_media_refresh(job_id: int):
        result = sixpan_offline_sync.retry_media_refresh(job_id, trigger="admin_manual")
        if result.get("not_found"):
            return jsonify(result), 404
        if result.get("rejected") or result.get("conflict"):
            return jsonify(result), 409
        return jsonify(result)

    callback_service = RcloneCallbackService(
        CallbackDependencies(
            db=db,
            rclone=rclone_service,
            safe_int=_safe_int,
            callback_level=_rclone_callback_level,
            enqueue_organizer=_enqueue_organizer_from_rclone_completed_items,
            cancelled_status=JOB_CANCELLED,
        )
    )

    def api_rclone_callback():
        body, status_code = callback_service.handle(request.get_json(silent=True) or {})
        return jsonify(body), status_code

    @app.errorhandler(Exception)
    def handle_error(error: Exception):
        if isinstance(error, RequestEntityTooLarge):
            return jsonify(
                {
                    "success": False,
                    "error_code": "request_too_large",
                    "message": "请求体过大",
                    "max_bytes": app.config.get("MAX_CONTENT_LENGTH"),
                }
            ), 413
        if isinstance(error, HTTPException):
            return jsonify({"success": False, "message": error.description}), error.code or 500
        trace_id = uuid.uuid4().hex
        app.logger.exception("请求处理失败 trace_id=%s", trace_id)
        return jsonify(
            {
                "success": False,
                "error_code": "internal_error",
                "message": "服务器内部错误",
                "trace_id": trace_id,
            }
        ), 500

    app.register_blueprint(
        create_system_blueprint(
            SystemRouteContext(
                app_name=lambda: app_config.app_name or APP_NAME,
                readiness_probe=lambda: db.get_app_settings(),
                dependency_status=_admin_system_status,
                admin_required=admin_required,
                log_readiness_error=lambda exc: app.logger.exception("readiness check failed", exc_info=exc),
            )
        )
    )
    app.register_blueprint(
        create_auth_blueprint(
            AuthRouteContext(
                csrf_enabled=lambda: _config_bool(security_config, "csrf_enabled", False),
                rate_limit_login=lambda: _rate_limit("admin_login", "admin_login_rate_limit"),
                verify_password=_verify_admin_password,
                admin_profile=_admin_profile,
                is_logged_in=_is_admin_logged_in,
                security_status=_build_security_status,
                admin_required=admin_required,
            )
        )
    )
    app.register_blueprint(
        create_settings_blueprint(
            SettingsRouteContext(
                admin_required=admin_required,
                config=api_admin_config,
                history_summary=api_admin_maintenance_history_summary,
                cleanup_history=api_admin_maintenance_cleanup_history,
                advanced_config=api_admin_advanced_config,
                advanced_config_update=api_admin_advanced_config_update,
                advanced_export=api_admin_advanced_config_export,
                settings=api_admin_settings,
                settings_update=api_admin_settings_update,
                settings_update_all=api_admin_settings_update_all,
                notifications_config=api_admin_notifications_config,
                notifications_update=api_admin_notifications_update,
                notifications_test=api_admin_notifications_test,
                notifications_deliveries=api_admin_notifications_deliveries,
                notifications_retry=api_admin_notifications_retry,
            )
        )
    )
    app.register_blueprint(
        create_rclone_blueprint(
            RcloneRouteContext(
                admin_required=admin_required,
                handlers={
                    "admin_status": api_admin_rclone_status,
                    "admin_start": api_admin_rclone_start,
                    "admin_stop": api_admin_rclone_stop,
                    "admin_logs": api_admin_rclone_logs,
                    "admin_runs": api_admin_rclone_runs,
                    "admin_events": api_admin_rclone_events,
                    "admin_files": api_admin_rclone_file_events,
                    "admin_file_retry": api_admin_rclone_file_retry,
                    "admin_check": api_admin_rclone_check,
                    "admin_webdav_config": api_admin_rclone_webdav_config,
                    "admin_webdav_config_update": api_admin_rclone_webdav_config_update,
                    "admin_webdav_config_test": api_admin_rclone_webdav_config_test,
                    "legacy_status": api_rclone_status,
                    "legacy_start": api_rclone_start,
                    "legacy_stop": api_rclone_stop,
                    "legacy_logs": api_rclone_logs,
                    "legacy_runs": api_rclone_runs,
                    "legacy_events": api_rclone_events,
                    "legacy_files": api_rclone_file_events,
                    "legacy_check": api_rclone_check,
                },
            )
        )
    )
    app.register_blueprint(
        create_organizer_blueprint(
            OrganizerRouteContext(
                admin_required=admin_required,
                handlers={
                    "tasks": api_admin_organizer_tasks,
                    "scan": api_admin_organizer_scan,
                    "task_detail": api_admin_organizer_task_detail,
                    "rebuild": api_admin_organizer_rebuild,
                    "mapping_update": api_admin_organizer_mapping_update,
                    "mappings_batch_update": api_admin_organizer_mappings_batch_update,
                    "approve": api_admin_organizer_approve,
                    "apply": api_admin_organizer_apply,
                    "skip": api_admin_organizer_skip,
                    "retry": api_admin_organizer_retry,
                    "delete": api_admin_organizer_delete,
                    "runs": api_admin_organizer_runs,
                    "rollback": api_admin_organizer_rollback,
                },
            )
        )
    )
    app.register_blueprint(
        create_updates_blueprint(
            UpdatesRouteContext(
                admin_required=admin_required,
                handlers={
                    "subscriptions": api_admin_update_subscriptions,
                    "subscription_create": api_admin_update_subscription_create,
                    "subscription_detail": api_admin_update_subscription_detail,
                    "subscription_update": api_admin_update_subscription_update,
                    "subscription_delete": api_admin_update_subscription_delete,
                    "subscription_run": api_admin_update_subscription_run,
                    "subscription_refresh_snapshot": api_admin_update_subscription_refresh_snapshot,
                    "subscription_preview": api_admin_update_subscription_preview,
                    "subscription_pause": api_admin_update_subscription_pause,
                    "subscription_enable": api_admin_update_subscription_enable,
                    "runs": api_admin_update_runs,
                    "run_detail": api_admin_update_run_detail,
                    "candidates": api_admin_update_candidates,
                    "candidate_import": api_admin_update_candidate_import,
                    "candidate_reject": api_admin_update_candidate_reject,
                    "scheduler_run_due": api_admin_update_scheduler_run_due,
                    "scheduler_status": api_admin_update_scheduler_status,
                },
            )
        )
    )
    app.register_blueprint(
        create_trending_blueprint(
            TrendingRouteContext(
                admin_required=admin_required,
                handlers={
                    "status": api_admin_trending_status,
                    "run": api_admin_trending_run,
                    "runs": api_admin_trending_runs,
                    "candidates": api_admin_trending_candidates,
                    "candidate_detail": api_admin_trending_candidate_detail,
                    "candidate_search": api_admin_trending_candidate_search,
                    "candidate_resource_detail": api_admin_trending_candidate_resource_detail,
                    "candidate_resource_files": api_admin_trending_candidate_resource_files,
                    "candidate_import": api_admin_trending_candidate_import,
                    "candidate_subscribe": api_admin_trending_candidate_subscribe,
                    "candidate_ignore": api_admin_trending_candidate_ignore,
                    "candidate_restore": api_admin_trending_candidate_restore,
                },
            )
        )
    )
    app.register_blueprint(create_requests_blueprint(RequestsRouteContext(admin_required=admin_required, handlers={
        "dashboard": api_admin_dashboard,
        "requests": api_admin_requests,
        "request_detail": api_admin_request_detail,
        "request_approve": api_admin_request_approve,
        "request_reject": api_admin_request_reject,
        "request_cancel": api_admin_request_cancel,
    })))
    app.register_blueprint(create_jobs_blueprint(JobsRouteContext(admin_required=admin_required, handlers={
        "admin_jobs": api_admin_jobs,
        "admin_job_detail": api_admin_job_detail,
        "admin_job_retry": api_admin_job_retry,
        "admin_job_cancel": api_admin_job_cancel,
        "admin_job_delete": api_admin_job_delete,
        "admin_jobs_batch_retry": api_admin_jobs_batch_retry,
        "legacy_job_detail": api_job_detail,
        "legacy_job_retry": api_job_retry,
    })))
    app.register_blueprint(create_public_blueprint(PublicRouteContext(handlers={
        "index": index,
        "submit_page": submit_page,
        "request_status_page": request_status_page,
        "config": api_public_config,
        "trending": api_public_trending,
        "captcha": api_public_captcha,
        "search": api_public_search,
        "detect": api_public_detect,
        "manual_preview": api_public_manual_preview,
        "resource_detail": api_public_resource_detail,
        "resource_files": api_public_resource_files,
        "sixpan_parse": api_public_sixpan_parse,
        "btbtla_resolve": api_public_btbtla_resolve,
        "submit": api_public_submit,
        "request": api_public_request,
        "notification_verify_confirm": api_public_notification_verify_confirm,
        "notification_verify": api_public_notification_verify,
        "notification_unsubscribe_confirm": api_public_notification_unsubscribe_confirm,
        "notification_unsubscribe": api_public_notification_unsubscribe,
    })))
    app.register_blueprint(create_media_blueprint(MediaRouteContext(admin_required=admin_required, handlers={
        "libraries": api_admin_media_libraries,
        "running": api_admin_media_running,
        "refresh_logs": api_admin_media_refresh_logs,
        "admin_refresh": api_admin_media_refresh,
        "legacy_refresh": api_media_refresh,
    })))
    app.register_blueprint(create_cloud_compat_blueprint(CloudCompatRouteContext(admin_required=admin_required, handlers={
        "quark_check": api_quark_check,
        "quark_file_list": api_quark_file_list,
        "cloud139_check": api_cloud139_check,
        "cloud139_file_list": api_cloud139_file_list,
    })))
    app.register_blueprint(create_sixpan_blueprint(SixPanRouteContext(admin_required=admin_required, handlers={
        "tasks": api_admin_sixpan_tasks,
        "probe": api_admin_sixpan_probe,
        "oauth_device_code": api_admin_sixpan_oauth_device_code,
        "oauth_device_code_check": api_admin_sixpan_oauth_device_code_check,
        "sync": api_admin_sixpan_sync,
        "retry_media_refresh": api_admin_sixpan_retry_media_refresh,
    })))
    app.register_blueprint(create_adapters_blueprint(AdaptersRouteContext(admin_required=admin_required, handlers={
        "search_providers": api_admin_search_providers,
        "search_providers_update": api_admin_search_providers_update,
        "search_aliases": api_admin_search_aliases,
        "search_aliases_update": api_admin_search_aliases_update,
        "adapters": api_admin_adapters,
        "adapter_probe": api_admin_adapter_probe,
    })))
    app.register_blueprint(create_admin_shell_blueprint(AdminShellRouteContext(admin_required=admin_required, handlers={"login_page":admin_login_page,"admin_page":admin_page,"profile":api_admin_profile,"profile_update":api_admin_profile_update,"avatar":api_admin_profile_avatar,"site_logo":api_admin_site_logo})))
    app.register_blueprint(create_diagnostics_blueprint(DiagnosticsRouteContext(admin_required=admin_required, handlers={"system_logs":api_admin_system_logs,"system_events":api_admin_system_events,"task_logs":api_admin_task_logs,"btbtla_proxy_test":api_admin_btbtla_proxy_test,"openlist_test":api_admin_openlist_test,"openlist_dirs":api_admin_openlist_dirs,"tmdb_test":api_admin_tmdb_test,"tmdb_search":api_admin_tmdb_search,"tmdb_detail":api_admin_tmdb_detail,"ai_test":api_admin_ai_test})))
    app.register_blueprint(create_legacy_api_blueprint(LegacyApiRouteContext(admin_required=admin_required, handlers={"public_config":public_config,"search":api_search,"detect":api_detect,"import_resource":api_import,"jobs":api_jobs})))
    app.register_blueprint(create_callbacks_blueprint(CallbackRouteContext(rclone_callback=api_rclone_callback)))
    preserve_legacy_endpoints(
        app,
        {
            "system.health": "health",
            "system.livez": "livez",
            "system.readyz": "readyz",
            "system.dependencies": "dependencies",
            "system.openapi_json": "openapi_json",
            "system.swagger_docs": "swagger_docs",
            "admin_auth.login": "api_admin_login",
            "admin_auth.logout": "api_admin_logout",
            "admin_auth.admin_session": "api_admin_session",
            "admin_auth.security_status": "api_admin_security_status",
            "settings.config": "api_admin_config",
            "settings.history_summary": "api_admin_maintenance_history_summary",
            "settings.cleanup_history": "api_admin_maintenance_cleanup_history",
            "settings.advanced_config": "api_admin_advanced_config",
            "settings.advanced_config_update": "api_admin_advanced_config_update",
            "settings.settings": "api_admin_settings",
            "settings.settings_update": "api_admin_settings_update",
            "rclone_routes.admin_status": "api_admin_rclone_status",
            "rclone_routes.admin_start": "api_admin_rclone_start",
            "rclone_routes.admin_stop": "api_admin_rclone_stop",
            "rclone_routes.admin_logs": "api_admin_rclone_logs",
            "rclone_routes.admin_runs": "api_admin_rclone_runs",
            "rclone_routes.admin_events": "api_admin_rclone_events",
            "rclone_routes.admin_files": "api_admin_rclone_file_events",
            "rclone_routes.admin_file_retry": "api_admin_rclone_file_retry",
            "rclone_routes.admin_check": "api_admin_rclone_check",
            "rclone_routes.admin_webdav_config": "api_admin_rclone_webdav_config",
            "rclone_routes.admin_webdav_config_update": "api_admin_rclone_webdav_config_update",
            "rclone_routes.admin_webdav_config_test": "api_admin_rclone_webdav_config_test",
            "rclone_routes.legacy_status": "api_rclone_status",
            "rclone_routes.legacy_start": "api_rclone_start",
            "rclone_routes.legacy_stop": "api_rclone_stop",
            "rclone_routes.legacy_logs": "api_rclone_logs",
            "rclone_routes.legacy_runs": "api_rclone_runs",
            "rclone_routes.legacy_events": "api_rclone_events",
            "rclone_routes.legacy_files": "api_rclone_file_events",
            "rclone_routes.legacy_check": "api_rclone_check",
            "organizer_routes.tasks": "api_admin_organizer_tasks",
            "organizer_routes.scan": "api_admin_organizer_scan",
            "organizer_routes.task_detail": "api_admin_organizer_task_detail",
            "organizer_routes.rebuild": "api_admin_organizer_rebuild",
            "organizer_routes.mapping_update": "api_admin_organizer_mapping_update",
            "organizer_routes.approve": "api_admin_organizer_approve",
            "organizer_routes.apply": "api_admin_organizer_apply",
            "organizer_routes.skip": "api_admin_organizer_skip",
            "organizer_routes.retry": "api_admin_organizer_retry",
            "organizer_routes.delete": "api_admin_organizer_delete",
            "organizer_routes.runs": "api_admin_organizer_runs",
            "organizer_routes.rollback": "api_admin_organizer_rollback",
            "update_routes.subscriptions": "api_admin_update_subscriptions",
            "update_routes.subscription_create": "api_admin_update_subscription_create",
            "update_routes.subscription_detail": "api_admin_update_subscription_detail",
            "update_routes.subscription_update": "api_admin_update_subscription_update",
            "update_routes.subscription_delete": "api_admin_update_subscription_delete",
            "update_routes.subscription_run": "api_admin_update_subscription_run",
            "update_routes.subscription_refresh_snapshot": "api_admin_update_subscription_refresh_snapshot",
            "update_routes.subscription_preview": "api_admin_update_subscription_preview",
            "update_routes.subscription_pause": "api_admin_update_subscription_pause",
            "update_routes.subscription_enable": "api_admin_update_subscription_enable",
            "update_routes.runs": "api_admin_update_runs",
            "update_routes.run_detail": "api_admin_update_run_detail",
            "update_routes.candidates": "api_admin_update_candidates",
            "update_routes.candidate_import": "api_admin_update_candidate_import",
            "update_routes.candidate_reject": "api_admin_update_candidate_reject",
            "update_routes.scheduler_run_due": "api_admin_update_scheduler_run_due",
            "update_routes.scheduler_status": "api_admin_update_scheduler_status",
            "request_routes.dashboard": "api_admin_dashboard",
            "request_routes.requests": "api_admin_requests",
            "request_routes.request_detail": "api_admin_request_detail",
            "request_routes.request_approve": "api_admin_request_approve",
            "request_routes.request_reject": "api_admin_request_reject",
            "request_routes.request_cancel": "api_admin_request_cancel",
            "job_routes.admin_jobs": "api_admin_jobs",
            "job_routes.admin_job_detail": "api_admin_job_detail",
            "job_routes.admin_job_retry": "api_admin_job_retry",
            "job_routes.admin_job_cancel": "api_admin_job_cancel",
            "job_routes.admin_job_delete": "api_admin_job_delete",
            "job_routes.admin_jobs_batch_retry": "api_admin_jobs_batch_retry",
            "job_routes.legacy_job_detail": "api_job_detail",
            "job_routes.legacy_job_retry": "api_job_retry",
            "public_routes.index": "index",
            "public_routes.submit_page": "submit_page",
            "public_routes.request_status_page": "request_status_page",
            "public_routes.config": "api_public_config",
            "public_routes.trending": "api_public_trending",
            "public_routes.captcha": "api_public_captcha",
            "public_routes.search": "api_public_search",
            "public_routes.detect": "api_public_detect",
            "public_routes.manual_preview": "api_public_manual_preview",
            "public_routes.resource_detail": "api_public_resource_detail",
            "public_routes.resource_files": "api_public_resource_files",
            "public_routes.sixpan_parse": "api_public_sixpan_parse",
            "public_routes.btbtla_resolve": "api_public_btbtla_resolve",
            "public_routes.submit": "api_public_submit",
            "public_routes.request": "api_public_request",
            "media_routes.libraries": "api_admin_media_libraries",
            "media_routes.running": "api_admin_media_running",
            "media_routes.refresh_logs": "api_admin_media_refresh_logs",
            "media_routes.admin_refresh": "api_admin_media_refresh",
            "media_routes.legacy_refresh": "api_media_refresh",
            "cloud_compat_routes.quark_check": "api_quark_check",
            "cloud_compat_routes.quark_file_list": "api_quark_file_list",
            "cloud_compat_routes.cloud139_check": "api_cloud139_check",
            "cloud_compat_routes.cloud139_file_list": "api_cloud139_file_list",
            "sixpan_routes.tasks": "api_admin_sixpan_tasks",
            "sixpan_routes.probe": "api_admin_sixpan_probe",
            "sixpan_routes.oauth_device_code": "api_admin_sixpan_oauth_device_code",
            "sixpan_routes.oauth_device_code_check": "api_admin_sixpan_oauth_device_code_check",
            "sixpan_routes.sync": "api_admin_sixpan_sync",
            "sixpan_routes.retry_media_refresh": "api_admin_sixpan_retry_media_refresh",
            "adapter_routes.search_providers": "api_admin_search_providers",
            "adapter_routes.search_providers_update": "api_admin_search_providers_update",
            "adapter_routes.search_aliases": "api_admin_search_aliases",
            "adapter_routes.search_aliases_update": "api_admin_search_aliases_update",
            "adapter_routes.adapters": "api_admin_adapters",
            "adapter_routes.adapter_probe": "api_admin_adapter_probe",
            "admin_shell_routes.login_page": "admin_login_page",
            "admin_shell_routes.admin_page": "admin_page",
            "admin_shell_routes.profile": "api_admin_profile",
            "admin_shell_routes.profile_update": "api_admin_profile_update",
            "admin_shell_routes.avatar": "api_admin_profile_avatar",
            "admin_shell_routes.site_logo": "api_admin_site_logo",
            "diagnostic_routes.system_logs": "api_admin_system_logs",
            "diagnostic_routes.system_events": "api_admin_system_events",
            "diagnostic_routes.task_logs": "api_admin_task_logs",
            "diagnostic_routes.btbtla_proxy_test": "api_admin_btbtla_proxy_test",
            "diagnostic_routes.openlist_test": "api_admin_openlist_test",
            "diagnostic_routes.openlist_dirs": "api_admin_openlist_dirs",
            "diagnostic_routes.tmdb_test": "api_admin_tmdb_test",
            "diagnostic_routes.tmdb_search": "api_admin_tmdb_search",
            "diagnostic_routes.tmdb_detail": "api_admin_tmdb_detail",
            "diagnostic_routes.ai_test": "api_admin_ai_test",
            "legacy_api_routes.public_config": "public_config",
            "legacy_api_routes.search": "api_search",
            "legacy_api_routes.detect": "api_detect",
            "legacy_api_routes.import_resource": "api_import",
            "legacy_api_routes.jobs": "api_jobs",
            "callback_routes.rclone_callback": "api_rclone_callback",
        },
    )
    if role_runs(active_process_role, "worker"):
        rclone_worker_runtime.start()
        durable_worker_runtime.start()
        search_cache_maintenance_worker.start()
        event_retention_worker.start()
        rclone_config = app_config.raw.get("rclone", {}) if isinstance(app_config.raw.get("rclone"), dict) else {}
        recovery_delay_seconds = max(
            1,
            min(60, _config_int(rclone_config, "startup_recovery_delay_seconds", 2)),
        )

        def _activate_rclone_startup_recovery() -> None:
            try:
                result = rclone_service.activate_startup_recovery()
                if result.get("success") is False:
                    app.logger.warning("rclone startup recovery incomplete: %s", result)
            except Exception:  # noqa: BLE001
                app.logger.exception("rclone startup recovery failed")

        recovery_timer = threading.Timer(recovery_delay_seconds, _activate_rclone_startup_recovery)
        recovery_timer.daemon = True
        app.extensions["rclone_startup_recovery_timer"] = recovery_timer
        atexit.register(recovery_timer.cancel)
        recovery_timer.start()
    return app


def _create_subscription_from_hot_candidate(
    item: dict[str, Any],
    tmdb: Any,
    create_subscription: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """为热榜候选创建追更订阅，返回 (result, status_code)。

    TMDB 未配置/未匹配、TMDB 查询失败等场景均以错误消息降级，不抛出异常。
    """
    title = str(item.get("title") or "").strip()
    if not title:
        return {"success": False, "message": "候选缺少标题，无法创建追更订阅"}, 400
    raw_media_type = str(item.get("media_type") or "").strip().lower()
    category = raw_media_type if raw_media_type in {"movie", "tv", "anime", "variety"} else "tv"
    media_type = "movie" if category == "movie" else "tv"
    if not getattr(tmdb, "configured", False):
        return {"success": False, "message": "TMDB 未配置，无法自动匹配，请到追更页手动创建订阅"}, 400
    try:
        rows = tmdb.search(title, media_type)
    except Exception:  # noqa: BLE001
        return {"success": False, "message": "TMDB 查询失败，请稍后重试或到追更页手动创建订阅"}, 502
    if not rows:
        return {"success": False, "message": "TMDB 未匹配到该影视，请到追更页手动创建订阅"}, 400
    candidate_year = str(item.get("year") or "").strip()
    expected_titles = [
        str(item.get("title") or "").strip(),
        str(item.get("original_title") or "").strip(),
    ]
    scored = sorted(
        (
            (_hot_tmdb_match_score(expected_titles, candidate_year, media_type, row), row)
            for row in rows
            if isinstance(row, dict) and _hot_tmdb_result_id(row) > 0
        ),
        key=lambda entry: entry[0],
        reverse=True,
    )
    match_preview = [
        {
            "tmdb_id": _hot_tmdb_result_id(row),
            "title": str(row.get("title") or ""),
            "original_title": str(row.get("original_title") or ""),
            "year": str(row.get("year") or ""),
            "media_type": str(row.get("media_type") or media_type),
            "score": score,
        }
        for score, row in scored[:3]
    ]
    if not scored or scored[0][0] < 85:
        return {
            "success": False,
            "message": "TMDB 匹配置信度不足，已停止自动创建，请到追更页手动选择正确影视",
            "matches": match_preview,
        }, 409
    if len(scored) > 1 and scored[1][0] >= 75 and scored[0][0] - scored[1][0] < 8:
        return {
            "success": False,
            "message": "TMDB 匹配结果存在歧义，已停止自动创建，请到追更页手动选择正确影视",
            "matches": match_preview,
        }, 409
    best = scored[0][1]
    tmdb_id = _hot_tmdb_result_id(best)
    if not tmdb_id:
        return {"success": False, "message": "TMDB 未匹配到该影视，请到追更页手动创建订阅"}, 400
    aliases = []
    for value in expected_titles + [str(best.get("original_title") or "").strip()]:
        if value and value != str(best.get("title") or "").strip() and value not in aliases:
            aliases.append(value)
    payload = {
        "title": str(best.get("title") or title),
        "category": category,
        "media_type": media_type,
        "year": str(best.get("year") or item.get("year") or ""),
        "tmdb_id": tmdb_id,
        "season": _hot_record_season(item),
        "aliases": aliases,
    }
    try:
        subscription = create_subscription(payload)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}, 400
    except Exception:  # noqa: BLE001
        return {"success": False, "message": "创建追更订阅失败"}, 500
    subscription_id = int(subscription.get("id") or 0)
    if not subscription_id:
        return {"success": False, "message": "创建追更订阅失败"}, 500
    created = bool(subscription.get("_created", True))
    return {
        "success": True,
        "created": created,
        "message": "追更订阅已创建" if created else f"已绑定现有追更订阅 #{subscription_id}",
        "subscription_id": subscription_id,
    }, 200


def _hot_tmdb_match_score(
    expected_titles: list[str],
    expected_year: str,
    expected_media_type: str,
    result: dict[str, Any],
) -> int:
    expected = [_normalize_hot_tmdb_title(value, expected_year) for value in expected_titles]
    actual_year = str(result.get("year") or "").strip()
    actual = [
        _normalize_hot_tmdb_title(result.get("title"), actual_year),
        _normalize_hot_tmdb_title(result.get("original_title"), actual_year),
    ]
    expected = [value for value in expected if value]
    actual = [value for value in actual if value]
    title_score = 0
    for left in expected:
        for right in actual:
            if left == right:
                title_score = max(title_score, 70)
                continue
            shorter = min(len(left), len(right))
            if shorter >= 4 and (left in right or right in left):
                title_score = max(title_score, 52)
            ratio = SequenceMatcher(None, left, right).ratio()
            if ratio >= 0.92:
                title_score = max(title_score, 62)
            elif ratio >= 0.84:
                title_score = max(title_score, 52)

    score = title_score
    if expected_year and actual_year:
        score += 20 if expected_year == actual_year else -25
    elif expected_year and not actual_year:
        score -= 5
    result_media_type = str(result.get("media_type") or expected_media_type).strip().lower()
    score += 10 if result_media_type == expected_media_type else -30
    return max(0, min(100, int(score)))


def _hot_tmdb_result_id(result: dict[str, Any]) -> int:
    try:
        value = int(result.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _normalize_hot_tmdb_title(value: Any, year: str = "") -> str:
    text = str(value or "").strip().casefold()
    expected_year = str(year or "").strip()
    if expected_year:
        text = re.sub(
            rf"[\s._\-()（）\[\]【】]*{re.escape(expected_year)}[\s._\-()（）\[\]【】]*$",
            "",
            text,
        )
    return "".join(character for character in text if character.isalnum())
